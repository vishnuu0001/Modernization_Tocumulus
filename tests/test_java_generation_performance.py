import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.modernizer.conversion_pipeline import (
    _dom_cache_key,
    _mp_java_domain_analysis,
    _mp_java_verified_database_tables,
)
from services.validators import ValidationResult


class JavaGenerationEvidenceTests(unittest.TestCase):
    def test_java_conversion_budget_has_slow_model_headroom(self):
        from services.modernizer._shared import (
            _CONVERSION_FILE_MAX_SECONDS,
            _JAVA_CONVERSION_FILE_MAX_SECONDS,
        )

        self.assertGreaterEqual(_JAVA_CONVERSION_FILE_MAX_SECONDS, 1200)
        self.assertGreater(_JAVA_CONVERSION_FILE_MAX_SECONDS, _CONVERSION_FILE_MAX_SECONDS)

    def test_conversion_pool_schedules_largest_sources_first(self):
        from services.modernizer import conversion_pipeline

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            small = root / "Small.java"
            medium = root / "Medium.java"
            large = root / "Large.java"
            small.write_text("class S {}", encoding="utf-8")
            medium.write_text("class M {" + "x" * 100 + "}", encoding="utf-8")
            large.write_text("class L {" + "x" * 500 + "}", encoding="utf-8")

            _config, _priority, _configs, sources, _other, _hints = (
                conversion_pipeline._caf_classify_and_precompute(
                    [small, medium, large], {"language": "java"}, "java",
                )
            )

        self.assertEqual(["Large.java", "Medium.java", "Small.java"], [p.name for p in sources])

    def test_large_java_compatibility_source_is_preserved_without_discarded_inference(self):
        from services.modernizer import conversion_pipeline

        source = "package legacy;\npublic class Large {\n" + ("// evidence\n" * 1_300) + "// END-MARKER\n}\n"
        passed = ValidationResult("Large.java", "java", "compiler", True, [])

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "services.validators.validate_file", return_value=passed,
        ), patch(
            "services.modernizer.validation_orchestration._generate_validated",
        ) as generate:
            path = Path(temp_dir) / "Large.java"
            converted, result, attempts = conversion_pipeline._convert_file_with_llm(
                path, source, "java",
                {"id": "spring", "name": "Spring Boot 3", "language": " Java "},
                analysis={}, root_ns="modern", model="test-model", system="system",
                out_path="ModernizedApp/src/main/java/modern/Large.java",
            )

        self.assertEqual(source, converted)
        self.assertTrue(result.passed)
        self.assertEqual(1, attempts)
        generate.assert_not_called()

    def test_java_file_conversion_preserves_source_without_streaming_discarded_draft(self):
        from services.modernizer import conversion_pipeline

        source = "package legacy; public class Demo {}\n"
        passed = ValidationResult("Demo.java", "java", "compiler", True, [])
        streamed = []

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "services.validators.validate_file", return_value=passed,
        ), patch(
            "services.modernizer.validation_orchestration._generate_validated",
        ) as generate:
            path = Path(temp_dir) / "Demo.java"
            converted, result, attempts = conversion_pipeline._convert_file_with_llm(
                path, source, "java",
                {"id": "spring", "name": "Spring Boot 3", "language": "java"},
                analysis={}, root_ns="modern", model="test-model", system="system",
                out_path="ModernizedApp/src/main/java/modern/Demo.java",
                on_token=streamed.append,
            )

        self.assertEqual(source, converted)
        self.assertTrue(result.passed)
        self.assertEqual(1, attempts)
        self.assertEqual([], streamed)
        generate.assert_not_called()

    def test_non_java_conversion_uses_shared_single_draft_policy(self):
        from services.modernizer import conversion_pipeline
        from services.modernizer._shared import (
            _CONVERSION_FILE_MAX_SECONDS,
            _CONVERSION_MAX_ATTEMPTS,
        )

        source = "package legacy; public class Demo {}\n"
        passed = ValidationResult("Demo.cs", "csharp", "compiler", True, [])
        captured = {}
        streamed = []

        def fake_validated(_prompt, **kwargs):
            captured.update(kwargs)
            kwargs["on_token"]("public class Demo {}")
            return "namespace Modern; public class Demo {}\n", passed, 1

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            conversion_pipeline, "_read_conversion_cache", return_value=None,
        ), patch.object(
            conversion_pipeline, "_write_conversion_cache",
        ), patch(
            "services.modernizer.validation_orchestration._generate_validated",
            side_effect=fake_validated,
        ):
            path = Path(temp_dir) / "Demo.java"
            conversion_pipeline._convert_file_with_llm(
                path, source, "java",
                {"id": "dotnet", "name": ".NET 10", "language": "csharp"},
                analysis={}, root_ns="Modern", model="test-model", system="system",
                out_path="ModernizedApp/Modern/Demo.cs", on_token=streamed.append,
            )

        self.assertEqual(_CONVERSION_MAX_ATTEMPTS, captured["max_attempts"])
        self.assertEqual(_CONVERSION_FILE_MAX_SECONDS, captured["generation_max_seconds"])
        self.assertEqual(["public class Demo {}"], streamed)

    def test_large_non_java_conversion_uses_complete_source_without_truncation(self):
        from services.modernizer import conversion_pipeline
        from services.modernizer._shared import _TOKENS_XLARGE

        source = "public class Large {\n" + ("// evidence\n" * 1_300) + "// END-MARKER\n}\n"
        passed = ValidationResult("Large.cs", "csharp", "compiler", True, [])
        captured = {}

        def fake_validated(prompt, **kwargs):
            captured["prompt"] = prompt
            captured.update(kwargs)
            return "namespace Modern; public class Large {}\n", passed, 1

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            conversion_pipeline, "_read_conversion_cache", return_value=None,
        ), patch.object(
            conversion_pipeline, "_write_conversion_cache",
        ), patch(
            "services.modernizer.validation_orchestration._generate_validated",
            side_effect=fake_validated,
        ):
            conversion_pipeline._convert_file_with_llm(
                Path(temp_dir) / "Large.java", source, "java",
                {"id": "dotnet", "name": ".NET 10", "language": "csharp"},
                analysis={}, root_ns="Modern", model="test-model", system="system",
                out_path="ModernizedApp/Modern/Large.cs",
            )

        self.assertIn("// END-MARKER", captured["prompt"])
        self.assertNotIn("convert shown portion only", captured["prompt"])
        self.assertEqual(_TOKENS_XLARGE, captured["max_tokens"])

    def test_java_conversion_emits_file_and_token_progress_without_waiting_for_batch(self):
        from services.modernizer import conversion_pipeline

        progress_events = []

        def fake_convert(*args, **kwargs):
            source = args[0]
            on_token = args[-1]
            for _ in range(64):
                on_token("generated-token")
            return (
                f"ModernizedApp/{source.name}",
                "package modern; public class Demo {}\n",
                {"type": "llm_converted", "source": source.name},
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "Demo.java"
            source.write_text("package legacy; class Demo {}\n", encoding="utf-8")
            with patch("services.llm.check_status", return_value={
                "available": True, "active_model": "test-model",
            }), \
                 patch("services.llm.pick_codegen_model", return_value="test-model"), \
                 patch.object(conversion_pipeline, "_caf_convert_one_file", side_effect=fake_convert):
                conversion_pipeline._convert_all_files(
                    temp_dir, analysis={},
                    target={"id": "spring", "name": "Spring Boot 3", "language": "java"},
                    root_ns="modern", target_stack="spring",
                    on_progress=lambda phase, pct, message: progress_events.append(
                        (phase, pct, message)
                    ),
                )

        messages = [message for _phase, _pct, message in progress_events]
        self.assertTrue(any("[1/1] Starting Demo.java" in message for message in messages))
        self.assertTrue(any("tokens generated" in message for message in messages))
        self.assertTrue(any("[1/1] llm_converted" in message for message in messages))
        self.assertTrue(all(pct >= 89 for _phase, pct, _message in progress_events))

    def test_oversized_java_compatibility_source_is_preserved_without_context_truncation(self):
        from services.modernizer import conversion_pipeline

        source = "package legacy;\npublic class Huge {\n" + ("int value;\n" * 6_500) + "}\n"
        passed = ValidationResult("Huge.java", "java", "compiler", True, [])
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "services.validators.validate_file", return_value=passed,
        ), patch(
            "services.modernizer.validation_orchestration._generate_validated",
        ) as generate_validated:
            path = Path(temp_dir) / "Huge.java"
            converted, result, attempts = conversion_pipeline._convert_file_with_llm(
                path, source, "java",
                {"id": "spring", "name": "Spring Boot 3", "language": "java"},
                analysis={}, root_ns="modern", model="test-model", system="system",
                out_path="ModernizedApp/src/main/java/modern/Huge.java",
            )

        self.assertEqual(source, converted)
        self.assertTrue(result.passed)
        self.assertEqual(1, attempts)
        generate_validated.assert_not_called()

    def test_oversized_non_java_conversion_fails_instead_of_truncating_source(self):
        from services.modernizer import conversion_pipeline

        source = "public class Huge {\n" + ("int value;\n" * 6_500) + "}\n"
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            conversion_pipeline, "_read_conversion_cache", return_value=None,
        ), patch(
            "services.modernizer.validation_orchestration._generate_validated",
        ) as generate_validated:
            with self.assertRaisesRegex(RuntimeError, "refusing to truncate"):
                conversion_pipeline._convert_file_with_llm(
                    Path(temp_dir) / "Huge.java", source, "java",
                    {"id": "dotnet", "name": ".NET 10", "language": "csharp"},
                    analysis={}, root_ns="Modern", model="test-model", system="system",
                    out_path="ModernizedApp/Modern/Huge.cs",
                )

        generate_validated.assert_not_called()

    def test_spring_boolean_entity_contract_is_normalized_for_service_api(self):
        from services.modernizer.domain_generators.java import _normalize_spring_entity_contract

        content = "public class Customer {\n    private boolean isActive = true;\n}\n"
        normalized = _normalize_spring_entity_contract(content)

        self.assertIn("public Boolean getIsActive()", normalized)
        self.assertIn("public void setIsActive(Boolean active)", normalized)
        self.assertEqual(normalized.count("public Boolean getIsActive()"), 1)

    def test_java_database_evidence_ignores_javascript_and_comment_prose(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "jquery.min.js").write_text(
                "update this option; select all from the wrapper", encoding="utf-8"
            )
            (root / "Legacy.java").write_text(
                '// update PATH set this is only a comment\n'
                '@Entity class CustomerAccount {}\n'
                'String sql = "select id from CUSTOMER_ORDER where id = ?";\n',
                encoding="utf-8",
            )

            tables = _mp_java_verified_database_tables(temp_dir)

        self.assertEqual(tables, ["CUSTOMER_ACCOUNT", "CUSTOMER_ORDER"])

    def test_java_prompt_analysis_excludes_non_java_antipatterns(self):
        original = {
            "database": {"table_names": ["WRAPPER", "CUSTOMER"]},
            "antipatterns": [
                {"file": "vendor/jquery.min.js", "type": "large_file"},
                {"file": "src/Customer.java", "type": "god_class"},
            ],
        }

        filtered = _mp_java_domain_analysis(original, ["CUSTOMER"])

        self.assertEqual(filtered["database"]["table_names"], ["CUSTOMER"])
        self.assertEqual(filtered["antipatterns"], [original["antipatterns"][1]])
        self.assertEqual(original["database"]["table_names"], ["WRAPPER", "CUSTOMER"])

    def test_java_domain_cache_changes_when_source_changes_at_same_loc(self):
        target = {"id": "spring", "language": "java"}
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "Sample.java"
            analysis = {
                "folder_path": temp_dir,
                "metrics": {"total_loc": 1},
                "antipatterns": [],
            }
            source.write_text("class First {}", encoding="utf-8")
            first = _dom_cache_key("Core", target, "sample", [], analysis)
            source.write_text("class Other {}", encoding="utf-8")
            second = _dom_cache_key("Core", target, "sample", [], analysis)

        self.assertNotEqual(first, second)

    def test_java_domain_cache_does_not_scan_checkout_when_root_is_empty(self):
        from services.modernizer import conversion_pipeline

        analysis = {"folder_path": "", "metrics": {}, "antipatterns": []}
        target = {"id": "spring", "language": "java"}

        with patch.object(conversion_pipeline.os, "walk") as walk:
            key = _dom_cache_key("Core", target, "sample", [], analysis)

        walk.assert_not_called()
        self.assertEqual(64, len(key))

    def test_java_domain_cache_uses_pruned_metadata_snapshot(self):
        from services.modernizer import conversion_pipeline

        target = {"id": "spring", "language": "java"}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "src" / "Main.java"
            excluded = root / "node_modules" / "Noise.java"
            source.parent.mkdir()
            excluded.parent.mkdir()
            source.write_text("class Main {}", encoding="utf-8")
            excluded.write_text("class Noise {}", encoding="utf-8")
            analysis = {"folder_path": temp_dir, "metrics": {}, "antipatterns": []}

            conversion_pipeline._java_source_metadata_fingerprint.cache_clear()
            first = _dom_cache_key("Core", target, "sample", [], analysis)
            before = conversion_pipeline._java_source_metadata_fingerprint.cache_info()
            repeated = _dom_cache_key("Core", target, "sample", [], analysis)
            after = conversion_pipeline._java_source_metadata_fingerprint.cache_info()
            excluded.write_text("class NoiseChanged { int value; }", encoding="utf-8")
            excluded_change = _dom_cache_key("Core", target, "sample", [], analysis)
            source.write_text("class MainChanged { int value; }", encoding="utf-8")
            source_change = _dom_cache_key("Core", target, "sample", [], analysis)

        self.assertEqual(first, repeated)
        self.assertEqual(first, excluded_change)
        self.assertNotEqual(first, source_change)
        self.assertGreater(after.hits, before.hits)

    def test_domain_cache_changes_when_generation_model_changes(self):
        target = {"id": "spring", "language": "java"}
        analysis = {"metrics": {}, "antipatterns": []}

        qwen_key = _dom_cache_key(
            "Core", target, "sample", [], analysis, model="qwen3.5:9b",
        )
        deepseek_key = _dom_cache_key(
            "Core", target, "sample", [], analysis, model="deepseek-coder:6.7b",
        )

        self.assertNotEqual(qwen_key, deepseek_key)


class JavaDuplicateInferenceTests(unittest.TestCase):
    def test_java_domain_does_not_regenerate_already_validated_sources(self):
        from services.modernizer.domain_generators import dispatch

        def backend(_lang, files, *_args, **_kwargs):
            path = "ModernizedApp/services/core-service/src/main/java/Core.java"
            files[path] = "class Core {}"
            return {path}

        def frontend(_lang, _target, files, *_args, **_kwargs):
            path = "ModernizedApp/src/components/Core/CorePage.tsx"
            files[path] = "export const CorePage = () => null;"
            return {path}

        target = {
            "id": "spring",
            "name": "Spring Boot + React",
            "language": "java",
            "backend_tech": "Spring Boot 3",
            "frontend_tech": "React",
        }
        analysis = {
            "folder_path": "",
            "architecture": {},
            "metrics": {},
            "antipatterns": [],
        }
        with patch(
            "services.modernizer.conversion_pipeline._load_dom_cache", return_value=None
        ), patch(
            "services.modernizer.conversion_pipeline._save_dom_cache"
        ), patch.object(
            dispatch, "_dispatch_backend_generation", side_effect=backend
        ), patch.object(
            dispatch, "_maybe_generate_frontend", side_effect=frontend
        ), patch.object(
            dispatch, "_ollama_generate_all_sources"
        ) as blanket_generation:
            files = dispatch._llm_gen_domain(
                "Core", target, analysis, "sample", [], model="test-model"
            )

        blanket_generation.assert_not_called()
        provenance = json.loads(
            files["ModernizedApp/.strat-aqorynth/ollama-core-provenance.json"]
        )
        self.assertEqual(len(provenance["source_files"]), 2)
        self.assertEqual(provenance["generator"], "ollama")

    def test_invalid_java_domain_artifact_fails_instead_of_emitting_placeholder(self):
        from services.modernizer.domain_generators.java import _llm_domain_java

        failed = ValidationResult(
            "Controller.java", "java", "compiler", False, ["missing class terminator"]
        )
        with patch(
            "services.modernizer.validation_orchestration._generate_validated",
            return_value=("class Broken {", failed, 2),
        ):
            with self.assertRaisesRegex(RuntimeError, "controller generation failed"):
                _llm_domain_java(
                    files={}, domain="Core", root_ns="sample", domain_tables=[],
                    antipatterns=[], context="", prod_rules="", source_sec="",
                    guide_sec="", model="test-model", system="system", tables=[],
                    target={"backend_tech": "Spring Boot", "db_target": "postgres"},
                    on_step=None, generate=lambda *_args, **_kwargs: "",
                )

    def test_failed_java_conversion_is_not_cached(self):
        from services.modernizer import conversion_pipeline

        failed = ValidationResult(
            "Sample.java", "java", "compiler", False, ["syntax error"]
        )
        target = {
            "id": "spring",
            "name": "Spring Boot",
            "language": "java",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "Sample.java"
            source.write_text("class Sample {}", encoding="utf-8")
            with patch.object(
                conversion_pipeline, "_read_conversion_cache", return_value=None
            ), patch.object(
                conversion_pipeline, "_write_conversion_cache"
            ) as cache_write, patch(
                "services.modernizer.validation_orchestration._generate_validated",
                return_value=("class Broken {", failed, 2),
            ):
                conversion_pipeline._convert_file_with_llm(
                    source, source.read_text(encoding="utf-8"), "java", target,
                    analysis={}, root_ns="sample", model="test-model", system="system",
                    out_path="ModernizedApp/src/main/java/sample/Sample.java",
                )

        cache_write.assert_not_called()

    def test_preserved_java_policy_findings_defer_to_project_reconciliation(self):
        from services.modernizer import conversion_pipeline

        source = (
            "package com.shashi.service.impl;\n"
            "import org.springframework.stereotype.Service;\n"
            "@Service public class UserServiceImpl {\n"
            "  void run() { try { work(); } catch (Exception error) { throw error; } }\n"
            "  void work() {}\n"
            "}\n"
        )
        failed = ValidationResult(
            "ModernizedApp/src/main/java/com/shashi/service/impl/UserServiceImpl.java",
            "java", "compiler", False,
            ["Broad Exception/Throwable catches are forbidden in Spring web code; use typed exceptions and centralized handling"],
        )
        on_validation = Mock()
        with patch.object(
            conversion_pipeline, "_convert_file_with_llm",
            return_value=(source, failed, 1),
        ):
            path, converted, log = conversion_pipeline._caf_convert_with_llm(
                Path("UserServiceImpl.java"), source, "java",
                {"name": "Spring Boot 3", "language": " Java "}, {}, "shashi",
                "model", "system", "", 
                "ModernizedApp/src/main/java/com/shashi/service/impl/UserServiceImpl.java",
                [], {}, " JAVA ", "src/UserServiceImpl.java", on_validation,
            )

        self.assertEqual(source, converted)
        self.assertTrue(log["validation_deferred"])
        self.assertEqual("java_project_reconciliation_and_maven", log["deferred_to"])
        self.assertIn("Broad Exception", log["diagnostics"][0])
        on_validation.assert_not_called()

    def test_failed_non_preserved_conversion_still_fails_immediately(self):
        from services.modernizer import conversion_pipeline

        failed = ValidationResult(
            "ModernizedApp/Modern/UserServiceImpl.cs", "csharp", "compiler", False,
            ["CS1513: } expected"],
        )
        with patch.object(
            conversion_pipeline, "_convert_file_with_llm",
            return_value=("namespace Modern {", failed, 2),
        ):
            with self.assertRaisesRegex(RuntimeError, "strict source validation failed"):
                conversion_pipeline._caf_convert_with_llm(
                    Path("UserServiceImpl.java"), "class UserServiceImpl {}", "java",
                    {"name": ".NET 10", "language": "csharp"}, {}, "Modern",
                    "model", "system", "", "ModernizedApp/Modern/UserServiceImpl.cs",
                    [], {}, "csharp", "src/UserServiceImpl.java", None,
                )

    def test_repository_boolean_predicate_materializes_entity_field_and_setter(self):
        from services.modernizer.build_artifacts import _reconcile_java_entity_mutators

        module = "ModernizedApp/backend/auth-service"
        entity_path = f"{module}/src/main/java/com/example/Auth.java"
        output = {
            entity_path: (
                "package com.example; import jakarta.persistence.*; "
                "@Entity public class Auth { @Id private Long id; }"
            ),
            f"{module}/src/main/java/com/example/AuthRepository.java": (
                "package com.example; interface AuthRepository { "
                "java.util.List<Auth> findByIsActiveTrue(); }"
            ),
            f"{module}/src/main/java/com/example/AuthService.java": (
                "package com.example; class AuthService { void disable(Auth entity) { "
                "entity.setIsActive(false); } }"
            ),
        }

        _reconcile_java_entity_mutators(output)

        self.assertIn("private Boolean isActive = true;", output[entity_path])
        self.assertIn("void setIsActive(Boolean value)", output[entity_path])

    def test_spring_data_auditing_contract_adds_listener_bootstrap_and_imports(self):
        from services.modernizer.build_artifacts import (
            _add_known_java_imports,
            _reconcile_java_auditing_contracts,
        )

        module = "ModernizedApp/backend/orders"
        entity_path = f"{module}/src/main/java/com/example/Order.java"
        app_path = f"{module}/src/main/java/com/example/OrdersApplication.java"
        output = {
            entity_path: (
                "package com.example;\nimport jakarta.persistence.*;\n"
                "@Entity\nclass Order { @CreatedDate java.time.Instant createdAt; "
                "@LastModifiedDate java.time.Instant updatedAt; }\n"
            ),
            app_path: (
                "package com.example;\n@SpringBootApplication\n"
                "class OrdersApplication {}\n"
            ),
        }

        _reconcile_java_auditing_contracts(output)
        output = {path: _add_known_java_imports(content) for path, content in output.items()}

        self.assertIn("@EntityListeners(AuditingEntityListener.class)", output[entity_path])
        self.assertIn("import org.springframework.data.annotation.CreatedDate;", output[entity_path])
        self.assertIn("import org.springframework.data.annotation.LastModifiedDate;", output[entity_path])
        self.assertIn("import org.springframework.data.jpa.domain.support.AuditingEntityListener;", output[entity_path])
        self.assertIn("@EnableJpaAuditing", output[app_path])
        self.assertIn("import org.springframework.data.jpa.repository.config.EnableJpaAuditing;", output[app_path])


if __name__ == "__main__":
    unittest.main()
