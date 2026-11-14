from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from services.cobol_batch_pack import generate_cobol_batch_pack, validate_cobol_batch_pack
from services.modernizer import generate_from_prompt


CASE = json.loads(
    (Path(__file__).resolve().parents[1] / "evals" / "cases" / "cobol-batch.json")
    .read_text(encoding="utf-8")
)


def test_cobol_batch_pack_compiles_runs_and_matches_golden_output():
    output = generate_cobol_batch_pack("CobolEvaluation")
    evidence = validate_cobol_batch_pack(output, "CobolEvaluation")
    assert evidence["passed"] is True
    assert evidence["exit_code"] == 0
    assert evidence["checker"] == "gnucobol-build-and-golden-run"


def test_exact_cobol_evaluation_is_generated_and_evaluated_without_claiming_production_readiness():
    with patch(
        "services.modernizer.prompt_pipeline._pf_check_llm_availability",
        side_effect=AssertionError("deterministic COBOL pack must not call the LLM"),
    ):
        output, validation = generate_from_prompt(
            CASE["request"], target_stack=CASE["target_stack"],
            custom_stack_desc=CASE["run_instructions"],
            guide_text="=== client.md ===\n" + CASE["project_guidelines"],
            output_mode="project",
        )

    project = next(path.split("/", 1)[0] for path in output if path.endswith("src/BATCHPROC.cob"))
    expected = {
        f"{project}/src/BATCHPROC.cob", f"{project}/src/TRANREC.cpy",
        f"{project}/src/RPTREC.cpy", f"{project}/jcl/BATCHPROC.jcl",
        f"{project}/sample-input.dat", f"{project}/expected-output.dat",
        f"{project}/governance/generation-audit.json",
        f"{project}/governance/guideline-compliance.json",
        f"{project}/governance/input-output-traceability.json",
    }
    assert expected.issubset(output)
    assert validation["generation_validated"] is True
    assert validation["production_ready"] is False
    assert validation["failed"] == 0
    assert validation["governance"] == {"passed": True, "diagnostics": []}
    audit = json.loads(output[f"{project}/governance/generation-audit.json"])
    assert audit["generation_gate_passed"] is True
    assert audit["production_ready"] is False
    manifest = json.loads(output[f"{project}/governance/requirements-manifest.json"])
    assert manifest["inputs"][0]["name"] == "transactionFile"
