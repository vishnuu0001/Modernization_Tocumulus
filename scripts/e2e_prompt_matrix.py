"""Exercise every advertised modernization prompt through resolution and validation.

This is intentionally a service-level E2E harness: it uses the same catalog,
prompt resolver, deterministic generation fallback, strict file validators, and
project build runner as the API.  Vendor-only targets remain visible but are
reported as externally gated instead of being counted as local passes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from api.server import target_stacks
from services.build_runner import run_build
from services.modernizer import generate_from_prompt
from services.modernizer.domain_generators.stack_signals import _detect_domain_requirements
from services.modernizer.prompt_pipeline import (
    _pf_generate_deterministic_scaffold,
    _pf_generate_project_files_template,
    _pf_resolve_target,
)
from services.modernizer.scaffolds.polyglot import generate_polyglot_project
from services.modernizer.scaffolds.single_file_templates import _template_from_prompt
from services.validators import validate_file


_ARTIFACT_FILENAMES = {
    "yaml_artifact": "config.yaml", "json_artifact": "artifact.json",
    "toml_artifact": "config.toml", "xml_artifact": "artifact.xml",
    "markdown_artifact": "README.md", "graphql_schema": "schema.graphql",
    "protobuf_schema": "schema.proto", "dockerfile_artifact": "Dockerfile",
    "kubernetes_manifest": "deployment.yaml", "helm_chart": "values.yaml",
    "cloudformation_template": "template.yaml", "ansible_playbook": "playbook.yaml",
    "jenkins_pipeline": "Jenkinsfile", "github_actions": ".github/workflows/ci.yml",
}


def _prompt(stack: dict) -> str:
    if stack.get("category") in {
        "Configuration formats", "Documentation", "API contracts",
        "DevOps and cloud", "CI/CD",
    }:
        return (
            f"Generate one production-ready {stack.get('name')} artifact with complete "
            "validation, secure configuration, documentation, and no placeholders."
        )
    verb = (
        "Modernize the uploaded legacy application into"
        if stack.get("category") == "Legacy modernization"
        else "Create a production-ready application using"
    )
    return (
        f"{verb} {stack.get('backend')}, {stack.get('frontend')}, "
        f"{stack.get('database')}. Generate a complete multi-file project with "
        "dependency manifests, configuration, database migrations, validation, "
        "centralized error handling, tests, Docker assets, deployment manifests, "
        "health checks, documentation, and no placeholders or mock implementations."
    )


def _custom_description(stack: dict) -> str:
    return "; ".join((
        f"Preset: {stack.get('name') or stack['id']}",
        f"Language: {stack.get('language') or ''}",
        f"Backend: {stack.get('backend') or ''}",
        f"Frontend: {stack.get('frontend') or ''}",
        f"Database: {stack.get('database') or ''}",
    ))


def _generate_project(
    target: dict,
    signals: dict,
    language: str,
    prompt: str,
    full_stack: bool,
) -> dict[str, str]:
    """Run the same deterministic scaffold and fallback stages as generation."""
    output: dict[str, str] = {}
    record = output.__setitem__
    has_backend = bool(signals.get("backend"))
    has_frontend = bool(signals.get("frontend"))
    pack_owned = _pf_generate_deterministic_scaffold(
        target,
        language,
        signals,
        "PromptMatrix",
        (),
        has_backend,
        has_frontend,
        bool(_detect_domain_requirements(prompt)),
        record,
        lambda *_args: None,
        prompt,
    )
    _pf_generate_project_files_template(
        target,
        "PromptMatrix",
        prompt,
        full_stack,
        has_backend,
        has_frontend,
        language,
        output,
        pack_owned,
        record,
        lambda *_args: None,
    )
    return output


def run_matrix(
    *, build: bool, live: bool = False, stack_ids: set[str] | None = None,
) -> dict:
    catalog = asyncio.run(target_stacks())
    report: dict = {
        "catalog_count": len(catalog["stacks"]),
        "source_files_checked": 0,
        "passed": [],
        "externally_gated": [],
        "failures": [],
    }
    for stack in catalog["stacks"]:
        stack_id = stack["id"]
        if stack_ids and stack_id not in stack_ids:
            continue
        if not stack["available"]:
            report["externally_gated"].append({
                "id": stack_id,
                "reason": stack["blocked_reason"],
            })
            continue
        prompt = _prompt(stack)
        try:
            custom_description = "" if stack["native"] else _custom_description(stack)
            target, _signals, _full_stack, language, _requirements = _pf_resolve_target(
                prompt,
                stack["engine_target"],
                custom_description,
            )
            generation_summary = None
            if live:
                artifact_mode = stack.get("category") in {
                    "Configuration formats", "Documentation", "API contracts",
                    "DevOps and cloud", "CI/CD",
                }
                files, generation_summary = generate_from_prompt(
                    prompt,
                    stack["engine_target"],
                    custom_stack_desc=custom_description,
                    output_mode="single_file" if artifact_mode else "project",
                )
                if not generation_summary.get("generation_validated"):
                    raise RuntimeError(
                        "production generation did not reach its validated terminal state"
                    )
            else:
                artifact_filename = _ARTIFACT_FILENAMES.get(stack_id)
                if artifact_filename:
                    files = {artifact_filename: _template_from_prompt(
                        artifact_filename, prompt, target, "PromptMatrix",
                    )}
                else:
                    files = generate_polyglot_project(language, "PromptMatrix", "Orders", target)
                    if not files and stack["full_generation"]:
                        files = _generate_project(
                            target, _signals, language, prompt, _full_stack,
                        )
            if not files:
                raise RuntimeError(
                    f"no deterministic project scaffold for resolved language {language!r}"
                )
            source_failures = []
            for path, content in files.items():
                validation = validate_file(
                    path,
                    content,
                    language,
                    dialect_hint=str(target.get("db_target") or ""),
                )
                report["source_files_checked"] += 1
                if not validation.passed:
                    source_failures.append({
                        "path": path,
                        "checker": validation.checker,
                        "diagnostics": validation.diagnostics,
                    })
            if source_failures:
                raise RuntimeError(json.dumps(source_failures, ensure_ascii=False))
            build_checker = "not-requested"
            if build:
                with tempfile.TemporaryDirectory(
                    prefix="modernization-prompt-e2e-", ignore_cleanup_errors=True,
                ) as directory:
                    result = run_build(files, language, Path(directory))
                build_checker = result.checker
                if not result.passed:
                    raise RuntimeError(
                        f"{result.checker}: {json.dumps(result.errors_by_file, ensure_ascii=False)}"
                    )
            report["passed"].append({
                "id": stack_id,
                "language": language,
                "files": len(files),
                "build_checker": build_checker,
                "live_generation": live,
            })
        except Exception as exc:  # report all cases instead of stopping at the first gap
            report["failures"].append({"id": stack_id, "error": str(exc)})
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true", help="run the project build adapter")
    parser.add_argument("--report", type=Path, help="optional JSON report path")
    parser.add_argument("--stack", action="append", default=[], help="limit to a stack id")
    parser.add_argument("--summary", action="store_true", help="print counts instead of full JSON")
    parser.add_argument("--live", action="store_true", help="invoke the configured generation model")
    args = parser.parse_args()
    report = run_matrix(
        build=args.build, live=args.live, stack_ids=set(args.stack) or None,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(rendered + "\n", encoding="utf-8")
    if args.summary:
        print(
            f"catalog={report['catalog_count']} passed={len(report['passed'])} "
            f"failed={len(report['failures'])} gated={len(report['externally_gated'])} "
            f"files={report['source_files_checked']}"
        )
        for failure in report["failures"]:
            print(f"FAIL {failure['id']}: {failure['error']}")
    else:
        print(rendered)
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
