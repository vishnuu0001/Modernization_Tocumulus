# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — tests (test_validator_routing.py)
# Date: 2026-02-12
# ---------------------------------------------------------------------------
import subprocess
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from services.validators import (
    _EXTERNAL_VALIDATORS,
    _TREE_SITTER_LANGUAGES,
    _contains_jsx,
    _JAVAC_SYNTAX_PATTERNS,
    ValidationResult,
    _validate_cobol,
    _validate_command,
    _validate_csharp,
    _validate_external_language,
    _validate_java,
    _validate_sql,
    _validate_typescript,
    detect_source_language,
    validate_file,
)
from services.modernizer.target_config import TARGET_STACKS


class ValidatorRoutingTests(unittest.TestCase):
    def test_framework_dependency_diagnostics_fall_back_to_strict_parser(self):
        cases = (
            ("dart", "main.dart", "void main() {}", "Target of URI doesn't exist: package:flutter/material.dart"),
            ("elixir", "router.ex", "defmodule Demo.Router do\nend\n", "module Phoenix.Router is not loaded and could not be found"),
        )
        for language, path, source, diagnostic in cases:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as directory, patch(
                "services.validators.find_executable", return_value=f"{language}.exe",
            ), patch(
                "services.validators.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", diagnostic),
            ):
                result = _validate_external_language(
                    path, source, language, Path(directory),
                )

            self.assertTrue(result.passed, result.diagnostics)
            self.assertEqual("parser", result.checker)

    def test_mix_manifest_uses_parser_without_starting_mix_runtime(self):
        source = "defmodule Demo.MixProject do\n  use Mix.Project\n  def project, do: [app: :demo]\nend\n"
        with tempfile.TemporaryDirectory() as directory, patch(
            "services.validators.subprocess.run",
        ) as run:
            result = _validate_external_language(
                "mix.exs", source, "elixir", Path(directory),
            )

        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual("parser", result.checker)
        run.assert_not_called()

    def test_inaccessible_optional_compiler_falls_back_to_strict_parser(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "services.validators._resolve_validator_command", return_value="php.exe",
        ), patch(
            "services.validators.subprocess.run", side_effect=PermissionError("Access is denied"),
        ):
            result = _validate_command(
                "generated.php", "<?php echo 'ok';\n", "php", Path(directory),
                "php", ["-l"],
            )

        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual("parser", result.checker)

    def test_every_catalog_external_language_has_a_strict_parser_fallback(self):
        catalog_languages = {
            str(target.get("language") or "") for target in TARGET_STACKS.values()
        }
        external_catalog_languages = catalog_languages & set(_EXTERNAL_VALIDATORS)

        self.assertLessEqual(external_catalog_languages, set(_TREE_SITTER_LANGUAGES))

    def test_missing_optional_compilers_fall_back_to_strict_parsers(self):
        samples = {
            "elixir": ("generated.ex", 'defmodule Demo do\n  def greet(name), do: "Hello #{name}"\nend\n'),
            "erlang": ("generated.erl", "-module(demo).\n-export([greet/1]).\ngreet(Name) -> {ok, Name}.\n"),
            "julia": ("generated.jl", 'function greet(name::String)\n  return "Hello, $name"\nend\n'),
            "kotlin": ("generated.kt", 'fun greet(name: String): String = "Hello, $name"\n'),
            "swift": ("generated.swift", 'func greet(_ name: String) -> String { return "Hello, \\(name)" }\n'),
        }
        with patch("services.validators.find_executable", return_value=None):
            for language, (path, source) in samples.items():
                with self.subTest(language=language):
                    result = validate_file(path, source, language)
                    self.assertTrue(result.passed, result.diagnostics)
                    self.assertEqual("parser", result.checker)
                    self.assertEqual(language, result.language)

    def test_java_dangling_try_catch_finally_diagnostics_are_not_filtered_as_noise(self):
        messages = (
            "'catch' without 'try'",
            "'finally' without 'try'",
            "'try' without 'catch', 'finally' or resource declarations",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertTrue(any(pattern.search(message) for pattern in _JAVAC_SYNTAX_PATTERNS))

    def test_java_package_only_source_is_rejected(self):
        result = validate_file(
            "Mazdausa.java",
            "package com.example.model;\n",
            "java",
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("contains no type declaration" in item for item in result.diagnostics))

    def test_java_source_package_must_match_its_source_root_path(self):
        path = (
            "ModernizedApp/services/auth-service/src/main/java/"
            "com/comshashi/auth/model/Auth.java"
        )
        result = validate_file(
            path,
            "package com.shashi.auth.model;\npublic class Auth {}\n",
            "java",
        )

        self.assertFalse(result.passed)
        diagnostic = " ".join(result.diagnostics)
        self.assertIn("package com.shashi.auth.model", diagnostic)
        self.assertIn("source path package com.comshashi.auth.model", diagnostic)

    # Function: test_unknown_format_never_passes_as_advisory
    def test_unknown_format_never_passes_as_advisory(self):
        result = validate_file("generated.unknown", "apparently balanced\n", "generic")
        self.assertFalse(result.passed)
        self.assertEqual("unsupported-validator", result.checker)

    def test_svg_is_parsed_as_xml_with_xml_safe_completion_comment(self):
        source = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20">'
            '<circle cx="10" cy="10" r="8"/></svg>\n'
            '<!-- <<<END_OF_FILE>>> -->\n'
        )
        result = validate_file("generated.svg", source, "svg")
        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual("parser", result.checker)

    # Function: test_tsx_is_compiled_as_tsx
    def test_tsx_is_compiled_as_tsx(self):
        source = (
            "import React from 'react';\n"
            "export default function App() {\n"
            "  return (<main><h1>Hello</h1></main>);\n"
            "}\n"
        )
        result = validate_file("generated.tsx", source, "typescript")
        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual("compiler", result.checker)

    # Function: test_jsx_in_ts_gets_actionable_extension_error
    def test_jsx_in_ts_gets_actionable_extension_error(self):
        source = "export const App = () => (<main>Hello</main>);\n"
        result = validate_file("generated.ts", source, "typescript")
        self.assertFalse(result.passed)
        self.assertIn(".tsx", " ".join(result.diagnostics))

    def test_typescript_generic_arrow_is_not_misclassified_as_jsx(self):
        source = (
            "const parseJson = <T>(response: Response, schema: Schema<T>): Promise<T> => {\n"
            "  return response.json().then(value => schema.parse(value));\n"
            "};\n"
        )

        self.assertFalse(_contains_jsx(source))
        result = validate_file("ShashiService.ts", source, "typescript")
        self.assertNotIn("JSX syntax requires", " ".join(result.diagnostics))

    def test_generic_react_component_with_real_jsx_is_detected(self):
        source = (
            "import React from 'react';\n"
            "export const Item = <T extends object>(props: { value: T }) => "
            "(<div>{String(props.value)}</div>);\n"
        )
        self.assertTrue(_contains_jsx(source))

    def test_html_inside_javascript_string_is_not_misclassified_as_jsx(self):
        source = (
            "import { createApp } from 'vue';\n"
            "createApp({ template: '<main><h1>Ready</h1></main>' }).mount('#app');\n"
        )
        self.assertFalse(_contains_jsx(source))
        result = validate_file("main.ts", source, "typescript")
        self.assertTrue(result.passed, result.diagnostics)

    def test_vue_sfc_validates_embedded_template_script_and_style(self):
        source = (
            '<template><main><h1>{{ title }}</h1></main></template>\n'
            '<script setup lang="ts">\nconst title: string = "Ready";\n</script>\n'
            '<style scoped>main { color: blue; }</style>\n'
        )
        result = validate_file("src/App.vue", source, "typescript")
        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual("vue", result.language)

    def test_vue_sfc_rejects_unclosed_container_and_script_syntax(self):
        source = (
            '<template><main>Broken</main></template>\n'
            '<script setup lang="ts">\nconst title: = ;\n'
        )
        result = validate_file("src/App.vue", source, "typescript")
        self.assertFalse(result.passed)
        joined = " ".join(result.diagnostics)
        self.assertIn("closing <script>", joined)

    def test_framework_manifests_route_to_owned_adapters(self):
        samples = {
            "CMakeLists.txt": "cmake_minimum_required(VERSION 3.20)\nproject(demo C)\nadd_executable(app main.c)\nadd_test(NAME smoke COMMAND app --health)\n",
            "go.mod": "module example.com/demo\n\ngo 1.23\n",
            "DESCRIPTION": "Package: demo\nVersion: 1.0.0\n",
            "conf/routes": "GET /health controllers.HealthController.health\n",
            "demo.cabal": "cabal-version: 2.4\nname: demo\nversion: 1.0.0\nexecutable app\n  main-is: Main.hs\n",
            "rebar.config": "{erl_opts, [debug_info]}.\n",
            "schema.prisma": 'generator client { provider = "prisma-client-js" }\ndatasource db { provider = "postgresql" url = env("DATABASE_URL") }\n',
        }
        for path, source in samples.items():
            with self.subTest(path=path):
                result = validate_file(path, source, "typescript")
                self.assertTrue(result.passed, result.diagnostics)
                self.assertEqual("parser", result.checker)

    def test_python_predefined_module_globals_are_available_at_import_time(self):
        result = validate_file(
            "settings.py",
            "from pathlib import Path\nBASE_DIR = Path(__file__).resolve().parent\n",
            "python",
        )
        self.assertTrue(result.passed, result.diagnostics)

    # Function: test_compiler_backed_validators_use_strict_parser_when_unavailable
    def test_compiler_backed_validators_use_strict_parser_when_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("services.validators._JAVAC_PATH", None):
                self.assertTrue(_validate_java("Demo.java", "class Demo {}", root).passed)
                self.assertFalse(_validate_java("Demo.java", "class Demo {", root).passed)
            with patch("services.validators._TSC_AVAILABLE", False):
                self.assertTrue(_validate_typescript("demo.ts", "const n: number = 1;", root).passed)
                self.assertFalse(_validate_typescript("demo.ts", "const n: = ;", root).passed)
            with patch("services.validators._CSHARP_COMPILER_AVAILABLE", False):
                self.assertTrue(_validate_csharp("Demo.cs", "class Demo {}", root).passed)
                self.assertFalse(_validate_csharp("Demo.cs", "class Demo {", root).passed)
            with patch("services.validators._SQLGLOT_AVAILABLE", False), \
                 patch("services.validators._SQLFLUFF_AVAILABLE", False):
                self.assertFalse(_validate_sql("demo.sql", "SELECT 1;", "").passed)

    def test_angular_parameter_decorators_use_generated_project_compiler_mode(self):
        source = """
import { Inject } from '@angular/core';
import { DOCUMENT } from '@angular/common';
export class SampleComponent {
  constructor(@Inject(DOCUMENT) private readonly document: Document) {}
}
"""
        with tempfile.TemporaryDirectory() as directory:
            result = _validate_typescript(
                "ModernizedApp/frontend/src/app/features/sample/sample.component.ts",
                source,
                Path(directory),
            )
        self.assertTrue(result.passed, result.diagnostics)

    def test_plain_typescript_does_not_silently_enable_parameter_decorators(self):
        source = """
declare const Inject: (token: string) => ParameterDecorator;
export class PlainService {
  constructor(@Inject('TOKEN') private readonly value: object) {}
}
"""
        with tempfile.TemporaryDirectory() as directory:
            result = _validate_typescript("src/plain-service.ts", source, Path(directory))
        self.assertFalse(result.passed)
        self.assertIn("TS1206", "\n".join(result.diagnostics))

    # Function: test_csharp_survives_a_compiler_output_that_cant_be_decoded
    def test_csharp_survives_a_compiler_output_that_cant_be_decoded(self):
        """Regression test for a real production incident: a 9-hour Java
        modernization job lost every generated file when csc's output
        contained a byte the host's default codepage couldn't decode,
        crashing subprocess.run()'s internal reader thread and leaving
        proc.stdout/proc.stderr as None — which `"CS8805" in stderr + stdout`
        then turned into an unhandled TypeError that killed the entire job
        24 seconds after its last compiler repair had succeeded. _run_csc now
        pins encoding="utf-8", errors="replace" so this can't happen for real,
        but this test forces the None case directly (as insurance against any
        other path that could still produce it) and asserts _validate_csharp
        degrades to a normal failed ValidationResult instead of raising."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            undecodable_proc = subprocess.CompletedProcess(args=[], returncode=1, stdout=None, stderr=None)
            with patch("services.validators._CSHARP_COMPILER_AVAILABLE", True), \
                 patch("services.validators._run_csc", return_value=undecodable_proc):
                result = _validate_csharp("Demo.cs", "class Demo {}", root)
        self.assertFalse(result.passed)
        self.assertEqual("compiler", result.checker)

    # Function: test_ibm_fixed_cobol_structural_validation
    @patch("services.validators._validate_command")
    def test_ibm_fixed_cobol_structural_validation(self, compile_file):
        compile_file.return_value = ValidationResult("BANKBAT.cbl", "cobol", "compiler", True, [])
        source = "\n".join([
            "       IDENTIFICATION DIVISION.",
            "       PROGRAM-ID. BANKBAT.",
            "       ENVIRONMENT DIVISION.",
            "       INPUT-OUTPUT SECTION.",
            "       FILE-CONTROL.",
            "           SELECT TRANIN ASSIGN TO 'TRANIN'",
            "               ORGANIZATION IS SEQUENTIAL",
            "               FILE STATUS IS WS-TRANIN-STATUS.",
            "       DATA DIVISION.",
            "       FILE SECTION.",
            "       FD TRANIN.",
            "       01 TRANIN-RECORD PIC X(120).",
            "       WORKING-STORAGE SECTION.",
            "       01 WS-TRANIN-STATUS PIC XX.",
            "       PROCEDURE DIVISION.",
            "       0000-MAIN.",
            "           PERFORM 1000-PROCESS",
            "           STOP RUN.",
            "       1000-PROCESS.",
            "           DISPLAY 'PROCESSING'.",
        ]) + "\n"
        with tempfile.TemporaryDirectory() as directory:
            result = _validate_cobol("BANKBAT.cbl", source, Path(directory), "IBM DB2 z/OS")
        self.assertTrue(result.passed, result.diagnostics)
        arguments = compile_file.call_args.args[-1]
        self.assertIn("-std=ibm", arguments)
        self.assertIn("-fformat=fixed", arguments)

    # Function: test_python_overrides_wrong_stack_hint
    def test_python_overrides_wrong_stack_hint(self):
        source = "import os\n\ndef login(name: str) -> bool:\n    return bool(name)\n"
        self.assertEqual("python", detect_source_language(source, "csharp"))
        result = validate_file("generated.py", source, "csharp")
        self.assertEqual("python", result.language)
        self.assertEqual("compiler", result.checker)
        self.assertTrue(result.passed)

    def test_python_rejects_definition_time_names_imported_too_late(self):
        source = (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            "@app.get('/items')\n"
            "async def items(session=Depends(get_session)):\n"
            "    return []\n\n"
            "from fastapi import Depends\n"
        )

        result = validate_file("generated.py", source, "python")

        self.assertFalse(result.passed)
        self.assertTrue(any("Depends" in item for item in result.diagnostics))
        self.assertTrue(any("get_session" in item for item in result.diagnostics))
        self.assertTrue(any("move all imports" in item for item in result.diagnostics))

    # Function: test_csharp_is_not_confused_with_java
    def test_csharp_is_not_confused_with_java(self):
        source = "using System;\nnamespace Demo;\npublic sealed class User { }\n"
        self.assertEqual("csharp", detect_source_language(source, "java"))

    # Function: test_java_is_not_confused_with_csharp
    def test_java_is_not_confused_with_csharp(self):
        source = "package demo.auth;\nimport java.util.Optional;\npublic class User { }\n"
        self.assertEqual("java", detect_source_language(source, "csharp"))

    def test_java_single_file_preflight_ignores_classpath_cascade(self):
        source = """
package demo;
public class ServiceImpl implements MissingService {
    @Override public MissingDto load() { return MissingFactory.create(); }
}
"""
        result = validate_file("ServiceImpl.java", source, "java")
        self.assertTrue(result.passed, result.diagnostics)

    def test_java_single_file_preflight_keeps_real_syntax_errors(self):
        source = "package demo; public class Broken { void run( { }"
        result = validate_file("Broken.java", source, "java")
        self.assertFalse(result.passed)

    def test_java_preflight_rejects_nonexistent_jaxrs_response_factories(self):
        source = """
package demo;
import jakarta.ws.rs.core.Response;
public class CustomerController {
    Response create(Long id) {
        return Response.status(Response.Status.CREATED)
            .location(Response.locationBuilder("/api/customer/" + id).build()).build();
    }
}
"""
        result = validate_file("CustomerController.java", source, "java")
        self.assertFalse(result.passed)
        self.assertIn("Response has no locationBuilder", "\n".join(result.diagnostics))

    def test_java_single_file_preflight_rejects_split_blank_final_constructors(self):
        source = """
package demo;
public class UserServiceImpl {
    private final Object dbUtil;
    private final String tableName;
    public UserServiceImpl(Object dbUtil) { this.dbUtil = dbUtil; }
    public UserServiceImpl(String tableName) { this.tableName = tableName; }
}
"""
        result = validate_file("UserServiceImpl.java", source, "java")
        self.assertFalse(result.passed)
        self.assertTrue(
            any("might not have been initialized" in item for item in result.diagnostics),
            result.diagnostics,
        )

    def test_java_single_file_preflight_rejects_local_optional_contract_drift(self):
        source = """
import java.util.Optional;
public class Demo {
    static final class Train {}
    Train load() { return new Train(); }
    void render() { Optional<Train> train = load(); }
}
"""
        result = validate_file("Demo.java", source, "java")
        self.assertFalse(result.passed)
        self.assertTrue(
            any("incompatible types" in item for item in result.diagnostics),
            result.diagnostics,
        )

    # Function: test_spring_boot3_semantics_reject_legacy_and_non_production_controller
    def test_spring_boot3_semantics_reject_legacy_and_non_production_controller(self):
        source = """
package demo.orders;
import javax.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.context.request.RequestContextHolder;
@RestController
public class OrderController {
    @Autowired
    private OrderService service;
    public ResponseEntity<Order> create() {
        String key = "Idempotency-Key";
        try { return ResponseEntity.badRequest().body(new OrderResponse(key)); }
        catch (Exception error) { return ResponseEntity.badRequest().build(); }
    }
}
"""
        result = validate_file("OrderController.java", source, "java")
        self.assertFalse(result.passed)
        joined = "\n".join(result.diagnostics)
        self.assertIn("Jakarta namespace", joined)
        self.assertIn("constructor injection", joined)
        self.assertIn("RequestContextHolder", joined)
        self.assertIn("@RequestHeader", joined)
        self.assertIn("Broad Exception", joined)
        self.assertIn("generic/body type mismatch", joined)

    def test_spring_semantics_rejects_inject_and_resource_field_injection(self):
        for annotation in ("@Inject", "@Resource"):
            with self.subTest(annotation=annotation):
                source = f"""
package demo.orders;
import org.springframework.web.bind.annotation.RestController;
@RestController
public class OrderController {{
    {annotation}
    private OrderService service;
}}
"""
                result = validate_file("OrderController.java", source, "java")
                self.assertFalse(result.passed)
                self.assertIn("constructor injection", "\n".join(result.diagnostics))

    def test_spring_semantics_accepts_autowired_constructor_with_final_dependency(self):
        content = """
package com.example;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
@Service
public class BookingService {
    private final BookingRepository repository;

    @Autowired
    public BookingService(BookingRepository repository) {
        this.repository = repository;
    }
}
"""
        result = validate_file("BookingService.java", content, "java")
        self.assertNotIn(
            "field injection is forbidden",
            "\n".join(result.diagnostics),
        )

    def test_spring_semantics_rejects_package_private_qualified_field_injection(self):
        content = """
package com.example;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.stereotype.Service;
@Service
public class BookingService {
    @Autowired
    @Qualifier("primary")
    BookingRepository repository;
}
"""
        result = validate_file("BookingService.java", content, "java")
        self.assertIn(
            "field injection is forbidden",
            "\n".join(result.diagnostics),
        )

    # Function: test_spring_bootstrap_rejects_feature_responsibility_leakage
    def test_spring_bootstrap_rejects_feature_responsibility_leakage(self):
        source = """
package com.modernize.orders;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.web.bind.annotation.*;
import java.util.List;
@SpringBootApplication
public class OrderApplication {
    public static void main(String[] args) {
        SpringApplication.run(OrderApplication.class, args);
    }
}
"""
        result = validate_file("OrderApplication.java", source, "java")
        self.assertFalse(result.passed)
        self.assertIn("dedicated components", "\n".join(result.diagnostics))

    # Function: test_idempotency_header_rule_is_controller_aware
    def test_idempotency_header_rule_is_controller_aware(self):
        filter_source = """
package demo.orders;
import org.springframework.stereotype.Component;
@Component
public class IdempotencyFilter {
    private static final String HEADER = "Idempotency-Key";
}
"""
        filter_result = validate_file("IdempotencyFilter.java", filter_source, "java")
        self.assertNotIn("@RequestHeader", "\n".join(filter_result.diagnostics))

        controller_source = """
package demo.orders;
import org.springframework.web.bind.annotation.*;
@RestController
public class OrderController {
    @PostMapping("/orders")
    public String create(
        @RequestHeader(required = true, name = "Idempotency-Key") String idempotencyKey
    ) {
        return idempotencyKey;
    }
}
"""
        controller_result = validate_file("OrderController.java", controller_source, "java")
        self.assertTrue(controller_result.passed, controller_result.diagnostics)

    def test_external_request_dto_constraints_are_not_guessed_per_file(self):
        controller_source = """
package demo.orders;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.*;
@RestController
public class OrderController {
    @PostMapping("/orders")
    public void create(@Valid @RequestBody CreateOrderRequest request) {}
}
"""
        result = validate_file("OrderController.java", controller_source, "java")
        self.assertNotIn(
            "Bean Validation constraints",
            "\n".join(result.diagnostics),
        )

    def test_removed_spring_security_adapter_is_rejected(self):
        source = """
package demo.security;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.configuration.WebSecurityConfigurerAdapter;
@Configuration
public class SecurityConfig extends WebSecurityConfigurerAdapter {}
"""
        result = validate_file("SecurityConfig.java", source, "java")
        self.assertFalse(result.passed)
        self.assertIn("SecurityFilterChain", "\n".join(result.diagnostics))

    # Function: test_generated_order_controller_scope_and_import_failures_are_rejected
    def test_generated_order_controller_scope_and_import_failures_are_rejected(self):
        source = """
package com.modernize.orders.controller;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.*;
@RestController
public class OrderController {
    private final OrderService orderService;
    private final IdempotencyRepository idempotencyRepository;
    private final KafkaOrderEventPublisher kafkaOrderEventPublisher;
    @PostMapping
    public OrderResponse create(@Valid @RequestBody CreateOrderRequest request) {
        return orderService.create(request);
    }
    private static class CreateOrderRequest {}
    private static class OrderResponse {}
    private static class OrderListResponse {
        private final List<OrderResponse> orders = null;
    }
}
"""
        result = validate_file("OrderController.java", source, "java")
        self.assertFalse(result.passed)
        joined = "\n".join(result.diagnostics)
        self.assertIn("java.util.List", joined)
        self.assertIn("nested transport types", joined)
        self.assertIn("application service", joined)
        self.assertIn("no Jakarta Bean Validation constraints", joined)

    # Function: test_typescript_overrides_python_hint
    def test_typescript_overrides_python_hint(self):
        source = "import React from 'react';\nexport interface User { name: string }\n"
        self.assertEqual("typescript", detect_source_language(source, "python"))

    # Function: test_sql_is_detected_from_statement
    def test_sql_is_detected_from_statement(self):
        self.assertEqual("sql", detect_source_language("SELECT id FROM users;", "csharp"))

    # Function: test_ambiguous_content_keeps_explicit_hint
    def test_ambiguous_content_keeps_explicit_hint(self):
        self.assertEqual("python", detect_source_language("value = 1\n", "python"))

    # Function: test_extended_language_routing
    def test_extended_language_routing(self):
        samples = {
            "c": "#include <stdio.h>\nint main(void) { return 0; }\n",
            "cpp": "#include <iostream>\nint main() { std::cout << 1; }\n",
            "cobol": "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. HELLO.\n       PROCEDURE DIVISION.\n           STOP RUN.\n",
            "php": "<?php\n$name = 'user';\necho $name;\n",
            "ruby": "require 'json'\nclass User\n  def name\n    'user'\n  end\nend\n",
            "go": "package main\nimport \"fmt\"\nfunc main() { fmt.Println(\"ok\") }\n",
        }
        for language, source in samples.items():
            with self.subTest(language=language):
                self.assertEqual(language, detect_source_language(source, "csharp"))

    # Function: test_sqlite_c_header_is_available_to_strict_validation
    def test_sqlite_c_header_is_available_to_strict_validation(self):
        source = (
            "#include <sqlite3.h>\n"
            "int main(void) {\n"
            "    sqlite3 *database = 0;\n"
            "    return sqlite3_open(\":memory:\", &database);\n"
            "}\n"
        )
        result = validate_file("generated.c", source, "c")
        self.assertTrue(result.passed, result.diagnostics)

    # Function: test_machine_readable_artifacts_use_deterministic_parsers
    def test_machine_readable_artifacts_use_deterministic_parsers(self):
        valid = {
            "config.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n",
            "config.toml": "[service]\nport = 8080\n",
            "schema.graphql": "type Query { greeting: String! }\n",
            "main.tf": 'resource "null_resource" "demo" {}\n',
            "config.xml": "<configuration><value>ok</value></configuration>\n",
            "Dockerfile": "FROM alpine:3.20\nRUN echo ok\n",
            ".github/workflows/ci.yml": "name: CI\n'on': [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps: []\n",
            "Chart.yaml": "apiVersion: v2\nname: demo\nversion: 1.0.0\n",
            "templates/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{ .Values.name }}\n",
            "README.md": "# Demo\n\n```text\nexample\n```\n",
            "cloudformation.yaml": (
                "AWSTemplateFormatVersion: '2010-09-09'\n"
                "Resources:\n  Bucket:\n    Type: AWS::S3::Bucket\n"
                "    Properties:\n      BucketName: !Sub '${AWS::StackName}-data'\n"
            ),
        }
        for path, source in valid.items():
            with self.subTest(path=path):
                result = validate_file(path, source)
                self.assertTrue(result.passed, result.diagnostics)
                self.assertIn(result.checker, {"parser", "compiler"})

    def test_iac_artifacts_ignore_every_application_language_hint(self):
        bicep = "targetScope = 'subscription'\nparam location string = 'westeurope'\n"
        terraform = 'resource "null_resource" "demo" {}\n'
        supported_languages = sorted({
            str(target.get("language") or "") for target in TARGET_STACKS.values()
            if target.get("language")
        })
        for language_hint in supported_languages:
            with self.subTest(language_hint=language_hint, artifact="bicep"):
                result = validate_file("infrastructure/main.bicep", bicep, language_hint)
                self.assertTrue(result.passed, result.diagnostics)
                self.assertEqual("bicep", result.language)
                self.assertEqual("parser", result.checker)
            with self.subTest(language_hint=language_hint, artifact="terraform"):
                result = validate_file("infrastructure/main.tf", terraform, language_hint)
                self.assertTrue(result.passed, result.diagnostics)
                self.assertEqual("hcl", result.language)

    def test_razor_markup_is_not_compiled_as_standalone_csharp(self):
        result = validate_file(
            "Frontend/Orders/Orders.razor",
            "<h1>Orders</h1>\n@if (items.Count > 0) { <p>@items.Count</p> }\n",
            "csharp",
        )
        self.assertNotEqual("csharp", result.language)
        self.assertTrue(result.passed, result.diagnostics)

    def test_solution_rejects_non_msbuild_project_entries(self):
        source = """Microsoft Visual Studio Solution File, Format Version 12.00
Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Frontend", "frontend\\package.json", "{11111111-1111-1111-1111-111111111111}"
EndProject
"""
        result = validate_file("Banking.sln", source, "csharp")
        self.assertFalse(result.passed)
        self.assertIn("MSBuild project", " ".join(result.diagnostics))

    # Function: test_invalid_machine_readable_artifacts_fail
    def test_invalid_machine_readable_artifacts_fail(self):
        invalid = {
            "config.yaml": "items: [one,\n",
            "config.toml": "[service\nport = 8080\n",
            "schema.graphql": "type Query {",
            "main.tf": 'resource "null_resource" "demo" {\n',
            "config.xml": "<configuration>",
            "Dockerfile": "RUN echo missing-base\n",
            ".github/workflows/ci.yml": "name: CI\n'on': [push]\n",
            "Chart.yaml": "apiVersion: v2\nname: demo\n",
            "templates/deployment.yaml": "metadata:\n  name: {{ .Values.name }\n",
            "README.md": "# Demo\n\n```text\nunclosed\n",
            "cloudformation.yaml": "AWSTemplateFormatVersion: '2010-09-09'\n",
        }
        for path, source in invalid.items():
            with self.subTest(path=path):
                result = validate_file(path, source)
                self.assertFalse(result.passed)

    # Function: test_vendor_languages_fail_closed_without_compiler
    def test_vendor_languages_fail_closed_without_compiler(self):
        for path, language in {
            "demo.abap": "abap", "demo.pli": "pli", "demo.rpgle": "rpg",
            "demo.jcl": "jcl", "demo.nsp": "natural", "demo.cls": "apex",
        }.items():
            with self.subTest(language=language):
                result = validate_file(path, "placeholder")
                self.assertFalse(result.passed)
                self.assertEqual("missing-toolchain", result.checker)

    # Function: test_postgres_dialect_is_inferred_from_plpgsql
    def test_postgres_dialect_is_inferred_from_plpgsql(self):
        source = (
            "CREATE OR REPLACE FUNCTION update_attempts(p_username VARCHAR)\n"
            "RETURNS VOID AS $$\n"
            "BEGIN\n"
            "  UPDATE users AS u SET login_attempts = u.login_attempts + 1\n"
            "  WHERE u.username = p_username;\n"
            "END;\n"
            "$$ LANGUAGE plpgsql;\n"
        )
        result = validate_file("generated.sql", source, "sql")
        self.assertTrue(result.passed, result.diagnostics)

    # Function: test_sql_tautological_predicate_fails_safety_gate
    def test_sql_tautological_predicate_fails_safety_gate(self):
        source = (
            "UPDATE users SET login_attempts = login_attempts + 1\n"
            "WHERE username = username;\n"
        )
        result = validate_file("generated.sql", source, "sql")
        self.assertFalse(result.passed)
        self.assertIn("tautological predicate", " ".join(result.diagnostics))

    # Function: test_sql_dialect_matrix
    def test_sql_dialect_matrix(self):
        samples = {
            "ANSI SQL": "SELECT customer_id FROM customers WHERE active = 1;",
            "PostgreSQL 16": (
                "CREATE OR REPLACE FUNCTION f() RETURNS INTEGER AS $$ "
                "BEGIN RETURN 1; END; $$ LANGUAGE plpgsql;"
            ),
            "SQL Server 2022": (
                "CREATE OR ALTER PROCEDURE dbo.p AS BEGIN "
                "SELECT TOP (1) id FROM dbo.users; END;"
            ),
            "Oracle PL/SQL": "CREATE OR REPLACE PROCEDURE p AS BEGIN NULL; END;",
            "MySQL 8": "CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY);",
            "IBM Db2 SQL PL": "SELECT * FROM users FETCH FIRST 10 ROWS ONLY;",
            "SQLite 3": "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT);",
            "Google BigQuery": (
                "SELECT ARRAY_AGG(x IGNORE NULLS) "
                "FROM UNNEST([1, NULL, 2]) AS x;"
            ),
            "Snowflake": (
                "SELECT * FROM TABLE(FLATTEN(INPUT => PARSE_JSON('[1,2]')));"
            ),
        }
        for dialect, source in samples.items():
            with self.subTest(dialect=dialect):
                result = validate_file(
                    "generated.sql", source, "sql", dialect_hint=dialect,
                )
                self.assertTrue(result.passed, result.diagnostics)

    # Function: test_sql_dialect_mismatch_fails_before_repair
    def test_sql_dialect_mismatch_fails_before_repair(self):
        source = (
            "CREATE OR REPLACE FUNCTION f() RETURNS INTEGER AS $$ "
            "BEGIN RETURN 1; END; $$ LANGUAGE plpgsql;"
        )
        result = validate_file(
            "generated.sql", source, "sql", dialect_hint="Oracle PL/SQL",
        )
        self.assertFalse(result.passed)
        self.assertIn("dialect mismatch", " ".join(result.diagnostics).casefold())

    def test_sql_dialect_inference_ignores_comments_and_string_literals(self):
        source = (
            "-- If no account exists, raise exception to match the contract\n"
            "CREATE TABLE accounts (\n"
            "  account_id NUMBER PRIMARY KEY,\n"
            "  amount NUMBER(15, 2) NOT NULL,\n"
            "  note VARCHAR2(100) DEFAULT 'raise exception'\n"
            ");\n"
        )

        result = validate_file(
            "generated.sql", source, "sql", dialect_hint="Oracle PL/SQL",
        )

        self.assertTrue(result.passed, result.diagnostics)

    def test_oracle_package_preflight_rejects_mismatched_outer_end(self):
        source = (
            "CREATE OR REPLACE PACKAGE BODY credentials_pkg AS\n"
            "  PROCEDURE create_schema IS BEGIN NULL; END create_schema;\n"
            "END wrong_pkg;\n/\n"
        )
        result = validate_file(
            "generated.sql", source, "sql", dialect_hint="Oracle PL/SQL",
        )
        self.assertFalse(result.passed)
        self.assertIn("use END credentials_pkg", " ".join(result.diagnostics))

    def test_oracle_package_preflight_rejects_direct_ddl_but_accepts_dynamic_ddl(self):
        direct = (
            "CREATE OR REPLACE PACKAGE BODY credentials_pkg AS\n"
            "  PROCEDURE create_schema IS BEGIN\n"
            "    CREATE TABLE users (id NUMBER);\n"
            "  END create_schema;\n"
            "END credentials_pkg;\n/\n"
        )
        dynamic = direct.replace(
            "CREATE TABLE users (id NUMBER);",
            "EXECUTE IMMEDIATE 'CREATE TABLE users (id NUMBER)';",
        )
        failed = validate_file(
            "generated.sql", direct, "sql", dialect_hint="Oracle PL/SQL",
        )
        passed = validate_file(
            "generated.sql", dynamic, "sql", dialect_hint="Oracle PL/SQL",
        )
        self.assertFalse(failed.passed)
        self.assertIn("EXECUTE IMMEDIATE", " ".join(failed.diagnostics))
        self.assertTrue(passed.passed, passed.diagnostics)

    def test_oracle_package_parser_does_not_require_sqlplus_slash_command(self):
        source = (
            "CREATE OR REPLACE PACKAGE BODY credentials_pkg AS\n"
            "  PROCEDURE create_schema IS BEGIN NULL; END create_schema;\n"
            "END credentials_pkg;\n"
        )
        result = validate_file(
            "generated.sql", source, "sql", dialect_hint="Oracle PL/SQL",
        )
        self.assertTrue(result.passed, result.diagnostics)

    def test_oracle_sqlplus_rem_comments_are_parser_compatible(self):
        source = (
            "REM ============================================================\n"
            "REM Oracle deployment schema\n"
            "SET SERVEROUTPUT ON SIZE UNLIMITED\n"
            "VARIABLE p_account_id NUMBER\n"
            "EXEC :p_account_id := 1;\n"
            "CREATE TABLE accounts (\n"
            "  account_id NUMBER PRIMARY KEY,\n"
            "  amount NUMBER(15, 2) NOT NULL\n"
            ");\n"
        )

        result = validate_file(
            "generated.sql", source, "sql", dialect_hint="Oracle PL/SQL",
        )

        self.assertTrue(result.passed, result.diagnostics)

    def test_oracle_sqlplus_rem_does_not_hide_later_invalid_sql(self):
        source = (
            "REM Sample data follows\n"
            "SELECT FROM;\n"
        )

        result = validate_file(
            "generated.sql", source, "sql", dialect_hint="Oracle PL/SQL",
        )

        self.assertFalse(result.passed)
        self.assertNotIn("REM Sample data", " ".join(result.diagnostics))

    def test_oracle_preflight_rejects_replace_table_and_bind_variable_view(self):
        source = (
            "CREATE OR REPLACE TABLE ACCOUNTS (account_id NUMBER PRIMARY KEY);\n"
            "CREATE OR REPLACE VIEW V_BALANCE AS\n"
            "SELECT account_id FROM ACCOUNTS WHERE account_id = :p_account_id;\n"
        )
        result = validate_file(
            "generated.sql", source, "sql", dialect_hint="Oracle PL/SQL",
        )

        self.assertFalse(result.passed)
        diagnostics = " ".join(result.diagnostics)
        self.assertIn("CREATE OR REPLACE TABLE", diagnostics)
        self.assertIn("views cannot contain bind variables", diagnostics)

    def test_oracle_preflight_rejects_malformed_insert_all_and_ddl_slash(self):
        source = (
            "CREATE TABLE accounts (account_id NUMBER PRIMARY KEY);\n"
            "/\n"
            "INSERT ALL\n"
            "  SELECT account_id => :p_account_id FROM DUAL\n"
            "SELECT 1 FROM DUAL;\n"
        )

        result = validate_file(
            "generated.sql", source, "sql", dialect_hint="Oracle PL/SQL",
        )

        self.assertFalse(result.passed)
        diagnostics = " ".join(result.diagnostics)
        self.assertIn("INTO table (columns) VALUES", diagnostics)
        self.assertIn("named association", diagnostics)
        self.assertIn("follows ordinary SQL", diagnostics)

    def test_oracle_preflight_accepts_valid_insert_all_and_plsql_separator(self):
        source = (
            "INSERT ALL\n"
            "  INTO accounts (account_id, amount) VALUES (1, 10.00)\n"
            "  INTO accounts (account_id, amount) VALUES (2, 20.00)\n"
            "SELECT 1 FROM DUAL;\n"
            "CREATE OR REPLACE PROCEDURE p AS BEGIN NULL; END;\n"
            "/\n"
        )

        result = validate_file(
            "generated.sql", source, "sql", dialect_hint="Oracle PL/SQL",
        )

        self.assertTrue(result.passed, result.diagnostics)

    def test_oracle_preflight_reports_unterminated_block_comment_at_source_line(self):
        result = validate_file(
            "database/generated.sql",
            "SELECT 1 FROM DUAL;\n/* example intentionally documented\nSELECT 2 FROM DUAL;\n",
            "sql",
            dialect_hint="oracle",
        )

        self.assertFalse(result.passed)
        self.assertTrue(
            any("line 2: unterminated SQL block comment" in item for item in result.diagnostics),
            result.diagnostics,
        )

    def test_oracle_preflight_localizes_truncated_stored_procedure(self):
        result = validate_file(
            "database/generated.sql",
            "CREATE OR REPLACE PROCEDURE print_summary IS\n"
            "BEGIN\n"
            "  DBMS_OUTPUT.PUT_LINE('unfinished' ||\n"
            "SELECT 1 FROM DUAL;\n",
            "sql",
            dialect_hint="oracle",
        )

        self.assertFalse(result.passed)
        self.assertTrue(
            any(
                "oracle PL/SQL procedure print_summary is incomplete" in item
                and "missing outer END print_summary" in item
                for item in result.diagnostics
            ),
            result.diagnostics,
        )

    def test_oracle_preflight_accepts_named_and_unnamed_stored_unit_endings(self):
        named = validate_file(
            "database/named.sql",
            "CREATE OR REPLACE PROCEDURE p_named IS\n"
            "BEGIN NULL; END p_named;\n/\n",
            "sql",
            dialect_hint="oracle",
        )
        unnamed = validate_file(
            "database/unnamed.sql",
            "CREATE OR REPLACE PROCEDURE p_unnamed IS BEGIN NULL; END;",
            "sql",
            dialect_hint="oracle",
        )

        self.assertTrue(named.passed, named.diagnostics)
        self.assertTrue(unnamed.passed, unnamed.diagnostics)

    # Function: test_plain_varchar_is_not_mistaken_for_sql_server_dialect
    def test_plain_varchar_is_not_mistaken_for_sql_server_dialect(self):
        # Plain VARCHAR(n)/IDENTITY-as-sequence-options are standard ANSI/
        # Postgres syntax, not T-SQL-exclusive — a postgres-targeted schema
        # built entirely of these must not trip the dialect-mismatch check.
        source = (
            "CREATE TABLE IF NOT EXISTS Accounts (\n"
            "    Id INTEGER GENERATED ALWAYS AS IDENTITY (START WITH 1 INCREMENT BY 1) PRIMARY KEY,\n"
            "    AccountNumber VARCHAR(50) NOT NULL UNIQUE,\n"
            "    CreatedAt TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
            ");"
        )
        result = validate_file(
            "generated.sql", source, "sql", dialect_hint="postgres",
        )
        self.assertTrue(result.passed, result.diagnostics)

    # Function: test_malformed_sql_fails_in_every_configured_dialect
    def test_malformed_sql_fails_in_every_configured_dialect(self):
        for dialect in (
            "ANSI SQL", "PostgreSQL", "SQL Server", "Oracle", "MySQL",
            "IBM Db2", "SQLite", "BigQuery", "Snowflake",
        ):
            with self.subTest(dialect=dialect):
                result = validate_file(
                    "generated.sql", "SELECT FROM;", "sql", dialect_hint=dialect,
                )
                self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
