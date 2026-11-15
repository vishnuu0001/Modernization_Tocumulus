# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — build-runner fail-closed and extension coverage tests
# Date: 2026-07-23
# ---------------------------------------------------------------------------
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.build_runner import (
    BuildResult,
    _NPM_BUILD_TIMEOUT,
    _command_usable,
    _dispatch_build,
    _is_transient_toolchain_crash,
    _merge_path_values,
    _node_home_runtime,
    _npm_runtime,
    _parse_angular_diagnostic,
    _parse_maven_diagnostic,
    _parse_maven_project_diagnostic,
    _collect_maven_errors,
    _parse_parenthesized_diagnostic,
    _prepend_existing_msys_paths,
    _vite_manifest_errors,
    _which,
    _npm_compile,
    _python_test_commands,
    _run_npm_tsc_build,
    _run_sql_project_build,
    _run_npm_subprocess_with_retry,
    _run_process_tree,
    _run_maven_build,
    _run_dotnet_build,
    _run_artifact_project_build,
    run_build,
    toolchain_compatibility_error,
)


class BuildRunnerIntegrityTests(unittest.TestCase):
    def test_windows_timeout_terminates_the_package_manager_process_tree(self):
        class TimedOutProcess:
            pid = 4321
            returncode = 1

            def __init__(self):
                self.communicate_calls = 0

            def communicate(self, timeout=None):
                self.communicate_calls += 1
                if self.communicate_calls == 1:
                    raise subprocess.TimeoutExpired(["bundle", "install"], timeout)
                return "partial output", "timed out"

            def kill(self):
                return None

        process = TimedOutProcess()
        with (
            patch("services.build_runner.os.name", "nt"),
            patch("services.build_runner.subprocess.Popen", return_value=process),
            patch("services.build_runner.subprocess.run") as terminate,
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            _run_process_tree(["bundle", "install"], cwd=".", timeout=1)

        self.assertEqual(
            ["taskkill", "/PID", "4321", "/T", "/F"],
            terminate.call_args.args[0],
        )
        self.assertEqual(2, process.communicate_calls)

    def test_windows_portable_node_home_is_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for name in ("node.exe", "npm.cmd", "npx.cmd"):
                (home / name).touch()

            runtime = _node_home_runtime(home)

            self.assertIsNotNone(runtime)
            self.assertEqual(runtime[0], home / "node.exe")
            self.assertEqual(runtime[1], home / "npm.cmd")
            self.assertEqual(runtime[2], home / "npx.cmd")

    @patch("services.build_runner._project_node_major", return_value=22)
    @patch("services.build_runner._node_home_candidates")
    def test_npm_runtime_selects_windows_portable_node(self, node_homes, _major):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for name in ("node.exe", "npm.cmd", "npx.cmd"):
                (home / name).touch()
            node_homes.return_value = [home]

            npm, npx, environment = _npm_runtime(home / "package.json")

            self.assertEqual(npm, str(home / "npm.cmd"))
            self.assertEqual(npx, str(home / "npx.cmd"))
            self.assertEqual(environment["PATH"].split(os.pathsep, 1)[0], str(home))

    def test_python_pytest_style_tests_use_pytest_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_service.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
            commands = _python_test_commands("python", root)
        self.assertEqual(1, len(commands))
        self.assertEqual(["python", "-c"], commands[0][:2])
        self.assertIn("pytest.main", commands[0][2])
        self.assertEqual(".", commands[0][3])

    def test_python_test_discovery_ignores_restored_environment_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependency_test = (
                root / ".modernization-venv" / "Lib" / "site-packages" / "asgiref" / "testing.py"
            )
            dependency_test.parent.mkdir(parents=True)
            dependency_test.write_text("raise RuntimeError('not an application test')\n", encoding="utf-8")
            application_test = root / "app" / "tests" / "test_health.py"
            application_test.parent.mkdir(parents=True)
            application_test.write_text("def test_health():\n    assert True\n", encoding="utf-8")

            commands = _python_test_commands("python", root)

        self.assertEqual(1, len(commands))
        self.assertEqual(["python", "-c"], commands[0][:2])
        self.assertIn("pytest.main", commands[0][2])
        self.assertEqual("app", commands[0][3])

    def test_sql_project_route_parses_every_sql_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "V1__schema.sql").write_text(
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY);", encoding="utf-8",
            )
            result = _run_sql_project_build(root)
        self.assertTrue(result.passed, result.errors_by_file)
        self.assertEqual("sql-project-parser", result.checker)

    def test_artifact_project_route_parses_github_actions_and_jenkinsfile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ci.yml").write_text("name: ci\non: push\njobs: {}\n", encoding="utf-8")
            github = _run_artifact_project_build(root, "github_actions")
        self.assertTrue(github.passed, github.errors_by_file)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Jenkinsfile").write_text(
                "pipeline {\n  agent any\n  stages {\n    stage('test') {\n"
                "      steps {\n        echo 'ok'\n      }\n    }\n  }\n}\n",
                encoding="utf-8",
            )
            jenkins = _run_artifact_project_build(root, "jenkinsfile")
        self.assertTrue(jenkins.passed, jenkins.errors_by_file)

    @patch("services.build_runner._NPM_PATH", "npm")
    @patch("services.build_runner._NPX_PATH", "npx")
    @patch("services.build_runner._npm_test")
    @patch("services.build_runner._npm_compile")
    @patch("services.build_runner._npm_install")
    def test_npm_workspace_executes_declared_tests_after_build(self, install, compile_, test):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text(
                '{"scripts":{"build":"tsc","test":"vitest run"}}', encoding="utf-8",
            )
            install.return_value = subprocess.CompletedProcess([], 0, "installed", "")
            compile_.return_value = subprocess.CompletedProcess([], 0, "built", "")
            test.return_value = subprocess.CompletedProcess([], 0, "tested", "")
            result = _run_npm_tsc_build(root)
        self.assertTrue(result.passed)
        self.assertIn("npm-test", result.checker)
        test.assert_called_once()

    @patch("services.build_runner._DOTNET_PATH", "dotnet")
    @patch("services.build_runner._dotnet_solution_integrity_error", return_value=None)
    @patch("services.build_runner._find_one", return_value=Path("C:/tmp/Demo.sln"))
    @patch("services.build_runner.subprocess.run")
    def test_dotnet_route_builds_with_warnings_as_errors_then_runs_tests(self, run, _find, _integrity):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "build passed", ""),
            subprocess.CompletedProcess([], 0, "tests passed", ""),
        ]

        result = _run_dotnet_build(Path("C:/tmp"))

        self.assertTrue(result.passed)
        self.assertEqual("dotnet-build+test", result.checker)
        self.assertEqual("build", run.call_args_list[0].args[0][1])
        self.assertIn("-warnaserror", run.call_args_list[0].args[0])
        self.assertEqual("test", run.call_args_list[1].args[0][1])
        self.assertIn("--no-build", run.call_args_list[1].args[0])

    # Function: test_npm_compile_uses_dedicated_frontend_build_timeout
    @patch("services.build_runner._run_npm_subprocess_with_retry", return_value=object())
    def test_npm_compile_uses_dedicated_frontend_build_timeout(self, run_retry):
        project_dir = Path("C:/tmp/frontend")

        _npm_compile(project_dir, "ng build")

        run_retry.assert_called_once_with(
            unittest.mock.ANY,
            project_dir,
            _NPM_BUILD_TIMEOUT,
            "<build>",
            f"frontend build timed out after {_NPM_BUILD_TIMEOUT}s",
            env=unittest.mock.ANY,
        )

    # Function: test_which_falls_back_to_preferred_java_home_when_path_missing
    @patch("services.build_runner.find_executable", return_value=None)
    @patch("services.build_runner._preferred_java_home", return_value=Path("C:/Java/jdk-21"))
    @patch("pathlib.Path.is_file", return_value=True)
    def test_which_falls_back_to_preferred_java_home_when_path_missing(self, _is_file, _java_home, _find):
        resolved = _which("javac")
        normalized = str(resolved).replace("\\", "/").lower()
        self.assertTrue(normalized.endswith("java/jdk-21/bin/javac.exe"))

    @patch("services.build_runner.find_executable", return_value=r"C:\Java\jdk-17\bin\javac.exe")
    @patch("services.build_runner._preferred_java_home", return_value=Path("C:/Java/jdk-21"))
    @patch("pathlib.Path.is_file", return_value=True)
    def test_which_prefers_java_home_over_older_path_java(self, _is_file, _java_home, _find):
        resolved = _which("javac")
        normalized = str(resolved).replace("\\", "/").lower()
        self.assertTrue(normalized.endswith("java/jdk-21/bin/javac.exe"))

    # Function: test_which_falls_back_to_maven_home_when_path_missing
    @patch("services.build_runner.find_executable", return_value=None)
    def test_which_falls_back_to_maven_home_when_path_missing(self, _find):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "bin" / "mvn.cmd"
            executable.parent.mkdir()
            executable.touch()
            with patch.dict("services.build_runner.os.environ", {"MAVEN_HOME": directory}):
                resolved = _which("mvn")
        self.assertEqual(executable, Path(resolved))

    # Function: test_windows_path_helpers_preserve_precedence_and_deduplicate
    @patch("services.build_runner.os.path.isdir")
    def test_windows_path_helpers_preserve_precedence_and_deduplicate(self, isdir):
        isdir.side_effect = lambda path: path.endswith("ucrt64\\bin")
        entries, seen = _merge_path_values([
            r" C:\Tools ;C:\SDK\\", r"c:\tools;C:\Other",
        ])
        _prepend_existing_msys_paths(entries, seen)
        self.assertEqual(
            entries,
            [r"C:\msys64\ucrt64\bin", r"C:\Tools", r"C:\SDK\\", r"C:\Other"],
        )

    # Function: test_diagnostic_parsers_preserve_paths_and_messages
    def test_diagnostic_parsers_preserve_paths_and_messages(self):
        self.assertEqual(
            _parse_parenthesized_diagnostic(
                r"C:\work\Demo.cs(12,7): error CS1002: ; expected [Demo.csproj]",
                "CS",
            ),
            (r"C:\work\Demo.cs", "12", "CS1002", "; expected"),
        )
        self.assertEqual(
            _parse_maven_diagnostic(r"[ERROR] C:\work\Demo.java:[9,18] ';' expected"),
            (r"C:\work\Demo.java", "';' expected"),
        )
        self.assertEqual(
            _parse_angular_diagnostic(r"Error: C:\work\App.tsx:4:9 - error TS1005: ')' expected."),
            (r"C:\work\App.tsx", "TS1005", "')' expected."),
        )

    # Function: test_maven_missing_module_is_attributed_to_parent_pom
    def test_maven_missing_module_is_attributed_to_parent_pom(self):
        line = (
            r"[ERROR] Child module C:\tmp\App\backend\domain-a-inventory\pom.xml "
            r"of C:\tmp\App\pom.xml does not exist"
        )
        parsed = _parse_maven_project_diagnostic(line)
        self.assertEqual(parsed[0], r"C:\tmp\App\pom.xml")
        self.assertIn("backend", parsed[1])

    def test_maven_compiler_continuation_details_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pom = root / "App" / "pom.xml"
            pom.parent.mkdir(parents=True)
            source = pom.parent / "src" / "main" / "java" / "p" / "Caller.java"
            combined = "\n".join((
                f"[ERROR] {source}:[10,5] method login cannot be applied to given types;",
                "[ERROR]   required: String,String,UserRole,HttpServletRequest,HttpServletResponse,UserService",
                "[ERROR]   found: String,String,UserRole,HttpServletRequest,HttpServletResponse",
                "[ERROR]   reason: actual and formal argument lists differ in length",
            ))

            errors = _collect_maven_errors(combined, pom, root)

        message = next(iter(errors.values()))[0]
        self.assertIn("required: String,String,UserRole", message)
        self.assertIn("found: String,String,UserRole", message)
        self.assertIn("reason: actual and formal argument lists differ", message)

    # Function: test_maven_project_diagnostic_uses_case_insensitive_delimiters
    def test_maven_project_diagnostic_uses_case_insensitive_delimiters(self):
        line = (
            r"[ERROR] CHILD MODULE C:\tmp\Child Module\pom.xml "
            r"OF C:\tmp\Parent App\pom.xml DOES NOT EXIST"
        )
        self.assertEqual(
            _parse_maven_project_diagnostic(line),
            (
                r"C:\tmp\Parent App\pom.xml",
                r"Declared Maven child module does not exist: C:\tmp\Child Module\pom.xml",
            ),
        )
        self.assertIsNone(_parse_maven_project_diagnostic("Child module " + "x" * 10000))

    # Function: test_erlc_probe_retains_effective_v_argument
    @patch("services.build_runner.executable_environment", return_value={})
    @patch("services.build_runner.subprocess.run")
    @patch("services.build_runner._which", return_value="erlc.exe")
    def test_erlc_probe_retains_effective_v_argument(self, _which_mock, run, _environment):
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        self.assertTrue(_command_usable("erlc"))
        self.assertEqual(run.call_args.args[0], ["erlc.exe", "-v"])

    # Function: test_vite_unresolved_import_is_attributed_to_package_manifest
    def test_vite_unresolved_import_is_attributed_to_package_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "App" / "frontend" / "package.json"
            package.parent.mkdir(parents=True)
            errors = _vite_manifest_errors(
                '[vite]: Rollup failed to resolve import "axios" from "src/api.ts".',
                package,
                root,
            )
        self.assertEqual(list(errors), ["App/frontend/package.json"])
        self.assertIn("axios", errors["App/frontend/package.json"][0])

    # Function: test_vite_local_alias_failure_is_attributed_to_importer
    def test_vite_local_alias_failure_is_attributed_to_importer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "App" / "frontend" / "package.json"
            importer = root / "App" / "frontend" / "src" / "App.tsx"
            importer.parent.mkdir(parents=True)
            errors = _vite_manifest_errors(
                f'[vite]: Rollup failed to resolve import "@/components/Card" from "{importer}".',
                package,
                root,
            )
        self.assertEqual(list(errors), ["App/frontend/src/App.tsx"])
        self.assertIn("@/components/Card", errors["App/frontend/src/App.tsx"][0])

    # Function: test_vite_could_not_resolve_local_asset_is_attributed_to_importer
    def test_vite_could_not_resolve_local_asset_is_attributed_to_importer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "App" / "frontend" / "package.json"
            errors = _vite_manifest_errors(
                'Could not resolve "./index.css" from "src/main.tsx"',
                package,
                root,
            )
        self.assertEqual(list(errors), ["App/frontend/src/main.tsx"])
        self.assertIn("./index.css", errors["App/frontend/src/main.tsx"][0])

    # Function: test_maven_build_uses_writable_service_repository
    @patch("services.build_runner._preferred_java_home", return_value=None)
    @patch("services.build_runner.subprocess.run")
    @patch("services.build_runner._MVN_PATH", "mvn.cmd")
    def test_maven_build_uses_writable_service_repository(self, run, _java_home):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pom.xml").write_text("<project/>", encoding="utf-8")
            result = _run_maven_build(root)
        self.assertTrue(result.passed)
        command = run.call_args.args[0]
        self.assertTrue(any(arg.startswith("-Dmaven.repo.local=") for arg in command))

    # Function: test_maven_repository_access_failure_retries_in_isolated_cache
    @patch("services.build_runner._preferred_java_home", return_value=None)
    @patch("services.build_runner.subprocess.run")
    @patch("services.build_runner._MVN_PATH", "mvn.cmd")
    def test_maven_repository_access_failure_retries_in_isolated_cache(self, run, _java_home):
        run.side_effect = [
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="cached dependency.jar: Access is denied.",
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pom.xml").write_text("<project/>", encoding="utf-8")
            result = _run_maven_build(root)
        self.assertTrue(result.passed)
        self.assertEqual(2, run.call_count)
        repositories = [
            next(arg for arg in call.args[0] if arg.startswith("-Dmaven.repo.local="))
            for call in run.call_args_list
        ]
        self.assertNotEqual(repositories[0], repositories[1])

    # Function: test_maven_symbol_details_are_preserved_for_repair
    @patch("services.build_runner._preferred_java_home", return_value=None)
    @patch("services.build_runner.subprocess.run")
    @patch("services.build_runner._MVN_PATH", "mvn.cmd")
    def test_maven_symbol_details_are_preserved_for_repair(self, run, _java_home):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src" / "main" / "java" / "Demo.java"
            source.parent.mkdir(parents=True)
            source.write_text("class Demo {}", encoding="utf-8")
            (root / "pom.xml").write_text("<project/>", encoding="utf-8")
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1,
                stdout=(
                    f"[ERROR] {source}:[5,12] cannot find symbol\n"
                    "[ERROR]   symbol: class ProductDto\n"
                    "[ERROR]   location: class Demo\n"
                ),
                stderr="",
            )
            result = _run_maven_build(root)
        diagnostics = result.errors_by_file["src/main/java/Demo.java"][0]
        self.assertIn("symbol: class ProductDto", diagnostics)
        self.assertIn("location: class Demo", diagnostics)

    # Function: test_transient_npm_crash_is_retried_and_recovers
    @patch("services.build_runner.time.sleep")
    @patch("services.build_runner._run_process_tree")
    def test_transient_npm_crash_is_retried_and_recovers(self, run, _sleep):
        crash = subprocess.CompletedProcess(
            args=["npm", "install"], returncode=1, stdout="",
            stderr="[low_level_alloc.cc : 554] RAW: Check new_pages != nullptr failed: VirtualAlloc failed",
        )
        success = subprocess.CompletedProcess(args=["npm", "install"], returncode=0, stdout="ok", stderr="")
        run.side_effect = [crash, success]

        result = _run_npm_subprocess_with_retry(
            ["npm", "install"], Path("."), 180, "<install>", "timed out",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(run.call_count, 2)
        _sleep.assert_called_once()

    # Function: test_transient_npm_crash_gives_up_after_max_retries
    @patch("services.build_runner.time.sleep")
    @patch("services.build_runner._run_process_tree")
    def test_transient_npm_crash_gives_up_after_max_retries(self, run, _sleep):
        crash = subprocess.CompletedProcess(
            args=["npm", "install"], returncode=1, stdout="",
            stderr="RAW: Check new_pages != nullptr failed: VirtualAlloc failed",
        )
        run.return_value = crash

        result = _run_npm_subprocess_with_retry(
            ["npm", "install"], Path("."), 180, "<install>", "timed out",
        )

        self.assertIs(crash, result)
        self.assertEqual(run.call_count, 3)  # 1 initial attempt + 2 retries

    # Function: test_real_dependency_failure_is_not_retried
    @patch("services.build_runner.time.sleep")
    @patch("services.build_runner._run_process_tree")
    def test_real_dependency_failure_is_not_retried(self, run, _sleep):
        not_found = subprocess.CompletedProcess(
            args=["npm", "install"], returncode=1, stdout="",
            stderr="npm error 404 Not Found - GET https://registry.npmjs.org/nonexistent-package",
        )
        run.return_value = not_found

        result = _run_npm_subprocess_with_retry(
            ["npm", "install"], Path("."), 180, "<install>", "timed out",
        )

        self.assertIs(not_found, result)
        self.assertEqual(run.call_count, 1)
        _sleep.assert_not_called()

    # Function: test_is_transient_toolchain_crash_ignores_successful_runs
    def test_is_transient_toolchain_crash_ignores_successful_runs(self):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="VirtualAlloc failed")
        self.assertFalse(_is_transient_toolchain_crash(ok))

    # Function: test_missing_manifest_fails_strict_build
    def test_missing_manifest_fails_strict_build(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_build({"src/App.ts": "export const value = 1;\n"}, "typescript", Path(directory))
        self.assertFalse(result.passed)
        self.assertEqual(result.checker, "missing-manifest")

    # Function: test_internal_build_error_fails_closed
    @patch("services.build_runner._materialize", side_effect=RuntimeError("boom"))
    def test_internal_build_error_fails_closed(self, _materialize):
        with tempfile.TemporaryDirectory() as directory:
            result = run_build({"main.py": "print('ok')\n"}, "python", Path(directory))
        self.assertFalse(result.passed)
        self.assertEqual(result.checker, "build-runner-error")

    def test_materialization_rejects_paths_outside_project_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_build(
                {"../escaped.py": "print('unsafe')\n"},
                "python",
                Path(directory),
            )
        self.assertFalse(result.passed)
        self.assertEqual("build-runner-error", result.checker)
        self.assertIn("outside the project boundary", " ".join(result.errors_by_file["<build>"]))

    def test_materialization_rejects_non_text_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_build(
                {"app.py": None},  # type: ignore[dict-item]
                "python",
                Path(directory),
            )
        self.assertFalse(result.passed)
        self.assertEqual("build-runner-error", result.checker)
        self.assertIn("does not contain text", " ".join(result.errors_by_file["<build>"]))

    # Function: test_unknown_build_route_fails_closed
    def test_unknown_build_route_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_build({"source.unknown": "content\n"}, "unknown", Path(directory))
        self.assertFalse(result.passed)
        self.assertEqual(result.checker, "unsupported-build-route")

    @patch("services.validators.validate_file")
    def test_sql_project_build_propagates_authoritative_dialect(self, validate):
        validate.return_value = type(
            "Validation", (), {"passed": True, "diagnostics": []},
        )()
        output = {
            "Demo/database/generated.sql": (
                "-- raise exception when no account exists\n"
                "CREATE TABLE accounts (id NUMBER PRIMARY KEY);\n"
            ),
        }

        with tempfile.TemporaryDirectory() as directory:
            result = run_build(
                output, "sql", Path(directory), sql_dialect="oracle",
            )

        self.assertTrue(result.passed, result.errors_by_file)
        self.assertEqual("oracle", validate.call_args.kwargs["dialect_hint"])

    # Function: test_dispatch_routes_c_family_without_falling_through
    @patch("services.build_runner._run_c_family_build")
    def test_dispatch_routes_c_family_without_falling_through(self, c_family_build):
        c_family_build.return_value = BuildResult(True, "c-build")
        result = _dispatch_build(Path("C:/tmp"), "c")
        self.assertTrue(result.passed)
        c_family_build.assert_called_once_with(Path("C:/tmp"), "c")

    def test_c_project_without_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_build({"README.md": "# C project\n"}, "c", Path(directory))
        self.assertFalse(result.passed)
        self.assertEqual("missing-source", result.checker)

    def test_artifact_project_requires_its_declared_format(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_build({"README.md": "# Not YAML\n"}, "yaml", Path(directory))
        self.assertFalse(result.passed)
        self.assertEqual("missing-artifact", result.checker)

    # Function: test_python_project_runs_generated_tests
    def test_python_project_runs_generated_tests(self):
        output = {
            "app/math_service.py": "def add(a: int, b: int) -> int:\n    return a + b\n",
            "tests/test_math_service.py": (
                "import unittest\n"
                "from app.math_service import add\n\n"
                "class MathTests(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(3, add(1, 2))\n"
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            result = run_build(output, "python", Path(directory))
        self.assertTrue(result.passed, result.errors_by_file)

    # Function: test_python_project_without_tests_fails_closed
    def test_python_project_without_tests_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_build({"app.py": "print('ok')\n"}, "python", Path(directory))
        self.assertFalse(result.passed)
        self.assertEqual(result.checker, "missing-tests")

    # Function: test_csharp_marker_does_not_require_c_compiler
    @patch("services.build_runner._parser_compatibility_error", return_value=None)
    @patch("services.build_runner._java_compatibility_error", return_value=None)
    @patch("services.build_runner._dotnet_compatibility_error", return_value=None)
    @patch("services.build_runner._refresh_tool_paths")
    def test_csharp_marker_does_not_require_c_compiler(self, _refresh, _dotnet, _java, _parser):
        self.assertIsNone(toolchain_compatibility_error("language:csharp"))

    # Function: test_java_readiness_reports_missing_javac
    @patch("services.build_runner.installed_java_majors", return_value=[21])
    @patch("services.build_runner._refresh_tool_paths")
    @patch("services.build_runner._command_usable", return_value=False)
    def test_java_readiness_reports_missing_javac(self, _usable, _refresh, _versions):
        error = toolchain_compatibility_error("Java 21 Spring Boot")
        self.assertIn("javac", error)

    # Function: test_java_readiness_reports_missing_jvm_build_tool
    @patch("services.build_runner.installed_java_majors", return_value=[21])
    @patch("services.build_runner._refresh_tool_paths")
    @patch("services.build_runner._command_usable")
    def test_java_readiness_reports_missing_jvm_build_tool(self, usable, _refresh, _versions):
        usable.side_effect = lambda command: command == "javac"
        error = toolchain_compatibility_error("Java 21 Spring Boot")
        self.assertIn("mvn/gradle", error)

    @patch("services.build_runner.installed_node_majors", return_value=[24])
    def test_angular_20_accepts_installed_node_24(self, _versions):
        from services.build_runner import _angular_node_runtime_available, _node_compatibility_error

        self.assertTrue(_angular_node_runtime_available(20))
        self.assertIsNone(_node_compatibility_error("Angular 20"))

    @patch("services.build_runner.installed_node_majors", return_value=[18])
    def test_angular_20_rejects_unsupported_node_major(self, _versions):
        from services.build_runner import _node_compatibility_error

        error = _node_compatibility_error("Angular 20")

        self.assertIn("[20, 22, 24]", error)
        self.assertIn("[18]", error)

    # Function: test_java_project_build_uses_maven_when_pom_exists
    @patch("services.build_runner._run_maven_build")
    @patch("services.build_runner._run_manifest_build")
    def test_java_project_build_uses_maven_when_pom_exists(self, manifest_build, maven_build):
        from services.build_runner import BuildResult, _run_java_project_build
        maven_build.return_value = BuildResult(True, "maven", raw_output="ok")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "demo").mkdir(parents=True, exist_ok=True)
            (root / "demo" / "pom.xml").write_text("<project/>\n", encoding="utf-8")
            result = _run_java_project_build(root)
        self.assertTrue(result.passed)
        maven_build.assert_called_once()
        manifest_build.assert_not_called()

    # Function: test_java_project_build_uses_gradle_when_gradle_manifest_exists
    @patch("services.build_runner._run_maven_build")
    @patch("services.build_runner._run_manifest_build")
    def test_java_project_build_uses_gradle_when_gradle_manifest_exists(self, manifest_build, maven_build):
        from services.build_runner import BuildResult, _run_java_project_build
        manifest_build.return_value = BuildResult(True, "gradle-build", raw_output="ok")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "demo").mkdir(parents=True, exist_ok=True)
            (root / "demo" / "build.gradle.kts").write_text("plugins { java }\n", encoding="utf-8")
            result = _run_java_project_build(root)
        self.assertTrue(result.passed)
        manifest_build.assert_called_once()
        maven_build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
