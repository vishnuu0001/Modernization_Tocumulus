import os
import re
import threading
import time
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from services.modernizer.prompt_pipeline import (
    _pf_check_llm_availability,
    _pf_expand_generated_source_closure,
    _pf_generate_project_files_llm,
    _pf_generate_and_record_file,
    _pf_file_max_tokens,
    _pf_order_generation_paths,
    _pf_generate_source_delta,
    _pf_repair_build_round,
    _pf_repair_strict_prebuild_output,
    _pf_repair_java_module_boundaries,
    _pf_validate_final_output,
    _pf_configured_file_workers,
)
from services.modernizer.conversion_pipeline import (
    _caf_configured_file_workers,
    _mp_check_llm_availability,
)
from services.modernizer.target_config import TARGET_STACKS


class GenerationPerformanceTests(unittest.TestCase):
    def test_conversion_cache_is_cleaned_and_strictly_revalidated(self):
        from services.modernizer import conversion_pipeline
        from services.validators import ValidationResult

        passed = ValidationResult("Demo/Shared.cs", "csharp", "compiler", True, [])
        target = {
            "id": "dotnet9", "name": ".NET 9", "language": "csharp",
            "db_target": "mssql",
        }
        cached = (
            '<generated_file filename="Shared.cs">\n'
            "namespace Demo; public class Shared {}\n</generated_file>\n"
        )
        with patch.object(conversion_pipeline, "_read_conversion_cache", return_value=cached), patch(
            "services.validators.validate_file", return_value=passed,
        ) as validate, patch(
            "services.modernizer.validation_orchestration._generate_validated",
        ) as generate:
            content, result, attempts = conversion_pipeline._convert_file_with_llm(
                Path("Shared.java"), "public class Shared {}", "java", target,
                {}, "demo", "model", "system", out_path="Demo/Shared.cs",
            )

        self.assertEqual("namespace Demo; public class Shared {}\n", content)
        self.assertIsNone(result)
        self.assertEqual(1, attempts)
        validate.assert_called_once()
        generate.assert_not_called()

    def test_invalid_conversion_cache_is_rejected_and_regenerated(self):
        from services.modernizer import conversion_pipeline
        from services.validators import ValidationResult

        failed = ValidationResult(
            "Demo/Shared.cs", "csharp", "compiler", False, ["CS1513: } expected"],
        )
        passed = ValidationResult("Demo/Shared.cs", "csharp", "compiler", True, [])
        target = {
            "id": "dotnet9", "name": ".NET 9", "language": "csharp",
            "db_target": "mssql",
        }
        with patch.object(
            conversion_pipeline, "_read_conversion_cache",
            return_value="namespace Demo; public class Shared {",
        ), patch.object(conversion_pipeline, "_write_conversion_cache") as write, patch(
            "services.validators.validate_file", return_value=failed,
        ), patch(
            "services.modernizer.validation_orchestration._generate_validated",
            return_value=("namespace Demo; public class Shared {}", passed, 2),
        ) as generate:
            content, result, attempts = conversion_pipeline._convert_file_with_llm(
                Path("Shared.java"), "public class Shared {}", "java", target,
                {}, "demo", "model", "system", out_path="Demo/Shared.cs",
            )

        self.assertEqual("namespace Demo; public class Shared {}", content)
        self.assertTrue(result.passed)
        self.assertEqual(2, attempts)
        generate.assert_called_once()
        write.assert_called_once()

    def test_generation_paths_use_longest_processing_time_order(self):
        paths = [
            "docs/readme.md",
            "src/appsettings.json",
            "src/components/Dashboard.tsx",
        ]

        ordered = _pf_order_generation_paths(paths)

        self.assertEqual("src/components/Dashboard.tsx", ordered[0])
        self.assertEqual("src/appsettings.json", ordered[1])
        self.assertEqual("docs/readme.md", ordered[2])

    def test_docker_generation_cache_is_persistent_and_deadlines_are_explicit(self):
        compose = Path("DeploymentArtifacts/Latest/compose.yaml").read_text(encoding="utf-8")
        self.assertIn("/app/data/runtime/llm-cache", compose)
        self.assertIn("MODERNIZATION_PLANNING_TOTAL_BUDGET_SECONDS", compose)
        self.assertIn("MODERNIZATION_FILE_GENERATION_MAX_SECONDS", compose)
        self.assertIn("MODERNIZATION_JAVA_FILE_GENERATION_MAX_SECONDS", compose)

    def test_generation_entrypoints_reuse_model_selected_by_status_probe(self):
        status_result = {
            "available": True,
            "active_model": "devstral-small-2:24b",
            "models": ["devstral-small-2:24b"],
        }
        with patch("services.llm.check_status", return_value=status_result) as status, patch(
            "services.llm.pick_codegen_model",
        ) as redundant_pick:
            self.assertEqual(
                (True, "devstral-small-2:24b"), _mp_check_llm_availability(),
            )
            self.assertEqual(
                (True, "devstral-small-2:24b"), _pf_check_llm_availability(),
            )

        self.assertEqual(2, status.call_count)
        redundant_pick.assert_not_called()

    def test_isolated_validation_cache_reuses_only_byte_identical_content(self):
        from services import validators
        from services.validators import ValidationResult

        validators._clear_validation_cache()
        calls = []

        def validate_java(path, content, _tmp):
            calls.append((path, content))
            return ValidationResult(path, "java", "compiler", True, [])

        try:
            with patch.object(validators, "_validate_java", side_effect=validate_java):
                first = validators.validate_file("Demo.java", "public class Demo {}", "java")
                second = validators.validate_file("Demo.java", "public class Demo {}", "java")
                changed = validators.validate_file(
                    "Demo.java", "public class Demo { int value; }", "java",
                )
        finally:
            validators._clear_validation_cache()

        self.assertTrue(first.passed and second.passed and changed.passed)
        self.assertEqual(2, len(calls))

    def test_identical_concurrent_conversions_share_one_model_call(self):
        from services.modernizer import conversion_pipeline
        from services.validators import ValidationResult

        generated = 0
        generated_lock = threading.Lock()
        passed = ValidationResult("Demo.cs", "csharp", "compiler", True, [])

        def generate(_prompt, **_kwargs):
            nonlocal generated
            with generated_lock:
                generated += 1
            time.sleep(0.05)
            return "namespace Demo; public class Shared {}", passed, 1

        target = {
            "id": "dotnet9", "name": ".NET 9", "language": "csharp",
            "db_target": "mssql",
        }
        source = "package legacy; public class Shared {}"
        with patch.object(conversion_pipeline, "_read_conversion_cache", return_value=None), patch.object(
            conversion_pipeline, "_write_conversion_cache",
        ), patch(
            "services.modernizer.validation_orchestration._generate_validated",
            side_effect=generate,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        conversion_pipeline._convert_file_with_llm,
                        Path("Shared.java"), source, "java", target, {}, "demo",
                        "model", "system", out_path="Demo/Shared.cs",
                    )
                    for index in range(2)
                ]
                results = [future.result() for future in futures]

        self.assertEqual(1, generated)
        self.assertEqual(results[0][0], results[1][0])

    def test_conversion_singleflight_preserves_distinct_output_contracts(self):
        from services.modernizer import conversion_pipeline
        from services.validators import ValidationResult

        generated = []
        passed = ValidationResult("Demo.cs", "csharp", "compiler", True, [])

        def generate(prompt, **_kwargs):
            generated.append(prompt)
            time.sleep(0.02)
            return "namespace Demo; public class Shared {}", passed, 1

        target = {
            "id": "dotnet9", "name": ".NET 9", "language": "csharp",
            "db_target": "mssql",
        }
        source = "package legacy; public class Shared {}"
        with patch.object(conversion_pipeline, "_read_conversion_cache", return_value=None), patch.object(
            conversion_pipeline, "_write_conversion_cache",
        ), patch(
            "services.modernizer.validation_orchestration._generate_validated",
            side_effect=generate,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        conversion_pipeline._convert_file_with_llm,
                        Path("Shared.java"), source, "java", target, {}, "demo",
                        "model", "system", out_path=f"Demo/Layer{index}/Shared.cs",
                    )
                    for index in range(2)
                ]
                [future.result() for future in futures]

        self.assertEqual(2, len(generated))

    def test_preselected_model_avoids_duplicate_ollama_probe(self):
        from services.modernizer import conversion_pipeline

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "appsettings.json").write_text("{}", encoding="utf-8")
            with patch("services.llm.check_status") as status, patch(
                "services.llm.pick_codegen_model",
            ) as pick:
                output = conversion_pipeline._convert_all_files(
                    temp_dir, {},
                    {"name": ".NET 9", "language": "csharp", "llm_persona": "expert"},
                    "Demo", "dotnet9", llm_model="model", llm_available=True,
                )

        status.assert_not_called()
        pick.assert_not_called()
        self.assertEqual({}, output)

    def test_deterministic_documentation_does_not_probe_ollama(self):
        from services.modernizer import docs_generation

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            docs_generation, "_gmd_check_llm",
        ) as check:
            docs = docs_generation._generate_modernization_docs(
                temp_dir, {}, {"name": ".NET 9", "language": "csharp"},
                "Demo", {},
            )

        check.assert_not_called()
        self.assertIn("ModernizedApp/MODERNIZATION_REPORT.md", docs)

    def test_csharp_prebuild_does_not_repair_isolated_semantic_noise(self):
        from services.validators import ValidationResult

        path = "Demo/Services/OrderService.cs"
        output = {path: "public class OrderService { MissingSibling value; }"}
        semantic_failure = ValidationResult(
            path, "csharp", "compiler", False,
            ["line 1 CS1061: 'MissingSibling' does not contain a definition for 'Save'"],
        )
        counts = {"checked": 1, "passed": 0, "failed": 1, "retried": 0, "by_checker": {}}

        with patch(
            "services.modernizer.prompt_pipeline._pf_validate_final_output",
            return_value=(counts, [{
                "path": path, "checker": "compiler", "diagnostics": semantic_failure.diagnostics,
            }]),
        ), patch(
            "services.modernizer.prompt_pipeline._pf_repair_build_round",
        ) as repair:
            _pf_repair_strict_prebuild_output(
                output, "csharp", "mssql", "", "", "model", "system",
                lambda *_args: None,
            )

        repair.assert_not_called()

    def test_java_full_stack_prebuild_repairs_typescript_parameter_order(self):
        path = "ModernizedApp/backend/legacy-core/src/components/Shashi/ShashiService.ts"
        output = {
            path: (
                "const fetchWithAbort = async <T>(\n"
                "  input: RequestInfo | URL,\n"
                "  init?: RequestInit & { signal?: AbortSignal },\n"
                "  schema: { parse(value: unknown): T }\n"
                "): Promise<T> => schema.parse(await (await fetch(input, init)).json());\n"
            )
        }

        _pf_repair_strict_prebuild_output(
            output, "java", "postgres", "", "", "model", "system",
            lambda *_args: None,
        )

        self.assertIn(
            "init: (RequestInit & { signal?: AbortSignal }) | undefined",
            output[path],
        )
        self.assertNotIn("init?:", output[path])

    def test_every_supported_language_honors_deployment_batch_profile(self):
        languages = {target["language"] for target in TARGET_STACKS.values()}
        with patch.dict(os.environ, {"MODERNIZATION_ASYNC_BATCH_WORKERS": "2"}, clear=True):
            for language in languages:
                with self.subTest(language=language):
                    self.assertEqual(2, _pf_configured_file_workers(language))
                    self.assertEqual(2, _caf_configured_file_workers(language, 4))

    def test_worker_configuration_is_bounded_and_invalid_values_fail_safe(self):
        with patch.dict(os.environ, {
            "MODERNIZATION_FILE_WORKERS": "99",
            "MODERNIZATION_JAVA_FILE_WORKERS": "invalid",
            "MODERNIZATION_WORKERS": "0",
            "MODERNIZATION_JAVA_CONVERSION_WORKERS": "99",
        }, clear=True):
            self.assertEqual(4, _pf_configured_file_workers("python"))
            self.assertEqual(1, _pf_configured_file_workers("java"))
            self.assertEqual(4, _caf_configured_file_workers("python", 4))
            self.assertEqual(4, _caf_configured_file_workers("java", 8))

    def test_stylesheets_use_a_bounded_output_budget(self):
        self.assertEqual(1536, _pf_file_max_tokens("frontend/src/styles.css"))
        self.assertEqual(1536, _pf_file_max_tokens("frontend/src/app/card.component.scss"))

    def test_governed_configuration_and_deployment_artifacts_have_complete_budget(self):
        for path in (
            "backend/appsettings.json",
            "pipelines/azure-devops/azure-pipelines.yml",
            "infrastructure/kubernetes/deployment.yaml",
            "infrastructure/bicep/main.bicep",
        ):
            with self.subTest(path=path):
                self.assertEqual(4096, _pf_file_max_tokens(path))

    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_truncated_file_is_discarded_and_retried_with_expanded_budget(self, generate):
        from services.validators import ValidationResult

        generate.side_effect = [
            RuntimeError(
                "LLM generation failed: LLM output was truncated at the configured token limit; "
                "no partial artifact was accepted"
            ),
            (
                '{"Logging":{"LogLevel":{"Default":"Information"}}}',
                ValidationResult("Demo/backend/appsettings.json", "json", "parser", True, []),
                1,
            ),
        ]
        output = {}
        progress_events = []
        target = {
            "name": ".NET",
            "backend_tech": ".NET 10",
            "frontend_tech": "Angular",
            "db_tech": "SQL Server",
        }

        _pf_generate_and_record_file(
            "backend/appsettings.json", 0, 1, "Demo", target, "csharp", "model", "system",
            "contracts", "namespace", "required", "manifest", "request", "guide", "stack",
            "template", "assessment", output,
            lambda path, content: output.__setitem__(path, content),
            lambda *_args: None,
            lambda *args: progress_events.append(args),
            "user prompt",
        )

        self.assertEqual([4096, 8192], [call.kwargs["max_tokens"] for call in generate.call_args_list])
        self.assertIn("TOKEN-LIMIT RECOVERY", generate.call_args_list[1].args[0])
        self.assertIn("Demo/backend/appsettings.json", output)
        self.assertTrue(any("discarded the incomplete response" in event[2] for event in progress_events))

    def test_complex_one_file_sql_project_uses_bounded_structural_generation(self):
        from services.validators import ValidationResult

        output = {}
        validations = []
        target = {
            "name": "Oracle Database", "language": "sql",
            "backend_tech": "Oracle PL/SQL", "frontend_tech": "",
            "db_tech": "Oracle Database", "db_target": "oracle",
        }
        requirement = (
            "Generate one .sql file.\n"
            "1. Create the sample tables.\n"
            "2. Create V_ACCOUNT_BALANCE.\n"
            "3. Create FN_GET_ACCOUNT_BALANCE.\n"
        )
        result = ValidationResult(
            "Demo/database/generated.sql", "sql", "compiler", True, [],
        )

        with patch(
            "services.modernizer.prompt_pipeline._pf_generate_structural_single_file",
            return_value=("CREATE VIEW V_ACCOUNT_BALANCE AS SELECT 1 n FROM dual;\n", result, 3),
        ) as structural, patch(
            "services.modernizer.validation_orchestration._generate_validated",
        ) as monolithic:
            _pf_generate_and_record_file(
                "database/generated.sql", 0, 1, "Demo", target, "sql", "model", "system",
                "", "", "", "  database/generated.sql", "Oracle Project", "", requirement,
                "", "", output,
                lambda path, content: output.__setitem__(path, content),
                lambda *args: validations.append(args),
                lambda *_args: None,
                "Oracle Project",
            )

        self.assertEqual(1, structural.call_count)
        self.assertEqual(0, monolithic.call_count)
        self.assertEqual(3, len(structural.call_args.kwargs["contract"].members))
        self.assertEqual("oracle", structural.call_args.kwargs["dialect"])
        self.assertIn("Demo/database/generated.sql", output)
        self.assertEqual([(result, 3)], validations)

    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_multi_file_sql_project_keeps_per_file_generation(self, generated):
        from services.validators import ValidationResult

        result = ValidationResult("Demo/database/schema.sql", "sql", "compiler", True, [])
        generated.return_value = ("CREATE TABLE demo (id NUMBER);\n", result, 1)
        output = {}
        target = {
            "name": "Oracle Database", "language": "sql",
            "backend_tech": "Oracle PL/SQL", "frontend_tech": "",
            "db_tech": "Oracle Database", "db_target": "oracle",
        }
        requirement = (
            "Generate separate SQL files.\n"
            "1. Create the sample tables.\n2. Create the views.\n3. Create the package.\n"
        )

        with patch(
            "services.modernizer.prompt_pipeline._pf_generate_structural_single_file",
        ) as structural:
            _pf_generate_and_record_file(
                "database/schema.sql", 0, 2, "Demo", target, "sql", "model", "system",
                "", "", "", "  database/schema.sql\n  database/views.sql", "Oracle Project",
                "", requirement, "", "", output,
                lambda path, content: output.__setitem__(path, content),
                lambda *_args: None, lambda *_args: None, "Oracle Project",
            )

        self.assertEqual(0, structural.call_count)
        self.assertEqual(1, generated.call_count)

    def test_test_files_use_the_standard_not_migration_token_budget(self):
        self.assertEqual(4096, _pf_file_max_tokens("tests/backend/TransactionServiceTests.cs"))

    def test_java_final_validation_runs_as_bounded_batch(self):
        output = {
            f"Demo/src/main/java/demo/Type{i}.java": f"class Type{i} {{}}"
            for i in range(6)
        }
        activity_lock = threading.Lock()
        active = 0
        max_active = 0

        def validate(path, _content, _language, **_kwargs):
            from services.validators import ValidationResult
            nonlocal active, max_active
            with activity_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with activity_lock:
                active -= 1
            return ValidationResult(path, "java", "compiler", True, [])

        with patch.dict(os.environ, {"MODERNIZATION_JAVA_VALIDATION_WORKERS": "3"}), patch(
            "services.validators.validate_file", side_effect=validate,
        ), patch(
            "services.modernizer.prompt_pipeline._java_generation_standards_report",
            return_value={"passed": True, "diagnostics": []},
        ):
            counts, failures = _pf_validate_final_output(
                output, "java", "postgres", lambda *_args: None,
            )

        self.assertEqual([], failures)
        self.assertEqual(7, counts["checked"])
        self.assertGreaterEqual(max_active, 3)

    def test_compiler_repairs_run_concurrently(self):
        output = {
            f"Demo/src/main/java/demo/Type{i}.java": f"class Type{i} {{}}"
            for i in range(4)
        }
        active = 0
        max_active = 0
        lock = threading.Lock()

        def generate(prompt, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            path = re.search(r"FILE PATH: (.+)", prompt).group(1).strip()
            with lock:
                active -= 1
            return output[path] + " // repaired"

        fixable = {path: ["line 1: ';' expected"] for path in output}
        with patch.dict(os.environ, {"MODERNIZATION_JAVA_REPAIR_WORKERS": "2"}), patch(
            "services.llm.generate", side_effect=generate,
        ):
            failures = _pf_repair_build_round(
                fixable, 1, 2, output, "", "", "model", "system",
                lambda *_args: None, "java",
            )
        self.assertEqual({}, failures)
        self.assertGreaterEqual(max_active, 2)
        self.assertTrue(all("// repaired" in value for value in output.values()))

    def test_java_closure_never_synthesizes_third_party_com_types(self):
        module = "Demo/services/notification-service"
        consumer = f"{module}/src/main/java/com/demo/notification/Notifier.java"
        output = {
            consumer: (
                "package com.demo.notification;\n"
                "import com.fasterxml.jackson.databind.node.ObjectNode;\n"
                "import com.fasterxml.jackson.databind.ObjectMapper;\n"
                "public class Notifier { ObjectNode payload; ObjectMapper mapper; }\n"
            ),
            f"{module}/src/main/java/com/demo/notification/App.java": (
                "package com.demo.notification; public class App {}"
            ),
        }
        added = _pf_expand_generated_source_closure(output, "Demo")
        self.assertFalse(any(path.endswith("ObjectNode.java") for path in added))
        self.assertFalse(any(path.endswith("ObjectMapper.java") for path in added))

    def test_java_closure_never_synthesizes_fully_qualified_spring_utility(self):
        module = "Demo/backend/legacy-core"
        consumer = f"{module}/src/main/java/com/demo/TrainUtil.java"
        output = {
            consumer: (
                "package com.demo;\n"
                "class TrainUtil { Object bean(Object request) { return "
                "org.springframework.web.context.support.WebApplicationContextUtils"
                ".getRequiredWebApplicationContext(request).getBean(Object.class); } }\n"
            ),
            f"{module}/src/main/java/com/demo/App.java": (
                "package com.demo; public class App {}"
            ),
        }

        added = _pf_expand_generated_source_closure(output, "Demo")

        self.assertFalse(any(path.endswith("WebApplicationContextUtils.java") for path in added))

    def test_java_closure_detects_dynamic_typescript_imports(self):
        output = {
            "Demo/frontend/src/app/app-routing.module.ts": (
                "const routes = [{ loadChildren: () => "
                "import('./features/orders/orders.module').then(m => m.OrdersModule) }];"
            )
        }
        added = _pf_expand_generated_source_closure(output, "Demo")
        self.assertIn("Demo/frontend/src/app/features/orders/orders.module.ts", added)

    def test_frontend_closure_routes_provider_to_tsx_and_type_import_to_ts(self):
        output = {
            "Demo/frontend/src/components/Shashi/ShashiPage.tsx": (
                "import type { TrainBean, HistoryBean } from '../types/shashi.types';\n"
                "import { useAuthContext } from '../auth/AuthProvider';\n"
                "import { UserServiceClient } from '../api/UserApiClient';\n"
                "export function ShashiPage() { const auth = useAuthContext(); "
                "UserServiceClient.getUserById(auth.userId); "
                "return <section>{auth.userId}</section>; }\n"
            )
        }

        added = _pf_expand_generated_source_closure(output, "Demo")

        types_path = "Demo/frontend/src/components/types/shashi.types.ts"
        provider_path = "Demo/frontend/src/components/auth/AuthProvider.tsx"
        client_path = "Demo/frontend/src/components/api/UserApiClient.ts"
        self.assertIn(types_path, added)
        self.assertIn(provider_path, added)
        self.assertIn(client_path, added)
        self.assertIn("TrainBean, HistoryBean", output[types_path])
        self.assertIn("Do not emit React, JSX", output[types_path])
        self.assertIn("TrainBean: observed properties", output[types_path])
        self.assertNotIn("return <section>", output[types_path])
        self.assertIn("never copy the consumer source", output[provider_path])
        self.assertIn("UserServiceClient.getUserById(...)", output[client_path])
        self.assertNotIn("return <section>", output[client_path])

    def test_java_files_run_in_parallel_dependency_waves(self):
        activity_lock = threading.Lock()
        active = 0
        max_active = 0
        completed = []
        service_started_after = []
        output = {}

        def fake_generate(fname, *args):
            nonlocal active, max_active
            record = args[-4]
            with activity_lock:
                active += 1
                max_active = max(max_active, active)
                if "/service/" in fname:
                    service_started_after.append(set(completed))
            time.sleep(0.04)
            record(f"Demo/{fname}", f"// {fname}")
            with activity_lock:
                completed.append(fname)
                active -= 1

        files = [
            "src/main/java/demo/model/Order.java",
            "src/main/java/demo/dto/OrderDto.java",
            "src/main/java/demo/service/OrderService.java",
        ]
        with patch.dict(os.environ, {"MODERNIZATION_JAVA_FILE_WORKERS": "2"}), patch(
            "services.modernizer.prompt_pipeline._pf_generate_and_record_file",
            side_effect=fake_generate,
        ):
            _pf_generate_project_files_llm(
                files, "Demo", {}, "java", "model", "system", "contracts", "namespace",
                "requirements", "manifest", "request", "guide", "stack", "template",
                "assessment", output, lambda path, content: output.__setitem__(path, content),
                lambda *_args: None, lambda *_args: None, "prompt",
            )

        self.assertGreaterEqual(max_active, 2)
        self.assertEqual(1, len(service_started_after))
        self.assertTrue(set(files[:2]).issubset(service_started_after[0]))
        self.assertEqual(3, len(output))

    def test_non_java_files_use_the_shared_async_batch_profile(self):
        activity_lock = threading.Lock()
        active = 0
        max_active = 0
        output = {}

        def fake_generate(fname, *args):
            nonlocal active, max_active
            record = args[-4]
            with activity_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            record(f"Demo/{fname}", f"// {fname}")
            with activity_lock:
                active -= 1

        with patch.dict(os.environ, {"MODERNIZATION_ASYNC_BATCH_WORKERS": "2", "MODERNIZATION_FILE_WORKERS": ""}):
            os.environ.pop("MODERNIZATION_FILE_WORKERS", None)
            with patch(
                "services.modernizer.prompt_pipeline._pf_generate_and_record_file",
                side_effect=fake_generate,
            ):
                _pf_generate_project_files_llm(
                    ["src/first.ts", "src/second.ts"], "Demo", {}, "typescript",
                    "model", "system", "contracts", "namespace", "requirements",
                    "manifest", "request", "guide", "stack", "template", "assessment",
                    output, lambda path, content: output.__setitem__(path, content),
                    lambda *_args: None, lambda *_args: None, "prompt",
                )

        self.assertEqual(2, max_active)

    def test_file_completion_updates_progress_before_wave_finishes(self):
        output = {}
        progress_events = []

        def fake_generate(fname, *args):
            record = args[-4]
            report_progress = args[-2]
            report_progress("llm", 52, f"Structural progress for {fname}")
            if fname.endswith("slow.ts"):
                time.sleep(0.08)
            record(f"Demo/{fname}", f"// {fname}")

        with patch.dict(os.environ, {"MODERNIZATION_FILE_WORKERS": "2"}), patch(
            "services.modernizer.prompt_pipeline._pf_generate_and_record_file",
            side_effect=fake_generate,
        ):
            _pf_generate_project_files_llm(
                ["src/fast.ts", "src/slow.ts"], "Demo", {}, "typescript", "model", "system",
                "contracts", "namespace", "requirements", "manifest", "request", "guide",
                "stack", "template", "assessment", output,
                lambda path, content: output.__setitem__(path, content),
                lambda *_args: None, lambda *args: progress_events.append(args), "prompt",
            )

        generated = [event for event in progress_events if "Generated " in event[2]]
        self.assertEqual(2, len(generated))
        self.assertIn("Generated 1/2", generated[0][2])
        self.assertIn("Generated 2/2", generated[1][2])
        self.assertLess(generated[0][1], generated[1][1])
        structural = [event for event in progress_events if "Structural progress" in event[2]]
        self.assertEqual(2, len(structural))
        self.assertTrue(all(event[1] >= 52 for event in structural))
        self.assertIn(52, [event[1] for event in structural])

    def test_generation_wave_uses_batch_aware_budget(self):
        output = {}
        observed_budgets = []

        def fake_generate(fname, *args):
            record = args[-4]
            record(f"Demo/{fname}", f"// {fname}")

        def run_round(
            executor, futures, *, round_budget_seconds, label, activity_by_key=None,
        ):
            observed_budgets.append(round_budget_seconds)
            self.assertEqual(set(futures.values()), set(activity_by_key or {}))
            done = set(futures)
            executor.shutdown(wait=True)
            return {future: futures[future] for future in done}, {}

        files = [f"tests/backend/Service{i}Tests.cs" for i in range(5)]
        with patch.dict(os.environ, {"MODERNIZATION_FILE_WORKERS": "1"}), patch(
            "services.modernizer.prompt_pipeline._pf_generate_and_record_file",
            side_effect=fake_generate,
        ), patch(
            "services.modernizer._shared._WAVE_ROUND_BUDGET_SECONDS", 1800,
        ), patch(
            "services.modernizer._shared._FILE_GENERATION_MAX_SECONDS", 600,
        ), patch(
            "services.modernizer._shared._run_bounded_round",
            side_effect=run_round,
        ):
            _pf_generate_project_files_llm(
                files, "Demo", {}, "csharp", "model", "system", "contracts", "namespace",
                "requirements", "manifest", "request", "guide", "stack", "template",
                "assessment", output, lambda path, content: output.__setitem__(path, content),
                lambda *_args: None, lambda *_args: None, "prompt",
            )

        self.assertEqual([3060], observed_budgets)
        self.assertEqual(5, len(output))

    def test_closure_does_not_expand_unrequested_tests_and_migrations(self):
        module = "Demo/services/orders-service"
        output = {
            f"{module}/src/main/java/com/demo/orders/entity/Order.java": (
                "package com.demo.orders.entity; @Entity public class Order {}"
            ),
            f"{module}/src/main/java/com/demo/orders/OrderApplication.java": (
                "package com.demo.orders; public class OrderApplication {}"
            ),
        }
        added = _pf_expand_generated_source_closure(output, "Demo")
        self.assertEqual([], added)
        self.assertFalse(any("/src/test/" in path for path in output))
        self.assertFalse(any("/db/migration/" in path for path in output))

    def test_boundary_repair_is_single_pass_per_file(self):
        orders = "Demo/services/orders-service"
        users = "Demo/services/users-service"
        path = f"{orders}/src/main/java/com/demo/orders/OrderService.java"
        output = {
            path: (
                "package com.demo.orders; import com.demo.users.User; "
                "public class OrderService { User user; }"
            ),
            f"{users}/src/main/java/com/demo/users/User.java": (
                "package com.demo.users; public class User {}"
            ),
        }
        repaired = set()
        with patch(
            "services.llm.generate",
            return_value="package com.demo.orders; public class OrderService {}",
        ) as generate:
            first = _pf_repair_java_module_boundaries(
                output, "model", "system", lambda *_args: None, repaired,
            )
            second = _pf_repair_java_module_boundaries(
                output, "model", "system", lambda *_args: None, repaired,
            )
        self.assertEqual(1, first)
        self.assertEqual(0, second)
        self.assertEqual(1, generate.call_count)

    def test_boundary_repair_rejects_semantic_non_convergence(self):
        orders = "Demo/services/orders-service"
        users = "Demo/services/users-service"
        path = f"{orders}/src/main/java/com/demo/orders/OrderService.java"
        original = (
            "package com.demo.orders; import com.demo.users.User; "
            "public class OrderService { User user; }"
        )
        output = {
            path: original,
            f"{users}/src/main/java/com/demo/users/User.java": (
                "package com.demo.users; public class User {}"
            ),
        }
        states = set()
        with patch("services.llm.generate", return_value=original):
            self.assertEqual(1, _pf_repair_java_module_boundaries(
                output, "model", "system", lambda *_args: None, states,
            ))
            with self.assertRaisesRegex(RuntimeError, "did not converge"):
                _pf_repair_java_module_boundaries(
                    output, "model", "system", lambda *_args: None, states,
                )

    def test_same_named_foreign_exceptions_are_localized_not_boundary_repaired(self):
        photoshop = "ModernizedApp/services/photoshop-service"
        mina = "ModernizedApp/services/mina-service"
        notification = "ModernizedApp/services/notification-service"
        service_path = (
            f"{photoshop}/src/main/java/com/mina/photoshop/service/PhotoshopService.java"
        )
        output = {
            service_path: (
                "package com.mina.photoshop.service; "
                "public class PhotoshopService { void find() { "
                "throw new ResourceNotFoundException(\"missing\"); } }"
            ),
            f"{mina}/src/main/java/com/mina/mina/service/ResourceNotFoundException.java": (
                "package com.mina.mina.service; "
                "public class ResourceNotFoundException extends RuntimeException {}"
            ),
            f"{notification}/src/main/java/com/mina/notification/service/ResourceNotFoundException.java": (
                "package com.mina.notification.service; "
                "public class ResourceNotFoundException extends RuntimeException {}"
            ),
        }
        with patch("services.llm.generate") as generate:
            repaired = _pf_repair_java_module_boundaries(
                output, "model", "system", lambda *_args: None, set(),
            )
        self.assertEqual(0, repaired)
        generate.assert_not_called()

        added = _pf_expand_generated_source_closure(output, "ModernizedApp")
        local_exception = (
            f"{photoshop}/src/main/java/com/mina/photoshop/exception/"
            "ResourceNotFoundException.java"
        )
        self.assertIn(local_exception, added)
        self.assertNotIn("com.mina.mina.service.ResourceNotFoundException", output[service_path])
        self.assertNotIn(
            "com.mina.notification.service.ResourceNotFoundException", output[service_path],
        )

    def test_explicit_foreign_exception_import_is_localized_before_boundary_check(self):
        photoshop = "ModernizedApp/services/photoshop-service"
        mina = "ModernizedApp/services/mina-service"
        service_path = f"{photoshop}/src/main/java/com/mina/photoshop/service/PhotoshopService.java"
        local_fqcn = "com.mina.photoshop.exception.ResourceNotFoundException"
        output = {
            service_path: (
                "package com.mina.photoshop.service;\n"
                "import com.mina.mina.service.ResourceNotFoundException;\n"
                "public class PhotoshopService { ResourceNotFoundException error; }"
            ),
            f"{mina}/src/main/java/com/mina/mina/service/ResourceNotFoundException.java": (
                "package com.mina.mina.service; "
                "public class ResourceNotFoundException extends RuntimeException {}"
            ),
        }
        added = _pf_expand_generated_source_closure(output, "ModernizedApp")
        self.assertIn(
            f"{photoshop}/src/main/java/{local_fqcn.replace('.', '/')}.java", added,
        )
        self.assertIn(f"import {local_fqcn};", output[service_path])
        output[
            f"{photoshop}/src/main/java/{local_fqcn.replace('.', '/')}.java"
        ] = (
            f"package {local_fqcn.rsplit('.', 1)[0]}; "
            "public class ResourceNotFoundException extends RuntimeException {}"
        )
        with patch("services.llm.generate") as generate:
            self.assertEqual(0, _pf_repair_java_module_boundaries(
                output, "model", "system", lambda *_args: None, set(),
            ))
        generate.assert_not_called()

    def test_complete_closure_delta_is_parallel(self):
        output = {f"Demo/src/Type{i}.java": f"contract-{i}" for i in range(4)}
        lock = threading.Lock()
        active = 0
        max_active = 0

        def generate(files, *_args, **_kwargs):
            nonlocal active, max_active
            path = next(iter(files))
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            files[path] = f"generated::{path}"
            with lock:
                active -= 1

        with patch.dict(os.environ, {
            "MODERNIZATION_CLOSURE_WORKERS": "2",
        }), patch(
            "services.modernizer.domain_generators.dispatch._ollama_generate_all_sources",
            side_effect=generate,
        ):
            _pf_generate_source_delta(
                output, list(output), {"language": "java"}, "Demo", "model", "system",
                lambda *_args: None,
            )
        self.assertGreaterEqual(max_active, 2)
        self.assertTrue(all(value.startswith("generated::") for value in output.values()))

    def test_java_closure_uses_async_batch_and_java_file_budget(self):
        from services.modernizer._shared import _JAVA_FILE_GENERATION_MAX_SECONDS

        output = {f"Demo/frontend/src/types/Type{i}.ts": f"contract-{i}" for i in range(2)}
        observed_budgets = []
        active = 0
        max_active = 0
        lock = threading.Lock()

        def generate(files, *_args, **kwargs):
            nonlocal active, max_active
            observed_budgets.append(kwargs["generation_max_seconds"])
            path = next(iter(files))
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            files[path] = f"generated::{path}"
            with lock:
                active -= 1

        with patch.dict(os.environ, {"MODERNIZATION_CLOSURE_WORKERS": "2"}, clear=False), patch(
            "services.modernizer.domain_generators.dispatch._ollama_generate_all_sources",
            side_effect=generate,
        ):
            previous = os.environ.get("MODERNIZATION_CLOSURE_WORKERS")
            try:
                _pf_generate_source_delta(
                    output, list(output), {"language": "java"}, "Demo", "model", "system",
                    lambda *_args: None,
                )
            finally:
                if previous is not None:
                    os.environ["MODERNIZATION_CLOSURE_WORKERS"] = previous

        self.assertEqual(2, max_active)
        self.assertEqual(
            [_JAVA_FILE_GENERATION_MAX_SECONDS, _JAVA_FILE_GENERATION_MAX_SECONDS],
            observed_budgets,
        )

    def test_external_frontend_test_imports_close_against_application_root(self):
        output = {
            "Demo/tests/frontend/transaction.service.spec.ts": (
                "import { environment } from './src/types/environment';\n"
                "import { Transaction } from './src/types/transaction';\n"
            ),
            "Demo/frontend/src/environments/environment.ts": (
                "export const environment = { production: false };\n"
            ),
        }

        added = _pf_expand_generated_source_closure(output, "Demo")

        self.assertNotIn("Demo/tests/frontend/src/types/environment.ts", output)
        self.assertNotIn("Demo/tests/frontend/src/types/transaction.ts", output)
        self.assertIn("Demo/frontend/src/types/transaction.ts", added)
        self.assertIn(
            "../../frontend/src/environments/environment",
            output["Demo/tests/frontend/transaction.service.spec.ts"],
        )

    def test_non_java_closure_retains_repair_budget_and_worker_default(self):
        from services.modernizer._shared import _REPAIR_CALL_MAX_SECONDS

        output = {"Demo/src/type.ts": "contract"}
        observed = []

        def generate(files, *_args, **kwargs):
            observed.append(kwargs["generation_max_seconds"])
            path = next(iter(files))
            files[path] = "generated"

        with patch.dict(os.environ, {}, clear=False), patch(
            "services.modernizer.domain_generators.dispatch._ollama_generate_all_sources",
            side_effect=generate,
        ):
            previous = os.environ.pop("MODERNIZATION_CLOSURE_WORKERS", None)
            try:
                _pf_generate_source_delta(
                    output, list(output), {"language": "csharp"}, "Demo", "model", "system",
                    lambda *_args: None,
                )
            finally:
                if previous is not None:
                    os.environ["MODERNIZATION_CLOSURE_WORKERS"] = previous

        self.assertEqual([_REPAIR_CALL_MAX_SECONDS], observed)

    def test_failed_closure_generation_does_not_retain_prompt_as_source(self):
        path = "Demo/frontend/src/types/ShashiTypes.ts"
        output = {path: "Create the missing TypeScript type-contract module"}

        with patch(
            "services.modernizer.domain_generators.dispatch._ollama_generate_all_sources",
            side_effect=RuntimeError("truncated"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Bounded source closure failed"):
                _pf_generate_source_delta(
                    output, [path], {"language": "java"}, "Demo", "model", "system",
                    lambda *_args: None,
                )

        self.assertNotIn(path, output)


if __name__ == "__main__":
    unittest.main()
