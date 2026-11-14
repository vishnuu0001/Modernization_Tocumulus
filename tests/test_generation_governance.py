from __future__ import annotations

import asyncio
import io
import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException, UploadFile

from api.server import _extract_guide_text, _project_creation_configuration
from services.generation_governance import (
    StructuredSplitRequest,
    applicable_rules,
    assert_frozen_manifest,
    atomize_guidelines,
    build_audit_artifacts,
    build_file_plan,
    clear_generation_notes,
    extract_attached_config_files,
    freeze_requirements_manifest,
    generation_notes_snapshot,
    load_language_standards,
    load_language_acceptance_policy,
    manifest_completeness_diagnostics,
    parse_generation_response,
    register_generation_notes,
    render_governance_prompt,
    validate_language_pack_coverage,
    validate_language_acceptance_coverage,
    validate_machine_rules,
    validate_split_files,
)
from services.modernizer.target_config import TARGET_STACKS
from services.modernizer.prompt_pipeline import (
    _pf_generate_structural_single_file, _pf_try_single_file,
    _pf_structural_member_workers, _prompt_structure_baseline,
    _requirement_coverage_diagnostics,
)
from services.modernizer.token_budgeting import FileContract
from services.validators import ValidationResult


TARGET = {
    "name": "Python 3.12 FastAPI",
    "language": "python",
    "backend_tech": "FastAPI 0.115 + Pydantic 2",
    "frontend_tech": "",
    "db_tech": "PostgreSQL 16 + SQLAlchemy 2",
    "db_target": "postgres",
    "deploy": "Kubernetes",
}


def test_long_form_markdown_manifest_retains_structured_requirements_generically():
    prompt = """# Inventory Platform
## REST API
| Method | Route | Purpose |
|---|---|---|
| POST | /api/items | Create item |
| GET | /api/items/{id} | Get item |
## Database schema containing:
- Item
- Warehouse
## Core features
- Reserve inventory atomically
- Release expired reservations
## Integrations
- Publish events to a message broker
## Non-functional requirements
- Distributed tracing
- Deny-by-default authorization
## Expected deliverables
- OpenAPI specification
- GitHub Actions workflow
"""
    manifest = freeze_requirements_manifest(prompt, TARGET)

    assert len(manifest["inputs"]) == 2
    assert len(manifest["outputs"]) == 2
    assert len(manifest["entities"]) >= 2
    assert len(manifest["use_cases"]) >= 4
    assert len(manifest["integrations"]) == 1
    assert len(manifest["deliverables"]) == 2
    assert manifest_completeness_diagnostics(prompt, manifest) == []


def test_manifest_extracts_authoritative_requirements_from_run_instructions():
    instructions = """Create one Oracle SQL file.
My tables:
- ACCOUNTS: [account_id, account_no, opening_balance]
- TRANSACTIONS: [txn_id, account_id, amount]
## Expected deliverables
- DDL for both tables
- Account balance query
"""
    manifest = freeze_requirements_manifest(
        "OracleProject",
        {**TARGET, "language": "sql", "db_tech": "Oracle 23ai", "db_target": "oracle"},
        run_instructions=instructions,
    )

    assert [item["verbatim"] for item in manifest["entities"]] == [
        "ACCOUNTS: account_id, account_no, opening_balance",
        "TRANSACTIONS: txn_id, account_id, amount",
    ]
    assert len(manifest["deliverables"]) == 2
    assert manifest_completeness_diagnostics("OracleProject", manifest) == []


def test_ci_accountability_verbs_are_normalized_before_final_governance_gate():
    path = "Demo/.github/workflows/ci.yml"
    clear_generation_notes("Demo/")
    parse_generation_response(
        """<generated_file>
name: CI
</generated_file>
<generation_notes>
{"applied_rule_ids":[],"not_applicable_rules":[],"conflicts_resolved":[],"assumptions":[],"manifest_ids_covered":["INT-001"],"manifest_coverage":[{"id":"INT-001","actions":["build","deploy","push","scan"]}],"self_check":{"complete":true}}
</generation_notes>""",
        path,
    )

    actions = generation_notes_snapshot("Demo/")[path]["manifest_coverage"][0]["actions"]
    assert actions == ["implement", "configure", "transmit", "validate"]


def test_directory_only_project_tree_becomes_concrete_build_plan_artifacts():
    prompt = """Organize the solution exactly as follows:
```text
Example.sln
src/
  Example.Api/
  Example.Domain/
tests/
  Example.UnitTests/
frontend/
  web-client/
infrastructure/
  bicep/
```
"""
    required = _prompt_structure_baseline(prompt, "csharp")
    assert "Example.sln" in required
    assert "src/Example.Api/Example.Api.csproj" in required
    assert "src/Example.Domain/Example.Domain.csproj" in required
    assert "tests/Example.UnitTests/Example.UnitTests.csproj" in required
    assert "frontend/web-client/package.json" in required
    assert "infrastructure/bicep/main.bicep" in required


def test_generic_acceptance_gate_rejects_missing_prompt_contracts_and_artifacts():
    prompt = """Build a .NET 10 and Angular application with unit tests.
| Method | Route | Purpose |
|---|---|---|
| POST | /api/widgets | Create widget |
Include OpenAPI, Bicep, and GitHub Actions.
"""
    output = {
        "Demo/backend/Demo.csproj": "<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>",
        "Demo/frontend/package.json": '{"dependencies":{"@angular/core":"1.0.0"}}',
        "Demo/frontend/angular.json": "{}",
        "Demo/frontend/src/main.ts": "bootstrapApplication(AppComponent);",
        "Demo/backend/.github/workflows/ci.yml": "name: misplaced",
    }
    diagnostics = _requirement_coverage_diagnostics(output, prompt, "csharp")
    joined = "\n".join(diagnostics)
    assert "Requested endpoint is missing: POST /api/widgets" in joined
    assert "Requested .NET 10 target framework" in joined
    assert "test .csproj" in joined
    assert "repository root" in joined
    assert "OpenAPI contract is missing" in joined
    assert "Bicep infrastructure is missing" in joined


@pytest.mark.parametrize(
    ("language", "prompt", "output", "expected"),
    [
        (
            "python", "Build a Python FastAPI service using SQLAlchemy and pytest testing.",
            {"Demo/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"},
            ("dependency manifest", "Pydantic", "Alembic", "runnable test artifact", "test framework"),
        ),
        (
            "typescript", "Build a strict React TypeScript app with automated testing.",
            {
                "Demo/package.json": '{"scripts":{"build":"tsc"},"dependencies":{"react":"18.2.0","react-dom":"18.2.0"}}',
                "Demo/tsconfig.json": '{"compilerOptions":{"strict":false}}',
                "Demo/src/main.tsx": "createRoot(root).render(<App />);",
            },
            ("runnable test artifact", "runnable test script", "strict mode"),
        ),
        (
            "sql", "Create versioned SQL migrations for monetary amounts, foreign keys, row-version concurrency, and testing.",
            {"Demo/schema.sql": "CREATE TABLE transfer (id INTEGER PRIMARY KEY, amount FLOAT);"},
            ("runnable test artifact", "versioned", "foreign-key", "NUMERIC/DECIMAL", "row-version"),
        ),
    ],
)
def test_cross_language_acceptance_is_fail_closed(language, prompt, output, expected):
    diagnostics = "\n".join(_requirement_coverage_diagnostics(output, prompt, language))
    for fragment in expected:
        assert fragment in diagnostics


def test_oracle_number_precision_satisfies_monetary_sql_requirement():
    prompt = "Create an Oracle schema for monetary transfer amounts."
    output = {
        "Demo/database/schema.sql": (
            "CREATE TABLE transfers ("
            "transfer_id NUMBER PRIMARY KEY, amount NUMBER(15, 2) NOT NULL);"
        ),
    }

    diagnostics = _requirement_coverage_diagnostics(output, prompt, "sql")

    assert not any("precision and scale" in diagnostic for diagnostic in diagnostics)


def test_manifest_is_frozen_complete_and_tamper_evident():
    manifest = freeze_requirements_manifest(
        """Inputs:
- email: required email, maximum length 254
- amount: decimal between 0.01 and 9999
Outputs:
- receipt: JSON transfer receipt
Roles:
- operator
""",
        TARGET,
    )
    assert [item["id"] for item in manifest["inputs"]] == ["IN-001", "IN-002"]
    assert manifest["outputs"][0]["states"] == [
        "success", "empty", "validation_error", "unauthorized", "forbidden",
        "not_found", "conflict", "partial", "unexpected_error",
    ]
    assert manifest["inputs"][0]["constraints"]["max_length"] == 254
    assert manifest["inputs"][1]["type"] == "decimal"
    assert_frozen_manifest(manifest)
    manifest["inputs"][0]["name"] = "tampered"
    with pytest.raises(ValueError, match="checksum"):
        assert_frozen_manifest(manifest)


def test_guidelines_preserve_verbatim_scope_precedence_and_conflicts():
    guidelines = """=== client.md ===
- Use pytest for every test.
- Security: never log `password`.
"""
    instructions = "Use unittest for backend tests."
    result = atomize_guidelines(guidelines, instructions)
    assert [rule["id"] for rule in result["rules"]] == ["G-001", "G-002", "R-001"]
    assert result["rules"][0]["verbatim"] == "- Use pytest for every test."
    assert result["rules"][1]["precedence"] == 1
    assert result["conflicts"][0]["winner_rule_id"] == "G-001"
    assert [r["id"] for r in applicable_rules(result["rules"], "tests/test_api.py", "python")] == [
        "G-001", "G-002", "R-001",
    ]


def test_embedded_do_not_text_does_not_turn_canonical_json_into_banned_tokens():
    canonical = '''LOCKED CANONICAL CONTRACTS (all generated files must conform):
{
  "schema_version": 1,
  "target_stack": "Generate SQL. Do not hard-code literals.",
  "error_model": {"fields": ["code", "message"]},
  "checksum": "abc123"
}'''

    rules = atomize_guidelines(canonical)

    assert rules["rules"] == []

    corrected = atomize_guidelines(
        canonical + "\n\nMANDATORY REVIEW CORRECTIONS FOR THIS RUN:\nNever use `eval`."
    )
    assert [rule["verbatim"] for rule in corrected["rules"]] == ["Never use `eval`."]
    assert corrected["rules"][0]["machine_check"] == {
        "kind": "banned_tokens", "values": ["eval"],
    }


def test_machine_rules_ignore_platform_evidence_and_documentation():
    rules = atomize_guidelines("Never use `eval`.")
    evidence_only = {
        "Demo/governance/requirements-manifest.json": '{"rule": "eval"}',
        "Demo/.strat-aqorynth/provenance.json": '{"prompt": "never use eval"}',
        "Demo/README.md": "The generated source must never use eval.",
        "Demo/src/main.py": "def run():\n    return 1\n",
    }
    assert validate_machine_rules(evidence_only, rules, "python") == []

    evidence_only["Demo/src/main.py"] = "def run():\n    return eval('1')\n"
    assert validate_machine_rules(evidence_only, rules, "python") == [
        "Demo/src/main.py: G-001 bans 'eval'",
    ]


def test_attached_machine_config_is_retained_for_generated_repository():
    text = "=== .editorconfig ===\nroot = true\n\n[*]\nindent_size = 2\n"
    assert extract_attached_config_files(text) == {
        ".editorconfig": "root = true\n\n[*]\nindent_size = 2\n"
    }


def test_every_registered_language_has_an_editable_versioned_pack():
    languages = {target["language"] for target in TARGET_STACKS.values()}
    assert validate_language_pack_coverage(languages) == []
    for language in languages:
        packs = load_language_standards(language, next(t for t in TARGET_STACKS.values() if t["language"] == language))
        assert packs and all(pack["id"] and pack["version"] for pack in packs)
        assert any(pack.get("acceptance_policy", {}).get("language") == language for pack in packs)


def test_every_registered_language_has_acceptance_policy_and_project_build_route():
    from services.build_runner import PRODUCTION_PROJECT_BUILD_LANGUAGES

    languages = {target["language"] for target in TARGET_STACKS.values()}
    assert validate_language_acceptance_coverage(languages) == []
    assert languages <= PRODUCTION_PROJECT_BUILD_LANGUAGES
    for language in languages:
        policy = load_language_acceptance_policy(language)
        assert policy["language"] == language
        assert "manifests" in policy and "tests" in policy

    for artifact_language in ("github_actions", "jenkinsfile", "kubernetes", "hcl", "protobuf"):
        packs = load_language_standards(artifact_language, {"language": artifact_language})
        assert packs[0]["id"] == "iac-deployment"
        assert packs[0]["acceptance_policy"]["language"] == artifact_language


def test_governed_response_and_safe_structured_split():
    clear_generation_notes()
    notes = {
        "applied_rule_ids": ["G-001"], "not_applicable_rules": [],
        "conflicts_resolved": [], "assumptions": [],
        "manifest_ids_covered": ["IN-001", "OUT-001"],
        "manifest_coverage": [
            {"id": "IN-001", "actions": ["capture", "validate", "audit"]},
            {"id": "OUT-001", "actions": ["produce", "render", "error-state"]},
        ],
        "self_check": {"complete": True},
    }
    content = parse_generation_response(
        "<generated_file>\nprint('ok')\n</generated_file>\n<generation_notes>"
        + json.dumps(notes) + "</generation_notes>",
        "Demo/src/main.py",
    )
    assert content == "print('ok')\n"
    assert generation_notes_snapshot()["Demo/src/main.py"]["format_valid"] is True

    with pytest.raises(StructuredSplitRequest) as raised:
        parse_generation_response(
            '<split_request>{"reason":"large","files":[{"path":"src/a.py","purpose":"A"}]}</split_request>',
            "Demo/src/main.py",
        )
    assert validate_split_files(raised.value) == [{"path": "src/a.py", "purpose": "A"}]
    unsafe = StructuredSplitRequest("x", ({"path": "../escape.py", "purpose": "bad"},), "bad")
    with pytest.raises(ValueError, match="unsafe"):
        validate_split_files(unsafe)


def test_malformed_generation_notes_never_leak_envelope_into_source():
    clear_generation_notes()
    content = parse_generation_response(
        "<generated_file>\npublic class AccountService {}\n</generated_file>\n"
        "<generation_notes>{\"self_check\": {\"complete\": true}</generation_notes>",
        "Demo/backend/src/main/java/demo/AccountService.java",
    )
    assert content == "public class AccountService {}\n"
    notes = generation_notes_snapshot()["Demo/backend/src/main/java/demo/AccountService.java"]
    assert notes["format_valid"] is False
    assert "invalid generation notes JSON" in notes["error"]


def test_machine_enforcement_and_phase_e_reports():
    clear_generation_notes()
    manifest = freeze_requirements_manifest("Inputs:\n- email: required\nOutputs:\n- result: JSON", TARGET)
    rules = atomize_guidelines("Never use `eval`.\n", "")
    output = {"Demo/src/main.py": "def run():\n    return eval('1')\n"}
    diagnostics = validate_machine_rules(output, rules, "python")
    assert diagnostics == ["Demo/src/main.py: G-001 bans 'eval'"]

    plan = build_file_plan(["src/main.py"], manifest, rules, "python")
    artifacts, audit_diagnostics = build_audit_artifacts(
        "Demo", manifest, rules, load_language_standards("python", TARGET), plan, output, "python",
    )
    assert "Demo/governance/input-output-traceability.json" in artifacts
    assert any("G-001" in item for item in audit_diagnostics)


def test_per_file_governance_prompt_uses_relevant_slice_without_repeating_raw_request():
    request = (
        "Inputs:\n- transfer amount: required decimal\n"
        "Outputs:\n- transfer receipt: JSON response\n"
        "Non-functional requirements:\n- structured logging and health probes\n"
        "UNIQUE_RAW_REQUEST_MARKER"
    )
    manifest = freeze_requirements_manifest(request, TARGET)
    rendered = render_governance_prompt(
        manifest, atomize_guidelines("Never use `eval`."), [],
        path="backend/controllers/TransferController.py", language="python",
    )

    assert "UNIQUE_RAW_REQUEST_MARKER" not in rendered
    assert "request_sha256" in rendered
    assert "prompt_scope" in rendered
    assert "IN-001" in rendered
    assert "OUT-001" in rendered
    assert "G-001" in rendered

    planning_rendered = render_governance_prompt(
        manifest, atomize_guidelines("Never use `eval`."), [], language="python",
    )
    assert "<generated_file>" not in planning_rendered
    assert "per-file source envelope applies only" in planning_rendered
    assert "<generated_file>" in rendered


def test_every_supported_language_accepts_all_advertised_manifest_id_categories():
    manifest = freeze_requirements_manifest(
        """Inputs:
- request: required object
Outputs:
- result: JSON response
Entities:
- Transfer: persisted money transfer
Use cases:
- Submit a transfer
Roles:
- operator
Integrations:
- payment network
Non-functional requirements:
- Structured logging and health probes
""",
        TARGET,
    )
    expected_ids = {
        item["id"]
        for category in (
            "inputs", "outputs", "entities", "use_cases", "roles_permissions",
            "integrations", "non_functional_requirements",
        )
        for item in manifest[category]
    }
    assert any(identifier.startswith("NFR-") for identifier in expected_ids)

    coverage = []
    for identifier in sorted(expected_ids):
        actions = (
            ["capture", "validate", "audit"] if identifier.startswith("IN-") else
            ["produce", "render", "error-state"] if identifier.startswith("OUT-") else
            ["audit"]
        )
        coverage.append({"id": identifier, "actions": actions})

    path = "Demo/config/application.json"
    rules = atomize_guidelines()
    clear_generation_notes()
    register_generation_notes(path, {
        "applied_rule_ids": [],
        "not_applicable_rules": [],
        "conflicts_resolved": [],
        "assumptions": [],
        "manifest_ids_covered": sorted(expected_ids),
        "manifest_coverage": coverage,
        "self_check": {"complete": True},
    })
    output = {path: "{}\n"}

    for language in sorted({target["language"] for target in TARGET_STACKS.values()}):
        plan = build_file_plan(["config/application.json"], manifest, rules, language)
        artifacts, diagnostics = build_audit_artifacts(
            "Demo", manifest, rules, [], plan, output, language,
        )
        assert not [item for item in diagnostics if "unknown manifest" in item], language
        traceability = json.loads(artifacts["Demo/governance/input-output-traceability.json"])
        assert expected_ids == {row["id"] for row in traceability}, language


def test_guideline_upload_accepts_editorconfig_and_rejects_lossy_text():
    upload = UploadFile(filename=".editorconfig", file=io.BytesIO(b"root = true\n"))
    _images, text = asyncio.run(_extract_guide_text([upload]))
    assert text == "=== .editorconfig ===\nroot = true\n"

    invalid = UploadFile(filename="rules.md", file=io.BytesIO(b"\xff"))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(_extract_guide_text([invalid]))
    assert raised.value.status_code == 400


def test_multi_file_rules_are_applied_for_every_catalog_stack():
    rules = atomize_guidelines()
    for stack_id, target in TARGET_STACKS.items():
        manifest = freeze_requirements_manifest(
            "Build a governed service. Inputs: request payload. Outputs: operation result.",
            target,
        )
        paths = ["src/application.py", "tests/application_test.py"]
        plan = build_file_plan(paths, manifest, rules, target["language"])
        output = {f"Demo/{path}": "value = 1\n" for path in paths}
        clear_generation_notes("Demo/")

        artifacts, _diagnostics = build_audit_artifacts(
            "Demo", manifest, rules, [], plan, output, target["language"],
            single_artifact=False,
        )
        audit = json.loads(artifacts["Demo/governance/generation-audit.json"])
        trace = json.loads(artifacts["Demo/governance/input-output-traceability.json"])

        assert audit["evaluation_mode"] == {
            "requested": "multi_file",
            "applied": "multi_file",
            "implementation_files": paths,
            "mismatch": False,
        }, stack_id
        assert trace and all(row["evaluation_mode"] == "multi_file" for row in trace), stack_id
        assert all(row["planning_status"] in {"planned", "unplanned"} for row in trace), stack_id
    clear_generation_notes("Demo/")


def test_single_artifact_exemptions_cannot_leak_into_multiple_implementation_files():
    manifest = freeze_requirements_manifest(
        "Build an inventory service. Inputs: SKU. Outputs: availability result.", TARGET,
    )
    rules = atomize_guidelines()
    paths = ["src/inventory.py", "tests/test_inventory.py"]
    plan = build_file_plan(paths, manifest, rules, "python")
    output = {f"Demo/{path}": "value = 1\n" for path in paths}
    clear_generation_notes("Demo/")

    artifacts, diagnostics = build_audit_artifacts(
        "Demo", manifest, rules, [], plan, output, "python", single_artifact=True,
    )
    audit = json.loads(artifacts["Demo/governance/generation-audit.json"])
    trace = json.loads(artifacts["Demo/governance/input-output-traceability.json"])
    clear_generation_notes("Demo/")

    assert audit["evaluation_mode"]["requested"] == "single_artifact"
    assert audit["evaluation_mode"]["applied"] == "multi_file"
    assert audit["evaluation_mode"]["mismatch"] is True
    assert any("evaluation mode mismatch" in item.casefold() for item in diagnostics)
    assert all(row["evaluation_mode"] == "multi_file" for row in trace)
    assert all("not_applicable_actions" not in row for row in trace)


def test_single_artifact_mode_allows_only_one_implementation_plus_readme():
    manifest = freeze_requirements_manifest("Create one Python source file.", TARGET)
    rules = atomize_guidelines()
    paths = ["generated.py", "README.md"]
    plan = build_file_plan(paths, manifest, rules, "python")
    output = {"Demo/generated.py": "print('ok')\n", "Demo/README.md": "# Demo\n"}
    clear_generation_notes("Demo/")

    artifacts, diagnostics = build_audit_artifacts(
        "Demo", manifest, rules, [], plan, output, "python", single_artifact=True,
    )
    audit = json.loads(artifacts["Demo/governance/generation-audit.json"])
    clear_generation_notes("Demo/")

    assert audit["evaluation_mode"]["applied"] == "single_artifact"
    assert audit["evaluation_mode"]["implementation_files"] == ["generated.py"]
    assert not [item for item in diagnostics if "evaluation mode mismatch" in item.casefold()]


def test_guideline_upload_rejects_text_that_would_be_silently_truncated():
    upload = UploadFile(filename="security.md", file=io.BytesIO(b"x" * 60_001))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(_extract_guide_text([upload]))
    assert raised.value.status_code == 413
    assert "without truncation" in raised.value.detail


def test_project_creation_preserves_guideline_traceability_metadata():
    configuration, mode = _project_creation_configuration({
        "configuration": {
            "origin_mode": "prompt",
            "generation_guidelines": "Never log credentials.",
            "guideline_documents": [{"name": "security.md"}],
            "guidelines_sha256": "abc123",
        },
    })
    assert mode == "prompt"
    assert configuration["generation_guidelines"] == "Never log credentials."
    assert configuration["guideline_documents"] == [{"name": "security.md"}]
    assert configuration["guidelines_sha256"] == "abc123"


def test_empty_python_exception_class_is_not_a_placeholder_but_function_pass_is():
    from services.generation_governance import validate_machine_rules

    exception_only = {
        "Demo/generated.py": (
            "class AuthenticationError(Exception):\n"
            "    \"\"\"Raised for invalid credentials.\"\"\"\n"
            "    pass\n"
        ),
    }
    function_stub = {"Demo/generated.py": "def validate():\n    pass\n"}
    guidelines = {"rules": []}

    assert validate_machine_rules(exception_only, guidelines, "python") == []
    assert any(
        "incomplete placeholder" in item
        for item in validate_machine_rules(function_stub, guidelines, "python")
    )


def test_declarative_python_marker_and_redundant_pass_are_not_placeholders():
    from services.generation_governance import validate_machine_rules

    source = {
        "Demo/generated.py": (
            "class Base(DeclarativeBase):\n"
            "    pass\n\n"
            "async def startup():\n"
            "    logger.info('starting')\n"
            "    pass\n"
        ),
    }

    assert validate_machine_rules(source, {"rules": []}, "python") == []


def test_guarded_react_null_render_is_not_an_incomplete_placeholder():
    from services.generation_governance import _first_placeholder, validate_machine_rules

    source = (
        "interface Props { visible: boolean }\n"
        "export function StatusPanel({ visible }: Props) {\n"
        "  if (!visible) {\n"
        "    return null; // Intentionally render nothing while hidden.\n"
        "  }\n"
        "  return <section aria-label=\"status\">Ready</section>;\n"
        "}\n"
    )

    assert _first_placeholder(source, "frontend/StatusPanel.tsx") is None
    assert validate_machine_rules(
        {"frontend/StatusPanel.tsx": source}, {"rules": []}, "typescript",
    ) == []


def test_unconditional_react_null_and_typescript_service_null_remain_blocked():
    from services.generation_governance import _first_placeholder

    null_component = "export function EmptyPage() {\n  return null;\n}\n"
    null_service = "export function loadUser(): User | null {\n  return null;\n}\n"

    assert _first_placeholder(null_component, "frontend/EmptyPage.tsx") is not None
    assert _first_placeholder(null_service, "frontend/UserService.ts") is not None


def test_single_file_generation_gives_every_validation_attempt_a_complete_budget():
    from services.modernizer._shared import _SINGLE_FILE_DRAFT_MAX_SECONDS

    result = SimpleNamespace(passed=True, checker="parser", path="generated.py", language="python", diagnostics=[])
    with patch(
        "services.modernizer.validation_orchestration._generate_validated",
        return_value=("print('ok')\n# <<<END_OF_FILE>>>\n", result, 1),
    ) as generated:
        output, validation = _pf_try_single_file(
            "print hello", {**TARGET, "llm_persona": "a Python engineer"}, "python", "Demo", "",
            "", "", "", "", [], "model", lambda *_args: None,
        )
    assert output["__single_file__"] == "print('ok')\n"
    assert validation["generation_validated"] is True
    assert validation["token_budget"]["strategy"] == "single_shot"
    assert generated.call_args.kwargs["generation_attempt_max_seconds"] == _SINGLE_FILE_DRAFT_MAX_SECONDS
    assert generated.call_args.kwargs["generation_max_seconds"] == _SINGLE_FILE_DRAFT_MAX_SECONDS
    assert generated.call_args.kwargs["max_attempts"] == 2


def test_single_file_generation_switches_to_structural_fallback_on_timeout():
    result = SimpleNamespace(
        passed=True, checker="parser", path="generated.sql", language="sql", diagnostics=[],
    )
    progress_events = []
    with patch(
        "services.modernizer.validation_orchestration._generate_validated",
        side_effect=RuntimeError(
            "LLM generation failed: timed out waiting for an Ollama inference slot"
        ),
    ) as generated, patch(
        "services.modernizer.prompt_pipeline._pf_generate_structural_single_file",
        return_value=("SELECT 1 FROM dual;\n", result, 3),
    ) as structural:
        output, validation = _pf_try_single_file(
            "Generate Oracle SQL", {
                **TARGET, "language": "sql", "db_tech": "Oracle 23ai",
                "db_target": "oracle", "llm_persona": "an Oracle engineer",
            }, "sql", "Demo", "", "", "", "", "", [], "model",
            lambda *args: progress_events.append(args),
        )

    assert output["__single_file__"] == "SELECT 1 FROM dual;\n"
    assert validation["generation_validated"] is True
    assert validation["token_budget"]["strategy"] == "structural"
    assert generated.call_count == 1
    assert structural.call_count == 1
    assert any("switching to structural member generation" in event[2] for event in progress_events)


def test_large_single_file_contract_skips_the_slow_monolithic_draft():
    result = SimpleNamespace(
        passed=True, checker="parser", path="generated.sql", language="sql", diagnostics=[],
    )
    requirements = "\n".join(
        f"{index}. procedure TASK_{index} implements deliverable {index}"
        for index in range(1, 13)
    )
    events = []
    with patch(
        "services.modernizer.validation_orchestration._generate_validated",
    ) as generated, patch(
        "services.modernizer.prompt_pipeline._pf_generate_structural_single_file",
        return_value=("SELECT 1 FROM dual;\n", result, 12),
    ) as structural:
        output, validation = _pf_try_single_file(
            requirements, {
                **TARGET, "language": "sql", "db_tech": "Oracle 23ai",
                "db_target": "oracle", "llm_persona": "an Oracle engineer",
            }, "sql", "Demo", "", "", "", "", "", [], "model",
            lambda *args: events.append(args),
        )

    assert output["__single_file__"] == "SELECT 1 FROM dual;\n"
    assert generated.call_count == 0
    assert structural.call_count == 1
    assert validation["token_budget"]["strategy"] == "structural"
    assert validation["token_budget"]["generation_calls"] == 12
    assert validation["token_budget"]["contract_members"] == 12
    assert validation["retried"] == 0
    assert any("starting structural member generation directly" in event[2] for event in events)


def test_structural_sql_generates_and_validates_independent_members():
    from services.modernizer._shared import _adaptive_num_ctx

    contract = FileContract(
        "Demo/generated.sql", "default",
        [
            {"name": name, "marker_name": name, "coverage_name": name,
             "type": "method", "signature": f"view {name}"}
            for name in ("V_LEDGER", "V_AVAILABLE")
        ],
        [],
    )

    def fake_generate(prompt, **_kwargs):
        assert "SKELETON ONLY" not in prompt
        name = "V_LEDGER" if "view V_LEDGER" in prompt else "V_AVAILABLE"
        return f"CREATE OR REPLACE VIEW {name} AS SELECT 1 AS amount FROM dual;\n"

    valid = ValidationResult("Demo/generated.sql", "sql", "parser", True, [])
    with patch("services.llm.generate", side_effect=fake_generate) as generated, patch(
        "services.validators.validate_file", return_value=valid,
    ) as validate:
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create both Oracle views", system="SQL only", model="model",
            contract=contract, language="sql", dialect="oracle",
            progress=lambda *_args: None, max_seconds=30,
        )

    assert content.index("V_LEDGER") < content.index("V_AVAILABLE")
    assert result.passed is True
    assert calls == 2
    assert generated.call_count == 2
    assert "<<<END_OF_FILE>>>" in validate.call_args.args[1]
    for call in generated.call_args_list:
        assert call.kwargs["num_ctx"] == _adaptive_num_ctx(
            len("SQL only") + len(call.args[0]), call.kwargs["max_tokens"],
        )


def test_structural_sql_retries_a_timed_out_member_with_compact_prompt():
    contract = FileContract(
        "OracleDbScript/generated.sql", "default",
        [{
            "name": "V_ACCOUNT_BALANCE", "marker_name": "V_ACCOUNT_BALANCE",
            "coverage_name": "V_ACCOUNT_BALANCE", "type": "method",
            "signature": "Create view V_ACCOUNT_BALANCE",
        }],
        ["Oracle"],
    )
    valid = ValidationResult("OracleDbScript/generated.sql", "sql", "parser", True, [])
    with patch("services.llm.generate", side_effect=[
        TimeoutError("Ollama generation exceeded the 600s aggregate budget"),
        "CREATE OR REPLACE VIEW V_ACCOUNT_BALANCE AS SELECT 1 amount FROM dual;\n",
    ]) as generated, patch("services.validators.validate_file", return_value=valid):
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create an Oracle balance view", system="SQL only", model="model",
            contract=contract, language="sql", dialect="oracle",
            progress=lambda *_args: None, max_seconds=30,
        )

    assert "CREATE OR REPLACE VIEW V_ACCOUNT_BALANCE" in content
    assert result.passed is True
    assert calls == 2
    assert "ORIGINAL FILE CONTRACT" in generated.call_args_list[0].args[0]
    assert "ORIGINAL FILE CONTRACT" not in generated.call_args_list[1].args[0]


def test_structural_sql_discards_only_dangling_statement_after_member_timeout():
    contract = FileContract(
        "OracleDbScript/generated.sql", "default",
        [{
            "name": "requirement_2", "marker_name": "requirement_2",
            "coverage_name": "", "type": "method",
            "signature": "Sample INSERT data covering all transaction statuses",
        }],
        ["Oracle"],
    )

    def timeout_with_complete_rows_and_dangling_tail(prompt, **kwargs):
        partial = (
            "INSERT INTO transactions (txn_id, status) VALUES (1, 'POSTED');\n"
            "INSERT INTO transactions (txn_id, status) VALUES (2, 'PENDING');\n"
            "INSERT INTO transactions (txn_id, status) VALUES (3,"
            if "ORIGINAL FILE CONTRACT" in prompt else
            "INSERT INTO transactions (txn_id, status) VALUES (4,"
        )
        kwargs["on_token"](partial)
        raise TimeoutError("Ollama generation exceeded the member budget")

    events = []
    with patch(
        "services.llm.generate", side_effect=timeout_with_complete_rows_and_dangling_tail,
    ) as generated:
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create one Oracle SQL file", system="Oracle SQL only", model="model",
            contract=contract, language="sql", dialect="oracle",
            progress=lambda *args: events.append(args), max_seconds=30,
        )

    assert generated.call_count == 2
    assert calls == 2
    assert result.passed is True
    assert "VALUES (3," not in content
    assert "VALUES (1, 'POSTED')" in content
    assert "VALUES (2, 'PENDING')" in content
    assert any("discarded only the invalid trailing statement" in event[2] for event in events)


def test_structural_member_double_timeout_retains_longest_streamed_fragment():
    contract = FileContract(
        "OracleDbScript/generated.sql", "default",
        [{"name": "quality_checks", "marker_name": "quality_checks",
          "coverage_name": "quality_checks", "type": "method",
          "signature": "Create data-quality checks"}],
        ["Oracle"],
    )
    passed = ValidationResult(contract.path, "sql", "compiler", True, [])

    def timeout_with_partial(prompt, **kwargs):
        partial = (
            "SELECT account_id FROM accounts WHERE opening_balance < 0;\n"
            if "ORIGINAL FILE CONTRACT" in prompt
            else "SELECT account_id FROM accounts\n"
        )
        kwargs["on_token"](partial)
        raise TimeoutError("Ollama generation exceeded the 334s per-file budget")

    events = []
    with patch("services.llm.generate", side_effect=timeout_with_partial) as generated, patch(
        "services.validators.validate_file", return_value=passed,
    ):
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create Oracle data-quality checks", system="Oracle SQL only",
            model="model", contract=contract, language="sql", dialect="oracle",
            progress=lambda *args: events.append(args), max_seconds=334,
        )

    assert generated.call_count == 2
    assert calls == 2
    assert result.passed is False
    assert "opening_balance < 0" in content
    assert "exceeded its bounded generation attempts" in "\n".join(result.diagnostics)
    assert any("retaining the longest streamed fragment" in event[2] for event in events)


def test_whole_file_double_timeout_retains_partial_without_third_generation_call():
    contract = FileContract("Demo/generated.py", "default", [], [])
    parsed = ValidationResult(contract.path, "python", "parser", True, [])

    def timeout_with_partial(prompt, **kwargs):
        partial = (
            "def completed_section():\n    return 1\n"
            if "ORIGINAL FILE CONTRACT" in prompt
            else "def completed_section():\n    return 1\n\ndef unfinished_section():\n"
        )
        kwargs["on_token"](partial)
        raise TimeoutError("Ollama generation exceeded the 334s per-file budget")

    with patch("services.llm.generate", side_effect=timeout_with_partial) as generated, patch(
        "services.validators.validate_file", return_value=parsed,
    ), patch(
        "services.modernizer.validation_orchestration._generate_validated",
    ) as repaired:
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create a complete Python module", system="Python only", model="model",
            contract=contract, language="python", dialect="",
            progress=lambda *_args: None, max_seconds=334,
        )

    assert generated.call_count == 2
    assert repaired.call_count == 0
    assert calls == 2
    assert result.passed is False
    assert "unfinished_section" in content
    assert "exceeded its bounded generation attempts" in "\n".join(result.diagnostics)


def test_whole_file_repair_timeout_retains_original_invalid_artifact():
    contract = FileContract("Demo/generated.py", "default", [], [])
    failed = ValidationResult(
        contract.path, "python", "compiler", False, ["python parse error"],
    )
    events = []
    with patch("services.llm.generate", return_value="def broken(:\n") as generated, patch(
        "services.validators.validate_file", return_value=failed,
    ), patch(
        "services.modernizer.validation_orchestration._generate_validated",
        side_effect=TimeoutError("Ollama generation exceeded the 334s per-file budget"),
    ) as repaired:
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create a complete Python module", system="Python only", model="model",
            contract=contract, language="python", dialect="",
            progress=lambda *args: events.append(args), max_seconds=334,
        )

    assert generated.call_count == 1
    assert repaired.call_count == 1
    assert calls == 2
    assert content == "def broken(:\n"
    assert result.passed is False
    assert "bounded complete-file repair failed" in "\n".join(result.diagnostics)
    assert any("retaining the original artifact" in event[2] for event in events)


def test_structural_skeleton_timeout_retains_stream_and_generates_members():
    contract = FileContract(
        "Demo/Service.java", "default",
        [{"name": "run", "marker_name": "run", "coverage_name": "run",
          "type": "method", "signature": "Implement public void run()"}],
        [],
    )
    passed = ValidationResult(contract.path, "java", "compiler", True, [])
    failed = ValidationResult(contract.path, "java", "compiler", False, ["missing class close"])

    def generate(prompt, **kwargs):
        if "SKELETON ONLY" in prompt:
            kwargs["on_token"]("public class Service {\n")
            raise TimeoutError("timed out waiting for an Ollama inference slot")
        return "public void run() {}\n"

    def validate(_path, content, _language, dialect_hint=""):
        return passed if content.strip() == "public void run() {}" else failed

    events = []
    with patch("services.llm.generate", side_effect=generate) as generated, patch(
        "services.validators.validate_file", side_effect=validate,
    ):
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create Service", system="Java only", model="model",
            contract=contract, language="java", dialect="",
            progress=lambda *args: events.append(args), max_seconds=30,
        )

    assert generated.call_count == 2
    assert calls == 2
    assert "public class Service" in content
    assert "public void run()" in content
    assert result.passed is False
    assert any("skeleton exhausted" in event[2].casefold() for event in events)


def test_structural_sql_repairs_only_invalid_member_not_complete_file():
    contract = FileContract(
        "OracleDbScript/generated.sql", "default",
        [
            {"name": "ledger", "marker_name": "ledger", "coverage_name": "ledger",
             "type": "method", "signature": "Create ledger view"},
            {"name": "summary", "marker_name": "summary", "coverage_name": "summary",
             "type": "method", "signature": "Create summary procedure"},
        ],
        ["Oracle"],
    )
    passed = ValidationResult(contract.path, "sql", "compiler", True, [])
    failed = ValidationResult(
        contract.path, "sql", "compiler", False, ["oracle parse error: invalid summary"],
    )

    def generate(prompt, **_kwargs):
        if "PREVIOUS ATTEMPT FAILED VALIDATION" in prompt:
            assert "summary" in prompt.casefold()
            return "CREATE OR REPLACE PROCEDURE summary AS BEGIN NULL; END;\n/\n"
        if "Create ledger view" in prompt:
            return "CREATE OR REPLACE VIEW ledger AS SELECT 1 amount FROM dual;\n"
        if "Create summary procedure" in prompt:
            return "BAD SUMMARY\n"
        raise AssertionError("complete-file generation must not be requested")

    def validate(_path, content, _language, dialect_hint=""):
        assert dialect_hint == "oracle"
        if "BAD SUMMARY" in content:
            return failed
        return passed

    with patch("services.llm.generate", side_effect=generate) as generated, patch(
        "services.validators.validate_file", side_effect=validate,
    ):
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create ledger and summary", system="Oracle SQL only", model="model",
            contract=contract, language="sql", dialect="oracle",
            progress=lambda *_args: None, max_seconds=600,
        )

    assert result.passed is True
    assert "BAD SUMMARY" not in content
    assert "PROCEDURE summary" in content
    assert calls == 3
    assert generated.call_count == 3
    assert generated.call_args_list[-1].kwargs["max_seconds"] == 240.0


def test_structural_sql_member_repair_timeout_retains_artifact_as_validation_failure():
    contract = FileContract(
        "OracleDbScript/generated.sql", "default",
        [{"name": "summary", "marker_name": "summary", "coverage_name": "summary",
          "type": "method", "signature": "Create summary procedure"}],
        ["Oracle"],
    )
    failed = ValidationResult(
        contract.path, "sql", "compiler", False, ["oracle parse error: invalid summary"],
    )

    with patch(
        "services.llm.generate",
        side_effect=["BAD SUMMARY\n", TimeoutError("Ollama generation exceeded 240s")],
    ) as generated, patch(
        "services.validators.validate_file", return_value=failed,
    ):
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create summary", system="Oracle SQL only", model="model",
            contract=contract, language="sql", dialect="oracle",
            progress=lambda *_args: None, max_seconds=600,
        )

    assert result.passed is False
    assert "BAD SUMMARY" in content
    assert calls == 2
    assert generated.call_count == 2
    assert generated.call_args_list[-1].kwargs["max_seconds"] == 240.0


def test_structural_svg_uses_deterministic_xml_envelope_and_inner_fragment():
    contract = FileContract("Artwork/generated.svg", "default", [], [])

    def fake_generate(prompt, **_kwargs):
        assert "SKELETON ONLY" not in prompt
        assert "inside the existing outer <svg> root" in prompt
        return '<circle cx="512" cy="512" r="240" fill="#2563eb"/>\n'

    with patch("services.llm.generate", side_effect=fake_generate) as generated:
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create one accessible blue-circle SVG", system="SVG source only",
            model="model", contract=contract, language="svg", dialect="",
            progress=lambda *_args: None, max_seconds=30,
        )

    assert content.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert '<circle cx="512"' in content
    assert content.rstrip().endswith("</svg>")
    assert result.passed is True
    assert calls == 1
    assert generated.call_count == 1


def test_structural_members_honor_single_lane_override_and_emit_live_heartbeats():
    contract = FileContract(
        "Demo/generated.sql", "default",
        [{
            "name": "V_LEDGER", "marker_name": "V_LEDGER",
            "coverage_name": "V_LEDGER", "type": "method",
            "signature": "Create view V_LEDGER",
        }],
        [],
    )
    events = []
    stream_updates = []

    def progress(*args):
        events.append(args)

    progress.on_single_file_stream = lambda token, reset=False: stream_updates.append((token, reset))

    def fake_generate(_prompt, **kwargs):
        kwargs["on_token"]("CREATE OR REPLACE VIEW V_LEDGER ")
        kwargs["on_token"]("AS SELECT 1 amount FROM dual;\n")
        return "CREATE OR REPLACE VIEW V_LEDGER AS SELECT 1 amount FROM dual;\n"

    valid = ValidationResult("Demo/generated.sql", "sql", "parser", True, [])
    with patch.dict(os.environ, {"MODERNIZATION_SINGLE_FILE_MEMBER_WORKERS": "1"}), patch(
        "services.llm.generate", side_effect=fake_generate,
    ), patch("services.validators.validate_file", return_value=valid):
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create V_LEDGER", system="SQL only", model="model",
            contract=contract, language="sql", dialect="oracle",
            progress=progress, max_seconds=30,
        )

    assert "V_LEDGER" in content
    assert result.passed is True
    assert calls == 1
    assert any("across 1 parallel lane(s)" in event[2] for event in events)
    assert any("starting member 1/1" in event[2] for event in events)
    assert any("characters streamed" in event[2] for event in events)
    assert any("CREATE OR REPLACE VIEW" in token for token, reset in stream_updates if not reset)


def test_structural_sql_removes_inline_fence_artifacts_before_validation():
    contract = FileContract(
        "Demo/generated.sql", "default",
        [{
            "name": "REPORT", "marker_name": "REPORT",
            "coverage_name": "REPORT", "type": "method",
            "signature": "Create the REPORT query",
        }],
        [],
    )

    with patch.dict(os.environ, {"MODERNIZATION_SINGLE_FILE_MEMBER_WORKERS": "1"}), patch(
        "services.llm.generate",
        return_value="SELECT 1 AS REPORT_```sql\nFROM dual;\n",
    ):
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create REPORT", system="SQL only", model="model",
            contract=contract, language="sql", dialect="oracle",
            progress=lambda *_args: None, max_seconds=30,
        )

    assert "```" not in content
    assert result.passed is True, result.diagnostics
    assert calls == 1


def test_structural_members_run_as_bounded_parallel_batch_and_assemble_in_order():
    contract = FileContract(
        "Demo/generated.sql", "default",
        [
            {
                "name": "V_FIRST", "marker_name": "V_FIRST",
                "coverage_name": "V_FIRST", "type": "method",
                "signature": "Create view V_FIRST",
            },
            {
                "name": "V_SECOND", "marker_name": "V_SECOND",
                "coverage_name": "V_SECOND", "type": "method",
                "signature": "Create view V_SECOND",
            },
        ],
        [],
    )
    events = []
    activity_lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_generate(prompt, **kwargs):
        nonlocal active, max_active
        with activity_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            # Finish the second declaration first to prove assembly follows the
            # contract rather than thread completion order.
            if "Implement only this member responsibility:\nCreate view V_FIRST" in prompt:
                time.sleep(0.06)
                source = "CREATE VIEW V_FIRST AS SELECT 1 AS value;\n"
            else:
                time.sleep(0.02)
                source = "CREATE VIEW V_SECOND AS SELECT 2 AS value;\n"
            kwargs["on_token"](source)
            return source
        finally:
            with activity_lock:
                active -= 1

    valid = ValidationResult("Demo/generated.sql", "sql", "parser", True, [])
    env = {
        "MODERNIZATION_SINGLE_FILE_MEMBER_WORKERS": "4",
        "OLLAMA_MAX_CONCURRENT_REQUESTS": "2",
    }
    with patch.dict(os.environ, env, clear=False), patch(
        "services.llm.generate", side_effect=fake_generate,
    ), patch("services.validators.validate_file", return_value=valid):
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create both views", system="SQL only", model="model",
            contract=contract, language="sql", dialect="sqlserver",
            progress=lambda *args: events.append(args), max_seconds=30,
        )

    assert max_active == 2
    assert result.passed is True
    assert calls == 2
    assert content.index("CREATE VIEW V_FIRST") < content.index("CREATE VIEW V_SECOND")
    assert any("across 2 parallel lane(s)" in event[2] for event in events)


def test_structural_member_width_is_capped_by_inference_capacity():
    with patch.dict(os.environ, {
        "MODERNIZATION_SINGLE_FILE_MEMBER_WORKERS": "4",
        "OLLAMA_MAX_CONCURRENT_REQUESTS": "2",
    }, clear=True):
        assert _pf_structural_member_workers(12) == 2

    with patch.dict(os.environ, {
        "MODERNIZATION_SINGLE_FILE_MEMBER_WORKERS": "1",
        "OLLAMA_MAX_CONCURRENT_REQUESTS": "2",
    }, clear=True):
        assert _pf_structural_member_workers(12) == 1
