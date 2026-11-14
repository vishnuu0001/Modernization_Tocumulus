# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — tests (test_job_validation_state.py)
# Date: 2025-10-22
# ---------------------------------------------------------------------------
import unittest
import json
import queue
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from api.server import (
    app,
    _failed_strict_validation,
    _validation_failure_message,
    _evaluate_release_gate,
    _generation_audit_has_unresolved_issues,
    _job_list_summary,
    _job_response,
    _job_status_response,
    _project_creation_configuration,
    _record_worker_failure,
    _require_admin,
    _security_findings,
    _single_file_failed_validation,
)


class SingleFileValidationStateTests(unittest.TestCase):
    def test_generation_audit_blocks_only_current_unresolved_issues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixed = root / "fixed.md"
            fixed.write_text(
                "# Generation Audit\n\n## Per-file validation\n\n"
                "- Demo.java (compiler, fixed on retry after 2 attempts): fixed\n",
                encoding="utf-8",
            )
            failing = root / "failing.md"
            failing.write_text(
                "# Generation Audit\n\n## Per-file validation\n\n"
                "- Demo.java (compiler, still FAILING after 2 attempts): broken\n",
                encoding="utf-8",
            )

            self.assertFalse(_generation_audit_has_unresolved_issues(fixed))
            self.assertTrue(_generation_audit_has_unresolved_issues(failing))

    def test_security_scan_ignores_password_validation_message_but_finds_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            safe = root / "AuthPage.tsx"
            safe.write_text(
                "errors.password = 'Password is required';\n"
                "const [password, setPassword] = useState('');\n",
                encoding="utf-8",
            )
            self.assertEqual([], _security_findings(root))

            unsafe = root / "settings.json"
            unsafe.write_text('{"password": "ProductionSecret123"}', encoding="utf-8")
            self.assertEqual(
                ["Hard-coded password: settings.json"],
                _security_findings(root),
            )

    def test_governance_failure_message_is_not_mislabeled_as_strict_source_failure(self):
        message = _validation_failure_message({
            "strict_checked": 1,
            "strict_passed": 1,
            "generation_validated": False,
            "governance": {"passed": False, "diagnostics": ["IN-001: no traceability evidence"]},
        })
        self.assertIn("passed source validation but failed governance", message)
        self.assertIn("IN-001", message)

    def test_compiler_failure_message_includes_checker_and_diagnostic(self):
        message = _validation_failure_message({
            "strict_checked": 1,
            "files": [{
                "passed": False, "checker": "compiler",
                "diagnostics": ["line 4: invalid syntax"],
            }],
        })
        self.assertIn("failed compiler validation", message)
        self.assertIn("invalid syntax", message)

    def test_project_build_failure_message_includes_file_level_maven_error(self):
        path = "Demo/backend/src/main/java/com/app/ErrorHandlerServlet.java"
        message = _validation_failure_message({
            "strict_checked": 1,
            "build": {
                "passed": False,
                "checker": "maven",
                "remaining_errors": {
                    path: [
                        "local variables referenced from a lambda expression "
                        "must be final or effectively final",
                    ],
                },
            },
        }, require_project_build=True)

        self.assertIn("Generated project failed maven", message)
        self.assertIn("ErrorHandlerServlet.java", message)
        self.assertIn("effectively final", message)
        self.assertNotIn("whole-project validation did not pass", message)

    # Function: test_unresolved_cobol_template_is_rejected_before_generation
    # Function: test_passed_single_file_is_not_failed
    def test_passed_single_file_is_not_failed(self):
        self.assertFalse(_single_file_failed_validation(
            {"__single_file__": "print('ok')"}, {"failed": 0},
        ))

    # Function: test_failed_single_file_is_failed
    def test_failed_single_file_is_failed(self):
        self.assertTrue(_single_file_failed_validation(
            {"__single_file__": "invalid"}, {"failed": 1},
        ))

    # Function: test_project_validation_does_not_use_single_file_state
    def test_project_validation_does_not_use_single_file_state(self):
        self.assertFalse(_single_file_failed_validation(
            {"src/app.py": "invalid"}, {"failed": 1},
        ))

    # Function: test_project_build_failure_is_terminal_validation_failure
    def test_project_build_failure_is_terminal_validation_failure(self):
        validation = {
            "failed": 0, "strict_checked": 2,
            "build": {"passed": False, "checker": "c-build"},
        }
        self.assertTrue(_failed_strict_validation(validation, True))

    # Function: test_project_requires_whole_output_validation
    def test_project_requires_whole_output_validation(self):
        self.assertTrue(_failed_strict_validation(
            {"failed": 0, "strict_checked": 2, "build": None}, True,
        ))

    # Function: test_explicit_production_readiness_failure_is_terminal
    def test_explicit_production_readiness_failure_is_terminal(self):
        self.assertTrue(_failed_strict_validation(
            {
                "failed": 0,
                "strict_checked": 3,
                "production_ready": False,
                "build": {"passed": True, "checker": "maven"},
            },
            True,
        ))

    # Function: test_release_gate_uses_validation_evidence_without_claiming_production_readiness
    def test_release_gate_uses_validation_evidence_without_claiming_production_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = root / "output"
            validation_root = root / "validation"
            contracts_root = root / "contracts"
            output_root.mkdir()
            validation_root.mkdir()
            contracts_root.mkdir()
            (output_root / "Application.java").write_text("class Application {}\n", encoding="utf-8")
            (output_root / "ApplicationTest.java").write_text("class ApplicationTest {}\n", encoding="utf-8")
            (validation_root / "artifact.json").write_text(json.dumps({
                "checked": 2,
                "strict_checked": 2,
                "failed": 0,
                "generation_validated": True,
                "production_ready": False,
                "build": {"passed": True, "checker": "maven"},
            }), encoding="utf-8")
            (contracts_root / "contracts.json").write_text("{}\n", encoding="utf-8")
            project = {
                "configuration": {"origin_mode": "existing_source"},
                "snapshots": [{"kind": "contracts", "path": str(contracts_root)}],
            }
            output = {"path": str(output_root)}
            validation = {"path": str(validation_root)}

            with patch("api.server.validate_contracts", return_value={"valid": True, "warnings": []}):
                gate = _evaluate_release_gate(project, output, validation)

            self.assertTrue(gate["passed"])
            self.assertEqual([], gate["blockers"])
            self.assertIn("does not assert production readiness", " ".join(gate["warnings"]))
            self.assertTrue(gate["checks"]["generation_validation"])
            self.assertFalse(gate["checks"]["production_readiness_asserted_by_generation"])

    def test_release_gate_migrates_legacy_prompt_contract_schema_without_mutating_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = root / "output"
            validation_root = root / "validation"
            contracts_root = root / "contracts"
            output_root.mkdir()
            validation_root.mkdir()
            contracts_root.mkdir()
            (output_root / "generated.sql").write_text(
                "CREATE TABLE ACCOUNTS (account_id NUMBER PRIMARY KEY);\n",
                encoding="utf-8",
            )
            (validation_root / "artifact.json").write_text(json.dumps({
                "checked": 1, "strict_checked": 1, "failed": 0,
                "generation_validated": True, "production_ready": False,
                "build": {"passed": True, "checker": "sql-project-parser"},
            }), encoding="utf-8")
            legacy = {
                "schema_version": 1,
                "target_stack": (
                    "Create one Oracle SQL file.\nMy tables:\n"
                    "- ACCOUNTS: [account_id, opening_balance]\n"
                    "- TRANSACTIONS: [txn_id, account_id, amount]\n"
                    "Include DDL and balance queries."
                ),
                "contract_source": "prompt-requirements-manifest",
                "domain_models": [], "database_schema": [],
                "checksum": "legacy-checksum",
            }
            contract_path = contracts_root / "contracts.json"
            contract_path.write_text(json.dumps(legacy), encoding="utf-8")
            project = {
                "configuration": {"origin_mode": "prompt"},
                "snapshots": [{"kind": "contracts", "path": str(contracts_root)}],
            }

            gate = _evaluate_release_gate(
                project, {"path": str(output_root)}, {"path": str(validation_root)},
            )

            self.assertTrue(gate["passed"], gate["blockers"])
            self.assertTrue(gate["checks"]["canonical_contracts"])
            self.assertEqual([], gate["blockers"])
            self.assertEqual(legacy, json.loads(contract_path.read_text(encoding="utf-8")))

    # Function: test_release_gate_still_blocks_failed_generation_validation
    def test_release_gate_still_blocks_failed_generation_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = root / "output"
            validation_root = root / "validation"
            contracts_root = root / "contracts"
            output_root.mkdir()
            validation_root.mkdir()
            contracts_root.mkdir()
            (output_root / "ApplicationTest.java").write_text("class ApplicationTest {}\n", encoding="utf-8")
            (validation_root / "artifact.json").write_text(json.dumps({
                "checked": 1,
                "strict_checked": 1,
                "failed": 0,
                "generation_validated": False,
                "production_ready": False,
                "build": {"passed": True, "checker": "maven"},
            }), encoding="utf-8")
            (contracts_root / "contracts.json").write_text("{}\n", encoding="utf-8")
            project = {
                "configuration": {"origin_mode": "existing_source"},
                "snapshots": [{"kind": "contracts", "path": str(contracts_root)}],
            }

            with patch("api.server.validate_contracts", return_value={"valid": True, "warnings": []}):
                gate = _evaluate_release_gate(
                    project, {"path": str(output_root)}, {"path": str(validation_root)},
                )

            self.assertFalse(gate["passed"])
            self.assertIn("Code generation validation did not pass", gate["blockers"])

    # Function: test_project_deletion_requires_server_side_admin_role
    def test_project_deletion_requires_server_side_admin_role(self):
        non_admin_request = SimpleNamespace(
            state=SimpleNamespace(auth={"sub": "user@example.test", "role": "user"}),
        )
        with self.assertRaises(HTTPException) as denied:
            _require_admin(non_admin_request, "delete governed projects")
        self.assertEqual(403, denied.exception.status_code)
        self.assertIn("delete governed projects", denied.exception.detail)

        admin_request = SimpleNamespace(
            state=SimpleNamespace(auth={"sub": "admin@example.test", "role": "admin"}),
        )
        self.assertIsNone(_require_admin(admin_request, "delete governed projects"))

    # Function: test_proxy_compatible_delete_route_is_registered
    def test_proxy_compatible_delete_route_is_registered(self):
        methods = {
            method
            for route in app.routes
            if getattr(route, "path", None) == "/api/projects/{project_id}/delete"
            for method in (getattr(route, "methods", None) or set())
        }
        self.assertEqual({"POST"}, methods)

    # Function: test_prompt_custom_stack_definition_is_optional
    def test_prompt_custom_stack_definition_is_optional(self):
        configuration, origin_mode = _project_creation_configuration({
            "project_prompt": "Build a Java 21 Spring Boot service with PostgreSQL.",
            "configuration": {
                "origin_mode": "prompt",
                "target_stack": "custom",
                "engine_target": "custom",
            },
        })
        self.assertEqual("prompt", origin_mode)
        self.assertEqual("", configuration["custom_stack_desc"])
        self.assertEqual("Inferred from project prompt", configuration["target_stack_name"])
        self.assertEqual(
            "Build a Java 21 Spring Boot service with PostgreSQL.",
            configuration["project_prompt"],
        )

    # Function: test_failed_worker_state_is_terminal_and_persistable
    def test_failed_worker_state_is_terminal_and_persistable(self):
        from unittest.mock import patch
        from api import server

        job_id = "test-worker-failure"
        server._JOBS[job_id] = {"job_id": job_id, "events": []}
        try:
            with patch("api.server._persist_job") as persist:
                _record_worker_failure(job_id, RuntimeError("classification failed"))
            self.assertEqual("failed", server._JOBS[job_id]["status"])
            self.assertEqual("failed", server._JOBS[job_id]["phase"])
            self.assertIn("classification failed", server._JOBS[job_id]["error"])
            details = server._JOBS[job_id]["error_details"]
            self.assertEqual("RuntimeError", details["root_cause_type"])
            self.assertEqual("classification failed", details["root_cause"])
            self.assertIn("source", details)
            self.assertIn("error_id", details)
            persist.assert_any_call(job_id)
        finally:
            server._JOBS.pop(job_id, None)

    def test_late_generation_callbacks_cannot_resurrect_failed_job(self):
        from unittest.mock import patch
        from api import server

        job_id = "test-late-generation-callback"
        callbacks = {}
        server._JOBS[job_id] = {
            "job_id": job_id, "status": "queued", "phase": "queued",
            "progress": 0, "events": [], "output": {},
        }

        def fail_after_capturing_callbacks(
            _prompt, _target, _images, on_progress, _custom_stack,
            **kwargs,
        ):
            callbacks["progress"] = on_progress
            callbacks["file"] = kwargs["on_file"]
            raise RuntimeError("generation wave expired")

        try:
            with patch(
                "services.modernizer.generate_from_prompt",
                side_effect=fail_after_capturing_callbacks,
            ), patch("api.server._persist_job"):
                server._prompt_worker(job_id, "prompt", "dotnet_angular", [], output_mode="project")

                self.assertEqual("failed", server._JOBS[job_id]["status"])
                callbacks["progress"]("llm", 92, "late progress")
                callbacks["file"]("Demo/Late.cs", "class Late {}")

            self.assertEqual("failed", server._JOBS[job_id]["status"])
            self.assertEqual("failed", server._JOBS[job_id]["phase"])
            self.assertEqual({}, server._JOBS[job_id]["output"])
        finally:
            server._JOBS.pop(job_id, None)
            server._JOB_QUEUES.pop(job_id, None)
            server._job_file(job_id).unlink(missing_ok=True)

    def test_single_file_worker_streams_preview_without_accepting_it_as_output(self):
        from api import server

        job_id = "test-live-single-file-preview"
        server._JOBS[job_id] = {
            "job_id": job_id, "status": "queued", "phase": "queued",
            "progress": 0, "events": [], "output": None,
        }
        server._JOB_QUEUES[job_id] = queue.Queue(maxsize=32)

        def generate(_prompt, _target, _images, on_progress, _custom_stack, **_kwargs):
            self.assertTrue(callable(getattr(on_progress, "on_single_file_stream", None)))
            on_progress.on_single_file_stream("", True)
            on_progress.on_single_file_stream("s", False)
            # Tiny model chunks stay buffered instead of copying the entire
            # growing preview string under the jobs lock for every token.
            self.assertEqual("", server._JOBS[job_id]["live_output"])
            on_progress.on_single_file_stream("elect " + (" " * 505), False)
            self.assertEqual(512, len(server._JOBS[job_id]["live_output"]))
            return (
                {"__single_file__": "select 1 from dual;\n"},
                {"failed": 0, "strict_checked": 1, "generation_validated": True},
            )

        try:
            with patch("services.modernizer.generate_from_prompt", side_effect=generate), patch("api.server._persist_job"):
                server._prompt_worker(job_id, "prompt", "custom", [], output_mode="single_file")

            job = server._JOBS[job_id]
            self.assertEqual("completed", job["status"])
            self.assertEqual("select 1 from dual;\n", job["live_output"])
            self.assertEqual("final", job["live_output_phase"])
            self.assertFalse(any(event.get("type") == "code_delta" for event in job["events"]))
            queued = []
            while not server._JOB_QUEUES[job_id].empty():
                queued.append(server._JOB_QUEUES[job_id].get_nowait())
            self.assertTrue(any(event.get("type") == "code_delta" for event in queued))
        finally:
            server._JOBS.pop(job_id, None)
            server._JOB_QUEUES.pop(job_id, None)
            server._job_file(job_id).unlink(missing_ok=True)

    def test_worker_uses_generator_effective_project_mode_for_final_build_gate(self):
        from api import server

        job_id = "test-effective-project-mode"
        server._JOBS[job_id] = {
            "job_id": job_id, "status": "queued", "phase": "queued",
            "progress": 0, "events": [], "output": None,
        }
        server._JOB_QUEUES[job_id] = queue.Queue(maxsize=32)

        def generate(*_args, **_kwargs):
            return (
                {"Demo/src/main.py": "print('ok')\n"},
                {
                    "output_mode": "project", "failed": 0, "strict_checked": 1,
                    "generation_validated": True, "build": None,
                },
            )

        try:
            with patch(
                "services.modernizer.generate_from_prompt", side_effect=generate,
            ), patch("api.server._persist_job"):
                server._prompt_worker(
                    job_id, "full project requirements", "custom", [],
                    output_mode="single_file",
                )

            job = server._JOBS[job_id]
            self.assertEqual("project", job["effective_output_mode"])
            self.assertEqual("validation_failed", job["status"])
        finally:
            server._JOBS.pop(job_id, None)
            server._JOB_QUEUES.pop(job_id, None)
            server._job_file(job_id).unlink(missing_ok=True)

    def test_running_job_response_omits_large_partial_output(self):
        response = _job_response({
            "job_id": "active", "status": "running", "events": [],
            "output": {"Demo/A.java": "x" * 100_000, "Demo/B.java": "class B {}"},
        })
        self.assertIsNone(response["output"])
        self.assertEqual(2, response["generated_file_count"])

    def test_completed_job_response_keeps_downloadable_output(self):
        output = {"Demo/A.java": "class A {}"}
        response = _job_response({
            "job_id": "done", "status": "completed", "events": [], "output": output,
        })
        self.assertEqual(output, response["output"])
        self.assertEqual(1, response["generated_file_count"])

    def test_status_response_is_bounded_and_omits_growing_job_details(self):
        response = _job_status_response({
            "job_id": "active", "status": "running", "progress": 86,
            "phase": "llm", "updated_at": "now", "error": None,
            "analysis": {"files": ["x" * 100_000]},
            "events": [{"message": "x" * 100_000}],
            "output": {"Demo/A.java": "x" * 100_000},
        })

        self.assertEqual(86, response["progress"])
        self.assertEqual(1, response["generated_file_count"])
        self.assertNotIn("analysis", response)
        self.assertNotIn("events", response)
        self.assertNotIn("output", response)

    def test_job_list_summary_accepts_legacy_checkpoint_fields(self):
        response = _job_list_summary({
            "job_id": "legacy-job",
            "status": "cancelled",
            "progress": 50,
            "updated_at": "2026-11-08T09:58:56+00:00",
        })

        self.assertEqual("legacy-job", response["job_id"])
        self.assertIsNone(response["folder_path"])
        self.assertEqual(response["updated_at"], response["created_at"])
        self.assertEqual("cancelled", response["status"])

    def test_lightweight_job_status_route_is_registered(self):
        methods = {
            method
            for route in app.routes
            if getattr(route, "path", None) == "/api/modernize/jobs/{job_id}/status"
            for method in (getattr(route, "methods", None) or set())
        }
        self.assertEqual({"GET"}, methods)

    def test_restart_recovers_requested_cancellation_as_cancelled(self):
        from api import server

        job_id = "restart-cancel-requested"
        snapshot = {
            "job_id": job_id,
            "status": "failed",
            "phase": "interrupted",
            "cancel_requested": True,
            "output": {"partial.sql": "select 1 from dual;"},
            "events": [],
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            server, "_JOBS_DIR", Path(temporary),
        ), patch.object(server._ACCESS_STORE, "sync_job"):
            (Path(temporary) / f"{job_id}.json").write_text(
                json.dumps(snapshot), encoding="utf-8",
            )
            server._JOBS.pop(job_id, None)
            server._LAST_JOB_PERSIST.pop(job_id, None)
            try:
                server._load_persisted_jobs()
                restored = server._JOBS[job_id]
                self.assertEqual("cancelled", restored["status"])
                self.assertEqual("cancelled", restored["phase"])
                self.assertIn("explicit operator request", restored["error"])
            finally:
                server._JOBS.pop(job_id, None)
                server._LAST_JOB_PERSIST.pop(job_id, None)


if __name__ == "__main__":
    unittest.main()
