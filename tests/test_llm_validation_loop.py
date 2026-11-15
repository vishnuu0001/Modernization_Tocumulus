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

    def test_typescript_assertions_are_not_misclassified_as_jsx(self):
        from services.validators import _contains_jsx, validate_file

        source = "export const decode = (value: unknown): string => <string>value;\n"
        self.assertFalse(_contains_jsx(source))
        self.assertTrue(validate_file("AuthService.ts", source, "typescript").passed)

    def test_actual_jsx_still_requires_a_tsx_path(self):
        from services.validators import _contains_jsx, validate_file

        source = "export const AuthView = () => <main>Authentication</main>;\n"
        self.assertTrue(_contains_jsx(source))
        result = validate_file("AuthService.ts", source, "typescript")
        self.assertFalse(result.passed)
        self.assertTrue(any("TS17004" in item for item in result.diagnostics))

    @patch("services.llm.generate")
    def test_plain_ts_jsx_repair_discards_wrong_component_role(self, generate):
        generated_component = "export const login = () => <main>Login</main>;\n"
        generated_transport = (
            "export async function login(): Promise<string> {\n"
            "  return 'token';\n"
            "}\n"
        )
        generate.side_effect = [generated_component, generated_transport]

        content, result, attempts = _generate_validated(
            "FILE CONTRACT: export a non-React login transport function",
            model="test-model", system="typescript", max_tokens=1024, num_ctx=4096,
            rel_path="frontend/src/api/auth.api.ts", language="typescript",
            max_attempts=3, forbid_jsx=True,
        )

        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual(2, attempts)
        self.assertEqual(generated_transport, content)
        repair_prompt = generate.call_args_list[1].args[0]
        self.assertIn("PREVIOUS ATTEMPT USED THE WRONG FILE ROLE", repair_prompt)
        self.assertIn("Discard the previous React/JSX attempt completely", repair_prompt)
        self.assertNotIn(generated_component.strip(), repair_prompt)

    @patch("services.validators.validate_file")
    @patch("services.llm.generate")
    def test_plain_ts_placeholder_repair_reauthors_from_contract(self, generate, validate):
        stub = (
            "export async function loadUser(): Promise<User> {\n"
            "  return null;\n"
            "}\n"
        )
        complete = (
            "export async function loadUser(): Promise<User> {\n"
            "  const response = await fetch('/api/users/me');\n"
            "  if (!response.ok) throw new Error(`User request failed: ${response.status}`);\n"
            "  return response.json() as Promise<User>;\n"
            "}\n"
        )
        generate.side_effect = [stub, complete]
        validate.return_value = ValidationResult(
            "frontend/src/api/user.service.client.ts", "typescript", "compiler", True, [],
        )

        content, result, attempts = _generate_validated(
            "FILE CONTRACT: implement loadUser as a typed HTTP transport operation",
            model="test-model", system="typescript", max_tokens=2048, num_ctx=4096,
            rel_path="frontend/src/api/user.service.client.ts", language="typescript",
            max_attempts=3, forbid_jsx=True,
        )

        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual(2, attempts)
        self.assertEqual(complete, content)
        repair_prompt = generate.call_args_list[1].args[0]
        self.assertIn("PREVIOUS ATTEMPT WAS AN INCOMPLETE TRANSPORT STUB", repair_prompt)
        self.assertIn("Never return null as a stub", repair_prompt)
        self.assertNotIn(stub.strip(), repair_prompt)

    @patch("services.validators.validate_file")
    @patch("services.llm.generate")
    def test_guarded_nullable_transport_result_does_not_trigger_repair(self, generate, validate):
        complete = (
            "export async function findCustomer(id: string): Promise<Customer | null> {\n"
            "  const response = await fetch(`/api/customers/${encodeURIComponent(id)}`);\n"
            "  if (response.status === 404) return null;\n"
            "  if (!response.ok) throw new Error(`Lookup failed: ${response.status}`);\n"
            "  return response.json() as Promise<Customer>;\n"
            "}\n"
        )
        generate.return_value = complete
        validate.return_value = ValidationResult(
            "frontend/CustomerService.ts", "typescript", "compiler", True, [],
        )

        content, result, attempts = _generate_validated(
            "FILE CONTRACT: implement a nullable customer lookup",
            model="test-model", system="typescript", max_tokens=2048, num_ctx=4096,
            rel_path="frontend/CustomerService.ts", language="typescript",
            max_attempts=3, forbid_jsx=True,
        )

        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual(1, attempts)
        self.assertEqual(complete, content)
        generate.assert_called_once()

    @patch("services.llm.generate")
    def test_plain_ts_eof_parenthesis_failure_reauthors_complete_module(self, generate):
        from services.modernizer._shared import _TOKENS_XLARGE

        truncated = (
            "export const trainApi = (() => {\n"
            "  return { list: async () => [] };\n"
            "};\n"
        )
        complete = (
            "export const trainApi = (() => {\n"
            "  return { list: async (): Promise<unknown[]> => [] };\n"
            "})();\n"
        )
        generate.side_effect = [truncated, complete]

        content, result, attempts = _generate_validated(
            "FILE CONTRACT: export a bounded train API client",
            model="test-model", system="typescript", max_tokens=2048, num_ctx=4096,
            rel_path="frontend/src/api/train.ts", language="typescript",
            max_attempts=3, forbid_jsx=True,
        )

        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual(2, attempts)
        self.assertEqual(complete, content)
        self.assertEqual(_TOKENS_XLARGE, generate.call_args.kwargs["max_tokens"])
        repair_prompt = generate.call_args_list[1].args[0]
        self.assertIn("PREVIOUS ATTEMPT WAS STRUCTURALLY INCOMPLETE", repair_prompt)
        self.assertNotIn(truncated.strip(), repair_prompt)

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
        self.assertIn(
            "PREVIOUS ATTEMPT WAS STRUCTURALLY INCOMPLETE",
            generate.call_args.args[0],
        )
        self.assertNotIn(truncated, generate.call_args.args[0])
        self.assertNotIn("Fix ONLY the reported issues", generate.call_args.args[0])

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

    @patch("services.validators.validate_file")
    @patch("services.llm.generate")
    def test_repeated_typescript_parser_diagnostic_can_reach_third_attempt(self, generate, validate):
        invalid = "export function load(): Promise<void> => { return Promise.resolve(); }\n"
        complete = "export function load(): Promise<void> { return Promise.resolve(); }\n"
        generate.side_effect = [invalid, invalid, complete]
        repeated = ValidationResult(
            "CustomerService.ts", "typescript", "compiler", False,
            ["line 1:47: TS1144: '{' or ';' expected."],
        )
        validate.side_effect = [
            repeated,
            repeated,
            ValidationResult("CustomerService.ts", "typescript", "compiler", True, []),
        ]

        content, result, attempts = _generate_validated(
            "Generate customer API", model="test-model", system="typescript",
            max_tokens=6144, num_ctx=8192, rel_path="CustomerService.ts",
            language="typescript", max_attempts=3,
        )

        self.assertEqual(complete, content)
        self.assertTrue(result.passed)
        self.assertEqual(3, attempts)
        self.assertEqual(3, generate.call_count)

    def test_typescript_declaration_arrow_repair_is_scoped(self):
        from services.modernizer.validation_orchestration import (
            _repair_typescript_declaration_arrows,
        )

        source = (
            "export async function load(): Promise<void> => { return; }\n"
            "export class Api {\n"
            "  async save(): Promise<void> => { return; }\n"
            "  remove = async (): Promise<void> => { return; };\n"
            "}\n"
            "useEffect(() => { return undefined; }, []);\n"
            "export const parse = (value: string): string => value;\n"
        )
        repaired = _repair_typescript_declaration_arrows(source)

        self.assertIn("function load(): Promise<void> {", repaired)
        self.assertIn("async save(): Promise<void> {", repaired)
        self.assertIn("remove = async (): Promise<void> =>", repaired)
        self.assertIn("useEffect(() => {", repaired)
        self.assertIn("const parse = (value: string): string =>", repaired)
        from services.validators import validate_file
        validation = validate_file("CustomerService.ts", repaired, "typescript")
        self.assertTrue(validation.passed, validation.diagnostics)

    def test_typescript_parser_diagnostics_retain_source_location(self):
        from services.validators import validate_file

        result = validate_file(
            "CustomerService.ts",
            "export function load(): Promise<void> => { return Promise.resolve(); }\n",
            "typescript",
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("line 1:" in item and "TS1144" in item for item in result.diagnostics))

    def test_typescript_component_repairs_cover_missing_arrow_and_invalid_default_const(self):
        from services.modernizer.validation_orchestration import (
            _repair_typescript_source,
            _repair_typescript_unmatched_export_prefix,
        )
        from services.validators import validate_file

        missing_arrow = (
            "export const CustomerPage = (name: string): JSX.Element {\n"
            "  return <main>{name}</main>;\n"
            "}\n"
        )
        repaired_customer = _repair_typescript_source(missing_arrow)
        self.assertIn("): JSX.Element => {", repaired_customer)
        self.assertTrue(
            validate_file("CustomerPage.tsx", repaired_customer, "typescript").passed
        )

        auth_tail = (
            "const AuthForm = () => <main>Authentication</main>;\n"
            "}; export default const AuthPage = AuthForm;\n"
        )
        normalized_auth = _repair_typescript_source(auth_tail)
        first_result = validate_file("AuthPage.tsx", normalized_auth, "typescript")
        self.assertFalse(first_result.passed)
        self.assertTrue(any("TS1128" in item for item in first_result.diagnostics))
        repaired_auth = _repair_typescript_unmatched_export_prefix(
            normalized_auth, first_result.diagnostics,
        )
        self.assertTrue(
            validate_file("AuthPage.tsx", repaired_auth, "typescript").passed
        )
        self.assertIn("export const AuthPage", repaired_auth)
        self.assertIn("export default AuthPage;", repaired_auth)

    def test_malformed_component_tail_is_replaced_from_compiler_location(self):
        from services.modernizer.validation_orchestration import (
            _repair_typescript_malformed_default_export,
        )
        from services.validators import validate_file

        source = (
            "export const CustomerPage = () => <main>Customer</main>;\n"
            "}; export default CustomerPage = CustomerPage;\n"
        )
        repaired = _repair_typescript_malformed_default_export(
            source,
            [
                "line 2:1: TS1128: Declaration or statement expected.",
                "line 2:20: TS1109: Expression expected.",
            ],
            "ModernizedApp/src/components/Customer/CustomerPage.tsx",
        )

        self.assertEqual(
            "export const CustomerPage = () => <main>Customer</main>;\n"
            "export default CustomerPage;\n",
            repaired,
        )
        result = validate_file("CustomerPage.tsx", repaired, "typescript")
        self.assertTrue(result.passed, result.diagnostics)

    @patch("services.llm.generate")
    def test_typed_react_handler_arrows_and_malformed_tail_repair_together(self, generate):
        generate.return_value = (
            "import type React from 'react';\n"
            "const submit: React.FormEventHandler<HTMLFormElement> = async (event) {\n"
            "  event.preventDefault();\n"
            "};\n"
            "const load: () => Promise<void> = async () { return; };\n"
            "const CustomerView = () => <main>Customer</main>;\n"
            "}; export default = CustomerView;\n"
        )

        content, result, attempts = _generate_validated(
            "Generate customer page", model="test-model", system="react",
            max_tokens=4096, num_ctx=8192, rel_path="CustomerPage.tsx",
            language="typescript", max_attempts=3,
        )

        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual(1, attempts)
        self.assertIn("async (event) => {", content)
        self.assertIn("async () => {", content)
        self.assertIn("export default CustomerView;", content)
        generate.assert_called_once()

    def test_validation_failure_excerpt_is_bounded_to_diagnostic_lines(self):
        from services.modernizer.validation_orchestration import (
            _validation_failure_excerpt,
        )

        source = "\n".join(f"line {index}" for index in range(1, 20))
        excerpt = _validation_failure_excerpt(
            source,
            ["line 10:16: TS1005: '=>' expected."],
        )

        self.assertEqual("9: line 9 | 10: line 10 | 11: line 11", excerpt)
        self.assertNotIn("line 1 |", excerpt)

        no_location = _validation_failure_excerpt(
            "first\nsecond\nthird\nfourth\nfifth\nsixth\nseventh\neighth\n",
            ["TS17004: JSX syntax requires a .tsx filename"],
        )
        self.assertIn("1: first", no_location)
        self.assertIn("8: eighth", no_location)

    def test_compiler_guided_arrow_repair_handles_react_hook_callback(self):
        from services.modernizer.validation_orchestration import (
            _repair_typescript_missing_arrows_from_diagnostics,
        )
        from services.validators import validate_file

        source = (
            "import { useEffect } from 'react';\n"
            "export const CustomerPage = () => {\n"
            "  useEffect(() {\n"
            "    return undefined;\n"
            "  }, []);\n"
            "  return <main>Customer</main>;\n"
            "};\n"
        )
        first = validate_file("CustomerPage.tsx", source, "typescript")
        self.assertEqual(["line 3:16: TS1005: '=>' expected."], first.diagnostics)

        repaired = _repair_typescript_missing_arrows_from_diagnostics(
            source, first.diagnostics,
        )

        self.assertIn("useEffect(() => {", repaired)
        final = validate_file("CustomerPage.tsx", repaired, "typescript")
        self.assertTrue(final.passed, final.diagnostics)

    def test_compiler_guided_arrow_repair_handles_tab_display_column(self):
        from services.modernizer.validation_orchestration import (
            _repair_typescript_missing_arrows_from_diagnostics,
        )

        source = "\tuseEffect(() {\n\t\treturn undefined;\n\t}, []);\n"
        repaired = _repair_typescript_missing_arrows_from_diagnostics(
            source,
            ["line 1:16: TS1005: '=>' expected."],
        )

        self.assertIn("\tuseEffect(() => {", repaired)

    def test_source_cleaner_removes_orphan_governance_protocol_tags(self):
        from services.modernizer.validation_orchestration import _clean_generated_content
        from services.validators import validate_file

        source = (
            "import { useEffect } from 'react';\n"
            "export const AuthContext = () => {\n"
            "  useEffect(() => {}, []);\n"
            "  return <main>Authenticated</main>;\n"
            "};\n"
            "</generation_notes>\n"
        )

        cleaned = _clean_generated_content(source)

        self.assertNotIn("generation_notes", cleaned)
        result = validate_file("AuthContext.tsx", cleaned, "typescript")
        self.assertTrue(result.passed, result.diagnostics)

    @patch("services.llm.generate")
    def test_auth_context_hook_and_orphan_notes_repair_in_one_attempt(self, generate):
        generate.return_value = (
            "import { useEffect } from 'react';\n"
            "export const AuthContext = () => {\n"
            "  useEffect(() {\n"
            "    window.localStorage.getItem('auth-token');\n"
            "  }, []);\n"
            "  return <main>Authenticated</main>;\n"
            "};\n"
            "</generation_notes>\n"
        )

        content, result, attempts = _generate_validated(
            "Generate auth context", model="test-model", system="react",
            max_tokens=4096, num_ctx=8192, rel_path="AuthContext.tsx",
            language="typescript", max_attempts=3,
        )

        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual(1, attempts)
        self.assertIn("useEffect(() => {", content)
        self.assertNotIn("generation_notes", content)
        generate.assert_called_once()

    @patch("services.llm.generate")
    def test_generate_validated_accepts_deterministically_repaired_tsx_without_llm_retry(self, generate):
        generate.return_value = (
            "const AuthForm = () => <main>Authentication</main>;\n"
            "}; export default const AuthPage = AuthForm;\n"
        )

        content, result, attempts = _generate_validated(
            "Generate auth page", model="test-model", system="react",
            max_tokens=2048, num_ctx=4096, rel_path="AuthPage.tsx",
            language="typescript", max_attempts=3,
        )

        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual(1, attempts)
        self.assertIn("export default AuthPage;", content)
        generate.assert_called_once()

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
