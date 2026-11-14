import unittest
from unittest.mock import patch

from services.build_runner import BuildResult
from services.modernizer.build_artifacts import (
    _java_lexical_tail_state,
    _reconcile_java_checked_parse_exceptions,
    _reconcile_java_console_logging_calls,
    _reconcile_java_generation_output,
)
from services.modernizer.prompt_pipeline import (
    _java_generation_standards_report,
    _pf_repair_build_round,
    _pf_run_build_and_repair,
)
from services.modernizer.conversion_pipeline import (
    _preserve_java_compatibility_source,
    _preserve_java_jdbc_source_when_provider_contract_drifts,
    _preserve_java_source_contract,
    _preserve_java_top_level_type_shape,
)


class JavaGenerationFailureIsolationTests(unittest.TestCase):
    def test_java_compatibility_module_preserves_complete_source_graph(self):
        source = "package legacy; public class TrainService { public void load() {} }\n"
        converted = "package modern; public class TrainService { public void load() {} }\n"
        self.assertEqual(source, _preserve_java_compatibility_source(source, converted))

    def test_java_conversion_preserves_primitive_and_static_public_contracts(self):
        source = """package demo;
public class TrainBean {
    public long getTrainNo() { return 0L; }
    public static TrainBean load(String id) { return null; }
}
"""
        converted = """package demo;
public class TrainBean {
    public Long getTrainNo() { return 0L; }
    public TrainBean load(String id) { return null; }
}
"""
        self.assertEqual(source, _preserve_java_source_contract(source, converted))

    def test_java_conversion_preserves_implicit_no_arg_constructor(self):
        source = "package demo; public class TrainService { public void load() {} }\n"
        converted = """package demo;
public class TrainService {
    public TrainService(DBUtil dbUtil) {}
    public void load() {}
}
"""
        self.assertEqual(source, _preserve_java_source_contract(source, converted))

    def test_java_conversion_rejects_unfulfilled_entity_reference(self):
        source = "package demo; public class TrainService { public void load() {} }\n"
        converted = """package demo;
public class TrainService {
    public void load() { TrainEntity entity = new TrainEntity(); }
}
"""
        self.assertEqual(source, _preserve_java_source_contract(source, converted))

    def test_java_conversion_allows_imported_entity_reference(self):
        source = "package demo; public class TrainService { public void load() {} }\n"
        converted = """package demo;
import demo.model.TrainEntity;
public class TrainService {
    public void load() { TrainEntity entity = new TrainEntity(); }
}
"""
        self.assertEqual(converted, _preserve_java_source_contract(source, converted))

    def test_java_to_java_conversion_preserves_interface_topology(self):
        source = "package demo; public interface TrainService { void add(); }\n"
        converted = "package demo; public class TrainService { public void add() {} }\n"

        self.assertEqual(
            source,
            _preserve_java_top_level_type_shape(source, converted),
        )
        valid_conversion = "package demo; public interface TrainService { void add(); void remove(); }\n"
        self.assertEqual(
            valid_conversion,
            _preserve_java_top_level_type_shape(source, valid_conversion),
        )

    def test_compiler_repair_cannot_change_interface_into_class(self):
        path = "Demo/src/main/java/demo/TrainService.java"
        original = "package demo; public interface TrainService { void add(); }\n"
        output = {path: original}
        with patch(
            "services.llm.generate",
            return_value="package demo; public class TrainService { public void add() {} }\n",
        ):
            _pf_repair_build_round(
                {path: ["cannot find symbol"]}, 1, 0, output, "", "",
                "test-model", "system", lambda *_args: None, "java",
            )

        self.assertEqual(original, output[path])

    def test_record_extended_by_generated_bean_is_promoted_before_maven(self):
        booking_path = (
            "Demo/backend/legacy-core/src/main/java/com/shashi/beans/BookingDetails.java"
        )
        history_path = (
            "Demo/backend/legacy-core/src/main/java/com/shashi/beans/HistoryBean.java"
        )
        output = {
            booking_path: """package com.shashi.beans;
public record BookingDetails(String mailId, int seats, Double amount) {
    public String getMailId() { return mailId; }
}
""",
            history_path: """package com.shashi.beans;
public class HistoryBean extends BookingDetails {
    private String transId;
    public String getTransId() { return transId; }
    public void setTransId(String transId) { this.transId = transId; }
}
""",
        }

        _reconcile_java_generation_output(output, "Demo")

        booking = next(
            value for path, value in output.items()
            if path.endswith("/beans/BookingDetails.java")
        )
        history = next(
            value for path, value in output.items()
            if path.endswith("/beans/HistoryBean.java")
        )
        self.assertIn("public class BookingDetails", booking)
        self.assertNotIn("record BookingDetails", booking)
        self.assertIn("public BookingDetails() {}", booking)
        self.assertIn(
            "public BookingDetails(String mailId, int seats, Double amount)",
            booking,
        )
        self.assertIn("setMailId(String value)", booking)
        self.assertIn("extends BookingDetails", history)

    def test_record_with_mutating_setters_is_promoted_to_mutable_bean(self):
        path = "Demo/backend/legacy-core/src/main/java/com/shashi/beans/AdminBean.java"
        output = {path: """package com.shashi.beans;
public record AdminBean(String pWord, String fName) {
    @Override
    public void setPWord(String pWord) { this.pWord = pWord; }
    @Override
    public String getPWord() { return pWord; }
}
"""}

        _reconcile_java_generation_output(output, "Demo")

        repaired = next(value for key, value in output.items() if key.endswith("AdminBean.java"))
        self.assertIn("public class AdminBean", repaired)
        self.assertIn("public AdminBean() {}", repaired)
        self.assertIn("setPWord(String value)", repaired)
        self.assertNotIn("record AdminBean", repaired)
        self.assertNotIn("@Override", repaired)

    def test_reconciliation_repairs_html_quote_seam_and_self_named_interface(self):
        servlet = "Demo/backend/legacy-core/src/main/java/com/shashi/ChangeUserPwd.java"
        mapper = "Demo/backend/legacy-core/src/main/java/com/shashi/service/mapper/RowMapper.java"
        output = {
            servlet: """package com.shashi;
class ChangeUserPwd { void render(java.io.PrintWriter pw) {
    pw.println("<div><p>Change Password</p> + "</div>");
} }
""",
            mapper: """package com.shashi.service.mapper;
import org.springframework.jdbc.core.RowMapper;
public class RowMapper implements RowMapper<String> { }
""",
            "Demo/backend/legacy-core/src/main/java/com/shashi/UsesMapper.java": """package com.shashi;
import com.shashi.service.mapper.RowMapper;
class UsesMapper { RowMapper mapper; }
""",
        }

        _reconcile_java_generation_output(output, "Demo")

        servlet_source = next(value for key, value in output.items() if key.endswith("ChangeUserPwd.java"))
        mapper_source = next(value for key, value in output.items() if key.endswith("/RowMapper.java"))
        self.assertIn('pw.println("<div><p>Change Password</p></div>");', servlet_source)
        self.assertIn(
            "implements org.springframework.jdbc.core.RowMapper<String>", mapper_source,
        )
        self.assertNotIn("import org.springframework.jdbc.core.RowMapper;", mapper_source)

    def test_reconciliation_removes_prose_tokens_imported_as_nested_types(self):
        path = "Demo/backend/legacy-core/src/main/java/com/shashi/ResponseCode.java"
        output = {
            path: """package com.shashi;
import com.shashi.servlets.BookTrainPayment.is;
import com.shashi.beans.UserBean;
public enum ResponseCode { OK; UserBean user; }
""",
            "Demo/backend/legacy-core/src/main/java/com/shashi/beans/UserBean.java": (
                "package com.shashi.beans; public class UserBean {}\n"
            ),
        }

        _reconcile_java_generation_output(output, "Demo")

        repaired = next(value for key, value in output.items() if key.endswith("ResponseCode.java"))
        self.assertNotIn("BookTrainPayment.is", repaired)
        self.assertIn("import com.shashi.beans.UserBean;", repaired)

    def test_reconciliation_resolves_problem_detail_import_ambiguity(self):
        path = "Demo/backend/legacy-core/src/main/java/com/shashi/error/GlobalExceptionHandler.java"
        output = {path: """package com.shashi.error;
import com.shashi.servlets.dto.ProblemDetail;
import org.springframework.http.ProblemDetail;
import org.springframework.http.HttpStatus;
public class GlobalExceptionHandler {
    ProblemDetail handle() {
        return ProblemDetail.forStatusAndDetail(HttpStatus.BAD_REQUEST, "invalid");
    }
}
"""}

        _reconcile_java_generation_output(output, "Demo")

        repaired = next(value for key, value in output.items() if key.endswith("GlobalExceptionHandler.java"))
        self.assertNotIn("com.shashi.servlets.dto.ProblemDetail", repaired)
        self.assertEqual(1, repaired.count("import org.springframework.http.ProblemDetail;"))

    def test_instance_utility_dependency_is_merged_into_business_constructor(self):
        path = "Demo/backend/legacy-core/src/main/java/com/shashi/service/impl/UserServiceImpl.java"
        output = {
            path: """package com.shashi.service.impl;
import com.shashi.constant.UserRole;
import com.shashi.utility.DBUtil;
public class UserServiceImpl {
  private final String TABLE_NAME;
  public UserServiceImpl(UserRole role) { this.TABLE_NAME = role.toString(); }
  void load() { DBUtil.getConnection(); }
}
""",
            "Demo/backend/legacy-core/src/main/java/com/shashi/utility/DBUtil.java": """package com.shashi.utility;
@org.springframework.context.annotation.Configuration
public class DBUtil { public Object getConnection() { return null; } }
""",
        }

        _reconcile_java_generation_output(output, "Demo")
        repaired = next(
            value for candidate, value in output.items()
            if candidate.endswith("/service/impl/UserServiceImpl.java")
        )

        self.assertIn("private final DBUtil dbUtil;", repaired)
        self.assertIn("public UserServiceImpl(UserRole role, DBUtil dbUtil)", repaired)
        self.assertIn("this.TABLE_NAME = role.toString();", repaired)
        self.assertIn("this.dbUtil = dbUtil;", repaired)
        self.assertEqual(1, repaired.count("public UserServiceImpl("))
        self.assertNotIn("public UserServiceImpl(DBUtil dbUtil)", repaired)

    def test_split_final_field_constructors_from_repair_are_reunified(self):
        service = "Demo/backend/legacy-core/src/main/java/com/shashi/service/impl/UserServiceImpl.java"
        output = {
            service: """package com.shashi.service.impl;
import com.shashi.constant.UserRole;
import com.shashi.utility.DBUtil;
public class UserServiceImpl {
  private final DBUtil dbUtil;
  private final String TABLE_NAME;
  public UserServiceImpl(DBUtil dbUtil) { this.dbUtil = dbUtil; }
  public UserServiceImpl(UserRole role) { this.TABLE_NAME = role.toString(); }
}
""",
            "Demo/backend/legacy-core/src/main/java/com/shashi/utility/DBUtil.java": """package com.shashi.utility;
@org.springframework.context.annotation.Configuration
public class DBUtil {}
""",
        }

        _reconcile_java_generation_output(output, "Demo")
        _reconcile_java_generation_output(output, "Demo")

        repaired = next(
            value for candidate, value in output.items()
            if candidate.endswith("/service/impl/UserServiceImpl.java")
        )
        self.assertIn("public UserServiceImpl(UserRole role, DBUtil dbUtil)", repaired)
        self.assertIn("this.TABLE_NAME = role.toString();", repaired)
        self.assertIn("this.dbUtil = dbUtil;", repaired)
        self.assertEqual(1, repaired.count("public UserServiceImpl("))

    def test_legacy_servlet_optional_local_tracks_concrete_service_contract(self):
        service = "Demo/backend/legacy-core/src/main/java/com/shashi/service/TrainService.java"
        servlet = "Demo/backend/legacy-core/src/main/java/com/shashi/servlets/UserAvailServlet.java"
        output = {
            service: """package com.shashi.service;
import com.shashi.beans.TrainBean;
public interface TrainService {
  TrainBean getTrainById(String trainNo);
}
""",
            servlet: """package com.shashi.servlets;
import java.util.Optional;
import com.shashi.beans.TrainBean;
import com.shashi.service.TrainService;
class UserAvailServlet {
  private final TrainService trainService;
  UserAvailServlet(TrainService trainService) { this.trainService = trainService; }
  void load(String trainNo) {
    Optional<TrainBean> trainOpt = trainService.getTrainById(trainNo);
  }
}
""",
            "Demo/backend/legacy-core/src/main/java/com/shashi/beans/TrainBean.java": (
                "package com.shashi.beans; public class TrainBean {}\n"
            ),
        }

        _reconcile_java_generation_output(output, "Demo")
        _reconcile_java_generation_output(output, "Demo")

        repaired = next(
            value for candidate, value in output.items()
            if candidate.endswith("/servlets/UserAvailServlet.java")
        )
        self.assertIn(
            "Optional<TrainBean> trainOpt = java.util.Optional.ofNullable("
            "trainService.getTrainById(trainNo));",
            repaired,
        )
        self.assertEqual(1, repaired.count("java.util.Optional.ofNullable("))

    def test_sql_server_target_removes_oracle_duplicate_key_marker(self):
        path = "Demo/backend/src/main/java/com/app/UserService.java"
        source = """package com.app;
public class UserService {
  boolean duplicate(Exception error) {
    return error.getMessage().toUpperCase().contains("ORA-00001");
  }
}
"""
        sql_server = {path: source}
        _reconcile_java_generation_output(
            sql_server, "Demo", {"backend_tech": "Spring Boot", "db_target": "mssql"},
        )
        repaired = next(
            value for candidate, value in sql_server.items()
            if candidate.endswith("/com/app/UserService.java")
        )
        self.assertNotIn("ORA-00001", repaired)
        self.assertIn('contains("2627")', repaired)
        self.assertIn('contains("2601")', repaired)

        oracle = {path: source}
        _reconcile_java_generation_output(
            oracle, "Demo", {"backend_tech": "Spring Boot", "db_target": "oracle"},
        )
        oracle_source = next(
            value for candidate, value in oracle.items()
            if candidate.endswith("/com/app/UserService.java")
        )
        self.assertIn("ORA-00001", oracle_source)

    def test_legacy_web_utility_resolves_new_managed_constructor_dependency(self):
        utility_path = "Demo/backend/src/main/java/com/app/TrainUtil.java"
        output = {
            utility_path: """package com.app;
import jakarta.servlet.http.HttpServletRequest;
class TrainUtil {
  static void login(HttpServletRequest request, UserRole role) {
    UserService service = new UserServiceImpl(role);
  }
}
""",
            "Demo/backend/src/main/java/com/app/UserServiceImpl.java": """package com.app;
class UserServiceImpl implements UserService {
  public UserServiceImpl(UserRole role, DBUtil dbUtil) {}
}
""",
            "Demo/backend/src/main/java/com/app/DBUtil.java": """package com.app;
@org.springframework.context.annotation.Configuration
class DBUtil {}
""",
        }

        _reconcile_java_generation_output(output, "Demo")

        utility = output[utility_path]
        self.assertIn("new UserServiceImpl(role,", utility)
        self.assertIn(
            "getRequiredWebApplicationContext(request.getServletContext()).getBean(DBUtil.class)",
            utility,
        )

    def test_framework_web_context_shadow_is_removed_and_import_is_canonical(self):
        shadow = "Demo/backend/legacy-core/src/main/java/com/app/util/WebApplicationContextUtils.java"
        consumer = "Demo/backend/legacy-core/src/main/java/com/app/TrainUtil.java"
        output = {
            shadow: """package com.app.util;
import org.springframework.web.context.support.WebApplicationContextUtils;
public class WebApplicationContextUtils {}
""",
            consumer: """package com.app;
import com.app.util.WebApplicationContextUtils;
class TrainUtil { WebApplicationContextUtils context; }
""",
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        self.assertNotIn(shadow, output)
        consumer_source = next(
            value for path, value in output.items() if path.endswith("/com/app/TrainUtil.java")
        )
        self.assertIn(
            "import org.springframework.web.context.support.WebApplicationContextUtils;",
            consumer_source,
        )

    def test_void_authorization_helper_is_not_used_as_boolean_predicate(self):
        utility = "Demo/backend/legacy-core/src/main/java/com/app/TrainUtil.java"
        servlet = "Demo/backend/legacy-core/src/main/java/com/app/EditUserProfile.java"
        output = {
            utility: """package com.app;
class TrainUtil {
  public static void validateUserAuthorization(Object request, Object role) { authorize(); }
  private static void authorize() {}
}
""",
            servlet: """package com.app;
class EditUserProfile {
  void edit(Object request, Object role) {
    if (!TrainUtil.validateUserAuthorization(request, role)) {
      throw new IllegalStateException("denied");
    }
    render();
  }
  void render() {}
}
""",
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        repaired = next(
            value for path, value in output.items() if path.endswith("/com/app/EditUserProfile.java")
        )
        self.assertIn("TrainUtil.validateUserAuthorization(request, role);", repaired)
        self.assertNotIn("if (!TrainUtil.validateUserAuthorization", repaired)
        self.assertNotIn('IllegalStateException("denied")', repaired)
        self.assertIn("render();", repaired)

    def test_injected_service_constructor_does_not_capture_request_from_endpoint(self):
        controller = "Demo/backend/legacy-core/src/main/java/com/app/UpdateUserProfile.java"
        output = {
            controller: """package com.app;
class UpdateUserProfile {
  private final UserService userService;
  public UpdateUserProfile(UserService userService) {
    this.userService = new UserServiceImpl(UserRole.CUSTOMER);
  }
  void update(HttpServletRequest req) {}
}
""",
            "Demo/backend/legacy-core/src/main/java/com/app/UserServiceImpl.java": """package com.app;
class UserServiceImpl implements UserService {
  public UserServiceImpl(UserRole role, DBUtil dbUtil) {}
}
""",
            "Demo/backend/legacy-core/src/main/java/com/app/DBUtil.java": """package com.app;
@org.springframework.stereotype.Component class DBUtil {}
""",
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        repaired = next(
            value for path, value in output.items() if path.endswith("/UpdateUserProfile.java")
        )
        self.assertIn("this.userService = userService;", repaired)
        self.assertNotIn("new UserServiceImpl", repaired)
        self.assertNotIn("req.getServletContext()", repaired)

    def test_web_context_lookup_is_limited_to_request_owning_method(self):
        utility = "Demo/backend/legacy-core/src/main/java/com/app/TrainUtil.java"
        output = {
            utility: """package com.app;
class TrainUtil {
  Object field = new UserServiceImpl(UserRole.CUSTOMER);
  static void login(HttpServletRequest request) {
    Object service = new UserServiceImpl(UserRole.CUSTOMER);
  }
}
""",
            "Demo/backend/legacy-core/src/main/java/com/app/UserServiceImpl.java": """package com.app;
class UserServiceImpl {
  public UserServiceImpl(UserRole role, DBUtil dbUtil) {}
}
""",
            "Demo/backend/legacy-core/src/main/java/com/app/DBUtil.java": """package com.app;
@org.springframework.stereotype.Component class DBUtil {}
""",
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        repaired = next(
            value for path, value in output.items() if path.endswith("/TrainUtil.java")
        )
        field, method = repaired.split("static void login", 1)
        self.assertNotIn("request.getServletContext()", field)
        self.assertIn("request.getServletContext()", method)

    def test_console_logging_uses_declared_class_not_comment_text(self):
        path = "Demo/backend/src/main/java/com/app/BookingServiceImpl.java"
        output = {
            path: """package com.app;
// Service implementation class for booking details
public class BookingServiceImpl {
  void save() { System.out.println("saved"); }
}
""",
        }

        _reconcile_java_console_logging_calls(output)

        self.assertIn("getLogger(BookingServiceImpl.class)", output[path])
        self.assertNotIn("getLogger(for.class)", output[path])

    def test_java_jdbc_conversion_cannot_invent_dbutil_domain_methods(self):
        source = """package com.app;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
class BookingServiceImpl {
  Object create(Object details) throws Exception {
    var connection = DBUtil.getConnection();
    PreparedStatement statement = connection.prepareStatement("INSERT INTO HISTORY VALUES(?)");
    statement.setObject(1, details);
    statement.executeUpdate();
    return details;
  }
}
"""
        converted = """package com.app;
class BookingServiceImpl {
  private final DBUtil dbUtil;
  Object create(Object details) { return dbUtil.createHistory(details); }
}
"""

        repaired = _preserve_java_jdbc_source_when_provider_contract_drifts(
            source, converted,
        )

        self.assertEqual(source, repaired)
        self.assertNotIn("dbUtil.createHistory", repaired)

    def test_java_jdbc_conversion_allows_existing_dbutil_connection_contract(self):
        source = (
            "class Service { void load() throws Exception { "
            "PreparedStatement p = DBUtil.getConnection().prepareStatement(\"select 1\"); "
            "ResultSet r = p.executeQuery(); } }"
        )
        converted = source.replace("DBUtil.getConnection()", "dbUtil.getConnection()")

        self.assertEqual(
            converted,
            _preserve_java_jdbc_source_when_provider_contract_drifts(source, converted),
        )
    def test_java_text_scans_are_cached_without_changing_content(self):
        from services.modernizer import build_artifacts

        source = "package demo; public record Item(Long id, String name) {}\n"
        output = {
            "Demo/backend/src/main/java/demo/Item.java": source,
        }
        build_artifacts._java_lexical_tail_state.cache_clear()
        build_artifacts._java_record_components_cached.cache_clear()

        first_state = build_artifacts._java_lexical_tail_state(source)
        build_artifacts._java_lexical_tail_state(source)
        first_components = build_artifacts._java_record_components(output)
        build_artifacts._java_record_components(output)

        self.assertEqual(("code", 0), first_state)
        self.assertEqual(["id", "name"], first_components[("Demo/backend", "Item")])
        self.assertEqual(1, build_artifacts._java_lexical_tail_state.cache_info().hits)
        self.assertEqual(1, build_artifacts._java_record_components_cached.cache_info().hits)

    def test_java_control_character_cleanup_preserves_printable_and_unicode_text(self):
        from services.modernizer.build_artifacts import _strip_invalid_java_control_characters

        path = "Demo/backend/src/main/java/demo/Message.java"
        output = {path: "class Message { String value = \"café\"; }\x00\x1f\x7f\n"}

        _strip_invalid_java_control_characters(output)

        self.assertEqual("class Message { String value = \"café\"; }\n", output[path])

    def test_type_index_rewrites_qualified_type_and_preserves_member_suffix(self):
        from services.modernizer.build_artifacts import _reconcile_java_type_imports

        output = {
            "Demo/backend/src/main/java/demo/OrderDto.java": (
                "package demo; public class OrderDto { static OrderDto builder() { return new OrderDto(); } }\n"
            ),
            "Demo/backend/src/main/java/demo/OrderService.java": (
                "package demo.service; public class OrderService { "
                "Object create() { return com.wrong.OrderDto.builder(); } }\n"
            ),
        }

        _reconcile_java_type_imports(output)

        service = output["Demo/backend/src/main/java/demo/OrderService.java"]
        self.assertIn("demo.OrderDto.builder()", service)
        self.assertNotIn("com.wrong.OrderDto", service)

    def test_legacy_full_stack_maven_closure_is_reconciled_before_build(self):
        base = "Demo/backend/legacy-core/src/main/java/com/shashi"
        output = {
            f"{base}/constant/UserRole.java": (
                "package com.shashi.constant; public enum UserRole { ADMIN, CUSTOMER }\n"
            ),
            f"{base}/constant/UserRole/ADMIN.java": (
                "package com.shashi.constant.UserRole; public enum ADMIN { INSTANCE }\n"
            ),
            f"{base}/servlets/AdminLogin.java": (
                "package com.shashi.servlets;\n"
                "import com.shashi.constant.UserRole.ADMIN;\n"
                "import com.shashi.constant.UserRole;\n"
                "class AdminLogin { UserRole role = UserRole.ADMIN; }\n"
            ),
            f"{base}/utility/DBUtil.java": (
                "package com.shashi.utility;\n"
                "import java.sql.Connection; import javax.sql.DataSource;\n"
                "import com.shashi.utility.exception.ClassNotFoundException;\n"
                "import org.springframework.jdbc.datasource.DataSourceTransactionManager;\n"
                "public class DBUtil { private final DataSource dataSource; private Connection con;\n"
                "public DBUtil(DataSource dataSource){this.dataSource=dataSource;}\n"
                "public void initialize(){try {Class.forName(\"driver\");} catch(ClassNotFoundException error){throw new RuntimeException(error);}}\n"
                "public Connection getConnection() throws TrainException {\n"
                "if (con == null || con.isClosed()) throw new TrainException(ResponseCode.DATABASE_CONNECTION_FAILURE, \"failed\", null);\n"
                "return con; } }\n"
            ),
            f"{base}/beans/TrainException.java": (
                "package com.shashi.beans; import com.shashi.constant.ResponseCode; import java.io.IOException;\n"
                "public class TrainException extends IOException {\n"
                "public TrainException(ResponseCode code) { super(code.toString()); }}\n"
            ),
            f"{base}/beans/BookingDetails.java": (
                "package com.shashi.beans; public class BookingDetails {\n"
                "private String mailId;\nprivate int seats;\n"
                "public void setMailId(String mailId){this.mailId=mailId;}\n"
                "public void setSeats(int seats){this.seats=seats;} }\n"
            ),
            f"{base}/beans/HistoryBean.java": (
                "package com.shashi.beans; public class HistoryBean extends BookingDetails {\n"
                "private String transId;\npublic void setTransId(String transId){this.transId=transId;} }\n"
            ),
            f"{base}/service/UserService.java": (
                "package com.shashi.service; public interface UserService {\n"
                "Object loginUser(String username, String password); }\n"
            ),
            f"{base}/service/impl/BookingServiceImpl.java": (
                "package com.shashi.service.impl;\n"
                "import com.shashi.utility.DBUtil; import com.shashi.beans.HistoryBean;\n"
                "import com.shashi.service.exception.SQLException;\n"
                "@org.springframework.stereotype.Service public class BookingServiceImpl {\n"
                "public HistoryBean load() { try { Connection c = DBUtil.getConnection();\n"
                "PreparedStatement p = c.prepareStatement(\"select 1\"); ResultSet r=p.executeQuery();\n"
                "return new HistoryBean(\"id\", \"mail\", 1); } catch (SQLException e) { return null; }} }\n"
            ),
            f"{base}/service/impl/TrainServiceImpl.java": (
                "package com.shashi.service.impl; import jakarta.persistence.EntityManager;\n"
                "public class TrainServiceImpl { EntityManager manager; }\n"
            ),
            f"{base}/servlets/UserLoginServlet.java": (
                "package com.shashi.servlets; import com.shashi.service.UserService;\n"
                "class UserLoginServlet { UserService userService;\n"
                "Object run(){ return userService.login(\"u\",\"p\"); }\n"
                "Object runWithRole(){ return userService.loginUser(\"u\",\"p\", UserRole.CUSTOMER); }\n"
                "Object runWrongRole(){ String uName=\"u\"; String pWord=\"p\"; return userService.loginUser(UserRole.CUSTOMER,uName); }}\n"
            ),
            f"{base}/servlets/ViewUserProfile.java": (
                "package com.shashi.servlets; class ViewUserProfile { PrintWriter writer; }\n"
            ),
            f"{base}/servlets/BookTrainPayment.java": (
                "package com.shashi.servlets; import jakarta.servlet.ServletException;\n"
                "import jakarta.servlet.http.HttpServlet; import jakarta.servlet.http.HttpServletRequest;\n"
                "import jakarta.servlet.http.HttpServletResponse; public class BookTrainPayment extends HttpServlet {\n"
                "public void doPost(HttpServletRequest req, HttpServletResponse res) throws ServletException {\n"
                "req.getRequestDispatcher(\"Payment.html\").forward(req,res); }} }\n"
            ),
            f"{base}/servlets/BookTrains.java": (
                "package com.shashi.servlets; import java.text.SimpleDateFormat;\n"
                "class BookTrains { void parse(String value) { try {\n"
                "SimpleDateFormat format = new SimpleDateFormat(\"yyyy-MM-dd\"); format.parse(value);\n"
                "} catch (RuntimeException error) { throw error; } } }\n"
            ),
            "Demo/backend/orders/src/main/java/com/example/orders/OrderService.java": (
                "package com.example.orders; public class OrderService {}\n"
            ),
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        self.assertNotIn(f"{base}/constant/UserRole/ADMIN.java", output)
        self.assertIn("import static com.shashi.constant.UserRole.ADMIN;", output[f"{base}/servlets/AdminLogin.java"])
        pom = output["Demo/backend/legacy-core/pom.xml"]
        self.assertIn("spring-boot-starter-data-jpa", pom)
        self.assertIn("spring-boot-starter-jdbc", pom)
        booking = output[f"{base}/service/impl/BookingServiceImpl.java"]
        self.assertIn("private final DBUtil dbUtil;", booking)
        self.assertIn("dbUtil.getConnection()", booking)
        self.assertIn("import java.sql.Connection;", booking)
        self.assertIn("import java.sql.PreparedStatement;", booking)
        self.assertIn("import java.sql.ResultSet;", booking)
        self.assertIn("import java.sql.SQLException;", booking)
        self.assertIn("public HistoryBean(String transId, String mailId, int seats)", output[f"{base}/beans/HistoryBean.java"])
        self.assertIn("userService.loginUser(", output[f"{base}/servlets/UserLoginServlet.java"])
        self.assertNotIn("UserRole.CUSTOMER", output[f"{base}/servlets/UserLoginServlet.java"])
        self.assertIn("import java.io.PrintWriter;", output[f"{base}/servlets/ViewUserProfile.java"])
        self.assertIn("new TrainException(ResponseCode.DATABASE_CONNECTION_FAILURE)", output[f"{base}/utility/DBUtil.java"])
        self.assertNotIn("com.shashi.utility.exception.ClassNotFoundException", output[f"{base}/utility/DBUtil.java"])
        self.assertIn("catch (java.sql.SQLException error)", output[f"{base}/utility/DBUtil.java"])
        self.assertIn("extends RuntimeException", output[f"{base}/beans/TrainException.java"])
        self.assertIn(
            "throws ServletException, java.io.IOException",
            output[f"{base}/servlets/BookTrainPayment.java"],
        )
        self.assertIn(
            "catch (java.text.ParseException | RuntimeException error)",
            output[f"{base}/servlets/BookTrains.java"],
        )

    def test_checked_parse_exception_is_attached_only_to_owning_try_block(self):
        path = "Demo/backend/legacy-core/src/main/java/com/app/BookTrains.java"
        output = {
            path: """package com.app;
import java.text.SimpleDateFormat;
class BookTrains {
  void book(String value) {
    try {
      authorize();
    } catch (java.text.ParseException | RuntimeException error) {
      throw error;
    }
    try {
      SimpleDateFormat inputFormat = new SimpleDateFormat("yyyy-MM-dd");
      inputFormat.parse(value);
    } catch (RuntimeException error) {
      throw error;
    }
  }
  void authorize() {}
}
""",
        }

        _reconcile_java_checked_parse_exceptions(output)

        repaired = output[path]
        self.assertEqual(1, repaired.count("java.text.ParseException"))
        authorization = repaired.split("try {", 2)[1]
        parsing = repaired.split("try {", 2)[2]
        self.assertIn("catch (RuntimeException error)", authorization)
        self.assertIn(
            "catch (java.text.ParseException | RuntimeException error)", parsing,
        )

    def test_private_servlet_helper_declares_thrown_servlet_exception(self):
        path = "Demo/backend/legacy-core/src/main/java/com/app/UserProfile.java"
        output = {
            path: """package com.app;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServlet;
class UserProfile extends HttpServlet {
  private void validateAuthorization() {
    try { authorize(); }
    catch (RuntimeException error) {
      throw new ServletException("Not authorized", error);
    }
  }
  private void authorize() {}
}
""",
        }

        _reconcile_java_generation_output(output, "Demo")

        source = next(value for key, value in output.items() if key.endswith("UserProfile.java"))
        self.assertIn(
            "validateAuthorization()  throws jakarta.servlet.ServletException",
            source,
        )

    def test_unterminated_java_comment_is_closed_before_java_reconciliation(self):
        source_path = "Demo/backend/legacy-core/src/main/java/com/example/ICLService.java"
        output = {
            source_path: (
                "package com.example;\n"
                "public interface ICLService {\n"
                "    void load();\n"
                "    /* generated migration notes ended unexpectedly {\n"
            ),
            "Demo/backend/orders/src/main/java/com/example/orders/OrderService.java": (
                "package com.example.orders; public class OrderService {}\n"
            ),
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        repaired = output[source_path]
        self.assertIn("*/", repaired)
        self.assertEqual(("code", 0), _java_lexical_tail_state(repaired))

    def test_truncated_duplicate_java_method_tail_is_removed(self):
        source_path = "Demo/backend/legacy-core/src/main/java/com/example/GenericDAO.java"
        output = {
            source_path: (
                "package com.example;\n"
                "public class GenericDAO {\n"
                "    public String findValueWithOutput(String schema, String procedure,\n"
                "                                      Object... parameters) throws Exception {\n"
                "        return null;\n"
                "    }\n"
                "\n"
                "    /** Duplicate generation tail. */\n"
                "    public String findValueWithOutput(String schema, String procedure,\n"
                "                                      Object... parameters) throws Exception\n"
            ),
            "Demo/backend/orders/src/main/java/com/example/orders/OrderService.java": (
                "package com.example.orders; public class OrderService {}\n"
            ),
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        repaired = output[source_path]
        self.assertEqual(1, repaired.count("findValueWithOutput("))
        self.assertEqual(("code", 0), _java_lexical_tail_state(repaired))

    def test_hibernate_jpa_lifecycle_imports_are_corrected(self):
        source_path = "Demo/backend/model-service/src/main/java/com/example/model/Entity.java"
        output = {
            source_path: (
                "package com.example.model;\n"
                "import org.hibernate.annotations.PrePersist;\n"
                "import org.hibernate.annotations.PreUpdate;\n"
                "public class Entity {\n"
                "    @PrePersist void create() {}\n"
                "    @PreUpdate void update() {}\n"
                "}\n"
            ),
            "Demo/backend/orders/src/main/java/com/example/orders/OrderService.java": (
                "package com.example.orders; public class OrderService {}\n"
            ),
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        repaired = output[source_path]
        self.assertIn("import jakarta.persistence.PrePersist;", repaired)
        self.assertIn("import jakarta.persistence.PreUpdate;", repaired)
        self.assertNotIn("org.hibernate.annotations.Pre", repaired)

    def test_explicit_spring_target_gets_baseline_despite_legacy_struts_imports(self):
        output = {
            "Demo/backend/legacy-core/src/main/java/com/example/LegacyAction.java": (
                "package com.example;\n"
                "import org.apache.struts.action.Action;\n"
                "public class LegacyAction extends Action {}\n"
            ),
            "Demo/backend/orders/src/main/java/com/example/orders/OrderService.java": (
                "package com.example.orders; public class OrderService {}\n"
            ),
        }

        _reconcile_java_generation_output(
            output,
            "Demo",
            {"backend_tech": "Java 21 Spring Boot 3 modernization from Struts"},
        )

        report = _java_generation_standards_report(output)
        self.assertTrue(report["passed"], report["diagnostics"])

    def test_print_stack_trace_is_replaced_with_parameterized_slf4j(self):
        path = "Demo/backend/legacy-core/src/main/java/com/example/Processor.java"
        output = {path: (
            "package com.example;\n"
            "import org.slf4j.Logger;\n"
            "import org.slf4j.LoggerFactory;\n"
            "public class Processor {\n"
            "    private static final Logger LOGGER = LoggerFactory.getLogger(Processor.class);\n"
            "    void run() {\n"
            "        try { work(); } catch (Exception exception) {\n"
            "            exception.printStackTrace();\n"
            "        }\n"
            "    }\n"
            "}\n"
        )}

        _reconcile_java_console_logging_calls(output)

        self.assertNotIn("printStackTrace", output[path])
        self.assertIn('LOGGER.error("Unhandled exception", exception);', output[path])

    def test_java_reconciliation_is_not_called_for_other_language_generation(self):
        cases = {
            "csharp": ("Demo/backend/Demo.cs", "public sealed class Demo {}\n", "dotnet"),
            "typescript": ("Demo/frontend/demo.ts", "export const demo = true;\n", "npm-tsc"),
            "python": ("Demo/backend/demo.py", "value = True\n", "python-compile"),
            "go": ("Demo/backend/demo.go", "package demo\n", "go"),
        }
        for language, (path, content, checker) in cases.items():
            with self.subTest(language=language), patch(
                "services.build_runner.run_build",
                return_value=BuildResult(True, checker, {}),
            ), patch(
                "services.modernizer.build_artifacts._reconcile_java_generation_output",
                side_effect=AssertionError(f"Java reconciliation reached {language} generation"),
            ) as java_reconcile:
                result = _pf_run_build_and_repair(
                    {path: content}, "Demo", language, False, "project", "", "", "model", "",
                    "system", lambda *_args: None,
                )

            self.assertTrue(result.passed)
            java_reconcile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
