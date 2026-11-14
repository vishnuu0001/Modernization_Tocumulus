import json
import unittest
from unittest.mock import patch

from services.modernizer.prompt_pipeline import (
    _npm_dependency_declaration_diagnostics,
    _path_format_examples,
    _parse_file_list_lines,
    _pf_enforce_governed_generation_files,
    _pf_expand_generated_source_closure,
    _pf_freeze_complete_file_plan,
    _pf_harden_framework_closure,
    _pf_generate_infra_scaffold,
    _pf_bind_sql_target_before_generation,
    _pf_infer_sql_dialect_from_output,
    _pf_effective_single_file_prompt,
    _pf_is_single_sql_artifact_project,
    _normalize_generation_output_mode,
    _pf_register_money_transfer_accountability,
    _pf_reconcile_governed_manifest,
    _pf_repair_csharp_initializer_assignments,
    _pf_repair_typescript_parameter_order,
    _pf_finalize_file_list,
    _pf_compute_plan_max_tokens,
    _pf_dotnet_solution_from_plan,
    _pf_deterministic_planning_fallback,
    _pf_plan_file_bounds,
    _pf_run_plan_generation,
    _pf_validate_manifest_for_duplicates,
    _pf_validate_final_output,
    _required_prompt_baseline,
    _requires_java_maven_multi_module,
)
from services.modernizer.scaffolds.money_transfer_demo import (
    _money_transfer_frontend_files,
    _money_transfer_schema_sql,
)
from services.modernizer.build_artifacts import _frontend_scaffold_files
from services.modernizer.bicep_scaffold import azure_bicep_scaffold, requested_bicep
from services.modernizer.target_config import TARGET_STACKS, resolve_sql_dialect_hint
from services.modernizer.validation_orchestration import (
    _audit_generated_project,
    _reconcile_csharp_duplicate_types,
)
from services.validators import validate_file


class GenerationPlannerResilienceTests(unittest.TestCase):
    def test_github_actions_is_required_for_every_requested_stack(self):
        target = {
            "language": "csharp",
            "backend_tech": ".NET 10 Web API",
            "frontend_tech": "Angular 20",
        }
        baseline = _required_prompt_baseline(
            target,
            "Bank",
            {"backend": ".NET", "frontend": "Angular"},
            "Build with Angular reactive forms and a GitHub Actions CI/CD workflow.",
        )
        self.assertIn(".github/workflows/ci.yml", baseline)

    def test_csharp_initializer_repair_uses_compiler_lines_and_preserves_named_arguments(self):
        path = "Demo/tests/TransactionServiceTests.cs"
        output = {
            path: (
                "namespace Demo.Tests;\n"
                "public record Outcome(decimal Amount);\n"
                "public class Tests\n"
                "{\n"
                "    public object Build() => new Outcome(Amount: 1m);\n"
                "    public object Broken() => new Model\n"
                "    {\n"
                "        Amount: 1m,\n"
                "        Balance: 2m\n"
                "    };\n"
                "}\n"
                "public class Model { public decimal Amount { get; set; } public decimal Balance { get; set; } }\n"
            )
        }

        repaired = _pf_repair_csharp_initializer_assignments(output)

        self.assertEqual({path}, repaired)
        self.assertIn("Outcome(Amount: 1m)", output[path])
        self.assertIn("Amount = 1m", output[path])
        self.assertIn("Balance = 2m", output[path])
        self.assertTrue(validate_file(path, output[path], "csharp").passed)

    def test_typescript_parameter_order_repair_preserves_positions_and_type_domain(self):
        path = "Demo/frontend/src/client.ts"
        output = {
            path: (
                "export function request(\n"
                "  formatter?: (value: string) => string,\n"
                "  retries: number,\n"
                "  headers?: Record<string, string>,\n"
                "  timeoutMs: number\n"
                "): void {}\n"
            )
        }
        self.assertIn("TS1016", "\n".join(validate_file(path, output[path], "typescript").diagnostics))

        repaired = _pf_repair_typescript_parameter_order(
            output, {path: ["TS1016: A required parameter cannot follow an optional parameter."]},
        )

        self.assertEqual({path}, repaired)
        self.assertIn("formatter: ((value: string) => string) | undefined", output[path])
        self.assertIn("headers: (Record<string, string>) | undefined", output[path])
        self.assertNotIn("TS1016", "\n".join(validate_file(path, output[path], "typescript").diagnostics))

    def test_csharp_duplicate_reconciliation_keeps_namespace_aligned_implementation(self):
        project = "CreateAFullStackSolutionForABank"
        canonical = f"{project}/backend/Repositories/TransactionRepository.cs"
        redundant = f"{project}/backend/Backend/DataAccess/TransactionRepository.cs"
        source = (
            "namespace CreateAFullStackSolutionForABank.Repositories;\n"
            "public class TransactionRepository : ITransactionRepository {}\n"
        )
        output = {canonical: source, redundant: source}

        reconciled = _reconcile_csharp_duplicate_types(output)

        self.assertEqual({redundant: canonical}, reconciled)
        self.assertIn("class TransactionRepository", output[canonical])
        self.assertNotIn("class TransactionRepository", output[redundant])
        self.assertEqual(
            [],
            _audit_generated_project(
                output,
                project,
                [
                    "backend/Repositories/TransactionRepository.cs",
                    "backend/Backend/DataAccess/TransactionRepository.cs",
                ],
            ),
        )

    def test_csharp_duplicate_reconciliation_preserves_partial_and_separate_namespaces(self):
        output = {
            "Demo/One.cs": "namespace Alpha;\npublic partial class Shared {}\n",
            "Demo/Two.cs": "namespace Alpha;\npublic partial class Shared {}\n",
            "Demo/Three.cs": "namespace Beta;\npublic class Shared {}\n",
        }

        self.assertEqual({}, _reconcile_csharp_duplicate_types(output))
        self.assertTrue(all("class Shared" in content for content in output.values()))

    def test_csharp_reconciliation_suppresses_nested_entrypoint_without_own_project(self):
        root_program = "Demo/backend/Program.cs"
        nested_program = "Demo/backend/Backend/Program.cs"
        output = {
            "Demo/backend/Demo.csproj": '<Project Sdk="Microsoft.NET.Sdk.Web" />\n',
            root_program: "var builder = WebApplication.CreateBuilder(args);\n",
            nested_program: "namespace Demo.Backend;\npublic class Program {}\n",
        }

        reconciled = _reconcile_csharp_duplicate_types(output)

        self.assertEqual(root_program, reconciled[nested_program])
        self.assertIn("WebApplication.CreateBuilder", output[root_program])
        self.assertNotIn("class Program", output[nested_program])

    def test_java_multi_module_request_gets_reactor_scale_without_monolith_baseline(self):
        prompt = (
            "Java 21 Spring Boot Maven multi-module build with separate Maven modules; "
            "order-service and inventory-service are independently deployable"
        )
        signals = {"backend": "Spring Boot", "frontend": "React"}
        target = {"language": "java"}

        self.assertTrue(_requires_java_maven_multi_module(prompt, "java"))
        self.assertEqual((60, 110), _pf_plan_file_bounds(True, 4, True))
        self.assertEqual([], _required_prompt_baseline(target, "Demo", signals, prompt))
        examples = _path_format_examples("java", True, "React", True)
        self.assertIn("backend/order-service/src/main/java", examples)

    def test_java_banking_baseline_covers_reported_contract_capabilities(self):
        prompt = (
            "Java 21 Spring Boot banking money transfers with REST API, OAuth2 JWT, "
            "Jakarta Bean Validation, Flyway, safe retry handling, structured JSON logging, "
            "JUnit unit tests and Spring Boot integration tests"
        )
        baseline = _required_prompt_baseline(
            {"language": "java", "backend_tech": "Java 21 Spring Boot 3"},
            "Bank",
            {"backend": "Spring Boot", "frontend": "Angular"},
            prompt,
        )

        self.assertIn("src/main/java/com/modernize/banking/api/TransferController.java", baseline)
        self.assertIn("src/main/java/com/modernize/banking/api/TransferRequest.java", baseline)
        self.assertIn("src/main/java/com/modernize/banking/config/SecurityConfig.java", baseline)
        self.assertIn("src/main/java/com/modernize/banking/config/RetryConfig.java", baseline)
        self.assertIn("src/main/resources/db/migration/V1__create_banking_schema.sql", baseline)
        self.assertIn("src/main/resources/logback-spring.xml", baseline)
        self.assertIn("src/test/java/com/modernize/banking/service/TransferServiceTest.java", baseline)
        self.assertIn("src/test/java/com/modernize/banking/integration/TransferIntegrationTest.java", baseline)

    def test_java_money_transfer_does_not_receive_csharp_canonical_pack(self):
        project = "Bank"
        output = {
            f"{project}/backend/src/main/java/com/modernize/banking/BankingApplication.java": (
                "package com.modernize.banking; public class BankingApplication {}\n"
            )
        }

        protected = _pf_enforce_governed_generation_files(
            output, project, True, "postgres", "java",
        )
        reconciled = _pf_reconcile_governed_manifest(
            ["backend/src/main/java/com/modernize/banking/BankingApplication.java"],
            output,
            project,
            True,
            "java",
        )

        self.assertEqual(set(), protected)
        self.assertFalse(any(path.casefold().endswith(".cs") for path in output))
        self.assertEqual(
            ["backend/src/main/java/com/modernize/banking/BankingApplication.java"],
            reconciled,
        )

    def test_structured_logback_json_pattern_satisfies_java_contract_coverage(self):
        output = {
            "Bank/backend/src/main/resources/logback-spring.xml": (
                '<configuration><file>logs/application.json.log</file>'
                '<pattern>{"timestamp":"%d","level":"%level"}</pattern></configuration>'
            )
        }
        from services.modernizer.prompt_pipeline import _requirement_coverage_diagnostics

        diagnostics = _requirement_coverage_diagnostics(
            output, "Java structured JSON logging", "java",
        )

        self.assertNotIn("Structured JSON logging configuration is required", diagnostics)

    def test_java_multi_module_finalization_does_not_flatten_service_roots(self):
        prompt = "Java Spring Boot Maven multi-module build; each service is independently deployable"
        source = "backend/order-service/src/main/java/com/acme/order/OrderService.java"
        result = _pf_finalize_file_list(
            [source], {"language": "java", "frontend_tech": "React"}, "Demo",
            True, 110, None, True, True, "java", {}, (),
            {"backend": "Spring Boot", "frontend": "React"}, prompt, True,
        )

        self.assertIn(source, result)
        self.assertIn(
            "backend/order-service/src/test/java/com/acme/order/OrderServiceTest.java",
            result,
        )
        self.assertNotIn(
            "backend/src/main/java/com/acme/order/OrderService.java",
            result,
        )

    def test_generated_source_closure_adds_local_contracts_but_not_foreign_entities(self):
        project = "Demo"
        auth = f"{project}/backend/auth-service"
        order = f"{project}/backend/order-service"
        product = f"{project}/backend/product-service"
        notification = f"{project}/backend/notification-service"
        output = {
            f"{auth}/src/main/java/com/app/auth/service/AuthService.java": (
                "package com.app.auth.service; public class AuthService {}"
            ),
            f"{auth}/src/main/java/com/app/auth/controller/AuthController.java": (
                "package com.app.auth.controller;\n"
                "import com.app.auth.dto.LoginRequest;\n"
                "class AuthController { AuthService service; LoginRequest request; }"
            ),
            f"{order}/src/main/java/com/app/order/entity/Order.java": (
                "package com.app.order.entity; public class Order {}"
            ),
            f"{notification}/src/main/java/com/app/notification/NotificationApplication.java": (
                "package com.app.notification; public class NotificationApplication {}"
            ),
            f"{product}/src/main/java/com/app/product/ProductService.java": (
                "package com.app.product;\nimport com.app.order.entity.Order;\n"
                "class ProductService { Order forbidden; ProductDto dto; }"
            ),
            f"{order}/src/main/java/com/app/order/service/OrderService.java": (
                "package com.app.order.service;\n"
                "import com.app.notification.event.InventoryUpdatedEvent;\n"
                "class OrderService { InventoryUpdatedEvent event; }"
            ),
            f"{project}/frontend/src/App.tsx": (
                "import AppRoutes from './routes/AppRoutes'; export default AppRoutes;"
            ),
        }

        added = _pf_expand_generated_source_closure(output, project)

        self.assertIn(
            f"{auth}/src/main/java/com/app/auth/dto/LoginRequest.java", added,
        )
        self.assertIn(
            f"{product}/src/main/java/com/app/product/dto/ProductDto.java", added,
        )
        self.assertNotIn(
            f"{product}/src/main/java/com/app/order/entity/Order.java", output,
        )
        local_event = (
            f"{order}/src/main/java/com/app/order/event/InventoryUpdatedEvent.java"
        )
        self.assertIn(local_event, added)
        self.assertIn(
            "import com.app.order.event.InventoryUpdatedEvent;",
            output[f"{order}/src/main/java/com/app/order/service/OrderService.java"],
        )
        self.assertIn(f"{project}/frontend/src/routes/AppRoutes.tsx", added)

    def test_money_transfer_schema_uses_detectable_sql_server_dialect(self):
        result = validate_file(
            "database/schema.sql", _money_transfer_schema_sql(), "sql", dialect_hint="postgres",
        )
        self.assertTrue(result.passed, result.diagnostics)

    def test_governance_replaces_mixed_sql_with_valid_sql_server_scripts(self):
        project = "CreateAFullStackSolutionForABank"
        output = {
            f"{project}/database/schema.sql": (
                "CREATE TABLE Accounts "
                "(Id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY);"
            ),
            f"{project}/backend/migrations/CreateTables.sql": "SELECT 1;",
        }

        protected = _pf_enforce_governed_generation_files(output, project, True, "mssql")

        for rel_path in (
            f"{project}/database/schema.sql",
            f"{project}/backend/migrations/CreateTables.sql",
        ):
            with self.subTest(path=rel_path):
                self.assertIn(rel_path, protected)
                result = validate_file(
                    rel_path, output[rel_path], "sql", dialect_hint="mssql",
                )
                self.assertTrue(result.passed, result.diagnostics)

    def test_machine_readable_database_target_wins_over_ambiguous_description(self):
        self.assertEqual(
            "mssql",
            resolve_sql_dialect_hint({
                "db_target": "mssql",
                "db_tech": "PostgreSQL / MS SQL (via API)",
            }),
        )

    def test_final_project_validation_forbids_implicit_ansi_sql(self):
        counts, failures = _pf_validate_final_output(
            {"Demo/database/schema.sql": "CREATE TABLE Accounts (Id INTEGER);"},
            "csharp",
            "",
            lambda *_args: None,
        )
        self.assertEqual(1, counts["failed"])
        self.assertEqual("UNCONFIGURED", failures[0]["dialect"])
        self.assertIn("generic ANSI fallback is prohibited", failures[0]["diagnostics"][0])

    def test_final_project_validation_records_normalized_sql_server_dialect(self):
        counts, failures = _pf_validate_final_output(
            {"Demo/database/schema.sql": "IF NOT EXISTS BROKEN TSQL"},
            "csharp",
            "mssql",
            lambda *_args: None,
        )
        self.assertEqual(1, counts["failed"])
        self.assertEqual("tsql", failures[0]["dialect"])
        self.assertIn("tsql parse error", failures[0]["diagnostics"][0].casefold())

    def test_azure_angular_pack_pins_ngmodule_entrypoint(self):
        files = _money_transfer_frontend_files(True)
        main = files["frontend/src/main.ts"]
        self.assertIn("platformBrowserDynamic", main)
        self.assertIn("./app/app.module", main)
        self.assertNotIn("route-config", main)

    def test_money_transfer_pack_removes_all_competing_feature_folder_variants(self):
        project = "CreateAFullStackSolutionForABank"
        competing_paths = (
            "frontend/src/app/features/transfer/transfer.component.ts",
            "frontend/src/app/features/transfers/transfer.component.ts",
            "frontend/src/app/features/money-transfer/transfer.component.ts",
        )
        output = {
            f"{project}/{path}": "export class CompetingTransferComponent {}"
            for path in competing_paths
        }

        protected = _pf_enforce_governed_generation_files(output, project, True, "mssql")

        for path in competing_paths:
            self.assertNotIn(f"{project}/{path}", output)
        canonical_path = (
            f"{project}/frontend/src/app/features/transactions/"
            "transfer-form.component.ts"
        )
        self.assertIn(canonical_path, output)
        self.assertIn(canonical_path, protected)

    def test_money_transfer_pack_removes_competing_msal_and_model_files(self):
        project = "CreateAFullStackSolutionForABank"
        competing_paths = (
            "frontend/src/app/auth/msal-auth.guard.ts",
            "frontend/src/app/auth/msal-interceptor.ts",
            "frontend/src/app/core/models/money-transfer.model.ts",
        )
        output = {
            f"{project}/{path}": "export class CompetingAuthOrModel {}"
            for path in competing_paths
        }

        _pf_enforce_governed_generation_files(output, project, True, "mssql")

        for path in competing_paths:
            self.assertNotIn(f"{project}/{path}", output)

    def test_money_transfer_manifest_drops_superseded_models_plan(self):
        project = "CreateAFullStackSolutionForABank"
        file_list = [
            "backend/Models/TransferResponseDto.cs",
            "backend/Controllers/TransactionsController.cs",
        ]
        output = {
            f"{project}/backend/Controllers/TransactionsController.cs": "public class TransactionsController {}",
        }

        reconciled = _pf_reconcile_governed_manifest(file_list, output, project, True)

        self.assertEqual(["backend/Controllers/TransactionsController.cs"], reconciled)

    @patch("services.generation_governance.register_generation_notes")
    def test_money_transfer_pack_registers_input_and_output_accountability(self, register):
        project = "CreateAFullStackSolutionForABank"
        controller = f"{project}/backend/Controllers/TransactionsController.cs"
        renderer = (
            f"{project}/frontend/src/app/features/transactions/"
            "transaction-list.component.html"
        )
        output = {controller: "controller", renderer: "template"}
        manifest = {"inputs": [{"id": "IN-001"}], "outputs": [{"id": "OUT-001"}]}
        guidelines = {"rules": []}

        _pf_register_money_transfer_accountability(
            output, project, manifest, guidelines, "csharp", True,
        )

        self.assertEqual(2, register.call_count)
        controller_notes = register.call_args_list[0].args[1]
        renderer_notes = register.call_args_list[1].args[1]
        self.assertEqual(
            {"capture", "validate", "audit"},
            set(controller_notes["manifest_coverage"][0]["actions"]),
        )
        self.assertEqual(
            {"produce", "error-state"},
            set(controller_notes["manifest_coverage"][1]["actions"]),
        )
        self.assertEqual(["render"], renderer_notes["manifest_coverage"][0]["actions"])

    def test_angular_scaffold_avoids_node_typing_resolution_conflict(self):
        files = _frontend_scaffold_files("Angular 17", "Demo", True)
        package = json.loads(files["frontend/package.json"])
        tsconfig = json.loads(files["frontend/tsconfig.json"])

        self.assertNotIn("@types/node", package["devDependencies"])
        self.assertTrue(tsconfig["compilerOptions"]["skipLibCheck"])
        self.assertEqual([], tsconfig["compilerOptions"]["types"])
        self.assertEqual("./src", tsconfig["compilerOptions"]["rootDir"])
        self.assertNotIn("baseUrl", tsconfig["compilerOptions"])
        angular = json.loads(files["frontend/angular.json"])
        build_options = angular["projects"]["demo"]["architect"]["build"]["options"]
        self.assertEqual("tsconfig.app.json", build_options["tsConfig"])

    def test_angular_scaffold_owns_accessible_global_stylesheet(self):
        files = _frontend_scaffold_files("Angular 17", "Demo", True)

        stylesheet = files["frontend/src/styles.css"]
        self.assertIn(":focus-visible", stylesheet)
        self.assertIn("prefers-reduced-motion", stylesheet)
        self.assertIn("box-sizing: border-box", stylesheet)
        self.assertNotIn("@import", stylesheet)

    def test_prebuild_hardening_repairs_legacy_angular_node_typings(self):
        output = {
            "Demo/frontend/package.json": json.dumps({
                "dependencies": {"@angular/core": "^17.0.0"},
                "devDependencies": {"@types/node": "20.11.30", "typescript": "5.2.2"},
            }),
            "Demo/frontend/tsconfig.json": json.dumps({
                "compilerOptions": {"moduleResolution": "bundler", "types": ["node"]},
            }),
        }

        _pf_harden_framework_closure(output)

        package = json.loads(output["Demo/frontend/package.json"])
        tsconfig = json.loads(output["Demo/frontend/tsconfig.json"])
        self.assertNotIn("@types/node", package["devDependencies"])
        self.assertNotIn("node", tsconfig["compilerOptions"]["types"])
        self.assertTrue(tsconfig["compilerOptions"]["skipLibCheck"])
        self.assertEqual("./src", tsconfig["compilerOptions"]["rootDir"])
        self.assertNotIn("baseUrl", tsconfig["compilerOptions"])

    def test_frontend_black_box_test_uses_sibling_frontend_package_boundary(self):
        output = {
            "Demo/frontend/package.json": json.dumps({
                "dependencies": {"@angular/core": "^17.0.0"},
                "devDependencies": {"vitest": "^2.0.0"},
            }),
            "Demo/tests/frontend/app.spec.ts": (
                "import { signal } from '@angular/core';\n"
                "import { describe } from 'vitest';\n"
            ),
        }
        self.assertEqual([], _npm_dependency_declaration_diagnostics(output))

    def test_prebuild_hardening_closes_npgsql_in_owning_dotnet_project(self):
        output = {
            "Demo/backend/Demo.csproj": (
                '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
                '  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n'
                '</Project>\n'
            ),
            "Demo/backend/Data/ConnectionFactory.cs": (
                "using Npgsql;\n"
                "public sealed class ConnectionFactory { "
                "public NpgsqlConnection Open() => new(\"Host=localhost\"); }\n"
            ),
            "Demo/worker/Worker.csproj": (
                '<Project Sdk="Microsoft.NET.Sdk.Worker">\n'
                '  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n'
                '</Project>\n'
            ),
            "Demo/worker/Worker.cs": "public sealed class Worker {}\n",
        }

        _pf_harden_framework_closure(output)
        _pf_harden_framework_closure(output)

        backend_project = output["Demo/backend/Demo.csproj"]
        self.assertEqual(1, backend_project.count('Include="Npgsql"'))
        self.assertNotIn("Npgsql", output["Demo/worker/Worker.csproj"])

    def test_prebuild_hardening_closes_use_npgsql_ef_provider(self):
        output = {
            "Demo/backend/Demo.csproj": (
                '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
                '  <PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup>\n'
                '</Project>\n'
            ),
            "Demo/backend/Program.cs": (
                "using Microsoft.EntityFrameworkCore;\n"
                "services.AddDbContext<AppDbContext>(o => o.UseNpgsql(connection));\n"
            ),
        }

        _pf_harden_framework_closure(output)

        project = output["Demo/backend/Demo.csproj"]
        self.assertIn('Include="Npgsql.EntityFrameworkCore.PostgreSQL" Version="10.0.0"', project)

    def test_accepts_json_objects_and_windows_paths(self):
        response = """```json
        {"files": [
          {"path": "backend\\\\Program.cs"},
          {"file_path": "frontend/src/app/app.component.ts"},
          ".github/workflows/ci.yml"
        ]}
        ```"""
        self.assertEqual(
            [
                "backend/Program.cs",
                "frontend/src/app/app.component.ts",
                ".github/workflows/ci.yml",
            ],
            _parse_file_list_lines(response),
        )

    def test_modular_path_canonicalizes_case_variant_project_roots(self):
        from services.modernizer.prompt_pipeline import _ensure_modular_path

        self.assertEqual(
            "frontend/src/app/core/auth/auth.service.ts",
            _ensure_modular_path(
                "Frontend/src/app/core/auth/auth.service.ts", "csharp", True, "Angular 20",
            ),
        )
        self.assertEqual(
            "backend/Controllers/TransferController.cs",
            _ensure_modular_path(
                "Backend/Controllers/TransferController.cs", "csharp", True, "Angular 20",
            ),
        )

    def test_finalize_filters_case_variant_frontend_files_owned_by_scaffold_pack(self):
        from services.modernizer.prompt_pipeline import _pf_finalize_file_list

        files = _pf_finalize_file_list(
            [
                "Frontend/src/app/core/auth/auth.service.ts",
                "Backend/Controllers/TransferController.cs",
            ],
            {"frontend_tech": "Angular 20"}, "Bank", True, 24, [], True, True,
            "csharp", {"Bank/frontend/package.json": "{}"},
            ("frontend/src/app/core/auth/",), {}, "bank transfer",
        )

        self.assertNotIn("frontend/src/app/core/auth/auth.service.ts", files)
        self.assertIn("backend/Controllers/TransferController.cs", files)

    def test_explicit_structure_tree_remains_authoritative_over_generic_scaffold(self):
        prompt = """```text
frontend/
  banking-angular/
    src/
```"""
        files = _pf_finalize_file_list(
            ["frontend/src/app/app.component.ts"],
            {"frontend_tech": "Angular 20"}, "Bank", True, 24, None, True, True,
            "csharp", {"Bank/frontend/package.json": "{}", "Bank/frontend/angular.json": "{}"},
            (), {}, prompt,
        )

        self.assertIn("frontend/banking-angular/package.json", files)
        self.assertIn("frontend/banking-angular/angular.json", files)

    def test_frozen_plan_includes_deterministic_scaffolds_without_governance_reports(self):
        output = {
            "Demo/README.md": "# Demo\n",
            "Demo/backend/Demo.csproj": "<Project />\n",
            "Demo/governance/requirements-manifest.json": "{}",
            "Demo/.strat-aqorynth/provenance.json": "{}",
        }
        frozen = _pf_freeze_complete_file_plan(
            ["backend/Program.cs", "infrastructure/main.bicep"], output, "Demo",
        )

        self.assertEqual(
            {"README.md", "backend/Demo.csproj", "backend/Program.cs", "infrastructure/main.bicep"},
            set(frozen),
        )

    def test_dotnet_solution_is_derived_only_from_planned_msbuild_projects(self):
        solution = _pf_dotnet_solution_from_plan([
            "src/Demo.Api/Demo.Api.csproj",
            "tests/Demo.Tests/Demo.Tests.csproj",
            "frontend/package.json",
            "Banking.sln",
        ])

        self.assertIn("src\\Demo.Api\\Demo.Api.csproj", solution)
        self.assertIn("tests\\Demo.Tests\\Demo.Tests.csproj", solution)
        self.assertNotIn("package.json", solution)
        self.assertTrue(validate_file("Banking.sln", solution, "csharp").passed)

    def test_requested_project_deliverables_are_language_neutral_baseline_files(self):
        prompt = "Provide OpenAPI, architecture diagram, runbook, and GitHub Actions."
        for language in sorted({
            str(target.get("language") or "") for target in TARGET_STACKS.values()
            if target.get("language")
        }):
            target = {
                "language": language, "backend_tech": language,
                "frontend_tech": "", "db_tech": "", "db_target": "",
            }
            with self.subTest(language=language):
                files = _required_prompt_baseline(target, "Demo", {}, prompt)
                self.assertIn("openapi.yaml", files)
                self.assertIn("docs/architecture.md", files)
                self.assertIn("docs/runbook.md", files)
                self.assertIn(".github/workflows/ci.yml", files)

    def test_sql_project_has_a_deterministic_planning_fallback(self):
        files = _required_prompt_baseline(
            {
                "language": "sql", "backend_tech": "Oracle PL/SQL",
                "frontend_tech": "", "db_tech": "Oracle Database",
            },
            "OracleDbGeneration",
            {"backend": "Oracle PL/SQL", "frontend": None},
            "Generate one complete Oracle SQL and PL/SQL script.",
        )

        self.assertEqual(["database/generated.sql"], files)

    @patch("services.llm.generate")
    def test_short_oracle_project_label_uses_full_custom_requirement_and_skips_planner(
        self, generate,
    ):
        custom = (
            "Custom stack prompt: Create one complete Oracle SQL / PL/SQL script. "
            "Deliverables (one .sql file, in this order): schema, views, and packages; "
            "Business criticality: Medium"
        )
        effective = _pf_effective_single_file_prompt("Oracle DB", custom)
        target = {
            "language": "sql", "backend_tech": "Oracle PL/SQL",
            "frontend_tech": "", "db_tech": "Oracle Database",
        }
        fallback = _pf_deterministic_planning_fallback(
            target, "OracleDb", {"backend": "Oracle PL/SQL", "frontend": None}, effective,
        )

        result = _pf_run_plan_generation(
            f"User request:\n{effective}", "define contracts", [], 800, 18,
            "model", "system", lambda *_args: None, fallback,
        )

        self.assertEqual(["database/generated.sql"], result[0])
        generate.assert_not_called()

    def test_every_native_target_has_a_deterministic_planning_fallback(self):
        for stack_id, target in TARGET_STACKS.items():
            with self.subTest(stack_id=stack_id):
                files = _pf_deterministic_planning_fallback(
                    target, "Demo", {}, "Create a production service",
                )
                self.assertTrue(files)
                self.assertEqual(len(files), len(set(files)))

    def test_go_fallback_contains_build_entrypoint_and_test(self):
        files = _pf_deterministic_planning_fallback(
            TARGET_STACKS["go_rest"], "Demo", {}, "Create a Go REST API",
        )
        self.assertIn("go.mod", files)
        self.assertIn("cmd/server/main.go", files)
        self.assertTrue(any(path.endswith("_test.go") for path in files))

    def test_explicit_one_sql_file_project_uses_single_artifact_governance(self):
        manifest = {
            "source": {
                "request_verbatim": "Oracle Project",
                "run_instructions_verbatim": (
                    "Custom stack prompt: Generate one .sql file with schema, views, and packages; "
                    "Business criticality: Medium"
                ),
            },
        }

        self.assertTrue(_pf_is_single_sql_artifact_project(
            ["database/generated.sql"], manifest, "sql",
        ))
        self.assertTrue(_pf_is_single_sql_artifact_project(
            ["database/generated.sql", "README.md"], manifest, "sql",
        ))
        self.assertFalse(_pf_is_single_sql_artifact_project(
            ["database/schema.sql", "database/views.sql"], manifest, "sql",
        ))
        self.assertFalse(_pf_is_single_sql_artifact_project(
            ["database/generated.sql", "src/helper.py"], manifest, "sql",
        ))
        self.assertFalse(_pf_is_single_sql_artifact_project(
            ["database/generated.sql"], manifest, "python",
        ))

    def test_requested_bicep_is_a_bounded_deterministic_cross_language_pack(self):
        self.assertTrue(requested_bicep("Deploy with Azure Bicep to AKS"))
        self.assertFalse(requested_bicep("Deploy with Terraform to AWS"))
        files = azure_bicep_scaffold("Enterprise Full Stack Banking Application")
        self.assertEqual(
            {
                "infrastructure/bicep/main.bicep",
                "infrastructure/bicep/modules/platform.bicep",
                "infrastructure/bicep/parameters/dev.bicepparam",
                "infrastructure/bicep/parameters/test.bicepparam",
                "infrastructure/bicep/parameters/prod.bicepparam",
            },
            set(files),
        )
        supported_languages = sorted({
            str(target.get("language") or "") for target in TARGET_STACKS.values()
            if target.get("language")
        })
        for language in supported_languages:
            for path, content in files.items():
                with self.subTest(language=language, path=path):
                    result = validate_file(path, content, language)
                    self.assertTrue(result.passed, result.diagnostics)
                    self.assertEqual("bicep", result.language)
                    self.assertEqual("parser", result.checker)

    def test_infrastructure_scaffold_records_bicep_before_the_llm_file_wave(self):
        recorded = {}
        _pf_generate_infra_scaffold(
            "python",
            {"deployment_kind": "kubernetes", "deploy": "Azure AKS"},
            "Demo",
            True,
            False,
            recorded.__setitem__,
            lambda *_args: None,
            "Azure Bicep infrastructure with development, testing, and production parameters",
        )

        self.assertIn("Demo/infrastructure/bicep/main.bicep", recorded)
        self.assertIn("Demo/infrastructure/bicep/modules/platform.bicep", recorded)
        self.assertIn("Demo/infrastructure/bicep/parameters/prod.bicepparam", recorded)

    def test_manifest_parser_rejects_member_expressions_as_file_paths(self):
        self.assertEqual(
            ["backend/Models/TransferRequest.cs"],
            _parse_file_list_lines(
                "backend/Models/TransferRequest.cs\nbackend/Models/request.Amount\n"
                "t.amount\nt.description\nt.status\n"
            ),
        )

    def test_manifest_parser_preserves_supported_polyglot_and_build_extensions(self):
        self.assertEqual(
            [
                "src/main.py", "src/main.java", "database/schema.sql",
                "build.gradle", "build.sbt", "package.cabal", "infra/main.tfvars",
            ],
            _parse_file_list_lines(
                "src/main.py\nsrc/main.java\ndatabase/schema.sql\nbuild.gradle\n"
                "build.sbt\npackage.cabal\ninfra/main.tfvars\n"
            ),
        )

    def test_generation_output_mode_rejects_ambiguous_topology(self):
        self.assertEqual("project", _normalize_generation_output_mode(" PROJECT "))
        self.assertEqual("single_file", _normalize_generation_output_mode("single-file"))
        with self.assertRaisesRegex(ValueError, "wrong validation rules"):
            _normalize_generation_output_mode("files")

    def test_generated_npgsql_provider_is_authoritative_for_sql_validation(self):
        output = {
            "Demo/backend/Demo.csproj": (
                '<Project><ItemGroup><PackageReference Include="Npgsql" '
                'Version="8.0.3" /></ItemGroup></Project>'
            ),
            "Demo/backend/Data/Connection.cs": "using Npgsql;\n",
            "Demo/database/schema.sql": "SELECT 1;\n",
        }
        self.assertEqual("postgres", _pf_infer_sql_dialect_from_output(output))

    def test_generated_java_postgres_provider_is_authoritative_for_sql_validation(self):
        output = {
            "Demo/backend/pom.xml": (
                "<dependency><groupId>org.postgresql</groupId>"
                "<artifactId>postgresql</artifactId></dependency>"
            ),
            "Demo/backend/src/main/resources/application.yml": (
                "spring:\n  datasource:\n    url: jdbc:postgresql://postgres/app\n"
            ),
            "Demo/database/schema.sql": "SELECT 1;\n",
        }
        dialect = _pf_infer_sql_dialect_from_output(output)

        self.assertEqual("postgres", dialect)
        counts, failures = _pf_validate_final_output(
            output, "java", dialect, lambda *_args: None,
        )
        sql_failures = [failure for failure in failures if failure["language"] == "sql"]
        self.assertEqual([], sql_failures)
        self.assertGreaterEqual(counts["passed"], 1)

    def test_sql_target_is_bound_from_provider_before_file_generation(self):
        target = {"db_target": "", "db_tech": "(as per specification)"}
        dialect = _pf_bind_sql_target_before_generation(
            target,
            ["backend/src/main/resources/db/migration/V1__schema.sql"],
            {"Demo/backend/pom.xml": "<groupId>org.postgresql</groupId>"},
        )

        self.assertEqual("postgres", dialect)
        self.assertEqual("postgres", target["db_target"])
        self.assertEqual("PostgreSQL", target["db_tech"])

    def test_unconfigured_sql_plan_fails_before_file_generation(self):
        with self.assertRaisesRegex(ValueError, "before source generation"):
            _pf_bind_sql_target_before_generation(
                {"db_target": "", "db_tech": "(as per specification)"},
                ["database/schema.sql"],
                {},
            )

    def test_conflicting_generated_database_providers_do_not_guess_a_dialect(self):
        output = {
            "Demo/backend/Postgres.cs": "using Npgsql;\n",
            "Demo/backend/SqlServer.cs": "using Microsoft.Data.SqlClient;\n",
        }
        self.assertEqual("", _pf_infer_sql_dialect_from_output(output))

    def test_money_transfer_manifest_discards_superseded_model_taxonomy(self):
        output = {"Demo/backend/DTOs/TransferRequestDto.cs": "public class TransferRequestDto {}"}
        reconciled = _pf_reconcile_governed_manifest(
            ["backend/Backend/Models/TransferRequest.cs"], output, "Demo", True,
        )
        self.assertEqual([], reconciled)

    def test_recovers_complete_paths_from_truncated_json(self):
        response = (
            '{"files":[{"path":"backend/Program.cs"},'
            '{"path":"backend/Controllers/TransfersController.cs"},'
        )
        self.assertEqual(
            ["backend/Program.cs", "backend/Controllers/TransfersController.cs"],
            _parse_file_list_lines(response),
        )

    def test_rejects_unsafe_and_non_file_entries(self):
        response = """
        C:\\Windows\\system.ini
        /etc/passwd
        ../outside.py
        https://example.invalid/payload.py
        backend/Controllers
        backend/Program.cs
        """
        self.assertEqual(["backend/Program.cs"], _parse_file_list_lines(response))

    @patch("services.llm.generate", return_value="")
    def test_empty_llm_plan_uses_deterministic_baseline(self, _generate):
        progress_events = []
        result = _pf_run_plan_generation(
            "prompt", "", [], 100, 10, "model", "system",
            lambda phase, pct, message: progress_events.append((phase, pct, message)),
            ["backend/Program.cs", "backend/appsettings.json"],
        )
        self.assertEqual(
            ["backend/Program.cs", "backend/appsettings.json"],
            result[0],
        )
        self.assertTrue(any("deterministic baseline" in event[2] for event in progress_events))

    def test_full_stack_planning_budget_covers_structured_contract_sections(self):
        self.assertEqual(4096, _pf_compute_plan_max_tokens(True, "contracts"))
        self.assertEqual(8192, _pf_compute_plan_max_tokens(True, "contracts", True))

    @patch("services.llm.generate")
    def test_truncated_plan_retries_once_with_expanded_bounded_budget(self, generate):
        generate.side_effect = [
            RuntimeError(
                "LLM generation failed: LLM output was truncated at the configured token limit; "
                "no partial artifact was accepted"
            ),
            "backend/Program.cs\nbackend/Controllers/HealthController.cs\n",
        ]
        progress_events = []

        result = _pf_run_plan_generation(
            "prompt", "", [], 1024, 10, "model", "system",
            lambda phase, pct, message: progress_events.append((phase, pct, message)),
            ["backend/Program.cs"],
        )

        self.assertEqual(
            ["backend/Program.cs", "backend/Controllers/HealthController.cs"], result[0],
        )
        self.assertEqual([1024, 4096], [call.kwargs["max_tokens"] for call in generate.call_args_list])
        self.assertTrue(all(call.kwargs["stop"] == ("END_OF_PLAN",) for call in generate.call_args_list))
        self.assertTrue(any("resetting the incomplete response" in event[2] for event in progress_events))

    @patch("services.llm.generate")
    def test_explicit_one_sql_file_uses_deterministic_plan_without_llm(self, generate):
        events = []

        result = _pf_run_plan_generation(
            "Deliverables (one .sql file, in this order): schema, view, and package.",
            "", [], 4096, 10, "model", "system",
            lambda *args: events.append(args), ["database/generated.sql"],
        )

        self.assertEqual(["database/generated.sql"], result[0])
        self.assertEqual(0, generate.call_count)
        self.assertTrue(any("explicitly request one SQL artifact" in event[2] for event in events))

    @patch("services.llm.generate", return_value="database/schema.sql\ndatabase/views.sql\n")
    def test_multi_file_sql_request_keeps_advisory_llm_planning(self, generate):
        result = _pf_run_plan_generation(
            "Generate separate SQL files for schema and views.",
            "", [], 4096, 10, "model", "system",
            lambda *_args: None, ["database/generated.sql"],
        )

        self.assertEqual(["database/schema.sql", "database/views.sql"], result[0])
        self.assertEqual(1, generate.call_count)

    @patch("services.llm.generate")
    def test_repeated_truncated_plan_uses_validated_baseline_instead_of_failing(self, generate):
        truncation = RuntimeError(
            "LLM generation failed: LLM output was truncated at the configured token limit; "
            "no partial artifact was accepted"
        )
        generate.side_effect = [truncation, truncation, truncation]
        progress_events = []

        result = _pf_run_plan_generation(
            "prompt", "", [], 1024, 10, "model", "system",
            lambda phase, pct, message: progress_events.append((phase, pct, message)),
            ["backend/Program.cs", "backend/appsettings.json"],
        )

        self.assertEqual(
            ["backend/Program.cs", "backend/appsettings.json"], result[0],
        )
        self.assertEqual(
            [1024, 4096, 12000],
            [call.kwargs["max_tokens"] for call in generate.call_args_list],
        )
        self.assertTrue(any("deterministic baseline" in event[2] for event in progress_events))

    @patch("services.llm.generate")
    def test_non_truncation_planning_failure_is_not_retried(self, generate):
        generate.side_effect = RuntimeError("model unavailable")

        with self.assertRaisesRegex(RuntimeError, "Generation planning failed: model unavailable"):
            _pf_run_plan_generation(
                "prompt", "", [], 1024, 10, "model", "system",
                lambda *_args: None, ["backend/Program.cs"],
            )

        self.assertEqual(1, generate.call_count)

    @patch("services.llm.generate")
    def test_planning_timeout_uses_baseline_and_every_call_has_deadline(self, generate):
        generate.side_effect = TimeoutError("generation timed out")

        result = _pf_run_plan_generation(
            "prompt", "", [], 1024, 10, "model", "system",
            lambda *_args: None, ["backend/Program.cs"],
        )

        self.assertEqual(["backend/Program.cs"], result[0])
        self.assertGreater(generate.call_args.kwargs["max_seconds"], 0)

    @patch("services.llm.generate")
    def test_wrapped_per_file_budget_timeout_uses_validated_baseline(self, generate):
        generate.side_effect = RuntimeError(
            "LLM generation failed: Ollama generation exceeded the 82s per-file budget"
        )
        events = []

        result = _pf_run_plan_generation(
            "prompt", "", [], 4096, 10, "model", "system",
            lambda *args: events.append(args), ["database/generated.sql"],
        )

        self.assertEqual(["database/generated.sql"], result[0])
        self.assertEqual(1, generate.call_count)
        self.assertTrue(any("deterministic baseline" in event[2] for event in events))

    @patch("services.llm.generate")
    def test_inference_slot_timeout_uses_validated_baseline(self, generate):
        generate.side_effect = RuntimeError(
            "LLM generation failed: timed out waiting for an Ollama inference slot"
        )

        result = _pf_run_plan_generation(
            "prompt", "", [], 4096, 10, "model", "system",
            lambda *_args: None, ["src/generated.py"],
        )

        self.assertEqual(["src/generated.py"], result[0])

    @patch("services.llm.generate", return_value="")
    def test_advisory_manifest_validation_has_bounded_deadline(self, generate):
        original = ["backend/Program.cs"]
        result = _pf_validate_manifest_for_duplicates(
            original, "shared contract", "auth", "backend", "Demo", "required",
            [], 1024, 10, "model", "system", lambda *_args: None,
        )

        self.assertEqual(original, result[0])
        self.assertGreater(generate.call_args.kwargs["max_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
