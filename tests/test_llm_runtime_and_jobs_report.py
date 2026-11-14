from datetime import UTC, datetime, timedelta

from api import server
from services import llm_runtime


def test_profiles_are_model_and_host_bounded(monkeypatch):
    monkeypatch.setattr(llm_runtime.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(llm_runtime, "_gpu_memory_gb", lambda: None)
    small = llm_runtime.profiles_for_model("coder:7b")
    large = llm_runtime.profiles_for_model("coder:70b")

    assert small[-1]["inference_lanes"] == 4
    assert large[-1]["inference_lanes"] == 1
    assert [profile["id"] for profile in small] == ["safe", "balanced", "performance"]


def test_runtime_selection_is_persisted_and_changes_worker_width(tmp_path, monkeypatch):
    config = tmp_path / "llm-runtime.json"
    monkeypatch.setattr(llm_runtime, "_CONFIG_PATH", config)
    monkeypatch.setattr(llm_runtime.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(llm_runtime, "_gpu_memory_gb", lambda: None)

    runtime = llm_runtime.update_runtime("coder:7b", "performance", ["coder:7b"])

    assert runtime["selected_model"] == "coder:7b"
    assert runtime["effective"]["file_workers"] == 4
    assert llm_runtime.worker_width(12) == 4


def test_better_gpu_memory_unlocks_more_safe_lanes(monkeypatch):
    monkeypatch.setattr(llm_runtime.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(llm_runtime, "_gpu_memory_gb", lambda: 12)
    constrained = llm_runtime.profiles_for_model("coder:24b")[-1]["inference_lanes"]
    monkeypatch.setattr(llm_runtime, "_gpu_memory_gb", lambda: 80)
    accelerated = llm_runtime.profiles_for_model("coder:24b")[-1]["inference_lanes"]
    assert constrained == 1
    assert accelerated > constrained


def test_jobs_report_filters_and_calculates_quality_metrics(monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(server, "_JOBS", {
        "ok": {
            "job_id": "ok", "status": "completed", "target_stack": "java_spring",
            "created_at": (now - timedelta(minutes=4)).isoformat(), "updated_at": now.isoformat(),
            "output": {"App.java": "class App {}"}, "llm_model": "coder:7b",
            "optimization_profile": "performance",
        },
        "bad": {
            "job_id": "bad", "status": "validation_failed", "target_stack": "oracle_sql",
            "created_at": (now - timedelta(minutes=2)).isoformat(), "updated_at": now.isoformat(),
            "output": {"generated.sql": "select 1 from dual;"},
        },
    })

    report = server._jobs_report(tech_stack="java_spring")

    assert report["summary"]["total"] == 1
    assert report["summary"]["success_rate"] == 100
    assert report["jobs"][0]["output_available"] is True
    assert report["filter_options"]["tech_stacks"] == ["java_spring", "oracle_sql"]


def test_jobs_report_uses_governed_effective_stack_over_stale_submitted_preset(monkeypatch):
    now = datetime.now(UTC).isoformat()
    monkeypatch.setattr(server, "_JOBS", {
        "fastapi": {
            "job_id": "fastapi", "status": "completed", "target_stack": "dotnet8_blazor",
            "created_at": now, "updated_at": now, "output": {"generated.py": "x = 1"},
            "validation": {"governance": {"reports": {"requirements-manifest.json": {
                "stack": {"name": "FastAPI", "language": "python", "backend": "FastAPI"},
            }}}},
        },
    })

    report = server._jobs_report()

    assert report["jobs"][0]["requested_target_stack"] == "dotnet8_blazor"
    assert report["jobs"][0]["target_stack"] == "python_fastapi"
    assert report["filter_options"]["tech_stacks"] == ["python_fastapi"]
