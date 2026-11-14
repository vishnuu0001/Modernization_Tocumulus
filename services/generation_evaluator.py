"""Deterministic acceptance checks for governed generation evaluation cases."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from services.generation_governance import (
    assert_frozen_manifest,
    atomize_guidelines,
    freeze_requirements_manifest,
    load_language_standards,
)
from services.modernizer.target_config import TARGET_STACKS


CASE_ROOT = Path(__file__).resolve().parents[1] / "evals" / "cases"


def load_evaluation_cases() -> list[dict[str, Any]]:
    cases = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(CASE_ROOT.glob("*.json"))]
    if not cases:
        raise RuntimeError(f"No generation evaluation cases found in {CASE_ROOT}")
    return cases


def evaluate_case_definition(case: Mapping[str, Any]) -> dict[str, Any]:
    required = {"id", "target_stack", "request", "project_guidelines", "run_instructions", "expected"}
    missing = sorted(required - set(case))
    if missing:
        raise ValueError(f"Evaluation case lacks: {', '.join(missing)}")
    stack_id = str(case["target_stack"])
    if stack_id not in TARGET_STACKS:
        raise ValueError(f"Unknown evaluation target stack: {stack_id}")
    target = TARGET_STACKS[stack_id]
    manifest = freeze_requirements_manifest(
        str(case["request"]), target, run_instructions=str(case["run_instructions"]),
    )
    assert_frozen_manifest(manifest)
    guidelines = atomize_guidelines(
        str(case["project_guidelines"]), str(case["run_instructions"]),
    )
    standards = load_language_standards(str(target["language"]), target)
    input_ids = [item["id"] for item in manifest["inputs"]]
    output_ids = [item["id"] for item in manifest["outputs"]]
    if not input_ids or not output_ids or not guidelines["rules"] or not standards:
        raise ValueError(f"Evaluation case {case['id']} is not governance-complete")
    return {
        "id": case["id"], "language": target["language"], "manifest_checksum": manifest["checksum"],
        "guideline_checksum": guidelines["checksum"], "input_ids": input_ids,
        "output_ids": output_ids, "rule_ids": [rule["id"] for rule in guidelines["rules"]],
        "conflicts": guidelines["conflicts"],
        "standards": [{"id": pack["id"], "version": pack["version"]} for pack in standards],
        "passed": True,
    }
