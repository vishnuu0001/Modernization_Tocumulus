"""Regression tests for the prompt-driven single-file generation path."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from services.generation_governance import (
    build_audit_artifacts,
    build_file_plan,
    clear_generation_notes,
    freeze_requirements_manifest,
    generation_notes_snapshot,
    load_language_standards,
    parse_generation_response,
    register_generation_notes,
    render_governance_prompt,
)
from services.modernizer.prompt_pipeline import (
    _pf_effective_single_file_prompt,
    _pf_can_use_fast_single_file_model,
    _pf_extract_single_file_notes_block,
    _pf_register_verified_single_file_notes,
    _pf_enforceable_project_guidelines, _pf_single_file_run_guidelines,
    _pf_generate_structural_single_file,
    _pf_single_file_attempt,
    _pf_single_file_production_rules,
    _pf_single_file_repair_notes,
    _pf_try_single_file,
)
from services.modernizer.target_config import TARGET_STACKS
from services.modernizer.token_budgeting import FileContract
from services.modernizer.validation_orchestration import (
    _generation_template,
    _normalize_oracle_package_terminators,
    _repair_oracle_bind_variable_views,
    _repair_oracle_multirow_values,
)
from services.validators import ValidationResult, validate_file

TARGET = {
    "name": "Python 3.12 FastAPI",
    "language": "python",
    "backend_tech": "FastAPI 0.115 + Pydantic 2",
    "frontend_tech": "",
    "db_tech": "PostgreSQL 16 + SQLAlchemy 2",
    "db_target": "postgres",
    "llm_persona": "a Python engineer",
}
PASSED = SimpleNamespace(
    passed=True, checker="parser", path="generated.py", language="python", diagnostics=[],
)


def test_language_only_prompt_recovers_explicit_custom_stack_requirement():
    assert _pf_effective_single_file_prompt(
        "Python",
        "Custom stack prompt: Generate a Python Script to Check and Validate user name and password; "
        "Business criticality: Medium",
    ) == "Generate a Python Script to Check and Validate user name and password"


def test_oracle_label_recovers_custom_stack_database_requirement():
    assert _pf_effective_single_file_prompt(
        "Oracle",
        "Custom stack prompt: Create an Oracle schema with login credentials and stored procedures; "
        "Business criticality: Medium",
    ) == "Create an Oracle schema with login credentials and stored procedures"


def test_oracle_script_label_recovers_custom_stack_database_requirement():
    assert _pf_effective_single_file_prompt(
        "Oracle Script",
        "Custom stack prompt: Create an Oracle package to store and retrieve user credentials; "
        "Business criticality: Medium",
    ) == "Create an Oracle package to store and retrieve user credentials"


def test_plural_oracle_scripts_label_recovers_custom_stack_requirement():
    assert _pf_effective_single_file_prompt(
        "Oracle Scripts",
        "Custom stack prompt: Create an Oracle package to store login credentials; "
        "Business criticality: Medium",
    ) == "Create an Oracle package to store login credentials"


def test_structured_custom_stack_metadata_is_not_duplicated_as_run_guideline():
    structured = (
        "Custom stack prompt: Create an Oracle package; Language: SQL; "
        "Business criticality: Medium"
    )
    assert _pf_single_file_run_guidelines(structured) == ""
    assert _pf_single_file_run_guidelines(
        "Never log credentials. Use snake_case names."
    ) == "Never log credentials. Use snake_case names."


def test_platform_contract_and_project_metadata_are_not_reparsed_as_guidelines():
    guide = (
        'LOCKED CANONICAL CONTRACTS (all generated files must conform):\n'
        '{"schema_version": 1, "prompt": "Do not hard-code literals."}\n\n'
        'AUTHORITATIVE PROJECT DETAILS captured at project creation '\
        '(preserve these in generated design, configuration, documentation, and code where applicable):\n'
        '- Application: Demo\n'
    )
    assert _pf_enforceable_project_guidelines(guide) == ""
    assert _pf_enforceable_project_guidelines(
        guide + "\nMANDATORY REVIEW CORRECTIONS FOR THIS RUN:\nNever use `eval`."
    ) == "Never use `eval`."
    assert _pf_enforceable_project_guidelines("Never use `eval`.") == "Never use `eval`."


def test_user_uploaded_guidelines_survive_governed_project_envelope():
    guide = (
        'LOCKED CANONICAL CONTRACTS (all generated files must conform):\n{}\n\n'
        'AUTHORITATIVE PROJECT DETAILS captured at project creation\n- Application: Demo\n\n'
        'USER-PROVIDED GENERATION GUIDELINES (mandatory and traceable):\n'
        'Never log credentials.\nUse parameterized SQL.\n'
        'MANDATORY REVIEW CORRECTIONS FOR THIS RUN:\nUse immutable DTOs.'
    )
    assert _pf_enforceable_project_guidelines(guide) == (
        'Never log credentials.\nUse parameterized SQL.\n\nUse immutable DTOs.'
    )


def test_oracle_procedure_registers_mechanically_verified_trace_actions():
    path = "Oracle/generated.sql"
    target = {
        **TARGET,
        "name": "Oracle Database",
        "language": "sql",
        "backend_tech": "PL/SQL",
        "db_tech": "Oracle Database",
        "db_target": "oracle",
    }
    manifest = freeze_requirements_manifest(
        "Create an Oracle procedure to store and retrieve login credentials.", target,
    )
    code = (
        "CREATE OR REPLACE PROCEDURE store_credentials(\n"
        "  p_username IN VARCHAR2, p_password IN VARCHAR2, p_status OUT NUMBER\n"
        ") AS BEGIN\n"
        "  IF p_username IS NULL THEN RAISE_APPLICATION_ERROR(-20001, 'required'); END IF;\n"
        "  INSERT INTO login_credentials(username, password_hash) VALUES (p_username, p_password);\n"
        "  p_status := 0;\n"
        "EXCEPTION WHEN OTHERS THEN p_status := 1;\n"
        "END;\n/\n"
    )
    clear_generation_notes("Oracle/")
    _pf_register_verified_single_file_notes(
        code=code, governed_path=path, language="sql", manifest=manifest,
        guideline_set={"rules": []},
    )
    notes = generation_notes_snapshot("Oracle/")[path]

    claims = {claim["id"]: set(claim["actions"]) for claim in notes["manifest_coverage"]}
    for item in manifest["inputs"]:
        assert {"capture", "validate"} <= claims[item["id"]]
    for item in manifest["outputs"]:
        assert {"produce", "render", "error-state"} <= claims[item["id"]]

    rules = {"rules": [], "conflicts": [], "checksum": "0"}
    plan = build_file_plan(["generated.sql"], manifest, rules, "sql")
    plan[0]["requirement_ids"] = [
        item["id"]
        for category in (
            "inputs", "outputs", "entities", "use_cases", "roles_permissions",
            "integrations", "non_functional_requirements", "deliverables",
        )
        for item in manifest.get(category, [])
    ]
    _artifacts, diagnostics = build_audit_artifacts(
        "Oracle", manifest, rules, load_language_standards("sql", target),
        plan, {path: code}, "sql", single_artifact=True,
    )
    clear_generation_notes("Oracle/")

    assert not [item for item in diagnostics if "traceability evidence" in item]
    assert not [item for item in diagnostics if "accountability notes" in item]


def test_validated_oracle_project_artifact_reconciles_all_traceability_categories():
    path = "OracleProject/database/generated.sql"
    target = {
        **TARGET,
        "name": "Oracle Database", "language": "sql", "backend_tech": "PL/SQL",
        "db_tech": "Oracle Database", "db_target": "oracle",
    }
    manifest = freeze_requirements_manifest(
        "OracleProject", target, run_instructions="""Create one Oracle SQL file.
My tables:
- ACCOUNTS: [account_id, opening_balance]
- TRANSACTIONS: [txn_id, account_id, amount]
## Deliverables
1. Create both tables.
2. Add a balance function and query.
""",
    )
    code = """CREATE TABLE ACCOUNTS (
  account_id NUMBER PRIMARY KEY,
  opening_balance NUMBER NOT NULL CHECK (opening_balance >= 0)
);
CREATE TABLE TRANSACTIONS (
  txn_id NUMBER PRIMARY KEY,
  account_id NUMBER NOT NULL REFERENCES ACCOUNTS(account_id),
  amount NUMBER NOT NULL
);
CREATE OR REPLACE FUNCTION FN_BALANCE(p_account_id IN NUMBER) RETURN NUMBER AS
  v_balance NUMBER;
BEGIN
  IF p_account_id IS NULL THEN RAISE_APPLICATION_ERROR(-20001, 'required'); END IF;
  SELECT a.opening_balance + NVL(SUM(t.amount), 0) INTO v_balance
  FROM ACCOUNTS a LEFT JOIN TRANSACTIONS t ON t.account_id = a.account_id
  WHERE a.account_id = p_account_id GROUP BY a.opening_balance;
  RETURN v_balance;
EXCEPTION WHEN OTHERS THEN RAISE;
END;
/
"""
    rules = {"rules": [], "conflicts": [], "checksum": "0"}
    plan = build_file_plan(["database/generated.sql"], manifest, rules, "sql")
    all_ids = {
        item["id"]
        for category in (
            "inputs", "outputs", "entities", "use_cases", "roles_permissions",
            "integrations", "non_functional_requirements", "deliverables",
        )
        for item in manifest.get(category, [])
    }
    plan[0]["requirement_ids"] = sorted(all_ids)
    clear_generation_notes("OracleProject/")
    _pf_register_verified_single_file_notes(
        code=code, governed_path=path, language="sql", manifest=manifest,
        guideline_set=rules, requirement_ids=all_ids, complete_deliverable=True,
    )
    _artifacts, diagnostics = build_audit_artifacts(
        "OracleProject", manifest, rules, load_language_standards("sql", target),
        plan, {path: code}, "sql", single_artifact=True,
    )
    clear_generation_notes("OracleProject/")

    assert not [item for item in diagnostics if "traceability evidence" in item]
    assert not [item for item in diagnostics if "accountability notes" in item]


def test_parameterless_static_oracle_package_marks_only_implicit_input_not_applicable():
    target = {
        **TARGET,
        "name": "Oracle Database", "language": "sql", "backend_tech": "PL/SQL",
        "db_tech": "Oracle Database", "db_target": "oracle",
    }
    manifest = freeze_requirements_manifest(
        "Create an Oracle package that creates a user schema.", target,
    )
    rules = {"rules": [], "conflicts": [], "checksum": "0"}
    project = "StaticOracle"
    path = f"{project}/generated.sql"
    code = (
        "CREATE OR REPLACE PACKAGE schema_pkg AS\n"
        "  PROCEDURE create_schema;\nEND schema_pkg;\n/\n"
        "CREATE OR REPLACE PACKAGE BODY schema_pkg AS\n"
        "  PROCEDURE create_schema IS BEGIN\n"
        "    EXECUTE IMMEDIATE 'CREATE TABLE users (username VARCHAR2(50) NOT NULL)';\n"
        "  EXCEPTION WHEN OTHERS THEN RAISE_APPLICATION_ERROR(-20001, SQLERRM);\n"
        "  END create_schema;\nEND schema_pkg;\n/\n"
    )
    plan = build_file_plan(["generated.sql"], manifest, rules, "sql")
    plan[0]["requirement_ids"] = [
        item["id"] for category in ("inputs", "outputs")
        for item in manifest.get(category, [])
    ]
    clear_generation_notes(f"{project}/")
    _pf_register_verified_single_file_notes(
        code=code, governed_path=path, language="sql", manifest=manifest,
        guideline_set=rules,
    )
    artifacts, diagnostics = build_audit_artifacts(
        project, manifest, rules, load_language_standards("sql", target),
        plan, {path: code}, "sql", single_artifact=True,
    )
    clear_generation_notes(f"{project}/")
    trace = json.loads(artifacts[f"{project}/governance/input-output-traceability.json"])
    input_row = next(row for row in trace if row["kind"] == "input")

    assert not [
        item for item in diagnostics
        if item.startswith("IN-") and "traceability evidence" in item
    ]
    assert input_row["required_actions"] == []
    assert set(input_row["not_applicable_actions"]) == {"audit", "capture", "validate"}
    assert "no runtime input interface" in input_row["not_applicable_reason"]


def test_static_oracle_schema_does_not_require_an_assumed_runtime_output_interface():
    target = {
        **TARGET,
        "name": "Oracle Database", "language": "sql", "backend_tech": "PL/SQL",
        "db_tech": "Oracle Database", "db_target": "oracle",
    }
    manifest = freeze_requirements_manifest("Create an Oracle account schema.", target)
    assert any(
        "No discrete output list was supplied" in assumption["text"]
        for assumption in manifest["assumptions"]
    )
    rules = {"rules": [], "conflicts": [], "checksum": "0"}
    project = "OracleSchema"
    path = f"{project}/generated.sql"
    code = (
        "CREATE TABLE accounts (\n"
        "  account_id NUMBER GENERATED BY DEFAULT AS IDENTITY NOT NULL,\n"
        "  account_no VARCHAR2(30) NOT NULL,\n"
        "  CONSTRAINT pk_accounts PRIMARY KEY (account_id),\n"
        "  CONSTRAINT uk_accounts_no UNIQUE (account_no)\n"
        ");\n"
        "CREATE OR REPLACE VIEW v_accounts AS SELECT account_id, account_no FROM accounts;\n"
    )
    plan = build_file_plan(["generated.sql"], manifest, rules, "sql")
    plan[0]["requirement_ids"] = [
        item["id"] for category in ("inputs", "outputs")
        for item in manifest.get(category, [])
    ]
    clear_generation_notes(f"{project}/")
    _pf_register_verified_single_file_notes(
        code=code, governed_path=path, language="sql", manifest=manifest,
        guideline_set=rules,
    )
    artifacts, diagnostics = build_audit_artifacts(
        project, manifest, rules, load_language_standards("sql", target),
        plan, {path: code}, "sql", single_artifact=True,
    )
    clear_generation_notes(f"{project}/")
    output_row = next(
        row for row in json.loads(
            artifacts[f"{project}/governance/input-output-traceability.json"]
        ) if row["kind"] == "output"
    )

    assert not [item for item in diagnostics if item.startswith("OUT-")]
    assert output_row["required_actions"] == []
    assert set(output_row["not_applicable_actions"]) == {
        "error-state", "produce", "render",
    }
    assert "emitted static SQL artifact" in output_row["not_applicable_reason"]


def test_explicit_static_sql_output_contract_is_not_exempted():
    target = {
        **TARGET,
        "name": "Oracle Database", "language": "sql", "backend_tech": "PL/SQL",
        "db_tech": "Oracle Database", "db_target": "oracle",
    }
    manifest = freeze_requirements_manifest(
        "Create an Oracle schema.\nOutputs:\n- operation result: explicit runtime status",
        target,
    )
    assert not any(
        "No discrete output list was supplied" in assumption["text"]
        for assumption in manifest["assumptions"]
    )
    rules = {"rules": [], "conflicts": [], "checksum": "0"}
    project = "ExplicitSqlOutput"
    path = f"{project}/generated.sql"
    code = "CREATE TABLE accounts (account_id NUMBER PRIMARY KEY);\n"
    plan = build_file_plan(["generated.sql"], manifest, rules, "sql")
    plan[0]["requirement_ids"] = [item["id"] for item in manifest["outputs"]]
    clear_generation_notes(f"{project}/")
    _pf_register_verified_single_file_notes(
        code=code, governed_path=path, language="sql", manifest=manifest,
        guideline_set=rules,
    )
    _artifacts, diagnostics = build_audit_artifacts(
        project, manifest, rules, load_language_standards("sql", target),
        plan, {path: code}, "sql", single_artifact=True,
    )
    clear_generation_notes(f"{project}/")

    assert any(item.startswith("OUT-") and "no traceability evidence" in item for item in diagnostics)


def test_explicit_sql_input_is_not_exempted_when_generated_package_has_no_parameters():
    target = {
        **TARGET,
        "name": "Oracle Database", "language": "sql", "backend_tech": "PL/SQL",
        "db_tech": "Oracle Database", "db_target": "oracle",
    }
    manifest = freeze_requirements_manifest(
        "Create an Oracle package.\nInputs:\n- username: required string\nOutputs:\n- result: status",
        target,
    )
    rules = {"rules": [], "conflicts": [], "checksum": "0"}
    project = "ExplicitOracle"
    path = f"{project}/generated.sql"
    code = (
        "CREATE OR REPLACE PACKAGE schema_pkg AS PROCEDURE create_schema; END schema_pkg;\n/\n"
        "CREATE OR REPLACE PACKAGE BODY schema_pkg AS\n"
        "  PROCEDURE create_schema IS BEGIN NULL; END create_schema;\n"
        "END schema_pkg;\n/\n"
    )
    plan = build_file_plan(["generated.sql"], manifest, rules, "sql")
    plan[0]["requirement_ids"] = [item["id"] for item in manifest["inputs"]]
    clear_generation_notes(f"{project}/")
    _pf_register_verified_single_file_notes(
        code=code, governed_path=path, language="sql", manifest=manifest,
        guideline_set=rules,
    )
    _artifacts, diagnostics = build_audit_artifacts(
        project, manifest, rules, load_language_standards("sql", target),
        plan, {path: code}, "sql", single_artifact=True,
    )
    clear_generation_notes(f"{project}/")

    assert any(item.startswith("IN-") and "no traceability evidence" in item for item in diagnostics)


def test_angular_single_file_rules_do_not_require_browser_authentication_logging():
    rules = _pf_single_file_production_rules({"frontend_tech": "Angular 20"})

    assert "Full error handling with logging" not in rules
    assert "safe user-visible error state" in rules
    assert "do not use browser console/logger calls" in rules
    assert "Full error handling with logging" in _pf_single_file_production_rules(
        {"frontend_tech": ""}
    )


def test_notes_repair_accepts_complete_standalone_json_but_rejects_partial_json():
    complete = {
        "applied_rule_ids": [],
        "not_applicable_rules": [],
        "conflicts_resolved": [],
        "assumptions": [],
        "manifest_ids_covered": ["IN-001"],
        "manifest_coverage": [{"id": "IN-001", "actions": ["capture"]}],
        "self_check": {"complete": True},
    }
    recovered = _pf_extract_single_file_notes_block(
        "Here is the requested object:\n```json\n" + json.dumps(complete) + "\n```"
    )

    assert recovered.startswith("<generation_notes>")
    assert recovered.endswith("</generation_notes>")
    assert _pf_extract_single_file_notes_block('{"manifest_coverage": []}') == ""


def test_nested_generation_notes_schema_failure_is_recorded_without_crashing():
    path = "Demo/generated.ex"
    clear_generation_notes("Demo/")
    malformed = {
        "applied_rule_ids": [],
        "not_applicable_rules": [],
        "conflicts_resolved": [],
        "assumptions": [],
        "manifest_ids_covered": ["IN-001"],
        "manifest_coverage": ["IN-001"],
        "self_check": {"complete": True},
    }

    source = parse_generation_response(
        "<generated_file>\ndefmodule Demo do\nend\n</generated_file>\n"
        "<generation_notes>" + json.dumps(malformed) + "</generation_notes>",
        path,
    )
    notes = generation_notes_snapshot("Demo/")[path]
    clear_generation_notes("Demo/")

    assert source == "defmodule Demo do\nend\n"
    assert notes["format_valid"] is False
    assert "manifest_coverage[0] must be an object" in notes["error"]


def test_strict_elixir_source_registers_mechanically_verified_trace_actions():
    path = "Demo/generated.ex"
    manifest = freeze_requirements_manifest(
        "Validate a username and return a boolean result.",
        {**TARGET, "language": "elixir"},
    )
    code = (
        "defmodule Demo do\n"
        "  require Logger\n"
        "  def valid?(username) do\n"
        "    case username do\n"
        "      \"\" -> Logger.info(\"validation failed\"); false\n"
        "      _ -> Logger.info(\"validation passed\"); true\n"
        "    end\n"
        "  end\n"
        "end\n"
    )
    clear_generation_notes("Demo/")
    _pf_register_verified_single_file_notes(
        code=code, governed_path=path, language="elixir", manifest=manifest,
        guideline_set={"rules": []},
    )
    notes = generation_notes_snapshot("Demo/")[path]
    clear_generation_notes("Demo/")

    claims = {claim["id"]: set(claim["actions"]) for claim in notes["manifest_coverage"]}
    for item in manifest["inputs"]:
        assert claims[item["id"]] == {"capture", "validate", "audit"}
    for item in manifest["outputs"]:
        assert claims[item["id"]] == {"produce", "render", "error-state"}


def test_validation_outcome_print_is_verified_as_audit_without_logging_input_value():
    path = "Demo/generated.kt"
    manifest = freeze_requirements_manifest(
        "Validate a username and return a boolean result.",
        {**TARGET, "language": "kotlin"},
    )
    code = (
        "fun valid(username: String): Boolean {\n"
        "  val result = username.isNotBlank()\n"
        "  println(if (result) \"validation passed\" else \"validation failed\")\n"
        "  return result\n"
        "}\n"
    )
    clear_generation_notes("Demo/")
    _pf_register_verified_single_file_notes(
        code=code, governed_path=path, language="kotlin", manifest=manifest,
        guideline_set={"rules": []},
    )
    notes = generation_notes_snapshot("Demo/")[path]
    clear_generation_notes("Demo/")

    input_claim = next(
        claim for claim in notes["manifest_coverage"] if claim["id"].startswith("IN-")
    )
    assert set(input_claim["actions"]) == {"capture", "validate", "audit"}


def test_angular_form_evidence_is_registered_when_frontend_rule_is_not_path_applicable():
    path = "AngularScript/generated.ts"
    manifest = freeze_requirements_manifest(
        "Create an Angular login page.",
        {**TARGET, "language": "typescript", "frontend_tech": "Angular 20"},
    )
    guideline_set = {
        "rules": [{
            "id": "R-001", "verbatim": "Create an Angular login page",
            "layers": ["frontend"], "languages": ["typescript"],
            "machine_check": None,
        }],
    }
    code = (
        "@Component({template: `<form [formGroup]=\"loginForm\">"
        "<input formControlName=\"username\">"
        "@if (errorMessage) { <p>{{ errorMessage }}</p> }</form>`})\n"
        "export class LoginComponent {\n"
        "  errorMessage = '';\n"
        "  onSubmit(): void { if (this.loginForm.invalid) { this.errorMessage = 'Invalid'; return; } }\n"
        "}\n"
    )
    clear_generation_notes("AngularScript/")
    _pf_register_verified_single_file_notes(
        code=code, governed_path=path, language="typescript", manifest=manifest,
        guideline_set=guideline_set,
    )
    notes = generation_notes_snapshot("AngularScript/")[path]
    clear_generation_notes("AngularScript/")

    claims = {claim["id"]: set(claim["actions"]) for claim in notes["manifest_coverage"]}
    for item in manifest["inputs"]:
        assert {"capture", "validate"} <= claims[item["id"]]
    for item in manifest["outputs"]:
        assert {"produce", "render", "error-state"} <= claims[item["id"]]


def test_fast_single_file_tier_is_limited_to_proven_language_routes():
    for language in (
        "python", "typescript", "javascript", "sql", "shell", "elixir", "erlang",
        "julia", "kotlin", "swift",
    ):
        assert _pf_can_use_fast_single_file_model(language)
    assert not _pf_can_use_fast_single_file_model("cobol")


def test_real_single_file_prompt_is_not_replaced_by_custom_stack_metadata():
    prompt = "Generate a concise Python password validator."
    assert _pf_effective_single_file_prompt(
        prompt, "Custom stack prompt: Python 3.12 standard library",
    ) == prompt


def test_full_stack_target_preset_does_not_bypass_single_file_mode():
    """A Spring Boot + React preset must not turn 'one class' into a project."""
    sentinel = ({"__single_file__": "class Phone {}\n"}, {"generation_validated": True})
    with patch(
        "services.modernizer.prompt_pipeline._pf_try_single_file", return_value=sentinel,
    ) as single:
        result = _pf_single_file_attempt(
            "single_file", True, "model", True,
            "Create a utility class that validates international phone numbers.",
            {**TARGET_STACKS["spring_boot_react"], "llm_persona": "x"}, "java", "Demo",
            "", "", "", "", "", [], lambda *_a: None,
        )
    assert result is sentinel
    assert single.call_count == 1


def test_explicit_full_stack_request_still_expands_to_project():
    with patch("services.modernizer.prompt_pipeline._pf_try_single_file") as single:
        result = _pf_single_file_attempt(
            "single_file", True, "model", False,
            "Build a full-stack app with a React frontend and a Spring Boot backend.",
            TARGET, "java", "Demo", "", "", "", "", "", [], lambda *_a: None,
        )
    assert result is None
    assert single.call_count == 0


def test_single_file_requirements_come_from_the_prompt_not_the_preset_database():
    captured = {}

    def fake_try(*args, **_kwargs):
        captured["stack_reqs"] = args[6]
        return {"__single_file__": "x = 1\n"}, {}

    with patch("services.modernizer.prompt_pipeline._pf_try_single_file", side_effect=fake_try):
        _pf_single_file_attempt(
            "single_file", True, "model", False,
            "Create a FastAPI router that stores items in an in-memory dictionary.",
            TARGET, "python", "Demo", "", "",
            "\n- Database capability: PostgreSQL 16 (postgres). Use its native Java driver",
            "", "", [], lambda *_a: None,
        )
    assert "PostgreSQL" not in captured["stack_reqs"]


def test_project_template_rules_do_not_become_contract_members():
    """Template bullets used to inflate the estimate and force the slow path."""
    template = _generation_template("x", TARGET, {}, [])
    with patch(
        "services.modernizer.validation_orchestration._generate_validated",
        return_value=("x = 1\n# <<<END_OF_FILE>>>\n", PASSED, 1),
    ) as generated, patch(
        "services.modernizer.prompt_pipeline._pf_generate_structural_single_file",
    ) as structural:
        _output, validation = _pf_try_single_file(
            "Create a FastAPI router for inventory items.", TARGET, "python", "Demo", "",
            "", "", template, "", [], "model", lambda *_a: None,
        )
    assert structural.call_count == 0
    assert validation["token_budget"]["strategy"] == "single_shot"
    prompt = generated.call_args.args[0]
    assert "GENERATION TEMPLATE MODEL" not in prompt
    assert "Docker contexts" not in prompt
    assert "Database: not requested" in prompt
    assert "PostgreSQL 16 + SQLAlchemy 2" not in prompt


def test_validated_draft_without_end_sentinel_is_accepted_without_regeneration():
    with patch(
        "services.modernizer.validation_orchestration._generate_validated",
        return_value=("x = 1\n", PASSED, 1),
    ), patch(
        "services.modernizer.prompt_pipeline._pf_generate_structural_single_file",
    ) as structural:
        output, validation = _pf_try_single_file(
            "Create a constant module.", TARGET, "python", "Demo", "",
            "", "", "", "", [], "model", lambda *_a: None,
        )
    assert structural.call_count == 0
    assert output["__single_file__"] == "x = 1\n"
    assert validation["generation_validated"] is True


def test_syntax_valid_placeholder_is_repaired_before_governance():
    from services.modernizer.validation_orchestration import _generate_validated

    responses = [
        "def validate(value):\n    pass\n",
        "def validate(value):\n    return bool(value)\n",
    ]
    with patch("services.llm.generate", side_effect=responses) as generated:
        content, result, attempts = _generate_validated(
            "Generate a complete validator.",
            model="model", system="system", max_tokens=512, num_ctx=2048,
            rel_path="Demo/generated.py", language="python", max_attempts=2,
        )

    assert attempts == 2
    assert generated.call_count == 2
    assert result.passed is True
    assert "return bool(value)" in content
    assert "source-completeness" in generated.call_args_list[1].args[0]


def test_standard_single_file_draft_budget_is_not_capped_at_2048_tokens():
    from services.modernizer._shared import (
        _SINGLE_FILE_MIN_OUTPUT_TOKENS,
        _adaptive_num_ctx,
    )

    with patch(
        "services.modernizer.validation_orchestration._generate_validated",
        return_value=("x = 1\n", PASSED, 1),
    ) as generated:
        _pf_try_single_file(
            "Create a Python API module backed by a database.", TARGET, "python", "Demo", "",
            "", "", "", "", [], "model", lambda *_a: None,
        )
    assert generated.call_args.kwargs["max_tokens"] >= _SINGLE_FILE_MIN_OUTPUT_TOKENS
    prompt = generated.call_args.args[0]
    system = generated.call_args.kwargs["system"]
    assert generated.call_args.kwargs["num_ctx"] == _adaptive_num_ctx(
        len(system) + len(prompt), generated.call_args.kwargs["max_tokens"],
    )


def test_short_focused_script_uses_fast_model_and_concise_budget():
    from services.modernizer._shared import _SINGLE_FILE_SIMPLE_OUTPUT_TOKENS

    target = {**TARGET, "db_tech": "", "db_target": ""}
    with patch(
        "services.llm.pick_fast_codegen_model", return_value="qwen3.5:9b",
    ) as pick_fast, patch(
        "services.modernizer.validation_orchestration._generate_validated",
        return_value=("def valid():\n    return True\n", PASSED, 1),
    ) as generated:
        _output, validation = _pf_try_single_file(
            "Generate a Python script to validate a username and password.",
            target, "python", "Demo", "", "", "", "", "", [],
            "devstral-small-2:24b", lambda *_a: None,
        )

    assert pick_fast.call_count == 1
    assert generated.call_args.kwargs["model"] == "qwen3.5:9b"
    assert generated.call_args.kwargs["max_tokens"] == _SINGLE_FILE_SIMPLE_OUTPUT_TOKENS
    assert "FOCUSED SCRIPT SCOPE" in generated.call_args.args[0]
    assert "<generated_file>" not in generated.call_args.args[0]
    assert "Return raw source only" in generated.call_args.args[0]
    assert validation["token_budget"]["request_class"] == "simple"
    assert validation["model"] == "qwen3.5:9b"


def test_invalid_fast_language_draft_escalates_structural_retry_to_primary_model():
    target = {**TARGET, "language": "elixir", "db_tech": "", "db_target": ""}
    failed = SimpleNamespace(
        passed=False, checker="parser", path="generated.ex", language="elixir",
        diagnostics=["line 2: syntax error"],
    )
    passed = SimpleNamespace(
        passed=True, checker="parser", path="generated.ex", language="elixir",
        diagnostics=[],
    )
    with patch(
        "services.llm.pick_fast_codegen_model", return_value="qwen3.5:9b",
    ), patch(
        "services.modernizer.validation_orchestration._generate_validated",
        return_value=("defmodule Broken do\n", failed, 2),
    ), patch(
        "services.modernizer.prompt_pipeline._pf_generate_structural_single_file",
        return_value=("defmodule Demo do\nend\n", passed, 1),
    ) as structural:
        output, validation = _pf_try_single_file(
            "Generate a concise Elixir function.", target, "elixir", "Demo", "",
            "", "", "", "", [], "devstral-small-2:24b", lambda *_a: None,
        )

    assert structural.call_args.kwargs["model"] == "devstral-small-2:24b"
    assert output["__single_file__"] == "defmodule Demo do\nend\n"
    assert validation["generation_validated"] is True


def test_failed_structural_validation_is_retained_without_repeating_generation():
    failed = SimpleNamespace(
        passed=False, checker="compiler", path="generated.sql", language="sql",
        diagnostics=["oracle parse error: cross-member boundary"],
    )
    with patch(
        "services.modernizer.validation_orchestration._generate_validated",
        side_effect=TimeoutError("Ollama generation exceeded the 600s per-file budget"),
    ), patch(
        "services.modernizer.prompt_pipeline._pf_generate_structural_single_file",
        return_value=("SELECT 1 FROM dual;\n", failed, 12),
    ) as structural:
        output, validation = _pf_try_single_file(
            "Create an Oracle SQL script with twelve numbered deliverables.",
            {**TARGET, "language": "sql", "db_tech": "Oracle", "db_target": "oracle"},
            "sql", "Demo", "", "", "", "", "", [], "model", lambda *_a: None,
        )

    assert structural.call_count == 1
    assert output["__single_file__"] == "SELECT 1 FROM dual;\n"
    assert validation["generation_validated"] is False
    assert validation["failed"] == 1


def test_attached_guidance_keeps_standard_single_file_model_and_budget():
    from services.modernizer._shared import _SINGLE_FILE_MIN_OUTPUT_TOKENS

    with patch("services.llm.pick_fast_codegen_model") as pick_fast, patch(
        "services.modernizer.validation_orchestration._generate_validated",
        return_value=("x = 1\n", PASSED, 1),
    ) as generated:
        _output, validation = _pf_try_single_file(
            "Create a Python module.", TARGET, "python", "Demo", "", "", "", "",
            "Use the attached enterprise standard.", [], "devstral-small-2:24b",
            lambda *_a: None,
        )

    assert pick_fast.call_count == 0
    assert generated.call_args.kwargs["model"] == "devstral-small-2:24b"
    assert generated.call_args.kwargs["max_tokens"] >= _SINGLE_FILE_MIN_OUTPUT_TOKENS
    assert validation["token_budget"]["request_class"] == "standard"


def test_structural_prompt_never_requests_the_governed_envelope():
    manifest = freeze_requirements_manifest("Create a constant module.", TARGET)
    bundle = (manifest, {"rules": [], "conflicts": [], "checksum": "0"}, [])
    with patch(
        "services.modernizer.validation_orchestration._generate_validated",
        side_effect=TimeoutError("Ollama generation exceeded the 600s aggregate budget"),
    ), patch(
        "services.modernizer.prompt_pipeline._pf_generate_structural_single_file",
        return_value=("x = 1\n", PASSED, 1),
    ) as structural, patch(
        "services.modernizer.prompt_pipeline._pf_single_file_repair_notes",
    ):
        _pf_try_single_file(
            "Create a constant module.", TARGET, "python", "Demo", "",
            "", "", "", "", [], "model", lambda *_a: None, bundle,
        )
    prompt = structural.call_args.kwargs["prompt"]
    assert "<generated_file>" not in prompt
    assert "governed response envelope" not in prompt


def test_whole_file_structural_fallback_skips_the_skeleton_call():
    contract = FileContract("Demo/generated.py", "default", [], [])
    valid = ValidationResult("Demo/generated.py", "python", "parser", True, [])
    with patch(
        "services.llm.generate", return_value="VALUE = 1\n",
    ) as generated, patch("services.validators.validate_file", return_value=valid):
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create a constant module", system="Python only", model="model",
            contract=contract, language="python", dialect="",
            progress=lambda *_a: None, max_seconds=30,
        )
    assert content == "VALUE = 1\n"
    assert result.passed is True
    assert calls == 1
    assert generated.call_count == 1
    assert "SKELETON ONLY" not in generated.call_args.args[0]


def test_structural_oracle_package_gets_one_bounded_parser_repair():
    contract = FileContract("OracleScript/generated.sql", "default", [], [])
    invalid = (
        "CREATE OR REPLACE PACKAGE BODY user_credentials_pkg AS\n"
        "  PROCEDURE store_user IS BEGIN NULL; END store_user;\n"
        "END wrong_package_name;\n/\n"
    )
    repaired = (
        "CREATE OR REPLACE PACKAGE BODY user_credentials_pkg AS\n"
        "  PROCEDURE store_user IS BEGIN NULL; END store_user;\n"
        "END user_credentials_pkg;\n/\n"
    )
    failed = ValidationResult(
        contract.path, "sql", "compiler", False,
        ["oracle parse error: package terminator does not match"],
    )
    passed = ValidationResult(contract.path, "sql", "compiler", True, [])

    def validate(_path, content, _language, dialect_hint=""):
        assert dialect_hint == "oracle"
        return passed if "END user_credentials_pkg;" in content else failed

    with patch("services.llm.generate", side_effect=[invalid, repaired]) as generated, patch(
        "services.validators.validate_file", side_effect=validate,
    ):
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create an Oracle package", system="Oracle PL/SQL only", model="model",
            contract=contract, language="sql", dialect="oracle",
            progress=lambda *_a: None, max_seconds=30,
        )

    assert result.passed is True
    assert content == repaired
    assert calls == 2
    assert generated.call_count == 2
    repair_prompt = generated.call_args_list[1].args[0]
    assert "PREVIOUS ATTEMPT FAILED VALIDATION" in repair_prompt
    assert "package terminator does not match" in repair_prompt
    assert "EXECUTE IMMEDIATE" in repair_prompt


def test_structural_oracle_package_allows_a_second_bounded_parser_repair():
    contract = FileContract("OracleDbSchema/generated.sql", "default", [], [])
    invalid = (
        "CREATE OR REPLACE PACKAGE BODY credentials_pkg AS\n"
        "  PROCEDURE create_schema IS BEGIN CREATE TABLE users (id NUMBER); END create_schema;\n"
        "END credentials_pkg;\n/\n"
    )
    still_invalid = invalid.replace("users", "app_users")
    repaired = (
        "CREATE OR REPLACE PACKAGE BODY credentials_pkg AS\n"
        "  PROCEDURE create_schema IS BEGIN\n"
        "    EXECUTE IMMEDIATE 'CREATE TABLE app_users (id NUMBER)';\n"
        "  END create_schema;\n"
        "END credentials_pkg;\n/\n"
    )
    failed = ValidationResult(
        contract.path, "sql", "compiler", False,
        ["oracle PL/SQL package body credentials_pkg contains direct CREATE DDL; "
         "execute DDL through EXECUTE IMMEDIATE"],
    )
    passed = ValidationResult(contract.path, "sql", "compiler", True, [])

    def validate(_path, content, _language, dialect_hint=""):
        assert dialect_hint == "Oracle PL/SQL"
        return passed if "EXECUTE IMMEDIATE" in content else failed

    with patch(
        "services.llm.generate", side_effect=[invalid, still_invalid, repaired],
    ) as generated, patch(
        "services.validators.validate_file", side_effect=validate,
    ):
        content, result, calls = _pf_generate_structural_single_file(
            prompt="Create an Oracle schema package",
            system="Oracle PL/SQL only", model="model", contract=contract,
            language="sql", dialect="Oracle PL/SQL",
            progress=lambda *_a: None, max_seconds=30,
        )

    assert result.passed is True
    assert content == repaired
    assert calls == 3
    assert generated.call_count == 3


def test_oracle_package_script_separators_are_normalized_at_unit_boundaries():
    source = (
        "CREATE OR REPLACE PACKAGE credentials_pkg AS\n"
        "  PROCEDURE create_schema;\n"
        "END credentials_pkg;\n"
        "CREATE OR REPLACE PACKAGE BODY credentials_pkg AS\n"
        "  PROCEDURE create_schema IS\n"
        "  BEGIN\n"
        "    NULL;\n"
        "  END create_schema;\n"
        "END credentials_pkg;\n"
    )
    normalized = _normalize_oracle_package_terminators(source, "Oracle PL/SQL")

    assert normalized.count("\n/\n") == 2
    assert "END create_schema;\n/" not in normalized
    assert _normalize_oracle_package_terminators(normalized, "Oracle PL/SQL") == normalized
    assert _normalize_oracle_package_terminators(source, "postgres") == source


def test_oracle_sqlplus_separators_after_ordinary_sql_are_removed_only():
    source = (
        "INSERT INTO accounts (account_id) VALUES (1);\n"
        "/\n"
        "CREATE INDEX ix_accounts_id ON accounts (account_id);\n"
        "/\n"
        "BEGIN\n"
        "  NULL;\n"
        "END;\n"
        "/\n"
        "CREATE OR REPLACE FUNCTION account_count RETURN NUMBER IS\n"
        "BEGIN\n"
        "  RETURN 1;\n"
        "END account_count;\n"
        "/\n"
    )

    normalized = _normalize_oracle_package_terminators(source, "oracle")

    assert "VALUES (1);\n/" not in normalized
    assert "accounts (account_id);\n/" not in normalized
    assert "END;\n/" in normalized
    assert "END account_count;\n/" in normalized
    assert normalized.count("\n/\n") == 2
    assert _normalize_oracle_package_terminators(normalized, "oracle") == normalized
    assert _normalize_oracle_package_terminators(source, "postgres") == source
    result = validate_file("generated.sql", normalized, "sql", dialect_hint="oracle")
    assert result.passed, result.diagnostics


def test_oracle_bind_dependent_view_becomes_parameterized_query():
    source = (
        "CREATE OR REPLACE VIEW V_ACCOUNT_PERIOD_BALANCE AS\n"
        "SELECT account_id, SUM(amount) AS balance\n"
        "FROM transactions\n"
        "WHERE txn_date >= :p_start_date AND txn_date < :p_end_date\n"
        "GROUP BY account_id;\n"
        "CREATE OR REPLACE VIEW V_STATIC AS SELECT account_id FROM accounts;\n"
    )

    repaired = _repair_oracle_bind_variable_views(source, "oracle")

    assert "CREATE OR REPLACE VIEW V_ACCOUNT_PERIOD_BALANCE" not in repaired
    assert "-- Parameterized Oracle query V_ACCOUNT_PERIOD_BALANCE" in repaired
    assert "txn_date >= :p_start_date" in repaired
    assert "CREATE OR REPLACE VIEW V_STATIC" in repaired
    assert _repair_oracle_bind_variable_views(repaired, "oracle") == repaired
    assert _repair_oracle_bind_variable_views(source, "postgres") == source
    result = validate_file("generated.sql", repaired, "sql", dialect_hint="oracle")
    assert result.passed, result.diagnostics


def test_oracle_multirow_values_becomes_insert_all_without_touching_other_dialects():
    source = (
        "INSERT INTO accounts (account_id, account_name) VALUES\n"
        "(1, 'Alpha'),\n"
        "-- second governed fixture\n"
        "(2, 'Beta; still a literal');\n"
        "INSERT INTO audit_log (message) VALUES ('single row');\n"
    )

    repaired = _repair_oracle_multirow_values(source, "oracle")

    assert repaired.startswith("INSERT ALL\n")
    assert "INTO accounts (account_id, account_name) VALUES (1, 'Alpha')" in repaired
    assert "INTO accounts (account_id, account_name) VALUES (2, 'Beta; still a literal')" in repaired
    assert "SELECT 1 FROM DUAL;" in repaired
    assert "INSERT INTO audit_log (message) VALUES ('single row');" in repaired
    assert _repair_oracle_multirow_values(repaired, "oracle") == repaired
    assert _repair_oracle_multirow_values(source, "postgres") == source
    result = validate_file("generated.sql", repaired, "sql", dialect_hint="oracle")
    assert result.passed, result.diagnostics


def test_governance_prompt_can_show_all_requirements_for_a_single_artifact():
    manifest = freeze_requirements_manifest(
        "Create an inventory module.\nInputs: item SKU, quantity\nOutputs: valuation total",
        TARGET,
    )
    ids = [item["id"] for item in manifest.get("inputs", []) + manifest.get("outputs", [])]
    assert ids, "manifest fixture must contain input/output requirements"
    rules = {"rules": [], "conflicts": [], "checksum": "0"}
    scoped = render_governance_prompt(manifest, rules, [], path="Demo/generated.py", language="python")
    full = render_governance_prompt(
        manifest, rules, [], path="Demo/generated.py", language="python", all_requirements=True,
    )
    assert all(item_id in full.split("Known manifest IDs:")[1] for item_id in ids)
    assert "<generated_file>" in full
    assert scoped != full


def test_single_artifact_audit_marks_test_only_evidence_not_applicable():
    manifest = freeze_requirements_manifest(
        "Create an inventory module and audit validation outcomes.\n"
        "Inputs: item SKU\nOutputs: valuation total", TARGET,
    )
    rules = {"rules": [], "conflicts": [], "checksum": "0"}
    path = "Demo/generated.py"
    all_ids = [
        item["id"] for category in (
            "inputs", "outputs", "entities", "use_cases", "roles_permissions",
            "integrations", "non_functional_requirements", "deliverables",
        ) for item in manifest.get(category, [])
    ]
    plan = build_file_plan(["generated.py"], manifest, rules, "python")
    plan[0]["requirement_ids"] = all_ids
    coverage = [
        {"id": item_id, "actions": [
            "capture", "validate", "audit", "produce", "render", "error-state", "implement",
        ]}
        for item_id in all_ids
    ]
    clear_generation_notes("Demo/")
    parse_generation_response(
        "<generated_file>\nx = 1\n</generated_file>\n<generation_notes>"
        + __import__("json").dumps({
            "applied_rule_ids": [], "not_applicable_rules": [], "conflicts_resolved": [],
            "assumptions": [], "manifest_ids_covered": all_ids, "manifest_coverage": coverage,
            "self_check": {"complete": True},
        })
        + "</generation_notes>",
        path,
    )
    output = {path: "x = 1\n"}
    _artifacts, project_diagnostics = build_audit_artifacts(
        "Demo", manifest, rules, [], plan, output, "python",
    )
    _artifacts, single_diagnostics = build_audit_artifacts(
        "Demo", manifest, rules, [], plan, output, "python", single_artifact=True,
    )
    clear_generation_notes("Demo/")
    assert not [item for item in single_diagnostics if "traceability" in item]
    # Project semantics are unchanged: test evidence is still required there.
    if manifest.get("non_functional_requirements"):
        assert any("traceability" in item for item in project_diagnostics)


def test_single_artifact_audit_still_requires_the_models_own_claims():
    manifest = freeze_requirements_manifest(
        "Create an inventory module.\nInputs: item SKU\nOutputs: valuation total", TARGET,
    )
    rules = {"rules": [], "conflicts": [], "checksum": "0"}
    plan = build_file_plan(["generated.py"], manifest, rules, "python")
    plan[0]["requirement_ids"] = [item["id"] for item in manifest.get("inputs", [])]
    clear_generation_notes("Demo/")
    _artifacts, diagnostics = build_audit_artifacts(
        "Demo", manifest, rules, [], plan, {"Demo/generated.py": "x = 1\n"}, "python",
        single_artifact=True,
    )
    assert any("no traceability evidence" in item for item in diagnostics)


def test_traceability_is_deferred_when_every_planned_source_file_failed_validation():
    manifest = freeze_requirements_manifest(
        "Create an inventory module.\nInputs: item SKU\nOutputs: valuation total", TARGET,
    )
    rules = {"rules": [], "conflicts": [], "checksum": "0"}
    relative_path = "generated.py"
    governed_path = f"Demo/{relative_path}"
    plan = build_file_plan([relative_path], manifest, rules, "python")
    plan[0]["requirement_ids"] = [
        item["id"] for category in ("inputs", "outputs", "entities")
        for item in manifest.get(category, [])
    ]
    clear_generation_notes("Demo/")
    artifacts, diagnostics = build_audit_artifacts(
        "Demo", manifest, rules, [], plan, {governed_path: "def broken(:\n"},
        "python", single_artifact=True, unvalidated_paths={governed_path},
    )
    rows = json.loads(artifacts["Demo/governance/input-output-traceability.json"])
    clear_generation_notes("Demo/")

    assert not [item for item in diagnostics if "no traceability evidence" in item]
    assert all(row.get("traceability_deferred") for row in rows if row["planned_files"])


def test_single_file_input_audit_is_required_only_when_explicitly_requested():
    rules = {"rules": [], "conflicts": [], "checksum": "0"}
    path = "Demo/generated.py"
    for prompt, audit_required in (
        ("Validate a username and return a boolean.", False),
        ("Validate a username, audit the validation outcome, and return a boolean.", True),
    ):
        manifest = freeze_requirements_manifest(prompt, TARGET)
        plan = build_file_plan(["generated.py"], manifest, rules, "python")
        input_ids = [item["id"] for item in manifest["inputs"]]
        output_ids = [item["id"] for item in manifest["outputs"]]
        plan[0]["requirement_ids"] = [*input_ids, *output_ids]
        clear_generation_notes("Demo/")
        register_generation_notes(path, {
            "applied_rule_ids": [], "not_applicable_rules": [],
            "conflicts_resolved": [], "assumptions": [],
            "manifest_ids_covered": [*input_ids, *output_ids],
            "manifest_coverage": [
                {"id": item_id, "actions": ["capture", "validate"]}
                for item_id in input_ids
            ] + [
                {"id": item_id, "actions": ["produce", "render", "error-state"]}
                for item_id in output_ids
            ],
            "self_check": {"complete": True},
        })
        artifacts, diagnostics = build_audit_artifacts(
            "Demo", manifest, rules, [], plan, {path: "def valid(value): return bool(value)\n"},
            "python", single_artifact=True,
        )
        clear_generation_notes("Demo/")
        rows = json.loads(artifacts["Demo/governance/input-output-traceability.json"])
        input_rows = [row for row in rows if row["kind"] == "input"]

        assert any("no traceability evidence" in item for item in diagnostics) is audit_required
        assert all(("audit" in row["required_actions"]) is audit_required for row in input_rows)


def test_angular_single_component_does_not_require_client_side_input_audit_logging():
    angular_target = {
        **TARGET,
        "name": "Angular 20 + TypeScript",
        "language": "typescript",
        "backend_tech": "REST API",
        "frontend_tech": "Angular 20",
        "db_tech": "",
    }
    manifest = freeze_requirements_manifest(
        "Create a single-page Angular login form.\nInputs: username and password\nOutputs: login result",
        angular_target,
    )
    rules = {"rules": [], "conflicts": [], "checksum": "0"}
    path = "AngularLogin/generated.ts"
    plan = build_file_plan(["generated.ts"], manifest, rules, "typescript")
    all_ids = [
        item["id"]
        for category in ("inputs", "outputs")
        for item in manifest.get(category, [])
    ]
    plan[0]["requirement_ids"] = all_ids
    coverage = [
        {
            "id": item["id"],
            "actions": ["capture", "validate"],
        }
        for item in manifest["inputs"]
    ] + [
        {
            "id": item["id"],
            "actions": ["produce", "render", "error-state"],
        }
        for item in manifest["outputs"]
    ]
    clear_generation_notes("AngularLogin/")
    parse_generation_response(_notes_envelope(coverage), path)
    artifacts, diagnostics = build_audit_artifacts(
        "AngularLogin",
        manifest,
        rules,
        load_language_standards("typescript", angular_target),
        plan,
        {path: "@Component({template: `<form></form>`}) export class LoginComponent {}\n"},
        "typescript",
        single_artifact=True,
    )
    clear_generation_notes("AngularLogin/")

    assert not [item for item in diagnostics if item.startswith("IN-")]
    rows = json.loads(
        artifacts["AngularLogin/governance/input-output-traceability.json"]
    )
    input_rows = [row for row in rows if row["kind"] == "input"]
    assert input_rows
    assert all(row["required_actions"] == ["capture", "validate"] for row in input_rows)
    assert all(row["not_applicable_actions"] == ["audit"] for row in input_rows)


def test_invalid_notes_are_repaired_without_regenerating_the_source():
    from services.modernizer._shared import _adaptive_num_ctx

    path = "Demo/generated.py"
    clear_generation_notes("Demo/")
    notes = (
        '<generation_notes>{"applied_rule_ids":[],"not_applicable_rules":[],'
        '"conflicts_resolved":[],"assumptions":[],"manifest_ids_covered":[],'
        '"manifest_coverage":[],"self_check":{"complete":true}}</generation_notes>'
    )
    with patch("services.llm.generate", return_value=notes) as generated:
        _pf_single_file_repair_notes(
            code="x = 1\n", governed_path=path, system="s", governance_context="g",
            model="model", context_window=8192, progress=lambda *_a: None,
        )
    assert generated.call_count == 1
    assert generated.call_args.kwargs["max_tokens"] <= 2048
    assert generated.call_args.kwargs["num_ctx"] == _adaptive_num_ctx(
        len("s") + len(generated.call_args.args[0]),
        generated.call_args.kwargs["max_tokens"],
    )
    assert "Do not change or repeat it" in generated.call_args.args[0]
    assert generation_notes_snapshot("Demo/")[path]["format_valid"] is True
    clear_generation_notes("Demo/")


def test_trace_gap_notes_prompt_points_to_verified_source_evidence():
    path = "Demo/generated.py"
    clear_generation_notes("Demo/")
    notes = (
        '<generation_notes>{"applied_rule_ids":[],"not_applicable_rules":[],'
        '"conflicts_resolved":[],"assumptions":[],"manifest_ids_covered":["IN-001","OUT-001"],'
        '"manifest_coverage":[],"self_check":{"complete":true}}</generation_notes>'
    )
    code = (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def validate(value):\n"
        "    logger.info('validated')\n"
        "    print(value)\n"
        "    return value\n"
    )
    with patch("services.llm.generate", return_value=notes) as generated:
        _pf_single_file_repair_notes(
            code=code, governed_path=path, system="s", governance_context="g",
            model="model", context_window=8192, progress=lambda *_a: None,
            trace_gaps=["IN-001 (request): audit", "OUT-001 (result): produce, render"],
        )

    prompt = generated.call_args.args[0]
    assert "VERIFIED SOURCE EVIDENCE" in prompt
    assert '"id": "IN-001"' in prompt and '"audit"' in prompt
    assert '"id": "OUT-001"' in prompt and '"produce"' in prompt and '"render"' in prompt
    assert "Merge every ID/action above into manifest_coverage exactly" in prompt
    snapshot = generation_notes_snapshot("Demo/")[path]
    claims = {claim["id"]: claim["actions"] for claim in snapshot["manifest_coverage"]}
    assert "audit" in claims["IN-001"]
    assert {"produce", "render"}.issubset(claims["OUT-001"])
    assert snapshot["source_verified_action_additions"]
    clear_generation_notes("Demo/")


def test_invalid_trace_gap_repair_preserves_valid_source_evidence():
    path = "Oracle/generated.sql"
    existing = {
        "applied_rule_ids": [],
        "not_applicable_rules": [],
        "conflicts_resolved": [],
        "assumptions": ["source-proven"],
        "manifest_ids_covered": ["IN-001", "OUT-001"],
        "manifest_coverage": [
            {"id": "IN-001", "actions": ["capture", "validate"]},
            {"id": "OUT-001", "actions": ["produce", "render"]},
        ],
        "self_check": {"complete": True},
    }
    clear_generation_notes("Oracle/")
    register_generation_notes(path, existing)
    malformed = (
        '<generation_notes>{"applied_rule_ids":[],"not_applicable_rules":[],'
        '"conflicts_resolved":[],"assumptions":[],"manifest_ids_covered":[],'
        '"manifest_coverage":["OUT-001"],"self_check":{"complete":true}}'
        '</generation_notes>'
    )

    with patch("services.llm.generate", return_value=malformed):
        _pf_single_file_repair_notes(
            code="BEGIN NULL; EXCEPTION WHEN OTHERS THEN NULL; END;\n/\n",
            governed_path=path, system="s", governance_context="g", model="model",
            context_window=8192, progress=lambda *_a: None,
            trace_gaps=["OUT-001 (result): error-state"],
        )

    retained = generation_notes_snapshot("Oracle/")[path]
    clear_generation_notes("Oracle/")
    assert retained["format_valid"] is True
    assert retained["manifest_coverage"] == existing["manifest_coverage"]


def test_valid_inline_notes_skip_the_notes_repair_call():
    path = "Demo/generated.py"
    clear_generation_notes("Demo/")
    parse_generation_response(
        "<generated_file>\nx = 1\n</generated_file>\n<generation_notes>"
        '{"applied_rule_ids":[],"not_applicable_rules":[],"conflicts_resolved":[],'
        '"assumptions":[],"manifest_ids_covered":[],"manifest_coverage":[],'
        '"self_check":{"complete":true}}</generation_notes>',
        path,
    )
    with patch("services.llm.generate") as generated:
        _pf_single_file_repair_notes(
            code="x = 1\n", governed_path=path, system="s", governance_context="g",
            model="model", context_window=8192, progress=lambda *_a: None,
        )
    assert generated.call_count == 0
    clear_generation_notes("Demo/")


def _notes_envelope(coverage):
    import json
    return (
        "<generated_file>\nx = 1\n</generated_file>\n<generation_notes>"
        + json.dumps({
            "applied_rule_ids": [], "not_applicable_rules": [], "conflicts_resolved": [],
            "assumptions": [], "manifest_ids_covered": [c["id"] for c in coverage],
            "manifest_coverage": coverage, "self_check": {"complete": True},
        })
        + "</generation_notes>"
    )


def test_injected_default_nfrs_are_advisory_but_user_nfrs_are_enforced():
    rules = {"rules": [], "conflicts": [], "checksum": "0"}
    path = "Demo/generated.py"
    for prompt, expect_nfr_failure in (
        ("Create an inventory module.", False),
        ("Create an inventory module.\nNon-functional requirements:\n- Every change is audited",
         True),
    ):
        manifest = freeze_requirements_manifest(prompt, TARGET)
        nfr_ids = [item["id"] for item in manifest["non_functional_requirements"]]
        assert nfr_ids
        plan = build_file_plan(["generated.py"], manifest, rules, "python")
        plan[0]["requirement_ids"] = nfr_ids
        clear_generation_notes("Demo/")
        parse_generation_response(_notes_envelope([]), path)
        artifacts, diagnostics = build_audit_artifacts(
            "Demo", manifest, rules, [], plan, {path: "x = 1\n"}, "python", single_artifact=True,
        )
        clear_generation_notes("Demo/")
        failed = any(
            "traceability" in item and item.split(":")[0] in nfr_ids for item in diagnostics
        )
        assert failed is expect_nfr_failure, prompt
        rows = __import__("json").loads(artifacts["Demo/governance/input-output-traceability.json"])
        nfr_rows = [row for row in rows if row["id"] in nfr_ids]
        assert nfr_rows
        assert all(row["advisory"] is (not expect_nfr_failure) for row in nfr_rows)


def test_trace_gaps_trigger_one_targeted_notes_repair_and_reaudit():
    manifest = freeze_requirements_manifest(
        "Create an inventory module and audit validation outcomes.\n"
        "Inputs: item SKU\nOutputs: valuation total", TARGET,
    )
    bundle = (manifest, {"rules": [], "conflicts": [], "checksum": "0"}, [])
    in_ids = [item["id"] for item in manifest["inputs"]]
    out_ids = [item["id"] for item in manifest["outputs"]]
    partial = [{"id": i, "actions": ["capture", "validate"]} for i in in_ids] + [
        {"id": i, "actions": ["produce", "render", "error-state"]} for i in out_ids
    ]
    complete = [{"id": i, "actions": ["capture", "validate", "audit"]} for i in in_ids] + [
        {"id": i, "actions": ["produce", "render", "error-state"]} for i in out_ids
    ]

    def fake_validated(*_args, **kwargs):
        parse_generation_response(_notes_envelope(partial), kwargs["rel_path"])
        return "x = 1\n", PASSED, 1

    repaired_notes = _notes_envelope(complete).split("</generated_file>\n", 1)[1]
    with patch(
        "services.modernizer.validation_orchestration._generate_validated",
        side_effect=fake_validated,
    ), patch("services.llm.generate", return_value=repaired_notes) as generated:
        clear_generation_notes("Demo/")
        _output, validation = _pf_try_single_file(
            "Create an inventory module and audit validation outcomes.", TARGET, "python", "Demo", "",
            "", "", "", "", [], "model", lambda *_a: None, bundle,
        )
        clear_generation_notes("Demo/")
    assert generated.call_count == 1
    gap_prompt = generated.call_args.args[0]
    assert "TRACEABILITY GAPS" in gap_prompt and "audit" in gap_prompt
    assert validation["governance"]["passed"] is True
    assert validation["generation_validated"] is True
