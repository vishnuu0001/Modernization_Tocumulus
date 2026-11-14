# ---------------------------------------------------------------------------
# Scope: Java-only legacy asset routing and compiler-repair eligibility.
# ---------------------------------------------------------------------------
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.modernizer._shared import _make_output_path
from services.modernizer.build_artifacts import (
    _normalize_java_build_roots,
    _unwrap_generated_source_envelopes,
)
from services.modernizer.conversion_pipeline import _caf_convert_one_file
from services.modernizer.domain_generators.dispatch import _validated_domain_cache
from services.modernizer.domain_generators.typescript import _llm_domain_typescript
from services.modernizer.prompt_pipeline import (
    _pf_java_repair_candidates,
    _pf_repair_build_round,
)
from services.validators import ValidationResult


class JavaAssetPathRoutingTests(unittest.TestCase):
    def test_java_sources_resources_and_browser_assets_use_distinct_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                root / "src/main/java/com/example/Order.java":
                    "ModernizedApp/src/main/java/com/example/Order.java",
                root / "TransportationSetup/WEB-INF/web.xml":
                    "ModernizedApp/src/main/resources/TransportationSetup/WEB-INF/web.xml",
                root / "TransportationSetup/javascript/jquery.min.js":
                    "ModernizedApp/frontend/public/legacy/TransportationSetup/javascript/jquery.min.js",
                root / "TransportationSetup/styles/site.css":
                    "ModernizedApp/frontend/public/legacy/TransportationSetup/styles/site.css",
            }
            for source, expected in cases.items():
                with self.subTest(source=source):
                    actual = _make_output_path(source, root, "java", "Demo", "spring_boot")
                    self.assertEqual(actual.replace("\\", "/"), expected)

    def test_non_java_path_contract_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src/assets/site.js"
            actual = _make_output_path(source, root, "csharp", "Demo", "dotnet")
            self.assertEqual(actual.replace("\\", "/"), "ModernizedApp/src/Assets/site.cs")

    def test_java_reconciliation_routes_staged_frontend_outside_maven_modules(self):
        output = {
            "ModernizedApp/services/orders-service/src/main/java/demo/Order.java":
                "package demo; public class Order {}\n",
            "ModernizedApp/src/com/example/Legacy.java":
                "package com.example; public class Legacy {}\n",
            "ModernizedApp/src/components/Orders/OrdersPage.tsx":
                "export const OrdersPage = () => <main>Orders</main>;\n",
            "ModernizedApp/src/styles/site.scss": "main { color: blue; }\n",
            "ModernizedApp/backend/legacy-core/src/components/Auth/AuthService.ts":
                "export class AuthService {}\n",
        }

        _normalize_java_build_roots(output, "ModernizedApp")

        self.assertIn(
            "ModernizedApp/backend/legacy-core/src/com/example/Legacy.java", output,
        )
        self.assertIn(
            "ModernizedApp/frontend/src/components/Orders/OrdersPage.tsx", output,
        )
        self.assertIn("ModernizedApp/frontend/src/styles/site.scss", output)
        self.assertIn(
            "ModernizedApp/frontend/src/components/Auth/AuthService.ts", output,
        )
        self.assertNotIn(
            "ModernizedApp/backend/legacy-core/src/components/Orders/OrdersPage.tsx", output,
        )

    def test_reconciliation_removes_attributed_or_incomplete_source_envelopes(self):
        output = {
            "ModernizedApp/src/components/Orders/Page.tsx": (
                '<generated_file path="src/pages/Page.tsx">\n'
                "export const Page = () => <main>Orders</main>;\n"
            ),
            "ModernizedApp/src/components/Orders/api.ts": (
                "<generated_file>\nexport const load = async () => [];\n"
                "</generated_file>\n<generation_notes>{}</generation_notes>"
            ),
        }

        _unwrap_generated_source_envelopes(output)

        self.assertTrue(output["ModernizedApp/src/components/Orders/Page.tsx"].startswith("export"))
        self.assertNotIn("generated_file", "".join(output.values()))


class JavaDomainCacheAdmissionTests(unittest.TestCase):
    def test_cache_rejects_invalid_typescript_in_java_full_stack_project(self):
        cached = {
            "ModernizedApp/src/components/Auth/AuthService.ts":
                "<generated_file>\nexport class AuthService {\n",
        }
        failure = ValidationResult(
            "ModernizedApp/src/components/Auth/AuthService.ts",
            "typescript", "compiler", False, ["TS1005: '}' expected."],
        )

        with patch("services.validators.validate_file", return_value=failure) as validate:
            admitted = _validated_domain_cache(cached, {
                "language": "java", "db_target": "mssql",
            })

        self.assertIsNone(admitted)
        self.assertEqual(validate.call_args.args[2], "typescript")

    def test_cache_cleans_complete_envelope_before_admission(self):
        cached = {
            "ModernizedApp/src/components/Auth/AuthService.ts": (
                '<generated_file filename="AuthService.ts">\n'
                "export class AuthService {}\n</generated_file>\n"
            ),
        }
        success = ValidationResult(
            "ModernizedApp/src/components/Auth/AuthService.ts",
            "typescript", "compiler", True, [],
        )

        with patch("services.validators.validate_file", return_value=success):
            admitted = _validated_domain_cache(cached, {
                "language": "java", "db_target": "mssql",
            })

        self.assertEqual(
            admitted["ModernizedApp/src/components/Auth/AuthService.ts"],
            "export class AuthService {}\n",
        )

    def test_cache_validates_polyglot_source_using_its_extension(self):
        cached = {
            "ModernizedApp/services/orders/src/main.rs": "fn main() {}\n",
        }
        success = ValidationResult(
            "ModernizedApp/services/orders/src/main.rs",
            "rust", "compiler", True, [],
        )

        with patch("services.validators.validate_file", return_value=success) as validate:
            admitted = _validated_domain_cache(cached, {"language": "java"})

        self.assertIsNotNone(admitted)
        self.assertEqual(validate.call_args.args[2], "rust")


class JavaFrontendGenerationFailureTests(unittest.TestCase):
    def test_failed_frontend_validation_propagates_without_placeholder_source(self):
        files = {}
        failure = ValidationResult(
            "ModernizedApp/src/components/Auth/AuthPage.tsx",
            "typescript", "compiler", False, ["TS1005: '}' expected."],
        )

        with patch(
            "services.modernizer.validation_orchestration._generate_validated",
            return_value=("export const broken = () => {", failure, 2),
        ) as generated:
            with self.assertRaisesRegex(RuntimeError, "component generation failed"):
                _llm_domain_typescript(
                    files, "Auth", "Demo", [], "context", "rules", "source", "guide",
                    "model", "system", "React", {"name": "Java React"}, None,
                    lambda *_args, **_kwargs: "", None,
                )

        self.assertEqual({}, files)
        self.assertEqual(3, generated.call_args.kwargs["max_attempts"])


class JavaLegacyBrowserPreservationTests(unittest.TestCase):
    def test_browser_asset_is_preserved_without_calling_ollama(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "TransportationSetup/javascript/jquery.min.js"
            source.parent.mkdir(parents=True)
            original = "!function(a){a.legacy=true}(window);"
            source.write_text(original, encoding="utf-8")

            with patch(
                "services.modernizer.conversion_pipeline._caf_convert_with_llm",
                side_effect=AssertionError("browser assets must not invoke Ollama"),
            ):
                output_path, content, log_entry = _caf_convert_one_file(
                    source, root, "Demo", "spring_boot", "java",
                    {"name": "Spring Boot", "language": "java"}, {}, True,
                    "test-model", "java-system", "", {}, set(), {".js"}, {}, None,
                )

        self.assertEqual(content, original)
        self.assertTrue(output_path.endswith("/jquery.min.js"))
        self.assertEqual(log_entry["type"], "config_preserved")
        self.assertEqual(log_entry["asset_kind"], "legacy_frontend")

    def test_java_resource_is_preserved_without_invalid_migration_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "WEB-INF/web.xml"
            source.parent.mkdir(parents=True)
            original = '<?xml version="1.0" encoding="UTF-8"?>\n<web-app/>\n'
            source.write_text(original, encoding="utf-8")

            output_path, content, log_entry = _caf_convert_one_file(
                source, root, "Demo", "spring_boot", "java",
                {"name": "Spring Boot", "language": "java"}, {}, True,
                "test-model", "java-system", "", {}, {".xml"}, set(), {}, None,
            )

        self.assertEqual(content, original)
        self.assertIn("/src/main/resources/", output_path.replace("\\", "/"))
        self.assertEqual(log_entry["type"], "config_preserved")


class JavaCompilerRepairCandidateTests(unittest.TestCase):
    def test_only_genuine_java_and_generated_frontend_sources_are_repaired(self):
        fixable = {
            "ModernizedApp/src/main/java/com/example/App.java": ["cannot find symbol"],
            "ModernizedApp/frontend/src/App.tsx": ["TS2304: missing"],
            "ModernizedApp/Database/schema_postgres.sql": ["parser error"],
            "ModernizedApp/gateway/application.yml": ["invalid config"],
            "ModernizedApp/src/main/java/Legacy/javascript/jquery.min.java": ["illegal start"],
        }
        candidates, ignored = _pf_java_repair_candidates(fixable)

        self.assertEqual(
            set(candidates),
            {
                "ModernizedApp/src/main/java/com/example/App.java",
                "ModernizedApp/frontend/src/App.tsx",
            },
        )
        self.assertEqual(set(ignored), set(fixable) - set(candidates))

    def test_java_repair_inherits_async_batch_default(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def generate(_prompt, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return "public class Fixed {}"

        fixable = {
            "Demo/First.java": ["cannot find symbol"],
            "Demo/Second.java": ["cannot find symbol"],
        }
        output = {path: "public class Broken {}" for path in fixable}
        with patch("services.llm.generate", side_effect=generate), \
                patch.dict("os.environ", {"MODERNIZATION_JAVA_REPAIR_WORKERS": "2"}, clear=False):
            import os
            previous = os.environ.get("MODERNIZATION_JAVA_REPAIR_WORKERS")
            try:
                _pf_repair_build_round(
                    fixable, 1, 2, output, "", "", "test-model", "system",
                    lambda *_args: None, language="java",
                )
            finally:
                if previous is not None:
                    os.environ["MODERNIZATION_JAVA_REPAIR_WORKERS"] = previous

        self.assertEqual(max_active, 2)

    def test_frontend_repair_unwraps_governance_envelope_before_commit(self):
        path = "Demo/frontend/src/types/transaction.ts"
        output = {path: "export type Transaction = <div />;"}
        repaired = (
            "<generated_file>\n"
            "export interface Transaction { id: string; amount: number; }\n"
            "</generated_file>\n"
            "<generation_notes>\n"
            '{"applied_rule_ids":[],"not_applicable_rules":[],"conflicts_resolved":[],'
            '"assumptions":[],"manifest_ids_covered":[],"manifest_coverage":[],'
            '"self_check":{"complete":true}}\n'
            "</generation_notes>"
        )

        with patch("services.llm.generate", return_value=repaired):
            failures = _pf_repair_build_round(
                {path: ["TS17004: JSX syntax requires a .tsx filename"]},
                1, 2, output, "", "", "test-model", "system",
                lambda *_args: None, language="java",
            )

        self.assertEqual({}, failures)
        self.assertEqual(
            "export interface Transaction { id: string; amount: number; }\n",
            output[path],
        )
        self.assertNotIn("generated_file", output[path])
        self.assertNotIn("generation_notes", output[path])

    def test_invalid_frontend_repair_is_not_committed(self):
        path = "Demo/frontend/src/types/transaction.ts"
        original = "export interface Transaction { id: string; }\n"
        output = {path: original}

        with patch(
            "services.llm.generate",
            return_value="<generated_file>\nexport type Transaction = ;\n</generated_file>",
        ):
            failures = _pf_repair_build_round(
                {path: ["TS2304: Cannot find name 'Missing'"]},
                1, 2, output, "", "", "test-model", "system",
                lambda *_args: None, language="java",
            )

        self.assertIn(path, failures)
        self.assertEqual(original, output[path])

    def test_frontend_compiler_repair_promotes_runtime_exception_import(self):
        path = "Demo/frontend/src/api/UserApiClient.ts"
        output = {path: "export const broken = true;\n"}
        repaired = (
            "import type { TrainException, UserBean } from '../types/contracts';\n"
            "export function load(user: UserBean) {\n"
            "  if (!user) throw new TrainException(400, 'BAD_REQUEST', 'Missing user');\n"
            "  return user;\n"
            "}\n"
        )

        with patch("services.llm.generate", return_value=repaired):
            failures = _pf_repair_build_round(
                {path: ["TS1361: TrainException cannot be used as a value"]},
                1, 2, output, "", "", "test-model", "system",
                lambda *_args: None, language="java",
            )

        self.assertEqual({}, failures)
        self.assertIn("import { TrainException }", output[path])
        self.assertIn("import type { UserBean }", output[path])


if __name__ == "__main__":
    unittest.main()
