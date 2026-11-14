"""Consistent, bounded exception evidence for API and background-job boundaries."""
from __future__ import annotations

import re
import traceback
import uuid
from datetime import datetime, UTC
from pathlib import Path
from typing import Any


_MODULE_ROOT = Path(__file__).resolve().parent.parent
_SECRET_PATTERNS = (
    re.compile(r"(?i)(password|secret|token|api[_-]?key|authorization)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"),
)


def _bounded_text(value: Any, limit: int = 1200) -> str:
    text = " ".join(str(value or "").split()) or "No exception message was provided"
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]" if match.lastindex == 2 else "Bearer [REDACTED]", text)
    return text[:limit]


def _relative_source(path: str) -> str:
    if not path:
        return "unknown"
    candidate = Path(path).resolve()
    try:
        return candidate.relative_to(_MODULE_ROOT).as_posix()
    except ValueError:
        return candidate.name


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
    return chain


def _source_location(exc: BaseException) -> dict[str, Any]:
    extracted = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    frame = extracted[-1] if extracted else None
    return {
        "file": _relative_source(frame.filename) if frame else "unknown",
        "line": int(frame.lineno) if frame else None,
        "function": frame.name if frame else "unknown",
    }


def build_error_record(
    exc: BaseException,
    *,
    operation: str,
    component: str = "modernization",
    job_id: str | None = None,
    request_id: str | None = None,
    artifact_path: str | None = None,
) -> dict[str, Any]:
    """Create serializable failure evidence without persisting a full traceback."""
    chain = _exception_chain(exc)
    root = chain[-1]
    location = _source_location(root if root.__traceback__ else exc)
    message = _bounded_text(exc)
    root_message = _bounded_text(root)
    error_id = uuid.uuid4().hex[:16]
    retryable = isinstance(root, (TimeoutError, ConnectionError))
    if isinstance(root, OSError):
        retryable = getattr(root, "errno", None) not in {13, 22}
    record = {
        "error_id": error_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "component": _bounded_text(component, 120),
        "operation": _bounded_text(operation, 240),
        "exception_type": type(exc).__name__,
        "message": message,
        "root_cause_type": type(root).__name__,
        "root_cause": root_message,
        "source": location,
        "artifact_path": _bounded_text(artifact_path, 500) if artifact_path else None,
        "job_id": job_id,
        "request_id": request_id,
        "retryable": retryable,
        "chain": [
            {"type": type(item).__name__, "message": _bounded_text(item)}
            for item in chain
        ],
    }
    record["description"] = format_error_summary(record)
    return record


def format_error_summary(record: dict[str, Any], limit: int = 1200) -> str:
    source = record.get("source") or {}
    location = str(source.get("file") or "unknown")
    if source.get("line"):
        location += f":{source['line']}"
    if source.get("function") and source.get("function") != "unknown":
        location += f" ({source['function']})"
    artifact = f"; artifact={record['artifact_path']}" if record.get("artifact_path") else ""
    summary = (
        f"[{record.get('exception_type', 'Error')}] {record.get('operation', 'operation')} failed: "
        f"{record.get('message', 'Unknown error')}; root cause "
        f"{record.get('root_cause_type', 'Error')}: {record.get('root_cause', 'Unknown error')}; "
        f"source={location}{artifact}; error_id={record.get('error_id', 'unknown')}"
    )
    return summary[:limit]


def log_error_record(logger, record: dict[str, Any], exc: BaseException) -> None:
    """Write one searchable summary plus the original traceback."""
    logger.error(record["description"], exc_info=(type(exc), exc, exc.__traceback__))

