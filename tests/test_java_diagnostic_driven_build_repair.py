import inspect
import unittest
from unittest.mock import patch

from services.modernizer.build_artifacts import (
    _add_known_java_imports,
    _apply_deterministic_java_diagnostic_repairs,
    _java_lexical_tail_state,
    _reconcile_jboss_logging_placeholders,
    _reconcile_java_logger_api_from_diagnostics,
    _reconcile_java_entity_update_copy_contracts,
    _reconcile_java_missing_well_known_imports_from_diagnostics,
    _reconcile_java_private_access_from_diagnostics,
    _reconcile_java_static_invocations_from_diagnostics,
    _reconcile_java_transactional_import_conflicts_from_diagnostics,
    _remove_java_invalid_type_imports_from_diagnostics,
)
from services.modernizer._shared import _TOKENS_COMPONENT, _TOKENS_XLARGE
from services.modernizer.prompt_pipeline import (
    _pf_expand_java_provider_diagnostics,
    _pf_java_has_blocking_source_diagnostics,
    _pf_repair_build_round,
    _pf_run_build_and_repair,
)


class JavaEntityUpdateContractTests(unittest.TestCase):
    def test_generic_name_update_is_replaced_with_source_grounded_entity_fields(self):
        entity_path = (
            "ModernizedApp/backend/shashi-service/src/main/java/"
            "com/comshashi/shashi/model/Shashi.java"
        )
        service_path = (
            "ModernizedApp/backend/shashi-service/src/main/java/"
            "com/comshashi/shashi/service/ShashiServiceImpl.java"
        )
        output = {
            entity_path: (
                "package com.comshashi.shashi.model;\n"
                "@jakarta.persistence.Entity\n"
                "public class Shashi {\n"
                "  private Long id;\n"
                "  private String transId;\n"
                "  private String mailId;\n"
                "  private Integer seats;\n"
                "  private Boolean isActive = true;\n"
                "  private java.time.Instant createdAt;\n"
                "}\n"
            ),
            service_path: (
                "package com.comshashi.shashi.service;\n"
                "import com.comshashi.shashi.model.Shashi;\n"
                "class ShashiServiceImpl {\n"
                "  Shashi update(Long id, Shashi updated) {\n"
                "    java.util.Optional<Shashi> existing = repository.findById(id);\n"
                "    existing.get().setName(updated.getName());\n"
                "    existing.get().setIsActive(updated.getIsActive());\n"
                "    return repository.save(existing.get());\n"
                "  }\n"
                "}\n"
            ),
        }

        _reconcile_java_entity_update_copy_contracts(output)

        repaired = output[service_path]
        self.assertNotIn("setName", repaired)
        self.assertNotIn("getName", repaired)
        self.assertIn("setTransId(updated.getTransId())", repaired)
        self.assertIn("setMailId(updated.getMailId())", repaired)
        self.assertIn("setSeats(updated.getSeats())", repaired)
        self.assertEqual(1, repaired.count("setIsActive(updated.getIsActive())"))
        self.assertNotIn("setId", repaired)
        self.assertNotIn("setCreatedAt", repaired)


class JavaPrivateAccessDiagnosticRepairTests(unittest.TestCase):
    """"<symbol> has private access in <FQCN>" is a precise, unambiguous
    javac diagnostic — widening private -> public for exactly the named
    symbol in exactly the named owner is always behavior-preserving and
    always correct, unlike an LLM repair attempt for the same error."""

    def test_widens_a_private_field_referenced_from_another_class(self):
        entity_path = "ModernizedApp/backend/struct-service/src/main/java/com/mina/struct/model/Struct.java"
        impl_path = "ModernizedApp/backend/struct-service/src/main/java/com/mina/struct/service/StructServiceImpl.java"
        output = {
            entity_path: (
                "package com.mina.struct.model;\n\n"
                "public class Struct {\n"
                "    private Boolean isActive = true;\n"
                "}\n"
            ),
            impl_path: (
                "package com.mina.struct.service;\n\n"
                "import com.mina.struct.model.Struct;\n\n"
                "public class StructServiceImpl {\n"
                "    void touch(Struct s) { boolean b = s.isActive; }\n"
                "}\n"
            ),
        }
        errors_by_file = {
            impl_path: ["isActive has private access in com.mina.struct.model.Struct"],
        }
        changed = _reconcile_java_private_access_from_diagnostics(output, errors_by_file)

        self.assertIn(entity_path, changed)
        self.assertIn("public Boolean isActive = true;", output[entity_path])
        self.assertNotIn("private Boolean isActive", output[entity_path])
        # The consumer file itself is untouched — only the declaring file changes.
        self.assertIn("s.isActive", output[impl_path])

    def test_widens_a_private_nested_exception_class(self):
        path = "ModernizedApp/backend/photoshop-service/src/main/java/com/mina/photoshop/service/PhotoshopService.java"
        output = {
            path: (
                "package com.mina.photoshop.service;\n\n"
                "public class PhotoshopService {\n"
                "    private static class ResourceNotFoundException extends RuntimeException {\n"
                "        ResourceNotFoundException(String m) { super(m); }\n"
                "    }\n"
                "}\n"
            ),
        }
        errors_by_file = {
            path: [
                "com.mina.photoshop.service.PhotoshopService.ResourceNotFoundException has private "
                "access in com.mina.photoshop.service.PhotoshopService",
            ],
        }
        changed = _reconcile_java_private_access_from_diagnostics(output, errors_by_file)

        self.assertIn(path, changed)
        self.assertIn("public static class ResourceNotFoundException", output[path])
        self.assertNotIn("private static class ResourceNotFoundException", output[path])

    def test_no_op_when_diagnostic_does_not_match_the_shape(self):
        output = {"A.java": "package a;\npublic class A { private int x; }\n"}
        changed = _reconcile_java_private_access_from_diagnostics(
            output, {"A.java": ["some other compiler error"]},
        )
        self.assertEqual(changed, set())
        self.assertIn("private int x;", output["A.java"])


class JavaMissingImportDiagnosticRepairTests(unittest.TestCase):
    """A small, fixed set of common Spring Data types (Page, Pageable, ...)
    used in generated controllers/repositories without their import ever
    being added — inject it directly in the file the compiler named."""

    def test_adds_missing_page_import(self):
        path = "ModernizedApp/backend/mina-service/src/main/java/com/mina/mina/controller/MinaController.java"
        output = {
            path: (
                "package com.mina.mina.controller;\n\n"
                "public class MinaController {\n"
                "    Page<String> list() { return null; }\n"
                "}\n"
            ),
        }
        errors_by_file = {
            path: [
                "cannot find symbol — symbol: class Page — location: class "
                "com.mina.mina.controller.MinaController",
            ],
        }
        changed = _reconcile_java_missing_well_known_imports_from_diagnostics(output, errors_by_file)

        self.assertIn(path, changed)
        self.assertIn("import org.springframework.data.domain.Page;", output[path])

    def test_is_idempotent_when_import_already_present(self):
        path = "X.java"
        output = {
            path: (
                "package p;\n\n"
                "import org.springframework.data.domain.Page;\n\n"
                "public class X { Page<String> p; }\n"
            ),
        }
        errors_by_file = {
            path: ["cannot find symbol — symbol: class Page — location: class p.X"],
        }
        changed = _reconcile_java_missing_well_known_imports_from_diagnostics(output, errors_by_file)
        self.assertEqual(changed, set())
        self.assertEqual(output[path].count("import org.springframework.data.domain.Page;"), 1)

    def test_ignores_symbols_outside_the_known_set(self):
        # Must never inject an import for a project's own same-named type.
        path = "X.java"
        output = {path: "package p;\npublic class X { SomeCustomType v; }\n"}
        errors_by_file = {
            path: ["cannot find symbol — symbol: class SomeCustomType — location: class p.X"],
        }
        changed = _reconcile_java_missing_well_known_imports_from_diagnostics(output, errors_by_file)
        self.assertEqual(changed, set())


class JavaCombinedDeterministicRepairTests(unittest.TestCase):
    def test_apply_all_runs_both_passes_independently(self):
        page_path = "A.java"
        access_path = "B.java"
        owner_path = "C.java"
        output = {
            page_path: (
                "package p;\n\npublic class A {\n"
                "    Page<String> list() { return null; }\n}\n"
            ),
            access_path: (
                "package p;\n\nimport p.C;\n\npublic class B {\n"
                "    void f(C c) { boolean b = c.flag; }\n}\n"
            ),
            owner_path: (
                "package p;\n\npublic class C {\n"
                "    private boolean flag;\n}\n"
            ),
        }
        errors_by_file = {
            page_path: ["cannot find symbol — symbol: class Page — location: class p.A"],
            access_path: ["flag has private access in p.C"],
        }
        changed = _apply_deterministic_java_diagnostic_repairs(output, errors_by_file)

        self.assertIn(page_path, changed)
        self.assertIn(owner_path, changed)
        self.assertIn("import org.springframework.data.domain.Page;", output[page_path])
        self.assertIn("public boolean flag;", output[owner_path])


class JavaStructuralDiagnosticRepairTests(unittest.TestCase):
    def test_repairs_all_observed_compiler_corruption_shapes(self):
        train = "Demo/src/main/java/com/example/TrainService.java"
        booking = "Demo/src/main/java/com/example/BookingServiceImpl.java"
        user = "Demo/src/main/java/com/example/UserServiceImpl.java"
        servlet = "Demo/src/main/java/com/example/UserRegServlet.java"
        output = {
            train: (
                "package com.example;\npublic class TrainService {\n"
                "  void broken() { throw new RuntimeException();package com.example;\n"
                "public class TrainService { void fixed() {} }\n"
            ),
            booking: (
                "package com.example;\nimport java.sql.Connection;\nimport java.sql.PreparedStatement;\n"
                "public class BookingServiceImpl { void save() throws Exception {\n"
                "  try {\n    Connection con = dataSource.getConnection();\n"
                "  PreparedStatement ps = con.prepareStatement(\"SELECT 1\");\n"
                "  if (true) { return; } else { throw new RuntimeException(); } finally {\n"
                "    ps.close();\n    con.close();\n  }\n  } catch (Exception error) { throw error; }\n}\n}\n"
            ),
            user: (
                "package com.example;\npublic class UserServiceImpl {\n"
                "  void find(@org.springframework.data.domain.Pageable pageable) {}\n}\n"
            ),
            servlet: (
                "package com.example;\nimport java.io.PrintWriter;\n"
                "public class UserRegServlet { void post() throws Exception {\n"
                "  PrintWriter pw = null;\n  try (pw = response.getWriter()) { pw.flush(); }\n"
                "}\n}\n"
            ),
        }
        errors = {
            train: ["line 3: illegal start of expression", "line 3: not a statement"],
            booking: ["line 5: 'finally' without 'try'"],
            user: ["line 3: <identifier> expected"],
            servlet: [
                "line 4: the try-with-resources resource must either be a variable declaration "
                "or an expression denoting a reference to a final or effectively final variable"
            ],
        }

        changed = _apply_deterministic_java_diagnostic_repairs(output, errors)

        self.assertEqual(set(output), changed)
        self.assertEqual(1, output[train].count("package com.example;"))
        self.assertIn("public class TrainService { void fixed() {} }", output[train])
        self.assertIn("try (Connection con = dataSource.getConnection())", output[booking])
        self.assertIn("try (PreparedStatement ps = con.prepareStatement", output[booking])
        self.assertNotIn("finally", output[booking])
        self.assertIn("org.springframework.data.domain.Pageable pageable", output[user])
        self.assertNotIn("@org.springframework.data.domain.Pageable", output[user])
        self.assertIn("try (PrintWriter pw = response.getWriter())", output[servlet])
        self.assertNotIn("PrintWriter pw = null", output[servlet])
        for path in output:
            with self.subTest(path=path):
                self.assertEqual(("code", 0), _java_lexical_tail_state(output[path]))

    def test_repairs_invalid_message_exception_records_without_touching_domain_records(self):
        path = "Demo/src/main/java/com/example/AuthController.java"
        output = {
            path: (
                "package com.example;\n"
                "public class AuthController {\n"
                "    record ResourceNotFoundException(String message) extends RuntimeException {}\n"
                "    private record DuplicateKeyException(String message) extends RuntimeException {}\n"
                "    record ServiceException(String message, Throwable cause) extends RuntimeException {}\n"
                "    record AuditEvent(String action) {}\n"
                "}\n"
            ),
        }

        changed = _apply_deterministic_java_diagnostic_repairs(
            output, {path: ["line 3: '{' expected", "line 4: '{' expected"]},
        )

        self.assertEqual({path}, changed)
        repaired = output[path]
        self.assertIn(
            "private static final class ResourceNotFoundException extends RuntimeException",
            repaired,
        )
        self.assertIn(
            "private static final class DuplicateKeyException extends RuntimeException",
            repaired,
        )
        self.assertEqual(2, repaired.count("super(message);"))
        self.assertIn("super(message, cause);", repaired)
        self.assertIn("record AuditEvent(String action) {}", repaired)
        self.assertNotIn("record ResourceNotFoundException", repaired)
        self.assertEqual(("code", 0), _java_lexical_tail_state(repaired))

    def test_compiler_blockers_are_distinct_from_follow_up_findings(self):
        self.assertTrue(_pf_java_has_blocking_source_diagnostics([
            "line 129: illegal start of expression",
        ]))
        self.assertFalse(_pf_java_has_blocking_source_diagnostics([
            "missing required import java.util.Set",
            "Broad Exception catch should be narrowed",
        ]))


class JavaLegacyContractDiagnosticRepairTests(unittest.TestCase):
    def test_restores_static_utility_method_used_through_class(self):
        utility = "Demo/src/main/java/p/TrainUtil.java"
        servlet = "Demo/src/main/java/p/LoginServlet.java"
        output = {
            utility: (
                "package p;\npublic class TrainUtil {\n"
                "  public boolean isLoggedIn(String user) { return true; }\n"
                "}\n"
            ),
            servlet: (
                "package p;\npublic class LoginServlet {\n"
                "  boolean check(String user) { return TrainUtil.isLoggedIn(user); }\n"
                "}\n"
            ),
        }
        errors = {servlet: [
            "non-static method isLoggedIn(java.lang.String) cannot be referenced from a static context"
        ]}

        changed = _reconcile_java_static_invocations_from_diagnostics(output, errors)

        self.assertEqual({utility}, changed)
        self.assertIn("public static boolean isLoggedIn", output[utility])

    def test_restores_static_method_called_from_static_method_in_same_file(self):
        utility = "Demo/src/main/java/p/TrainUtil.java"
        output = {utility: (
            "package p;\npublic class TrainUtil {\n"
            "  public String readCookie(String key) { return key; }\n"
            "  public static String current(String key) { return readCookie(key); }\n"
            "}\n"
        )}
        errors = {utility: [
            "non-static method readCookie(java.lang.String) cannot be referenced from a static context"
        ]}

        changed = _reconcile_java_static_invocations_from_diagnostics(output, errors)

        self.assertEqual({utility}, changed)
        self.assertIn("public static String readCookie", output[utility])

    def test_signature_failure_is_also_attributed_to_provider(self):
        caller = "Demo/src/main/java/p/Login.java"
        provider = "Demo/src/main/java/p/TrainUtil.java"
        output = {
            caller: "package p; public class Login { void f() { TrainUtil.login(\"u\"); } }",
            provider: (
                "package p; public class TrainUtil { "
                "public static void login(String u, Object service) {} }"
            ),
        }
        error = "method login in class p.TrainUtil cannot be applied to given types;"

        expanded = _pf_expand_java_provider_diagnostics(
            {caller: [error]}, output, set(),
        )

        self.assertIn(provider, expanded)
        self.assertIn("Caller compatibility failure", expanded[provider][0])

    def test_missing_provider_method_is_attributed_to_provider(self):
        caller = "Demo/src/main/java/p/BookingServiceImpl.java"
        provider = "Demo/src/main/java/p/DBUtil.java"
        output = {
            caller: "package p; class BookingServiceImpl { DBUtil dbUtil; void x(){ dbUtil.createHistory(null); } }",
            provider: "package p; class DBUtil { Object getConnection(){ return null; } }",
        }
        diagnostics = {
            caller: [
                "cannot find symbol — symbol: method createHistory(p.HistoryBean) "
                "— location: variable dbUtil of type p.DBUtil",
            ],
        }

        expanded = _pf_expand_java_provider_diagnostics(diagnostics, output, set())

        self.assertIn(provider, expanded)
        self.assertIn("Caller compatibility failure", expanded[provider][0])

    def test_adds_compiler_proven_arraylist_and_service_imports(self):
        path = "Demo/src/main/java/p/BookingServiceImpl.java"
        output = {path: (
            "package p;\n@Service\npublic class BookingServiceImpl {\n"
            "  ArrayList<String> values;\n}\n"
        )}
        errors = {path: [
            "cannot find symbol — symbol: class ArrayList — location: class p.BookingServiceImpl",
            "cannot find symbol — symbol: class Service — location: class p.BookingServiceImpl",
        ]}

        changed = _reconcile_java_missing_well_known_imports_from_diagnostics(output, errors)

        self.assertEqual({path}, changed)
        self.assertIn("import java.util.ArrayList;", output[path])
        self.assertIn("import org.springframework.stereotype.Service;", output[path])

    def test_removes_compiler_proven_invalid_lowercase_type_import(self):
        path = "Demo/src/main/java/p/ResponseCode.java"
        output = {path: (
            "package p;\nimport p.BookTrainPayment.is;\npublic enum ResponseCode { OK }\n"
        )}
        errors = {path: [
            "cannot find symbol — symbol: class is — location: class p.ResponseCode"
        ]}

        changed = _remove_java_invalid_type_imports_from_diagnostics(output, errors)

        self.assertEqual({path}, changed)
        self.assertNotIn("BookTrainPayment.is", output[path])

    def test_reconciles_compiler_proven_jul_slf4j_api_mismatch(self):
        path = "Demo/src/main/java/p/TrainUtil.java"
        output = {path: (
            "package p;\nimport java.util.logging.Logger;\n"
            "public class TrainUtil {\n"
            "  private static final Logger log = Logger.getLogger(TrainUtil.class.getName());\n"
            "  void fail(Exception ex) { log.error(\"failed\", ex); }\n}\n"
        )}
        errors = {path: [
            "cannot find symbol — symbol: method error(java.lang.String,java.lang.Exception) "
            "— location: variable log of type java.util.logging.Logger"
        ]}

        changed = _reconcile_java_logger_api_from_diagnostics(output, errors)

        self.assertEqual({path}, changed)
        self.assertIn("import org.slf4j.Logger;", output[path])
        self.assertIn("import org.slf4j.LoggerFactory;", output[path])
        self.assertIn("LoggerFactory.getLogger(TrainUtil.class)", output[path])
        self.assertNotIn("java.util.logging.Logger", output[path])

    def test_reconciles_only_parameterized_jboss_logging_calls(self):
        path = "Demo/src/main/java/p/AuthController.java"
        output = {path: (
            "package p;\nimport org.jboss.logging.Logger;\n"
            "public class AuthController {\n"
            "  private static final Logger log = Logger.getLogger(AuthController.class);\n"
            "  void load(Long id, String name, Exception exception) {\n"
            "    log.info(\"Loaded id={}\", id);\n"
            "    log.warn(\"Pair {} {}\", id, name);\n"
            "    log.info(\"Progress 100% id={}\", id);\n"
            "    log.error(\"Request failed\", exception);\n"
            "  }\n}\n"
        )}

        changed = _reconcile_jboss_logging_placeholders(output)

        self.assertEqual({path}, changed)
        self.assertIn('log.infof("Loaded id=%s", id);', output[path])
        self.assertIn('log.warnf("Pair %s %s", id, name);', output[path])
        self.assertIn('log.infof("Progress 100%% id=%s", id);', output[path])
        self.assertIn('log.error("Request failed", exception);', output[path])

    def test_does_not_rewrite_slf4j_or_plain_jboss_calls(self):
        slf4j_path = "Demo/src/main/java/p/Slf4jService.java"
        jboss_path = "Demo/src/main/java/p/JBossService.java"
        output = {
            slf4j_path: (
                "package p;\nimport org.slf4j.Logger;\n"
                "class Slf4jService { Logger log; void load(Long id) { "
                'log.info("Loaded id={}", id); } }\n'
            ),
            jboss_path: (
                "package p;\nimport org.jboss.logging.Logger;\n"
                "class JBossService { private static final Logger log = "
                "Logger.getLogger(JBossService.class); void ready() { "
                'log.info("ready"); } }\n'
            ),
        }
        original = dict(output)

        changed = _reconcile_jboss_logging_placeholders(output)

        self.assertEqual(set(), changed)
        self.assertEqual(original, output)


class JavaTransactionalImportConflictRepairTests(unittest.TestCase):
    def test_known_import_reconciliation_respects_existing_same_simple_name(self):
        source = (
            "package p;\n\n"
            "import jakarta.transaction.Transactional;\n\n"
            "public class Service { @Transactional void save() {} }\n"
        )

        reconciled = _add_known_java_imports(source)

        self.assertIn("import jakarta.transaction.Transactional;", reconciled)
        self.assertNotIn(
            "import org.springframework.transaction.annotation.Transactional;", reconciled,
        )

    def test_compiler_proven_conflict_keeps_spring_transactional_import(self):
        path = "ModernizedApp/backend/orders/src/main/java/p/OrderService.java"
        output = {
            path: (
                "package p;\n\n"
                "import jakarta.transaction.Transactional;\n"
                "import org.springframework.transaction.annotation.Transactional;\n\n"
                "public class OrderService { @Transactional void save() {} }\n"
            )
        }
        errors = {
            path: [
                "a type with the same simple name is already defined by the single-type-import "
                "of org.springframework.transaction.annotation.Transactional",
                "reference to Transactional is ambiguous",
            ]
        }

        changed = _reconcile_java_transactional_import_conflicts_from_diagnostics(
            output, errors,
        )

        self.assertEqual({path}, changed)
        self.assertNotIn("import jakarta.transaction.Transactional;", output[path])
        self.assertEqual(
            1,
            output[path].count(
                "import org.springframework.transaction.annotation.Transactional;"
            ),
        )


class JavaTruncatedRepairTokenBudgetTests(unittest.TestCase):
    """_pf_repair_build_round previously sized its repair token budget off
    the CURRENT (already truncated) file content's own length, guaranteeing
    the same cutoff point on every repair round for a genuinely truncated
    file — the root cause of "reached end of file while parsing" / "'try'
    without 'catch'..." surviving every repair attempt instead of ever
    converging."""

    def _run(self, errors, current_content, language="java"):
        captured = {}

        def fake_generate(prompt, **kwargs):
            captured.update(kwargs)
            return "// repaired\nclass Foo {}"

        output = {"Foo.java": current_content}
        fixable = {"Foo.java": errors}
        with patch("services.llm.generate", side_effect=fake_generate):
            _pf_repair_build_round(
                fixable, 1, 0, output, "", "", "qwen3.5:9b", "sys", lambda *a: None, language,
            )
        return captured

    def test_truncation_error_gets_at_least_the_full_component_budget(self):
        short_truncated_content = "class Foo {\n    void bar() {\n        try {\n"
        captured = self._run(
            [
                "reached end of file while parsing",
                "'try' without 'catch', 'finally' or resource declarations",
            ],
            short_truncated_content,
        )
        self.assertEqual(captured.get("max_tokens"), _TOKENS_COMPONENT)

    def test_large_truncated_java_file_gets_size_aware_xlarge_budget(self):
        large_truncated_content = "class Foo {\n" + ("    void method() {}\n" * 1_500)
        captured = self._run(
            ["reached end of file while parsing"],
            large_truncated_content,
        )

        self.assertEqual(captured.get("max_tokens"), _TOKENS_XLARGE)

    def test_non_truncation_error_keeps_the_length_based_formula(self):
        short_content = "class Foo {}"
        captured = self._run(["cannot find symbol — symbol: class Bar"], short_content)
        expected = max(1024, min(_TOKENS_COMPONENT, len(short_content) // 3 + 768))
        self.assertEqual(captured.get("max_tokens"), expected)
        self.assertNotEqual(captured.get("max_tokens"), _TOKENS_COMPONENT)

    def test_truncation_shaped_error_on_a_non_java_file_keeps_the_formula(self):
        short_content = "function foo() {"
        output = {"Foo.cs": short_content}
        fixable = {"Foo.cs": ["reached end of file while parsing"]}
        captured = {}

        def fake_generate(prompt, **kwargs):
            captured.update(kwargs)
            return "// repaired"

        with patch("services.llm.generate", side_effect=fake_generate):
            _pf_repair_build_round(
                fixable, 1, 0, output, "", "", "qwen3.5:9b", "sys", lambda *a: None, "csharp",
            )
        expected = max(1024, min(_TOKENS_COMPONENT, len(short_content) // 3 + 768))
        self.assertEqual(captured.get("max_tokens"), expected)


class JavaDeterministicRepairWiringTests(unittest.TestCase):
    def test_run_build_and_repair_calls_the_deterministic_pass_before_llm_repair(self):
        # A real end-to-end run needs a real Maven/javac toolchain, which
        # isn't available here — verify the wiring statically instead: the
        # deterministic pass must be invoked, and must appear before the
        # LLM repair round call so it always gets first chance at a fixable
        # error before any LLM call is spent on it.
        source = inspect.getsource(_pf_run_build_and_repair)
        self.assertIn("_apply_deterministic_java_diagnostic_repairs(", source)
        det_index = source.index("_apply_deterministic_java_diagnostic_repairs(")
        llm_round_index = source.index("_pf_repair_build_round(")
        self.assertLess(
            det_index, llm_round_index,
            "deterministic repair must run before the LLM repair round",
        )


if __name__ == "__main__":
    unittest.main()
