"""Acceptance matrix for every language advertised by the target catalog."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from services.generation_governance import (
    atomize_guidelines,
    build_audit_artifacts,
    build_file_plan,
    clear_generation_notes,
    freeze_requirements_manifest,
    load_language_standards,
    validate_machine_rules,
)
from services.modernizer._shared import _DEFAULT_EXT_FOR_LANG
from services.modernizer.prompt_pipeline import (
    _pf_effective_single_file_prompt,
    _pf_register_verified_single_file_notes,
    _pf_single_file_run_guidelines,
)
from services.modernizer.target_config import TARGET_STACKS
from services.modernizer.target_config import _infer_target_language
from services.modernizer.token_budgeting import sentinel_comment
from services.modernizer.validation_orchestration import _single_file_extension
from services.validators import ValidationResult, _validate_external_language, validate_file


VALID_SINGLE_FILE_SOURCES = {
    "c": "int validate(const char *value) { if (!value || !*value) return -1; return 0; }\n",
    "clojure": '(ns demo.core)\n(defn validate [value] (if (empty? value) {:error true} {:ok true}))\n',
    "cobol": (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. GENERATED.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 USER-VALUE PIC X(40).\n"
        "       PROCEDURE DIVISION.\n"
        "           ACCEPT USER-VALUE.\n"
        "           IF USER-VALUE = SPACES\n"
        "               DISPLAY 'INVALID INPUT'\n"
        "           ELSE\n"
        "               DISPLAY 'VALID INPUT'\n"
        "           END-IF.\n"
        "           STOP RUN.\n"
    ),
    "cpp": '#include <string>\nint validate(const std::string& value) { if (value.empty()) return -1; return 0; }\n',
    "csharp": 'public static class Generated { public static bool Validate(string value) { if (string.IsNullOrWhiteSpace(value)) return false; return true; } }\n',
    "dart": "bool validate(String value) { if (value.isEmpty) return false; return true; }\n",
    "elixir": 'defmodule Generated do\n  def validate(value) do\n    if value == "", do: {:error, :invalid}, else: {:ok, true}\n  end\nend\n',
    "erlang": "-module(generated).\n-export([validate/1]).\nvalidate(Value) -> case Value of <<>> -> {error, invalid}; _ -> {ok, true} end.\n",
    "go": 'package main\n\nfunc validate(value string) bool { if value == "" { return false }; return true }\n',
    "haskell": 'validate :: String -> Either String Bool\nvalidate value = if null value then Left "invalid" else Right True\n',
    "java": 'final class generated { static boolean validate(String value) { if (value == null || value.isBlank()) return false; return true; } }\n',
    "javascript": 'export function validate(value) { if (!value) return { error: "invalid" }; return { ok: true }; }\n',
    "julia": 'function validate(value::String)\n  if isempty(value)\n    return (error = "invalid",)\n  end\n  return (ok = true,)\nend\n',
    "kotlin": 'fun validate(value: String): Boolean { if (value.isBlank()) return false; return true }\n',
    "lisp": '(defun validate (value) (if (or (null value) (string= value "")) (list :error t) (list :ok t)))\n',
    "php": '<?php\nfunction validate(string $value): bool { if ($value === "") { return false; } return true; }\n',
    "python": 'def validate(value: str) -> bool:\n    if not value:\n        return False\n    return True\n',
    "r": 'validate <- function(value) { if (is.null(value) || value == "") return(list(error = TRUE)); list(ok = TRUE) }\n',
    "ruby": 'def validate(value)\n  return { error: true } if value.nil? || value.empty?\n  { ok: true }\nend\n',
    "rust": 'pub fn validate(value: &str) -> Result<bool, &str> { if value.is_empty() { return Err("invalid"); } Ok(true) }\n',
    "scala": 'object Generated { def validate(value: String): Either[String, Boolean] = if (value.isEmpty) Left("invalid") else Right(true) }\n',
    "shell": 'validate() { if [ -z "$1" ]; then printf "invalid\\n"; return 1; fi; printf "valid\\n"; return 0; }\n',
    "sql": "CREATE OR REPLACE FUNCTION validate_value(p_value IN VARCHAR2) RETURN NUMBER AS BEGIN IF p_value IS NULL THEN RETURN -1; END IF; RETURN 1; EXCEPTION WHEN OTHERS THEN RETURN -2; END;\n/\n",
    "swift": 'func validate(_ value: String) -> Bool { if value.isEmpty { return false }; return true }\n',
    "typescript": 'export function validate(value: string): boolean { if (!value) return false; return true; }\n',
}

ARTIFACT_SINGLE_FILE_SOURCES = {
    "ansible": "- name: Validate configuration\n  hosts: all\n  tasks:\n    - debug:\n        msg: valid\n",
    "cloudformation": (
        "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n  Bucket:\n"
        "    Type: AWS::S3::Bucket\n    Properties:\n      BucketName: demo-generated-bucket\n"
    ),
    "dockerfile": "FROM alpine:3.20\nRUN printf 'valid\\n'\n",
    "github_actions": (
        "name: CI\n'on': [push]\njobs:\n  validate:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo valid\n"
    ),
    "graphql": "type Query { validationResult(value: String!): Boolean! }\n",
    "helm": "apiVersion: v2\nname: validation-chart\nversion: 1.0.0\n",
    "jenkinsfile": (
        "pipeline {\n  agent any\n  stages {\n    stage('Validate') {\n"
        "      steps {\n        echo 'valid'\n      }\n    }\n  }\n}\n"
    ),
    "json": '{"validation": {"required": true, "result": "valid"}}\n',
    "kubernetes": (
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: validation-config\n"
        "data:\n  result: valid\n"
    ),
    "markdown": "# Validation contract\n\nInput is required and the result is explicit.\n",
    "protobuf": (
        'syntax = "proto3";\nmessage ValidationRequest { string value = 1; }\n'
        "message ValidationResult { bool valid = 1; string error = 2; }\n"
    ),
    "toml": "[validation]\nrequired = true\nresult = \"valid\"\n",
    "xml": "<validation required=\"true\"><result>valid</result></validation>\n",
    "yaml": "validation:\n  required: true\n  result: valid\n",
}

EXPLICIT_DISPLAY_LANGUAGE_MAP = {
    "ABAP": "abap", "Ansible": "ansible", "Apex": "apex", "C": "c",
    "C#": "csharp", "C++": "cpp", "Clojure": "clojure",
    "CloudFormation": "cloudformation", "COBOL": "cobol", "Common Lisp": "lisp",
    "Dart": "dart", "Dockerfile": "dockerfile", "Elixir": "elixir",
    "Erlang": "erlang", "GitHub Actions": "github_actions", "Go": "go",
    "GraphQL": "graphql", "Haskell": "haskell", "Helm": "helm", "Java": "java",
    "JavaScript": "javascript", "Jenkinsfile": "jenkinsfile", "JSON": "json",
    "Julia": "julia", "Kotlin": "kotlin", "Kubernetes manifests": "kubernetes",
    "Markdown": "markdown", "PHP": "php", "PL/SQL": "sql", "Protobuf": "protobuf",
    "Python": "python", "R": "r", "Ruby": "ruby", "Rust": "rust",
    "Scala": "scala", "Shell": "shell", "SQL": "sql", "Swift": "swift",
    "TOML": "toml", "T-SQL": "sql", "TypeScript": "typescript",
    "XML": "xml", "YAML": "yaml",
}


def _catalog_targets_by_language() -> dict[str, dict]:
    result: dict[str, dict] = {}
    for target in TARGET_STACKS.values():
        language = str(target.get("language") or "")
        result.setdefault(language, target)
    return result


def test_every_catalog_language_has_a_single_file_source_and_extension():
    targets = _catalog_targets_by_language()

    assert set(VALID_SINGLE_FILE_SOURCES) == set(targets)
    assert all(_DEFAULT_EXT_FOR_LANG.get(language) for language in targets)
    for language, source in VALID_SINGLE_FILE_SOURCES.items():
        assert _single_file_extension(language, source) != ".txt", language


def test_every_catalog_language_label_recovers_structured_custom_requirement():
    requirement = "Validate one input and return success or an explicit error"
    structured = f"Custom stack prompt: {requirement}; Business criticality: Medium"
    for language in _catalog_targets_by_language():
        assert _pf_effective_single_file_prompt(
            f"{language} Script", structured,
        ) == requirement, language


def test_every_display_language_uses_authoritative_structured_language_field():
    for display, expected in EXPLICIT_DISPLAY_LANGUAGE_MAP.items():
        description = (
            f"Preset: generated target; Language: {display}; Backend: generated backend; "
            "Frontend: generated frontend; Database: generated database"
        )
        assert _infer_target_language(description) == expected, display


def test_every_catalog_language_passes_strict_single_file_source_validation():
    # Exercise the portable parser route for optional external toolchains. The
    # native compiler-specific tests continue to exercise installed compilers.
    with patch("services.validators.find_executable", return_value=None):
        for language, source in VALID_SINGLE_FILE_SOURCES.items():
            extension = _single_file_extension(language, source)
            source_with_sentinel = source.rstrip() + "\n" + sentinel_comment(language) + "\n"
            result = validate_file(f"generated{extension}", source_with_sentinel, language)
            assert result.passed, f"{language}: {result.checker}: {result.diagnostics}"
            assert result.checker in {"compiler", "parser"}, language


def test_scala_compiler_internal_crash_falls_back_to_strict_parser():
    compiler_crash = subprocess.CompletedProcess(
        ["scalac"], 1, "",
        "scala.reflect.internal.FatalError: bad constant pool index: 0",
    )
    parsed = ValidationResult("generated.scala", "scala", "parser", True, [])
    with tempfile.TemporaryDirectory() as directory, patch(
        "services.validators.find_executable", return_value="scalac",
    ), patch(
        "services.validators.subprocess.run", return_value=compiler_crash,
    ), patch(
        "services.validators._validate_tree_sitter", return_value=parsed,
    ) as parser:
        result = _validate_external_language(
            "generated.scala", VALID_SINGLE_FILE_SOURCES["scala"],
            "scala", Path(directory),
        )

    assert result.passed
    assert result.checker == "parser"
    parser.assert_called_once()


def test_every_catalog_stack_passes_source_derived_single_artifact_governance():
    for stack_id, target in TARGET_STACKS.items():
        language = target["language"]
        structured = (
            "Custom stack prompt: Validate one input and return success or an explicit error; "
            f"Language: {language}; Business criticality: Medium"
        )
        rules = atomize_guidelines("", _pf_single_file_run_guidelines(structured))
        assert not rules["rules"], stack_id
        project = "Single" + "".join(part.title() for part in stack_id.split("_"))
        relative_path = f"generated{_DEFAULT_EXT_FOR_LANG[language]}"
        governed_path = f"{project}/{relative_path}"
        manifest = freeze_requirements_manifest(
            "Validate one input and return success or an explicit error.\n"
            "Inputs:\n- value: required string\nOutputs:\n- result: success or error",
            target,
        )
        plan = build_file_plan([relative_path], manifest, rules, language)
        plan[0]["requirement_ids"] = [
            item["id"]
            for category in (
                "inputs", "outputs", "entities", "use_cases", "roles_permissions",
                "integrations", "non_functional_requirements", "deliverables",
            )
            for item in manifest.get(category, [])
        ]
        clear_generation_notes(f"{project}/")
        _pf_register_verified_single_file_notes(
            code=VALID_SINGLE_FILE_SOURCES[language],
            governed_path=governed_path,
            language=language,
            manifest=manifest,
            guideline_set=rules,
        )
        artifacts, diagnostics = build_audit_artifacts(
            project,
            manifest,
            rules,
            load_language_standards(language, target),
            plan,
            {governed_path: VALID_SINGLE_FILE_SOURCES[language]},
            language,
            single_artifact=True,
        )
        clear_generation_notes(f"{project}/")

        assert not diagnostics, f"{stack_id} ({language}): {diagnostics}"
        trace = json.loads(
            artifacts[f"{project}/governance/input-output-traceability.json"]
        )
        assert all(
            not row["missing_actions"] for row in trace if not row.get("advisory")
        ), f"{stack_id} ({language})"


def test_every_supported_artifact_passes_strict_validation_and_governance():
    for language, source in ARTIFACT_SINGLE_FILE_SOURCES.items():
        project = "Artifact" + language.title().replace("_", "")
        relative_path = f"generated{_DEFAULT_EXT_FOR_LANG[language]}"
        governed_path = f"{project}/{relative_path}"
        result = validate_file(relative_path, source, language)
        assert result.passed, f"{language}: {result.checker}: {result.diagnostics}"
        expected_checkers = (
            {"heuristic"} if language == "jenkinsfile" else {"compiler", "parser"}
        )
        assert result.checker in expected_checkers, language

        target = {
            "name": language, "language": language, "backend_tech": language,
            "frontend_tech": "", "db_tech": "", "db_target": "",
        }
        manifest = freeze_requirements_manifest(
            "Generate one strictly valid configuration or contract artifact.", target,
        )
        rules = {"rules": [], "conflicts": [], "checksum": "0"}
        plan = build_file_plan([relative_path], manifest, rules, language)
        plan[0]["requirement_ids"] = [
            item["id"]
            for category in (
                "inputs", "outputs", "entities", "use_cases", "roles_permissions",
                "integrations", "non_functional_requirements", "deliverables",
            )
            for item in manifest.get(category, [])
        ]
        clear_generation_notes(f"{project}/")
        _pf_register_verified_single_file_notes(
            code=source, governed_path=governed_path, language=language,
            manifest=manifest, guideline_set=rules,
        )
        _artifacts, diagnostics = build_audit_artifacts(
            project, manifest, rules, load_language_standards(language, target),
            plan, {governed_path: source}, language, single_artifact=True,
        )
        clear_generation_notes(f"{project}/")
        assert not diagnostics, f"{language}: {diagnostics}"


def test_every_supported_generation_language_excludes_platform_evidence_from_machine_rules():
    rules = atomize_guidelines("Never use `forbidden_generated_token`.")
    all_sources = {**VALID_SINGLE_FILE_SOURCES, **ARTIFACT_SINGLE_FILE_SOURCES}
    for language, source in all_sources.items():
        extension = _DEFAULT_EXT_FOR_LANG[language]
        output = {
            f"Demo/generated{extension}": source,
            "Demo/governance/requirements-manifest.json": (
                '{"schema_version":"1.0.0","prompt":"forbidden_generated_token"}'
            ),
            "Demo/.strat-aqorynth/provenance.json": (
                '{"request":"forbidden_generated_token"}'
            ),
            "Demo/README.md": "Do not use forbidden_generated_token in generated source.",
        }
        assert validate_machine_rules(output, rules, language) == [], language
