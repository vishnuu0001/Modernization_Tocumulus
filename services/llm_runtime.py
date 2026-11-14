"""Persisted, infrastructure-aware LLM execution profiles.

The profile is deployment-wide because Ollama model residency and inference
lanes are shared host resources. Changes are accepted only while generation is
idle (enforced by the API), so active jobs retain stable execution semantics.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = _ROOT / "data" / "runtime" / "llm-runtime.json"
_LOCK = threading.RLock()
_PROFILE_IDS = ("safe", "balanced", "performance")


def _model_billions(model: str) -> float:
    matches = re.findall(r"(?i)(\d+(?:\.\d+)?)b\b", model or "")
    return float(matches[-1]) if matches else 24.0


@lru_cache(maxsize=1)
def _gpu_memory_gb() -> float | None:
    """Return total visible NVIDIA memory without making GPU tooling mandatory."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=True,
        )
        values = [float(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        return round(sum(values) / 1024, 1) if values else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _host_lane_cap(model: str) -> int:
    size = _model_billions(model)
    gpu_memory = _gpu_memory_gb()
    if gpu_memory:
        estimated_resident_gb = max(5.0, size * 0.65 + 3.0)
        model_cap = max(1, min(4, int(gpu_memory / estimated_resident_gb)))
    else:
        model_cap = 4 if size <= 10 else 3 if size <= 18 else 2 if size <= 34 else 1
    cpu_count = os.cpu_count() or 2
    cpu_cap = 4 if cpu_count >= 16 else 2 if cpu_count >= 8 else 1
    try:
        configured_cap = max(1, min(8, int(os.getenv("OLLAMA_RUNTIME_MAX_LANES", "8"))))
    except ValueError:
        configured_cap = 1
    return max(1, min(model_cap, cpu_cap, configured_cap))


def profiles_for_model(model: str) -> list[dict[str, Any]]:
    cap = _host_lane_cap(model)
    widths = {
        "safe": 1,
        "balanced": min(2, cap),
        "performance": cap,
    }
    descriptions = {
        "safe": "One inference lane; lowest memory pressure and maximum stability.",
        "balanced": "Bounded parallel generation tuned for typical workstation GPUs.",
        "performance": "Use every safe lane inferred for this model and host.",
    }
    return [
        {
            "id": profile_id,
            "label": profile_id.title(),
            "description": descriptions[profile_id],
            "inference_lanes": widths[profile_id],
            "file_workers": widths[profile_id],
            "member_workers": widths[profile_id],
            "domain_workers": widths[profile_id],
            "validation_workers": min(8, max(widths[profile_id], (os.cpu_count() or 2) // 2)),
        }
        for profile_id in _PROFILE_IDS
    ]


def _read() -> dict[str, str]:
    try:
        value = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def requested_model() -> str:
    with _LOCK:
        return str(_read().get("model") or "").strip()


def runtime_status(available_models: list[str], default_model: str | None = None) -> dict[str, Any]:
    with _LOCK:
        stored = _read()
    requested = str(stored.get("model") or "").strip()
    selected = requested if requested in available_models else (default_model or (available_models[0] if available_models else ""))
    profile_id = str(stored.get("profile") or "balanced").casefold()
    if profile_id not in _PROFILE_IDS:
        profile_id = "balanced"
    profiles = profiles_for_model(selected)
    effective = next(item for item in profiles if item["id"] == profile_id)
    return {
        "selected_model": selected or None,
        "selected_profile": profile_id,
        "effective": effective,
        "profiles": profiles,
        "infrastructure": {
            "cpu_threads": os.cpu_count() or 1,
            "model_size_billions": _model_billions(selected),
            "gpu_memory_gb": _gpu_memory_gb(),
            "safe_inference_lane_cap": _host_lane_cap(selected),
        },
    }


def update_runtime(model: str, profile: str, available_models: list[str]) -> dict[str, Any]:
    model = str(model or "").strip()
    profile = str(profile or "balanced").casefold()
    if model not in available_models:
        raise ValueError("Select an installed LLM model")
    if profile not in _PROFILE_IDS:
        raise ValueError("Optimization profile must be safe, balanced, or performance")
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = _CONFIG_PATH.with_suffix(".tmp")
    with _LOCK:
        temporary.write_text(
            json.dumps({"model": model, "profile": profile}, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(_CONFIG_PATH)
    return runtime_status(available_models, model)


def worker_width(item_count: int, max_workers: int = 4) -> int:
    stored = _read()
    model = str(stored.get("model") or "")
    profile = str(stored.get("profile") or "balanced")
    effective = next(
        item for item in profiles_for_model(model) if item["id"] == profile
    ) if profile in _PROFILE_IDS else profiles_for_model(model)[1]
    return max(1, min(item_count or 1, max_workers, int(effective["file_workers"])))


def inference_lanes() -> int:
    return worker_width(8, max_workers=8)
