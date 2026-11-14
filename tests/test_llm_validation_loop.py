# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — tests (test_llm_validation_loop.py)
# Date: 2026-01-17
# ---------------------------------------------------------------------------
import unittest
from unittest.mock import patch

from services.modernizer import (
    _detect_domain_requirements,
    generate_from_prompt,
    _generate_validated,
    _unresolved_requirement_placeholders,
)
from services.modernizer import _single_file_extension
from services.validators import ValidationResult


class LlmValidationLoopTests(unittest.TestCase):
    # Function: test_requirement_placeholders_are_distinguished_from_html
    def test_requirement_placeholders_are_distinguished_from_html(self):
        prompt = "PROGRAM-ID <PROGRAM-ID>; render <div class=\"app\">ok</div>; input <file name and LRECL>"
        self.assertEqual(
            ["<PROGRAM-ID>", "<file name and LRECL>"],
            _unresolved_requirement_placeholders(prompt),
        )

    # Function: test_generic_ledger_name_does_not_enable_money_transfer_pack
    def test_generic_ledger_name_does_not_enable_money_transfer_pack(self):
        self.assertEqual(
            "",
            _detect_domain_requirements(
                "Build HealthLedger with entries containing descriptions and amount_cents"
            ),
        )
        self.assertIn(
            "ATOMIC",
            _detect_domain_requirements(
                "Build a bank account money transfer API that debits and credits balances"
            ),
        )

    # Function: test_file_only_language_cannot_claim_project_readiness
    def test_rust_has_a_dependency_aware_project_route(self):
        from services.build_runner import PRODUCTION_PROJECT_BUILD_LANGUAGES
        from services.modernizer.scaffolds.polyglot import generate_polyglot_project

        self.assertIn("rust", PRODUCTION_PROJECT_BUILD_LANGUAGES)
        files = generate_polyglot_project("rust", "Demo", "Orders", {})
        self.assertTrue(any(path.endswith("Cargo.toml") for path in files))
        self.assertTrue(any(path.endswith(".rs") for path in files))

    # Function: test_single_file_extension_uses_tsx_for_react_jsx
    def test_single_file_extension_uses_tsx_for_react_jsx(self):
        source = (
            "import React from 'react';\n"
            "export default function App() {\n"
            "  return (<main><h1>Hello</h1></main>);\n"
            "}\n"
        )
        self.assertEqual(".tsx", _single_file_extension("typescript", source))

    # Function: test_single_file_extension_keeps_plain_typescript_as_ts
    def test_single_file_extension_keeps_plain_typescript_as_ts(self):
        source = "export const add = (left: number, right: number): number => left + right;\n"
        self.assertEqual(".ts", _single_file_extension("typescript", source))

    def test_single_file_extension_preserves_svg_for_xml_validation(self):
        source = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="8"/></svg>\n'
        self.assertEqual(".svg", _single_file_extension("svg", source))

    @patch("services.validators.validate_file")
    @patch("services.llm.generate")
    def test_guarded_react_null_render_does_not_consume_repair_attempt(self, generate, validate):
        component = (
            "export function ShashiPage({ visible }: { visible: boolean }) {\n"
            "  if (!visible) {\n"
            "    return null;\n"
            "  }\n"
            "  return <main>Shashi</main>;\n"
            "}\n"
        )
        generate.return_value = component
        validate.return_value = ValidationResult(
            "ShashiPage.tsx", "typescript", "compiler", True, [],
        )

        content, result, attempts = _generate_validated(
            "Generate Shashi page", model="test-model", system="react",
            max_tokens=512, num_ctx=2048, rel_path="ShashiPage.tsx",
            language="typescript", max_attempts=2,
        )

        self.assertEqual(component, content)
        self.assertTrue(result.passed)
        self.assertEqual(1, attempts)
        generate.assert_called_once()

    @patch("services.validators.validate_file")
    @patch("services.llm.generate")
    def test_unconditional_null_only_component_still_enters_repair(self, generate, validate):
        stub = "export function ShashiPage() {\n  return null;\n}\n"
        complete = "export function ShashiPage() {\n  return <main>Shashi</main>;\n}\n"
        generate.side_effect = [stub, complete]
        validate.return_value = ValidationResult(
            "ShashiPage.tsx", "typescript", "compiler", True, [],
        )

        content, result, attempts = _generate_validated(
            "Generate Shashi page", model="test-model", system="react",
            max_tokens=512, num_ctx=2048, rel_path="ShashiPage.tsx",
            language="typescript", max_attempts=2,
        )

        self.assertEqual(complete, content)
        self.assertTrue(result.passed)
        self.assertEqual(2, attempts)
        self.assertEqual(2, generate.call_count)
        self.assertIn("incomplete placeholder", generate.call_args.args[0])

    @patch("services.validators.validate_file")
    @patch("services.llm.generate")
    def test_truncated_tsx_repair_uses_expanded_full_file_budget(self, generate, validate):
        from services.modernizer._shared import _TOKENS_XLARGE

        truncated = "export function ShashiPage() { return <div><section><input>"
        complete = (
            "export function ShashiPage() {\n"
            "  return <div><section><input aria-label=\"search\" /></section></div>;\n"
            "}\n"
        )
        generate.side_effect = [truncated, complete]
        validate.side_effect = [
            ValidationResult(
                "ShashiPage.tsx", "typescript", "compiler", False,
                ["TS17008: JSX element 'div' has no corresponding closing tag."],
            ),
            ValidationResult("ShashiPage.tsx", "typescript", "compiler", True, []),
        ]

        content, result, attempts = _generate_validated(
            "Generate Shashi page", model="test-model", system="react-system",
            max_tokens=6000, num_ctx=8192, rel_path="ShashiPage.tsx",
            language="typescript", max_attempts=3,
        )

        self.assertEqual(complete, content)
        self.assertTrue(result.passed)
        self.assertEqual(2, attempts)
        self.assertEqual(_TOKENS_XLARGE, generate.call_args.kwargs["max_tokens"])
        self.assertIn("TRUNCATION RECOVERY", generate.call_args.args[0])
        self.assertIn("Rewrite the entire file from the beginning", generate.call_args.args[0])

    @patch("services.validators.validate_file")
    @patch("services.llm.generate")
    def test_repeated_truncation_diagnostic_can_reach_third_attempt(self, generate, validate):
        truncated = "export function Page() { return <div>"
        complete = "export function Page() { return <div />; }\n"
        generate.side_effect = [truncated, truncated + "<section>", complete]
        repeated = ValidationResult(
            "Page.tsx", "typescript", "compiler", False,
            ["TS17008: JSX element 'div' has no corresponding closing tag."],
        )
        validate.side_effect = [
            repeated, repeated,
            ValidationResult("Page.tsx", "typescript", "compiler", True, []),
        ]

        content, result, attempts = _generate_validated(
            "Generate page", model="test-model", system="react",
            max_tokens=6000, num_ctx=8192, rel_path="Page.tsx",
            language="typescript", max_attempts=3,
        )

        self.assertEqual(complete, content)
        self.assertTrue(result.passed)
        self.assertEqual(3, attempts)
        self.assertEqual(3, generate.call_count)

    @patch("services.llm.generate")
    def test_runtime_exception_is_promoted_out_of_mixed_type_import_before_validation(self, generate):
        generate.return_value = (
            "import type { TrainException, HistoryBean } from '../types/contracts';\n"
            "export function decode(value: HistoryBean): HistoryBean {\n"
            "  if (!value) throw new TrainException(400, 'BAD_REQUEST', 'Missing value');\n"
            "  return value;\n"
            "}\n"
        )

        content, result, attempts = _generate_validated(
            "Generate client", model="test-model", system="typescript",
            max_tokens=512, num_ctx=2048, rel_path="UserApiClient.ts",
            language="typescript", max_attempts=2,
        )

        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual(1, attempts)
        self.assertIn("import { TrainException }", content)
        self.assertIn("import type { HistoryBean }", content)
        generate.assert_called_once()

    def test_runtime_import_repair_handles_default_namespace_inline_and_type_only_bindings(self):
        from services.modernizer.validation_orchestration import (
            _repair_typescript_runtime_type_imports,
        )

        source = (
            "import type Failure from './Failure';\n"
            "import type * as Status from './Status';\n"
            "import { type Request, type ErrorCode, regular } from './contracts';\n"
            "const failure = new Failure('bad');\n"
            "const code = Status.BAD_REQUEST;\n"
            "const selected = ErrorCode.INVALID;\n"
            "export function send(request: Request) { return regular(request, failure, code, selected); }\n"
        )

        repaired = _repair_typescript_runtime_type_imports(source)

        self.assertIn("import Failure from './Failure';", repaired)
        self.assertIn("import * as Status from './Status';", repaired)
        self.assertIn("import { regular, ErrorCode } from './contracts';", repaired)
        self.assertIn("import type { Request } from './contracts';", repaired)

    # Function: test_cobol_is_generated_then_repaired_by_llm_from_compiler_diagnostics
    @patch("services.validators.validate_file")
    @patch("services.llm.generate")
    @patch("services.llm.pick_compiler_repair_model", return_value="strong-repair-model")
    def test_cobol_is_generated_then_repaired_by_llm_from_compiler_diagnostics(
        self, pick_repair_model, generate, validate
    ):
        generate.side_effect = ["invalid cobol", ">>SOURCE FORMAT FREE\nIDENTIFICATION DIVISION.\n"]
        validate.side_effect = [
            ValidationResult("generated.cob", "cobol", "compiler", False, ["missing FD INPUT-FILE"]),
            ValidationResult("generated.cob", "cobol", "compiler", True, []),
        ]

        content, result, attempts = _generate_validated(
            "Generate a COBOL flat-file reader",
            model="test-model",
            system="portable GnuCOBOL",
            max_tokens=512,
            num_ctx=2048,
            rel_path="generated.cob",
            language="cobol",
            max_attempts=5,
        )

        self.assertTrue(result.passed)
        self.assertEqual(2, attempts)
        self.assertEqual(2, generate.call_count)
        self.assertEqual(2, validate.call_count)
        pick_repair_model.assert_called_once_with("test-model")
        self.assertEqual("strong-repair-model", generate.call_args.kwargs["model"])
        self.assertIs(False, generate.call_args.kwargs["think"])
        self.assertIn("missing FD INPUT-FILE", generate.call_args.args[0])
        self.assertIn("COBOL compiler rules", generate.call_args.args[0])
        self.assertIn("MANDATORY REPLACEMENTS", generate.call_args.args[0])
        self.assertIn(">>SOURCE FORMAT FREE", content)


if __name__ == "__main__":
    unittest.main()
