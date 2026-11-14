from __future__ import annotations

import asyncio
import json
import logging

from starlette.requests import Request

from api.server import contain_unhandled_request_errors
from services.error_management import build_error_record, log_error_record


def _nested_failure():
    try:
        raise ValueError("password=super-secret invalid configuration")
    except ValueError as exc:
        raise RuntimeError("generation failed") from exc


def test_error_record_includes_root_cause_source_and_redacts_secrets():
    try:
        _nested_failure()
    except RuntimeError as exc:
        record = build_error_record(
            exc,
            operation="generate single file",
            component="test",
            job_id="job-123",
            artifact_path="Demo/generated.py",
        )

    assert record["error_id"]
    assert record["exception_type"] == "RuntimeError"
    assert record["root_cause_type"] == "ValueError"
    assert "super-secret" not in record["root_cause"]
    assert "[REDACTED]" in record["root_cause"]
    assert record["source"]["file"].endswith("tests/test_error_management.py")
    assert record["source"]["line"]
    assert record["source"]["function"] == "_nested_failure"
    assert "Demo/generated.py" in record["description"]
    assert record["error_id"] in record["description"]


def test_error_record_marks_transient_failures_retryable():
    try:
        raise TimeoutError("provider timed out")
    except TimeoutError as exc:
        record = build_error_record(exc, operation="call provider")
    assert record["retryable"] is True


def test_log_error_record_preserves_exception_traceback(caplog):
    logger = logging.getLogger("tests.error-management")
    try:
        raise RuntimeError("bounded failure")
    except RuntimeError as exc:
        record = build_error_record(exc, operation="test logging")
        with caplog.at_level(logging.ERROR, logger=logger.name):
            log_error_record(logger, record, exc)
    assert record["error_id"] in caplog.text
    assert "Traceback" in caplog.text


def test_http_exception_boundary_returns_safe_correlated_response(caplog):
    request = Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/failure-probe",
        "raw_path": b"/api/failure-probe",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8085),
    })

    async def fail(_request):
        raise RuntimeError("database password=do-not-expose")

    with caplog.at_level(logging.ERROR, logger="api.server"):
        response = asyncio.run(contain_unhandled_request_errors(request, fail))

    payload = json.loads(response.body)
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == payload["error"]["request_id"]
    assert payload["error"]["error_id"]
    assert "do-not-expose" not in response.body.decode("utf-8")
    assert payload["error"]["error_id"] in caplog.text

