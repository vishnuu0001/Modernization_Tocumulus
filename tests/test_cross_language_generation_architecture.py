import json
from pathlib import Path
import tempfile
import unittest

from services.modernizer.build_artifacts import (
    _backend_manifest_files,
    _frontend_scaffold_files,
)
from services.modernizer.prompt_pipeline import (
    _default_file_list,
    _npm_dependency_declaration_diagnostics,
    _pf_generate_infra_scaffold,
    _pf_generate_project_files_template,
    _pf_resolve_target,
    _requirement_coverage_diagnostics,
)
from services.modernizer.conversion_pipeline import _mp_generate_build_files
from services.modernizer.scaffolds.polyglot import generate_polyglot_project
from services.modernizer.scaffolds.typescript import _gen_ts_component
from services.modernizer.target_config import TARGET_STACKS, _stack_profiles_for
from services.build_runner import run_build
from services.validators import validate_file


class CrossLanguageGenerationArchitectureTests(unittest.TestCase):
    def test_vue_targets_receive_vue_specific_generation_profile(self):
        profiles = _stack_profiles_for("typescript", TARGET_STACKS["vue3"])
        self.assertEqual("vue", profiles[0])
        self.assertIn("javascript_web", profiles)

    def test_every_catalog_polyglot_scaffold_routes_through_a_strict_adapter(self):
        checked = 0
        for target_id, target in TARGET_STACKS.items():
            output = generate_polyglot_project(
                str(target.get("language") or ""), "Demo", "Orders", target,
            )
            for path, content in output.items():
                with self.subTest(target=target_id, path=path):
                    result = validate_file(
                        path, content, str(target.get("language") or ""),
                        dialect_hint=str(target.get("db_target") or ""),
                    )
                    self.assertTrue(result.passed, result.diagnostics)
                    self.assertIn(result.checker, {"compiler", "parser"})
                    checked += 1
        self.assertGreaterEqual(checked, 150)

    def test_react_scaffold_runs_strict_typescript_before_bundle(self):
        files = _frontend_scaffold_files("React 18 + TypeScript", "Demo", False)
        package = json.loads(files["frontend/package.json"])
        self.assertEqual("tsc --noEmit && vite build", package["scripts"]["build"])

    def test_angular_fallback_is_native_and_has_a_complete_workspace(self):
        output = _mp_generate_build_files(
            "typescript", "angular_ts", "Demo", ["orders"],
        )
        _gen_ts_component(output, "Demo", "Orders", "angular_ts")

        self.assertIn("ModernizedApp/angular.json", output)
        self.assertIn("ModernizedApp/src/main.ts", output)
        page = output["ModernizedApp/src/components/Orders/OrdersPage.ts"]
        self.assertIn("@Component", page)
        self.assertIn("standalone: true", page)
        self.assertIn("implements OnInit", page)
        self.assertNotIn("export default {}", page)
        self.assertFalse(any(path.endswith("OrdersPage.tsx") for path in output))
        tsconfig = json.loads(output["ModernizedApp/tsconfig.app.json"])
        self.assertIn("src/**/*.ts", tsconfig["include"])

    def test_angular_framework_peers_are_pinned_to_coherent_patch_versions(self):
        expected = {
            17: ("17.3.12", "17.3.17"),
            18: ("18.2.14", "18.2.21"),
            19: ("19.2.25", "19.2.27"),
            20: ("20.3.35", "20.3.42"),
            21: ("21.2.26", "21.2.29"),
            22: ("22.2.5", "22.2.6"),
        }
        for major, (framework_version, tooling_version) in expected.items():
            with self.subTest(major=major):
                package = json.loads(_frontend_scaffold_files(
                    f"Angular {major} + TypeScript", "Demo", False,
                )["frontend/package.json"])
                framework_packages = (
                    "@angular/animations", "@angular/common", "@angular/compiler",
                    "@angular/core", "@angular/forms", "@angular/platform-browser",
                    "@angular/platform-browser-dynamic", "@angular/router",
                )
                self.assertTrue(all(
                    package["dependencies"][name] == framework_version
                    for name in framework_packages
                ))
                self.assertEqual(
                    framework_version, package["devDependencies"]["@angular/compiler-cli"],
                )
                self.assertEqual(
                    tooling_version, package["devDependencies"]["@angular/cli"],
                )
                self.assertEqual(
                    tooling_version,
                    package["devDependencies"]["@angular-devkit/build-angular"],
                )
                expected_zone = "~0.15.0" if major >= 19 else "~0.14.10"
                self.assertEqual(expected_zone, package["dependencies"]["zone.js"])
                self.assertNotIn("test", package["scripts"])

    def test_react_fallback_uses_vite_runtime_configuration(self):
        output = {}
        _gen_ts_component(output, "Demo", "Orders", "react_ts")
        service = output["ModernizedApp/src/components/Orders/OrdersService.ts"]
        self.assertIn("import.meta.env.VITE_API_URL", service)
        self.assertNotIn("process.env.REACT_APP_API_URL", service)

    def test_selected_target_supplies_omitted_layer_facts(self):
        angular, angular_signals, full_stack, language, _ = _pf_resolve_target(
            "Build an inventory dashboard", "angular_ts", "",
        )
        self.assertEqual("typescript", language)
        self.assertEqual("Angular", angular_signals["frontend"])
        self.assertIsNone(angular_signals["backend"])
        self.assertFalse(full_stack)
        self.assertIn("Angular", angular["frontend_tech"])

        spring, spring_signals, _, language, _ = _pf_resolve_target(
            "Build inventory management", "spring_boot", "",
        )
        self.assertEqual("java", language)
        self.assertIn("Spring Boot", spring_signals["backend"])
        self.assertEqual("postgres", spring["db_target"])

        node, node_signals, full_stack, language, _ = _pf_resolve_target(
            "Build inventory management", "node_express_react", "",
        )
        self.assertEqual("typescript", language)
        self.assertEqual("Node.js + Express", node_signals["backend"])
        self.assertEqual("React", node_signals["frontend"])
        self.assertTrue(full_stack)
        self.assertEqual("mongodb", node["db_target"])

    def test_prompt_facts_override_target_and_unknowns_fail_closed(self):
        target, signals, _, _, _ = _pf_resolve_target(
            "Use React as the frontend", "angular_ts", "",
        )
        self.assertEqual("React", signals["frontend"])
        self.assertEqual("React", target["frontend_tech"])
        with self.assertRaisesRegex(ValueError, "Unknown target stack"):
            _pf_resolve_target("Build an app", "missing-stack", "")

        custom, _, _, _, _ = _pf_resolve_target(
            "Build a Rust Axum API with no persistence", "custom", "",
        )
        self.assertEqual("rust", custom["language"])
        self.assertEqual("", custom["db_target"])

    def test_node_and_python_manifests_follow_framework_and_database(self):
        express = _backend_manifest_files(
            "typescript", "Orders", "Node.js + Express", False, False, "mongodb",
        )
        package = json.loads(express["backend/package.json"])
        self.assertIn("express", package["dependencies"])
        self.assertIn("mongoose", package["dependencies"])
        self.assertNotIn("pg", package["dependencies"])
        self.assertIn("backend/tsconfig.json", express)

        graphql = json.loads(_backend_manifest_files(
            "typescript", "Orders", "Node.js + GraphQL", False, False, "postgres",
        )["backend/package.json"])
        self.assertIn("@apollo/server", graphql["dependencies"])
        self.assertIn("pg", graphql["dependencies"])
        self.assertNotIn("express", graphql["dependencies"])

        django = _backend_manifest_files(
            "python", "Orders", "Django 5 + Django REST Framework", False, False, "postgres",
        )["requirements.txt"]
        self.assertIn("Django", django)
        self.assertIn("psycopg", django)
        self.assertNotIn("fastapi", django)

        mongo_fastapi = _backend_manifest_files(
            "python", "Orders", "Python FastAPI", False, False, "mongodb",
        )["requirements.txt"]
        self.assertIn("motor", mongo_fastapi)
        self.assertNotIn("asyncpg", mongo_fastapi)

    def test_react_vue_and_angular_scaffolds_are_bootstrap_closed(self):
        cases = {
            "React 18 + TypeScript + Vite": ("frontend/src/main.tsx", "frontend/src/App.tsx"),
            "Vue 3 + TypeScript + Vite": ("frontend/src/main.ts", "frontend/src/App.vue"),
            "Angular 17 + TypeScript": ("frontend/src/main.ts", "frontend/angular.json"),
        }
        for framework, required in cases.items():
            with self.subTest(framework=framework):
                files = _frontend_scaffold_files(framework, "Demo", False)
                self.assertIn("frontend/package.json", files)
                self.assertIn("frontend/tsconfig.json", files)
                for path in required:
                    self.assertIn(path, files)
                for path, content in files.items():
                    result = validate_file(path, content, "typescript")
                    self.assertTrue(result.passed, (path, result.diagnostics))
                if framework.startswith("Angular"):
                    self.assertIn("frontend/src/app/app.component.ts", files)
                    self.assertIn("bootstrapApplication", files["frontend/src/main.ts"])
                rooted = {f"Demo/{path}": content for path, content in files.items()}
                self.assertEqual([], _npm_dependency_declaration_diagnostics(rooted))

    def test_node_and_django_polyglot_contracts_are_framework_native(self):
        for target_id, tokens in {
            "node_express_react": ("express", "mongoose", "main.tsx"),
            "node_graphql_react": ("@apollo/server", "graphql", "main.tsx"),
            "javascript_node": ("express", "server.js"),
            "python_django": ("Django==", "manage.py", "test_health.py"),
        }.items():
            target = TARGET_STACKS[target_id]
            files = generate_polyglot_project(target["language"], "Demo", "Orders", target)
            searchable = "\n".join(files) + "\n" + "\n".join(files.values())
            for token in tokens:
                self.assertIn(token, searchable)

    def test_mobile_targets_do_not_become_browser_or_kubernetes_frontends(self):
        react_native = _frontend_scaffold_files("React Native 0.86 + TypeScript", "Demo", False)
        self.assertIn("mobile/App.tsx", react_native)
        self.assertNotIn("frontend/index.html", react_native)
        self.assertEqual([], _npm_dependency_declaration_diagnostics({
            f"Demo/{path}": content for path, content in react_native.items()
        }))

        flutter = _frontend_scaffold_files("Flutter", "Demo", False)
        self.assertIn("mobile/pubspec.yaml", flutter)
        backend = _backend_manifest_files(
            "dart", "Demo", ".NET 8 Web API", False, False, "postgres",
        )
        self.assertIn("backend/Demo.csproj", backend)

        generated = {}
        _pf_generate_infra_scaffold(
            "typescript", {"frontend": "React Native", "deployment_kind": "kubernetes", "deploy": "Kubernetes"},
            "Demo", False, True, generated.__setitem__, lambda *_: None,
        )
        self.assertFalse(any("docker-compose" in path or "/k8s/" in path for path in generated))

    def test_offline_polyglot_fallback_uses_buildable_native_contract(self):
        target = TARGET_STACKS["rust_axum_react"]
        output = {}
        file_list = _pf_generate_project_files_template(
            target, "Demo", "Build an Axum service", True, True, True, "rust",
            output, (), output.__setitem__, lambda *_: None,
        )
        self.assertTrue(any(path.endswith("Cargo.toml") for path in file_list))
        self.assertTrue(any(path.endswith("src/main.rs") for path in output))
        self.assertFalse(any(path.endswith(".cs") for path in output))

    def test_architecture_and_dependency_coverage_fail_closed(self):
        incomplete = {
            "Demo/frontend/package.json": '{"dependencies":{"react":"^18"}}',
            "Demo/frontend/src/App.tsx": "import axios from 'axios'; export default ()=>null;",
        }
        diagnostics = _requirement_coverage_diagnostics(
            incomplete, "Build a React application with automated tests", "typescript",
        )
        self.assertTrue(any("Undeclared npm dependency 'axios'" in item for item in diagnostics))
        self.assertTrue(any("React requires" in item for item in diagnostics))
        self.assertTrue(any("automated tests" in item for item in diagnostics))

    def test_unsupported_default_never_falls_back_to_csharp(self):
        self.assertEqual([], _default_file_list({"language": "rust"}, "Demo"))

    def test_native_dependency_free_scaffolds_compile_and_link(self):
        for target_id in ("c_native", "cpp_native", "cobol_db2", "shell_automation"):
            target = TARGET_STACKS[target_id]
            output = generate_polyglot_project(target["language"], "Demo", "Orders", target)
            with self.subTest(target=target_id), tempfile.TemporaryDirectory() as directory:
                result = run_build(output, target["language"], Path(directory))
                self.assertTrue(result.passed, result.errors_by_file)


if __name__ == "__main__":
    unittest.main()
