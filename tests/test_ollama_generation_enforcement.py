# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — tests (test_ollama_generation_enforcement.py)
# Date: 2026-02-01
# ---------------------------------------------------------------------------
import json
import unittest
from unittest.mock import patch

from services.modernizer.domain_generators.dispatch import (
    _ollama_generate_all_sources,
    _llm_gen_domain,
)
from services.validators import ValidationResult


class OllamaGenerationEnforcementTests(unittest.TestCase):
    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_token_cutoff_is_discarded_and_retried_once_with_concise_contract(self, generated):
        path = "Demo/frontend/src/components/Shashi/fetch.ts"
        generated.side_effect = [
            RuntimeError(
                "LLM output was truncated at the configured token limit; "
                "no partial artifact was accepted"
            ),
            (
                "export async function fetchApi(url: string): Promise<Response> { "
                "return fetch(url); }\n",
                ValidationResult(path, "typescript", "compiler", True, []),
                1,
            ),
        ]
        files = {
            path: (
                "Create the missing non-React TypeScript runtime module imported as ./fetch. "
                "Export exactly these consumed runtime bindings: fetchApi."
            ),
        }
        events = []

        _ollama_generate_all_sources(
            files, {"name": "React + Spring Boot", "language": "java"},
            "Shashi", "qwen-test", "system", events.append, None,
            generation_max_seconds=60,
        )

        self.assertEqual([4096, 8192], [
            call.kwargs["max_tokens"] for call in generated.call_args_list
        ])
        self.assertNotIn("TOKEN-LIMIT RECOVERY", generated.call_args_list[0].args[0])
        self.assertIn("TOKEN-LIMIT RECOVERY", generated.call_args_list[1].args[0])
        self.assertIn("Do not repeat sections", generated.call_args_list[1].args[0])
        self.assertTrue(all(call.kwargs["forbid_jsx"] for call in generated.call_args_list))
        self.assertEqual(60, generated.call_args_list[0].kwargs["generation_max_seconds"])
        self.assertLessEqual(
            generated.call_args_list[1].kwargs["generation_max_seconds"], 60,
        )
        self.assertEqual(
            "export async function fetchApi(url: string): Promise<Response> { "
            "return fetch(url); }\n",
            files[path],
        )
        self.assertTrue(any("discarding it" in event for event in events))

    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_repeated_token_cutoff_fails_closed_without_replacing_contract(self, generated):
        path = "Demo/frontend/src/components/constant/ResponseCode.ts"
        contract = (
            "Create the missing non-React TypeScript data-contract module imported as "
            "../constant/ResponseCode. Export exactly these consumed bindings: ResponseCode."
        )
        truncation = RuntimeError(
            "LLM output was truncated at the configured token limit; "
            "no partial artifact was accepted"
        )
        generated.side_effect = [truncation, truncation]
        files = {path: contract}

        with self.assertRaisesRegex(RuntimeError, "truncated at the configured token limit"):
            _ollama_generate_all_sources(
                files, {"name": "React + Spring Boot", "language": "java"},
                "Shashi", "qwen-test", "system", None, None,
                generation_max_seconds=60,
            )

        self.assertEqual(2, generated.call_count)
        self.assertEqual(contract, files[path])

    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_non_token_generation_failure_is_not_retried(self, generated):
        path = "Demo/frontend/src/components/Shashi/fetch.ts"
        generated.side_effect = RuntimeError("model unavailable")
        files = {path: "runtime contract"}

        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            _ollama_generate_all_sources(
                files, {"name": "React + Spring Boot", "language": "java"},
                "Shashi", "qwen-test", "system", None, None,
            )

        self.assertEqual(1, generated.call_count)
        self.assertEqual("runtime contract", files[path])

    # Function: test_every_executable_source_is_reauthored_by_validated_ollama_call
    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_every_executable_source_is_reauthored_by_validated_ollama_call(self, generated):
        generated.side_effect = lambda prompt, **kwargs: (
            f"OLLAMA::{kwargs['rel_path']}",
            ValidationResult(kwargs["rel_path"], kwargs["language"], "compiler", True, []),
            1,
        )
        files = {
            "Demo/src/App.kt": "deterministic Kotlin contract",
            "Demo/schema.sql": "deterministic SQL contract",
            "Demo/build.gradle.kts": "deterministic build metadata",
            "Demo/README.md": "documentation",
        }

        _ollama_generate_all_sources(
            files,
            {
                "name": "Kotlin Spring",
                "language": "kotlin",
                "db_target": "postgres",
                "db_tech": "PostgreSQL 16",
            },
            "Orders", "qwen-test", "system", None, None,
        )

        self.assertEqual("OLLAMA::Demo/src/App.kt", files["Demo/src/App.kt"])
        self.assertEqual("OLLAMA::Demo/schema.sql", files["Demo/schema.sql"])
        self.assertEqual("OLLAMA::Demo/build.gradle.kts", files["Demo/build.gradle.kts"])
        self.assertEqual("documentation", files["Demo/README.md"])
        self.assertEqual(3, generated.call_count)
        self.assertTrue(all(call.kwargs["dialect"] == "postgres" for call in generated.call_args_list))
        provenance = json.loads(files["Demo/.strat-aqorynth/ollama-orders-provenance.json"])
        self.assertEqual("ollama", provenance["generator"])
        self.assertEqual("qwen-test", provenance["model"])
        self.assertEqual(3, len(provenance["source_files"]))

    # Function: test_final_generation_pass_propagates_project_contract
    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_final_generation_pass_propagates_project_contract(self, generated):
        generated.return_value = (
            "class App {}",
            ValidationResult("Demo/App.java", "java", "compiler", True, []),
            1,
        )
        files = {"Demo/App.java": "controller file contract"}
        _ollama_generate_all_sources(
            files,
            {"name": "Spring Boot 3", "language": "java"},
            "Orders", "qwen-test", "system", None, None,
            user_request="Publish OrderCreated to Kafka",
            contracts="POST /api/orders returns OrderResponse",
            namespace_map="OrderResponse -> demo.api.OrderResponse",
            required_elements="OAuth2 JWT and Flyway",
            file_manifest="Demo/App.java\nDemo/pom.xml",
        )
        prompt = generated.call_args.args[0]
        self.assertIn("ORIGINAL USER REQUIREMENTS", prompt)
        self.assertIn("Publish OrderCreated to Kafka", prompt)
        self.assertIn("PROJECT CONTRACTS", prompt)
        self.assertIn("demo.api.OrderResponse", prompt)
        self.assertIn("OAuth2 JWT and Flyway", prompt)
        self.assertIn("Demo/pom.xml", prompt)
        self.assertIn("Implement only the responsibility assigned", prompt)
        self.assertIn("bootstrap classes must not contain controller", prompt)

    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_java_fullstack_sources_use_their_actual_frontend_language(self, generated):
        generated.side_effect = lambda prompt, **kwargs: (
            "generated",
            ValidationResult(kwargs["rel_path"], kwargs["language"], "compiler", True, []),
            1,
        )
        files = {
            "Demo/backend/src/main/java/com/app/App.java": "class App {}",
            "Demo/frontend/store/authStore.ts": "export const value = true;",
            "Demo/frontend/src/App.tsx": "export default function App() { return null; }",
        }

        _ollama_generate_all_sources(
            files, {"name": "React + Spring Boot", "language": "java"},
            "Demo", "qwen-test", "system", None, None,
        )

        languages = {
            call.kwargs["rel_path"]: call.kwargs["language"]
            for call in generated.call_args_list
        }
        self.assertEqual("java", languages["Demo/backend/src/main/java/com/app/App.java"])
        self.assertEqual("typescript", languages["Demo/frontend/store/authStore.ts"])
        self.assertEqual("typescript", languages["Demo/frontend/src/App.tsx"])

    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_sql_server_dialect_is_authoritative_during_ollama_generation(self, generated):
        generated.return_value = (
            "SELECT SYSUTCDATETIME();",
            ValidationResult("Demo/schema.sql", "sql", "compiler", True, []),
            1,
        )
        files = {"Demo/schema.sql": "CREATE TABLE Accounts (Id INT);"}

        _ollama_generate_all_sources(
            files,
            {
                "name": ".NET + SQL Server",
                "language": "csharp",
                "db_target": "mssql",
                "db_tech": "Microsoft SQL Server 2022 + EF Core 8",
            },
            "Payments", "qwen-test", "system", None, None,
        )

        self.assertEqual("tsql", generated.call_args.kwargs["dialect"])
        self.assertIn("AUTHORITATIVE SQL DIALECT: tsql", generated.call_args.args[0])

    def test_sql_generation_fails_closed_without_database_dialect(self):
        with self.assertRaisesRegex(ValueError, "authoritative relational db_target"):
            _ollama_generate_all_sources(
                {"Demo/schema.sql": "CREATE TABLE Accounts (Id INT);"},
                {"name": "Unconfigured", "language": "csharp"},
                "Payments", "qwen-test", "system", None, None,
            )

    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_failed_strict_validation_is_never_persisted_or_provenanced(self, generated):
        generated.return_value = (
            "export const broken = ;",
            ValidationResult("Demo/src/app.ts", "typescript", "compiler", False, ["TS1109"]),
            3,
        )
        files = {"Demo/src/app.ts": "validated contract"}

        with self.assertRaisesRegex(RuntimeError, "Strict source validation failed"):
            _ollama_generate_all_sources(
                files, {"name": "TypeScript", "language": "typescript"},
                "Demo", "qwen-test", "system", None, None,
            )

        self.assertEqual("validated contract", files["Demo/src/app.ts"])
        self.assertNotIn("Demo/.strat-aqorynth/ollama-demo-provenance.json", files)

    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_compiler_proven_jsx_migrates_atomically_from_ts_to_tsx(self, generated):
        source = "export const AuthView = () => <main>Authentication</main>;\n"
        generated.return_value = (
            source,
            ValidationResult(
                "Demo/frontend/src/components/AuthView.ts", "typescript", "compiler", False,
                ["TS17004: JSX syntax requires a .tsx filename; plain .ts files cannot contain JSX"],
            ),
            3,
        )
        files = {"Demo/frontend/src/components/AuthView.ts": "React component contract"}

        _ollama_generate_all_sources(
            files, {"name": "React + Spring Boot", "language": "java"},
            "Auth", "qwen-test", "system", None, None,
        )

        self.assertNotIn("Demo/frontend/src/components/AuthView.ts", files)
        self.assertEqual(source, files["Demo/frontend/src/components/AuthView.tsx"])
        provenance = json.loads(files["Demo/.strat-aqorynth/ollama-auth-provenance.json"])
        self.assertIn(
            "Demo/frontend/src/components/AuthView.tsx", provenance["source_files"],
        )

    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_data_contract_uses_compact_non_react_generation_prompt(self, generated):
        generated.return_value = (
            "export class ErrorResponseDto extends Error {}\n",
            ValidationResult("Demo/dto/Error.dto.ts", "typescript", "compiler", True, []),
            1,
        )
        contract = (
            "Create the missing non-React TypeScript data-contract module imported as "
            "./Error.dto. Export exactly these consumed bindings: ErrorResponseDto."
        )
        files = {"Demo/dto/Error.dto.ts": contract}

        _ollama_generate_all_sources(
            files, {"name": "React + Spring Boot", "language": "java"},
            "Auth", "qwen-test", "system", None, None,
            user_request="Render a large React authentication page with JSX",
            file_manifest="many unrelated project files",
        )

        prompt = generated.call_args.args[0]
        self.assertIn("non-React TypeScript data-contract file", prompt)
        self.assertIn("ErrorResponseDto", prompt)
        self.assertNotIn("ORIGINAL USER REQUIREMENTS", prompt)
        self.assertNotIn("large React authentication page", prompt)

    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_api_closure_uses_compact_non_react_prompt_and_forbids_jsx(self, generated):
        generated.return_value = (
            "export async function login(): Promise<string> { return 'token'; }\n",
            ValidationResult("Demo/frontend/src/api/auth.api.ts", "typescript", "compiler", True, []),
            1,
        )
        contract = (
            "Create the missing non-React TypeScript runtime module imported as ./auth.api "
            "by Demo/frontend/src/AuthPage.tsx. Export exactly these consumed runtime bindings: "
            "login. Implement a concise fetch-based API client."
        )
        files = {"Demo/frontend/src/api/auth.api.ts": contract}

        _ollama_generate_all_sources(
            files, {"name": "React + Spring Boot", "language": "java"},
            "Auth", "qwen-test", "system", None, None,
            user_request="Render a large React authentication page with JSX",
            file_manifest="many unrelated project files",
        )

        prompt = generated.call_args.args[0]
        self.assertIn("non-React plain TypeScript runtime/transport module", prompt)
        self.assertIn("auth.api", prompt)
        self.assertIn("prefer under 180 lines", prompt)
        self.assertIn("null stub returns are forbidden", prompt)
        self.assertNotIn("ORIGINAL USER REQUIREMENTS", prompt)
        self.assertNotIn("large React authentication page", prompt)
        self.assertTrue(generated.call_args.kwargs["forbid_jsx"])

    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_api_closure_never_masks_jsx_failure_by_migrating_to_tsx(self, generated):
        contract = (
            "Create the missing non-React TypeScript runtime module imported as ./auth.api "
            "by Demo/frontend/src/AuthPage.tsx. Export exactly these consumed runtime bindings: login."
        )
        generated.return_value = (
            "export const login = () => <main>Login</main>;\n",
            ValidationResult(
                "Demo/frontend/src/api/auth.api.ts", "typescript", "compiler", False,
                ["TS17004: JSX syntax requires a .tsx filename; plain .ts files cannot contain JSX"],
            ),
            3,
        )
        files = {"Demo/frontend/src/api/auth.api.ts": contract}

        with self.assertRaisesRegex(RuntimeError, "TS17004"):
            _ollama_generate_all_sources(
                files, {"name": "React + Spring Boot", "language": "java"},
                "Auth", "qwen-test", "system", None, None,
            )

        self.assertEqual(contract, files["Demo/frontend/src/api/auth.api.ts"])
        self.assertNotIn("Demo/frontend/src/api/auth.api.tsx", files)

    @patch("services.modernizer.validation_orchestration._generate_validated")
    def test_data_contract_never_masks_jsx_failure_by_migrating_to_tsx(self, generated):
        contract = (
            "Create the missing non-React TypeScript data-contract module imported as "
            "./Error.dto. Export exactly these consumed bindings: ErrorResponseDto."
        )
        generated.return_value = (
            "export const ErrorResponseDto = () => <main>Error</main>;\n",
            ValidationResult(
                "Demo/frontend/src/dto/Error.dto.ts", "typescript", "compiler", False,
                ["TS17004: JSX syntax requires a .tsx filename; plain .ts files cannot contain JSX"],
            ),
            3,
        )
        files = {"Demo/frontend/src/dto/Error.dto.ts": contract}

        with self.assertRaisesRegex(RuntimeError, "TS17004"):
            _ollama_generate_all_sources(
                files, {"name": "React + Spring Boot", "language": "java"},
                "Auth", "qwen-test", "system", None, None,
            )

        self.assertEqual(contract, files["Demo/frontend/src/dto/Error.dto.ts"])
        self.assertNotIn("Demo/frontend/src/dto/Error.dto.tsx", files)

    # Function: test_domain_generation_fails_closed_when_ollama_is_unavailable
    @patch("services.llm.check_status", return_value={"available": False})
    def test_domain_generation_fails_closed_when_ollama_is_unavailable(self, _status):
        with self.assertRaisesRegex(RuntimeError, "Ollama code generation is required"):
            _llm_gen_domain(
                "Orders",
                {
                    "name": "Kotlin Spring",
                    "language": "kotlin",
                    "backend_tech": "Spring Boot",
                    "frontend_tech": "REST API",
                    "db_tech": "PostgreSQL",
                    "db_target": "postgres",
                },
                {},
                "Demo",
                [],
            )


if __name__ == "__main__":
    unittest.main()
