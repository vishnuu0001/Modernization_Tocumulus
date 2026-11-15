import unittest

from services.analyzer import _build_targets
from services.modernizer.conversion_pipeline import (
    _mp_bind_authoritative_database_target,
    _mp_generate_database_scripts,
    _mp_resolve_target,
)


class CustomStackResolutionTests(unittest.TestCase):
    def test_explicit_fastapi_prompt_rebases_stale_dotnet_preset(self):
        from services.modernizer.prompt_pipeline import _pf_resolve_target

        target, signals, _full_stack, language, _requirements = _pf_resolve_target(
            "create a Python Fast API to check the availability of Azure Services",
            "dotnet8_blazor",
            "",
        )

        self.assertEqual("python", language)
        self.assertEqual("FastAPI", signals["backend"])
        self.assertNotIn("Blazor", target["name"])
        self.assertNotIn("EF Core", target["db_tech"])
        self.assertEqual("postgres", target["db_target"])

    def test_prompt_default_resolves_python_fast_api_to_fastapi_stack(self):
        from api.server import _resolve_prompt_analysis_target

        prompt = "create a Python Fast API to check the availability of Azure Services"
        resolved = _resolve_prompt_analysis_target(prompt, "prompt_inferred")

        self.assertEqual("python_fastapi", resolved)
        target, signals, _guide, _dapper, _deploy = _mp_resolve_target(
            resolved, prompt, "",
        )
        self.assertEqual("python", target["language"])
        self.assertEqual("FastAPI", signals["backend"])
        self.assertIn("FastAPI", target["backend_tech"])

    def test_prompt_resolution_preserves_explicit_selected_stack(self):
        from api.server import _resolve_prompt_analysis_target

        self.assertEqual(
            "spring_boot",
            _resolve_prompt_analysis_target("Build an API", "spring_boot"),
        )

    def test_prompt_backend_overrides_conflicting_explicit_preset(self):
        from api.server import _resolve_prompt_analysis_target

        self.assertEqual(
            "python_fastapi",
            _resolve_prompt_analysis_target(
                "create a Python Fast API to check Azure Services",
                "dotnet8_blazor",
            ),
        )

    def test_prompt_can_still_explicitly_request_aveva_mes(self):
        from api.server import _resolve_prompt_analysis_target

        self.assertEqual(
            "aveva_mes",
            _resolve_prompt_analysis_target(
                "Build an AVEVA MES production application", "prompt_inferred",
            ),
        )

    def test_named_unsupported_framework_is_not_mislabeled(self):
        from api.server import _resolve_prompt_analysis_target

        self.assertEqual(
            "custom",
            _resolve_prompt_analysis_target(
                "Build a Python Flask API", "prompt_inferred",
            ),
        )

    def test_prompt_without_stack_is_honestly_custom(self):
        from api.server import _resolve_prompt_analysis_target

        self.assertEqual(
            "custom",
            _resolve_prompt_analysis_target(
                "Build an availability checker", "prompt_inferred",
            ),
        )

    def test_custom_dotnet_angular_stack_is_decomposed(self):
        target, signals, _guide, _dapper, _deploy = _mp_resolve_target(
            "custom",
            "Modernize this application to .Net 9 with Angular 20 as UI.",
            "",
        )

        self.assertEqual("csharp", target["language"])
        self.assertEqual(".NET 9", target["backend_tech"])
        self.assertEqual("Angular 20", target["frontend_tech"])
        self.assertEqual("Angular 20", signals["frontend"])

    def test_custom_stack_recognizes_sql_server_aliases(self):
        for database_name in ("SQL Server", "MS SQL", "MSSQL", "Microsoft SQL", "SQLServer"):
            with self.subTest(database_name=database_name):
                target, _signals, _guide, _dapper, _deploy = _mp_resolve_target(
                    "custom",
                    f"Modernize to .NET 9 with Angular 20 and {database_name}.",
                    "",
                )
                self.assertEqual("mssql", target["db_target"])

    def test_explicit_sql_server_target_overrides_detected_oracle_source(self):
        target, _signals, _guide, _dapper, _deploy = _mp_resolve_target(
            "custom",
            "Modernize to Java 21 Spring Boot with React and SQL Server as DB.",
            "",
        )
        analysis = {
            "architecture": {"database": "Oracle"},
            "database": {"table_names": ["TRAIN"], "raw_sql_files": 3},
        }

        self.assertEqual("mssql", _mp_bind_authoritative_database_target(target, analysis))
        self.assertEqual("mssql", target["db_target"])
        self.assertEqual("Microsoft SQL Server", target["db_tech"])
        self.assertIn(
            "ModernizedApp/Database/schema_mssql.sql",
            _mp_generate_database_scripts("mssql", ["TRAIN"], [], analysis),
        )

    def test_generic_sql_target_never_falls_back_to_detected_oracle_source(self):
        target, _signals, _guide, _dapper, _deploy = _mp_resolve_target(
            "custom",
            "Modernize to Java 21 Spring Boot with React and SQL as DB.",
            "",
        )
        analysis = {
            "architecture": {"database": "Oracle"},
            "database": {"table_names": ["TRAIN"], "raw_sql_files": 3},
        }

        with self.assertRaisesRegex(ValueError, "generic SQL"):
            _mp_bind_authoritative_database_target(target, analysis)

    def test_database_free_custom_stack_does_not_receive_mssql_schema(self):
        target = {
            "name": ".NET 9 + Angular 20", "language": "csharp",
            "db_target": "", "db_tech": "(as per specification)",
        }
        analysis = {
            "architecture": {"database": "Unknown"},
            "database": {"table_names": [], "raw_sql_files": 0, "connection_strings": []},
        }

        self.assertEqual("", _mp_bind_authoritative_database_target(target, analysis))
        self.assertEqual({}, _mp_generate_database_scripts("", [], [], analysis))

    def test_omitted_target_database_preserves_unambiguous_source_provider(self):
        target = {
            "name": "Java 21 Spring Boot", "language": "java",
            "db_target": "", "db_tech": "(as per specification)",
        }
        analysis = {
            "architecture": {"database": "Oracle"},
            "database": {"table_names": ["TRAIN"], "raw_sql_files": 3},
        }

        self.assertEqual("oracle", _mp_bind_authoritative_database_target(target, analysis))
        self.assertEqual("oracle", target["db_target"])
        self.assertEqual("Oracle Database", target["db_tech"])
        self.assertNotIn(
            "ModernizedApp/Database/schema_mssql.sql",
            _mp_generate_database_scripts("oracle", ["TRAIN"], [], analysis),
        )

    def test_ambiguous_database_evidence_fails_before_generation(self):
        target = {
            "name": "Custom", "language": "java", "db_target": "",
            "db_tech": "(as per specification)",
        }
        analysis = {
            "architecture": {"database": "Unknown"},
            "database": {"table_names": ["TRAIN"], "raw_sql_files": 2},
        }

        with self.assertRaisesRegex(ValueError, "before code generation"):
            _mp_bind_authoritative_database_target(target, analysis)

    def test_explicit_dotnet_9_overrides_legacy_preset_and_aligns_ef_core(self):
        target, _signals, _guide, _dapper, _deploy = _mp_resolve_target(
            "aveva_mes",
            "Modernize the application in .Net 9.",
            "",
        )

        self.assertEqual(".NET 9", target["backend_tech"])
        self.assertEqual("Microsoft SQL Server 2022 + EF Core 9", target["db_tech"])

    def test_analysis_architecture_honors_dotnet_9_prompt(self):
        result = _build_targets(
            {}, {}, {"database": "Unknown"}, "aveva_mes",
            "Modernize the application in .Net 9.",
        )

        self.assertEqual(".NET 9", result["backend"])
        self.assertEqual("Microsoft SQL Server 2022 + EF Core 9", result["database"])

    def test_governed_analysis_includes_saved_project_prompt(self):
        from api.server import _analysis_target_description

        description = _analysis_target_description(
            {"project_prompt": "Modernize the application in .Net 9."},
            {},
        )
        result = _build_targets(
            {}, {}, {"database": "Unknown"}, "aveva_mes", description,
        )

        self.assertEqual(".NET 9", result["backend"])

    def test_reviewed_plan_sql_server_choice_reaches_transformation_target(self):
        from api.server import _normalize_governed_target_stack, _transformation_target_description

        description = _transformation_target_description(
            "Custom stack prompt: Java 21, React, SQL as DB",
            {"custom_stack_prompt": "Java 21, React, SQL as DB"},
            {
                "target_architecture": {"database": "SQL Server"},
                "database_conversion_approach": {"target": "SQL Server"},
            },
        )
        target, _signals, _guide, _dapper, _deploy = _mp_resolve_target(
            "custom", description, "",
        )

        self.assertEqual("mssql", target["db_target"])
        self.assertFalse(target["database_target_required"])

        reviewed_plan = {
            "target_architecture": {
                "stack": "Java 21 with Spring Boot 3, React 19, and PostgreSQL 17",
                "deployment": "Kubernetes",
            },
            "target_technologies": [
                "Java 21 with Spring Boot 3", "React 19", "PostgreSQL 17",
            ],
        }
        reviewed_description = _transformation_target_description("", {}, reviewed_plan)
        self.assertIn("Java 21 with Spring Boot 3", reviewed_description)
        self.assertEqual(
            "custom",
            _normalize_governed_target_stack(
                "Java 21 with Spring Boot 3", reviewed_description,
            ),
        )

    def test_governed_target_normalizes_catalog_names_and_fails_when_empty(self):
        from fastapi import HTTPException
        from api.server import _normalize_governed_target_stack

        self.assertEqual(
            "spring_boot",
            _normalize_governed_target_stack(
                "Java 21 Spring Boot 3 + PostgreSQL", "",
            ),
        )
        with self.assertRaises(HTTPException):
            _normalize_governed_target_stack("", "")

    def test_dotnet_companion_version_alignment_covers_supported_majors(self):
        for major in (8, 9, 10):
            with self.subTest(major=major):
                result = _build_targets(
                    {}, {}, {"database": "Unknown"}, "aveva_mes",
                    f"Modernize in .NET {major}",
                )
                self.assertEqual(f".NET {major}", result["backend"])
                self.assertIn(f"EF Core {major}", result["database"])


if __name__ == "__main__":
    unittest.main()
