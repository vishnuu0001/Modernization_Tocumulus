# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: api/server.py
# Date: 2025-08-15
# ---------------------------------------------------------------------------
"""
api/server.py
Modernization FastAPI backend.

Endpoints:
  GET  /api/health                              — liveness check
  GET  /api/auth/session                        — validate JWT and return session info
  POST /api/auth/refresh                        — exchange an expiring/expired-but-valid token for a fresh one

  POST /api/modernize/analyze                   — start deep analysis of a legacy folder
  POST /api/modernize/analyze-prompt            — generate code from a text prompt + screenshots
  GET  /api/modernize/jobs                      — list in-memory analysis jobs
  GET  /api/modernize/jobs/{job_id}             — get job status / report
  GET  /api/modernize/jobs/{job_id}/stream      — SSE real-time progress stream
  GET  /api/modernize/jobs/{job_id}/output      — download modernized code as ZIP
  DELETE /api/modernize/jobs/{job_id}           — cancel / remove a job

Serves React SPA (frontend/dist) on all non-/api paths.
Port: 18084
"""
from __future__ import annotations

import asyncio
import atexit
import base64
import csv
import copy
import hashlib
import hmac
import io
import json
import logging
import os
import queue
import re
import subprocess
import shutil
import tempfile
import time
import threading
import uuid
import zipfile
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, UTC
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.governance import (
    ProjectStore, compare_directories, comparison_html, comparison_pdf,
    effective_contracts_for_validation, generate_contracts, generate_plan,
    semantic_index, transformation_context, validate_contracts,
)
from services.access_control import AccessStore
from services.error_management import build_error_record, log_error_record
from services.python_runtime import enforce_safe_python_runtime

_PYTHON_RUNTIME = enforce_safe_python_runtime()

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except (ImportError, OSError):
    pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODERNIZATION_APP = "MODERNIZATION"
_INSECURE_DEFAULT_AUTH_SECRET = "change-this-auth-token-secret-in-production"
# How long past its `exp` a token may still be renewed via /api/auth/refresh.
# Long-running conversion jobs can outlive AUTH_TOKEN_TTL_SECONDS while a tab
# sits open polling for status; this lets that session self-heal (as long as
# the signature is still valid) instead of dying mid-job and forcing a
# re-login through the Portal, which is what left status polling stuck.
_REFRESH_GRACE_SECONDS = int(os.getenv("AUTH_REFRESH_GRACE_SECONDS", str(60 * 60)))

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _ACCESS_STORE.initialize()
    _load_database_jobs()
    _ACCESS_STORE.sync_projects([
        _PROJECT_STORE.get_project(project["id"]) for project in _PROJECT_STORE.list_projects()
    ])
    os.environ.pop("MODERNIZATION_BOOTSTRAP_ADMIN_PASSWORD", None)
    yield


app = FastAPI(
    title="Modernization API",
    description="Legacy code deep analysis and modernization service",
    version="1.0.0",
    lifespan=_lifespan,
)


def _request_id(request: Request) -> str:
    supplied = request.headers.get("X-Request-ID", "").strip()
    if supplied and re.fullmatch(r"[A-Za-z0-9._-]{8,100}", supplied):
        return supplied
    return uuid.uuid4().hex


@app.middleware("http")
async def contain_unhandled_request_errors(request: Request, call_next):
    """Keep one route failure from escaping the ASGI request boundary."""
    request_id = _request_id(request)
    request.state.request_id = request_id
    try:
        response = await call_next(request)
    except Exception as exc:
        record = build_error_record(
            exc,
            operation=f"{request.method} {request.url.path}",
            component="api",
            request_id=request_id,
        )
        log_error_record(logger, record, exc)
        response = JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "error_id": record["error_id"],
                    "request_id": request_id,
                    "message": "The request failed safely. Use the error ID to inspect server logs.",
                }
            },
        )
    response.headers["X-Request-ID"] = request_id
    return response


def _raise_internal_error(
    exc: BaseException,
    *,
    operation: str,
    artifact_path: str | None = None,
) -> None:
    """Log full evidence and expose only a stable support reference to clients."""
    record = build_error_record(
        exc,
        operation=operation,
        component="api",
        artifact_path=artifact_path,
    )
    log_error_record(logger, record, exc)
    raise HTTPException(
        status_code=500,
        detail={
            "code": "INTERNAL_ERROR",
            "error_id": record["error_id"],
            "message": "The operation failed safely. Use the error ID to inspect server logs.",
        },
    ) from exc

_PROJECT_STORE = ProjectStore()
_ACCESS_STORE = AccessStore()
_PROJECT_PLAN_LOCKS: dict[str, asyncio.Lock] = {}
_TOOLCHAIN_INSTALL_JOBS: dict[str, dict] = {}
_TOOLCHAIN_PACKAGES = {
    "dotnet8": "Microsoft.DotNet.SDK.8",
    "dotnet9": "Microsoft.DotNet.SDK.9",
    "dotnet10": "Microsoft.DotNet.SDK.10",
    "java17": "EclipseAdoptium.Temurin.17.JDK",
    "java21": "EclipseAdoptium.Temurin.21.JDK",
    "node": "OpenJS.NodeJS.LTS",
    "python312": "Python.Python.3.12",
    "go": "GoLang.Go",
    "php": "PHP.PHP.8.3",
    "ruby": "RubyInstallerTeam.RubyWithDevKit.3.3",
    "llvm": "LLVM.LLVM",
    "git": "Git.Git",
    "r": "RProject.R",
    "haskell": "commercialhaskell.stack",
    "julia": "Julialang.Julia",
    "dart": "Google.DartSDK",
    "swift": "Swift.Toolchain",
    # Flutter SDK is provisioned through Puro (Flutter version manager).
    "flutter": "pingbird.Puro",
    "ada": "AdaLang.Alire",
    "pascal": "FreePascal.FreePascalCompiler",
    "erlang": "Erlang.ErlangOTP",
    "ocaml": "Diskuv.OCaml",
    "prolog": "SWI-Prolog.SWI-Prolog",
    "lisp": "SBCL.SBCL",
}




# Function: _actor
def _actor(request: Request) -> str:
    """Return the authenticated subject for audit metadata (or local operator)."""
    state_payload = getattr(request.state, "auth", None)
    if state_payload:
        return str(state_payload.get("sub") or "local-operator")
    token = _extract_bearer_token(request.headers.get("Authorization", ""))
    if token:
        try:
            return str(_decode_access_token(token).get("sub") or "local-operator")
        except (ValueError, RuntimeError):
            pass
    return "local-operator"


def _validated_generation_output_mode(value: str) -> str:
    from services.modernizer.prompt_pipeline import _normalize_generation_output_mode
    try:
        return _normalize_generation_output_mode(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# Function: _require_admin
def _require_admin(request: Request, action: str = "perform this action") -> None:
    if (getattr(request.state, "auth", None) or {}).get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail=f"Administrator access is required to {action}",
        )


# Function: _cors_origins
def _cors_origins() -> list[str]:
    configured = (os.getenv("CORS_ORIGINS") or os.getenv("ALLOWED_ORIGINS") or "").strip()
    defaults = [
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5175",
        "http://127.0.0.1:5175",
        "http://localhost:8090",
        "http://127.0.0.1:8090",
    ]
    values = [origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()]
    origins = values or defaults
    return list(dict.fromkeys(origins))


# ─── Serve React SPA ─────────────────────────────────────────────────────────
_DIST_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _DIST_DIR.exists():
    _assets_dir = _DIST_DIR / "assets"
    if _assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    # Function: _spa_index_response
    def _spa_index_response():
        # The HTML shell selects a content-hashed JavaScript bundle. It must
        # never be cached, otherwise an already-fixed session policy can keep
        # booting an obsolete bundle after a service update.
        return FileResponse(
            str(_DIST_DIR / "index.html"),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    # Function: _favicon
    @app.get("/favicon.ico", include_in_schema=False)
    async def _favicon():
        ico = _DIST_DIR / "favicon.ico"
        return FileResponse(str(ico)) if ico.exists() else HTMLResponse("", status_code=204)

    # Function: _spa_or_error
    @app.exception_handler(StarletteHTTPException)
    async def _spa_or_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404 and not request.url.path.startswith("/api"):
            return _spa_index_response()
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    # Function: _index
    @app.get("/", include_in_schema=False)
    async def _index():
        return _spa_index_response()


# ─── Auth helpers ─────────────────────────────────────────────────────────────
# Function: _auth_required
def _auth_required() -> bool:
    # Standalone/local operation is the default. Network deployments can opt
    # into this module's own signed-token validation with AUTH_REQUIRED=true.
    return os.getenv("AUTH_REQUIRED", "false").lower() in {"1", "true", "yes"}


# Function: _token_secret
def _token_secret() -> str:
    secret = (os.getenv("AUTH_TOKEN_SECRET") or "").strip()
    if secret and secret != _INSECURE_DEFAULT_AUTH_SECRET:
        return secret
    if _auth_required():
        allow_insecure = os.getenv("ALLOW_INSECURE_AUTH_SECRET", "false").lower() in {"1", "true", "yes"}
        if allow_insecure:
            logger.warning("Using insecure AUTH_TOKEN_SECRET because ALLOW_INSECURE_AUTH_SECRET=true")
            return _INSECURE_DEFAULT_AUTH_SECRET
        raise RuntimeError(
            "AUTH_TOKEN_SECRET must be set to a strong non-default value when AUTH_REQUIRED=true"
        )
    return _INSECURE_DEFAULT_AUTH_SECRET


# Function: _fs_root_path
def _fs_root_path() -> Path | None:
    configured = (os.getenv("MODERNIZATION_FS_ROOT") or "").strip()
    if not configured:
        return None
    p = Path(configured)
    return p.resolve() if p.exists() else None


# Function: _is_path_within
def _is_path_within(base: Path, target: Path) -> bool:
    try:
        return os.path.commonpath([str(base), str(target)]) == str(base)
    except ValueError:
        return False


# Function: _b64url_decode
def _b64url_decode(text: str) -> bytes:
    padding = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


# Function: _b64url_encode
def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# Function: _extract_bearer_token
def _extract_bearer_token(authorization_header: str) -> str | None:
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


# Function: _decode_access_token
def _decode_access_token(token: str) -> dict:
    if not token:
        raise ValueError("Missing token")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise ValueError("Malformed token")
    payload_encoded = parts[1]
    expected_signature = _b64url_encode(
        hmac.new(
            _token_secret().encode("utf-8"),
            payload_encoded.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    )
    if not hmac.compare_digest(expected_signature, parts[2]):
        raise ValueError("Invalid token signature")
    payload = json.loads(_b64url_decode(payload_encoded).decode("utf-8"))
    if payload.get("typ") != "access":
        raise ValueError("Invalid token type")
    exp = int(payload.get("exp", 0))
    if exp <= int(time.time()):
        raise ValueError("Token expired")
    return payload


# Function: _decode_token_for_refresh
def _decode_token_for_refresh(token: str) -> dict:
    """Same signature check as _decode_access_token, but tolerates an `exp`
    that has already passed as long as it's within _REFRESH_GRACE_SECONDS.

    A renewal request only reaches here holding a token whose HMAC signature
    we can already verify, so the caller is not forging identity — it is
    asking to extend a session it legitimately held.
    """
    if not token:
        raise ValueError("Missing token")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise ValueError("Malformed token")
    payload_encoded = parts[1]
    expected_signature = _b64url_encode(
        hmac.new(
            _token_secret().encode("utf-8"),
            payload_encoded.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    )
    if not hmac.compare_digest(expected_signature, parts[2]):
        raise ValueError("Invalid token signature")
    payload = json.loads(_b64url_decode(payload_encoded).decode("utf-8"))
    if payload.get("typ") != "access":
        raise ValueError("Invalid token type")
    exp = int(payload.get("exp", 0))
    if exp <= int(time.time()) - _REFRESH_GRACE_SECONDS:
        raise ValueError("Token too old to refresh; sign in again")
    return payload


# Function: _issue_access_token
def _issue_access_token(payload: dict) -> tuple[str, int]:
    ttl_seconds = int(os.getenv("AUTH_TOKEN_TTL_SECONDS", str(8 * 60 * 60)))
    exp = int(time.time()) + ttl_seconds
    new_payload = {
        "typ": "access",
        "sub": payload.get("sub"),
        "role": payload.get("role"),
        "apps": payload.get("apps") or [],
        "exp": exp,
    }
    payload_b64 = _b64url_encode(json.dumps(new_payload).encode("utf-8"))
    signature = _b64url_encode(
        hmac.new(
            _token_secret().encode("utf-8"),
            payload_b64.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    )
    return f"v1.{payload_b64}.{signature}", exp


# Function: enforce_auth
@app.middleware("http")
async def enforce_auth(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.scope.get("path", request.url.path)
    # Accept the historical public prefix so cached clients and existing
    # bookmarks keep working. This is an in-process alias to this module's own
    # /api routes; it does not proxy to or depend on another application.
    if path == "/api/mod" or path.startswith("/api/mod/"):
        path = "/api" + path[len("/api/mod"):]
        request.scope["path"] = path
        request.scope["raw_path"] = path.encode("utf-8")
    public_paths = {
        "/api/health",
        "/api/auth/login",
        "/api/auth/credential",
        # Loopback-only operational probe used by the safe auto-publisher.
        # The endpoint itself rejects non-loopback clients.
        "/api/modernize/jobs/active-count",
        "/docs",
        "/openapi.json",
        "/redoc",
    }
    if not path.startswith("/api") or path in public_paths:
        return await call_next(request)
    # SSE streams, file downloads, and fs/ls cannot send custom headers — accept token via ?token= query param
    auth_header = request.headers.get("Authorization", "")
    if not auth_header and (path.endswith(("/stream", "/output", "/export")) or path.startswith("/api/fs/")):
        token_qp = request.query_params.get("token", "")
        if token_qp:
            auth_header = f"Bearer {token_qp}"
    token = _extract_bearer_token(auth_header)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    try:
        payload = _ACCESS_STORE.validate_session(token)
    except RuntimeError as exc:
        return JSONResponse(status_code=503, content={"error": str(exc)})
    except ValueError as exc:
        return JSONResponse(status_code=401, content={"error": str(exc)})
    role = payload.get("role")
    apps = payload.get("apps") or []
    if role != "admin" and MODERNIZATION_APP not in apps:
        return JSONResponse(status_code=403, content={"error": "Access denied for Modernization"})
    request.state.auth = payload
    response = await call_next(request)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and path.startswith("/api/projects") and response.status_code < 400:
        try:
            _ACCESS_STORE.sync_projects([
                _PROJECT_STORE.get_project(project["id"]) for project in _PROJECT_STORE.list_projects()
            ])
        except Exception:
            logger.exception("Failed to persist project state to PostgreSQL")
            return JSONResponse(status_code=503, content={"error": "Project state could not be persisted to PostgreSQL"})
    return response


# ─── In-memory job store ──────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_JOBS: dict[str, dict] = {}
_REQUIREMENTS_JOBS: dict[str, dict] = {}
_JOB_QUEUES: dict[str, queue.Queue] = {}
_JOB_FUTURES: dict[str, object] = {}
_JOB_CANCEL_EVENTS: dict[str, threading.Event] = {}
_JOBS_LOCK = threading.RLock()
_PERSIST_LOCK = threading.Lock()
_LAST_JOB_PERSIST: dict[str, float] = {}
_JOB_CHECKPOINT_SECONDS = max(0.25, float(os.getenv("MODERNIZATION_JOB_CHECKPOINT_SECONDS", "1.5")))
_JOB_EVENT_LIMIT = max(100, int(os.getenv("MODERNIZATION_JOB_EVENT_LIMIT", "1000")))
_JOB_STREAM_KEEPALIVE_SECONDS = max(
    5.0, float(os.getenv("MODERNIZATION_JOB_STREAM_KEEPALIVE_SECONDS", "15")),
)
# The production host has one Ollama/GPU inference lane. Two top-level jobs
# overlap their controller/entity/repair calls, causing both to hit the
# per-file deadline even though either job completes normally in isolation.
# Multi-GPU deployments can explicitly raise this override.
_JOB_WORKERS = max(1, min(8, int(os.getenv("MODERNIZATION_JOB_WORKERS", "1"))))
_JOB_EXECUTOR = ThreadPoolExecutor(max_workers=_JOB_WORKERS, thread_name_prefix="modernization-job")
try:
    _JOB_QUEUE_CAPACITY = max(0, int(os.getenv("MODERNIZATION_JOB_QUEUE_CAPACITY", "4")))
except ValueError:
    _JOB_QUEUE_CAPACITY = 4
_JOB_ADMISSION = threading.BoundedSemaphore(_JOB_WORKERS + _JOB_QUEUE_CAPACITY)
atexit.register(lambda: _JOB_EXECUTOR.shutdown(wait=False, cancel_futures=True))

# Job state is snapshotted to disk so a backend restart doesn't silently
# orphan every in-flight or completed job — this box restarts the
# Modernization process on its own every few minutes under load (see
# Data/logs/watchdog_master.log), which previously rebuilt `_JOBS` empty and
# turned every existing job_id into a permanent 404, download and all.
_JOBS_DIR = Path(os.getenv(
    "MODERNIZATION_JOB_STATE_DIR",
    str(Path(tempfile.gettempdir()) / "modernization_jobs"),
)).expanduser()
_JOBS_DIR.mkdir(parents=True, exist_ok=True)
_REQUIREMENTS_JOBS_DIR = _JOBS_DIR / "requirements"
_REQUIREMENTS_JOBS_DIR.mkdir(parents=True, exist_ok=True)
_JOB_TTL_HOURS = 7 * 24  # keep completed job state (and its downloadable output) for a week


# Function: _job_file
def _job_file(job_id: str) -> Path:
    # job_id is always server-generated (uuid4, or uuid4()[:8] for prompt
    # jobs) but sanitize defensively before touching the filesystem with it.
    safe_id = re.sub(r"[^\w-]", "_", job_id)
    return _JOBS_DIR / f"{safe_id}.json"


def _requirements_job_file(job_id: str) -> Path:
    safe_id = re.sub(r"[^\w-]", "_", job_id)
    return _REQUIREMENTS_JOBS_DIR / f"{safe_id}.json"


def _persist_requirements_job(job_id: str, *, force: bool = True) -> None:
    """Checkpoint BRD/FSD job state so polling survives a process recycle."""
    now = time.monotonic()
    persist_key = f"requirements:{job_id}"
    with _JOBS_LOCK:
        job = _REQUIREMENTS_JOBS.get(job_id)
        if not job:
            return
        if not force and now - _LAST_JOB_PERSIST.get(persist_key, 0.0) < _JOB_CHECKPOINT_SECONDS:
            return
        snapshot = copy.deepcopy(job)
    try:
        payload = json.dumps(snapshot, default=str)
        destination = _requirements_job_file(job_id)
        temporary = destination.with_suffix(".tmp")
        with _PERSIST_LOCK:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(destination)
            _LAST_JOB_PERSIST[persist_key] = now
        try:
            _ACCESS_STORE.sync_job(snapshot, "requirements")
        except Exception:
            logger.warning("Failed to persist requirements job %s to PostgreSQL", job_id, exc_info=True)
    except OSError:
        logger.warning("Failed to persist requirements job %s", job_id, exc_info=True)


# Function: _persist_job
def _persist_job(job_id: str, *, force: bool = True) -> None:
    """Atomically checkpoint bounded job state, throttling non-terminal events."""
    now = time.monotonic()
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        if not force and now - _LAST_JOB_PERSIST.get(job_id, 0.0) < _JOB_CHECKPOINT_SECONDS:
            return
        snapshot = copy.deepcopy(job)
        snapshot["events"] = snapshot.get("events", [])[-_JOB_EVENT_LIMIT:]
    # Persist a bounded operational trail so generation/build failures remain
    # diagnosable after a backend restart without unbounded snapshot growth.
    try:
        payload = json.dumps(snapshot, default=str)
        destination = _job_file(job_id)
        temporary = destination.with_suffix(".tmp")
        with _PERSIST_LOCK:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(destination)
            _LAST_JOB_PERSIST[job_id] = now
        try:
            _ACCESS_STORE.sync_job(snapshot)
        except Exception:
            logger.warning("Failed to persist job %s to PostgreSQL", job_id, exc_info=True)
    except OSError:
        logger.warning("Failed to persist job %s to disk", job_id, exc_info=True)


# Function: _sweep_old_jobs
def _sweep_old_jobs(max_age_hours: float = _JOB_TTL_HOURS) -> None:
    """Best-effort cleanup of job snapshots older than max_age_hours."""
    cutoff = time.time() - max_age_hours * 3600
    for f in _JOBS_DIR.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue


# Function: _load_persisted_jobs
def _load_persisted_jobs() -> None:
    """Restore job state from disk at process startup. A job still marked
    "running" belonged to a worker thread in the PREVIOUS process — that
    thread no longer exists, so it can never actually finish. Surface that
    plainly instead of leaving the UI's progress bar frozen forever with no
    explanation."""
    _sweep_old_jobs()
    for f in _JOBS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        job_id = data.get("job_id") or f.stem
        active_before_restart = data.get("status") in {"running", "pending", "queued"}
        interrupted_cancellation = bool(
            data.get("cancel_requested")
            and data.get("status") == "failed"
            and data.get("phase") == "interrupted"
        )
        if data.get("cancel_requested") and (active_before_restart or interrupted_cancellation):
            data["status"] = "cancelled"
            data["phase"] = "cancelled"
            data["error"] = "Generation cancelled by explicit operator request."
        elif active_before_restart:
            data["status"] = "failed"
            data["phase"] = "interrupted"
            partial = data.get("output") or {}
            data["error"] = (
                f"Job was interrupted by a backend restart after generating {len(partial)} "
                "file(s) — partial output is available for download below." if partial else
                "Job was interrupted by a backend restart. Please start a new job."
            )
        data.setdefault("events", [])
        _JOBS[job_id] = data
        _persist_job(job_id)


def _load_database_jobs() -> None:
    """Restore recent job checkpoints from PostgreSQL when the container volume is new."""
    try:
        snapshots = _ACCESS_STORE.load_jobs("modernization", max_age_days=7)
    except Exception:
        logger.warning("Failed to restore job checkpoints from PostgreSQL", exc_info=True)
        return

    def updated_at(value: dict) -> datetime:
        try:
            timestamp = datetime.fromisoformat(str(value.get("updated_at", "")))
            return timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=UTC)

    for snapshot in snapshots:
        job_id = str(snapshot.get("job_id") or "")
        if not job_id:
            continue
        with _JOBS_LOCK:
            current = _JOBS.get(job_id)
            if current and updated_at(current) >= updated_at(snapshot):
                continue
        data = copy.deepcopy(snapshot)
        active_before_restart = data.get("status") in {"running", "pending", "queued"}
        interrupted_cancellation = bool(
            data.get("cancel_requested")
            and data.get("status") == "failed"
            and data.get("phase") == "interrupted"
        )
        if data.get("cancel_requested") and (active_before_restart or interrupted_cancellation):
            data.update({
                "status": "cancelled",
                "phase": "cancelled",
                "error": "Generation cancelled by explicit operator request.",
                "updated_at": datetime.now(UTC).isoformat(),
            })
        elif active_before_restart:
            partial = data.get("output") or {}
            data.update({
                "status": "failed",
                "phase": "interrupted",
                "error": (
                    f"Job was interrupted by a backend restart after generating {len(partial)} "
                    "file(s) — partial output is available for download below." if partial else
                    "Job was interrupted by a backend restart. Please start a new job."
                ),
                "updated_at": datetime.now(UTC).isoformat(),
            })
        data.setdefault("events", [])
        with _JOBS_LOCK:
            _JOBS[job_id] = data
        _persist_job(job_id)


def _load_persisted_requirements_jobs() -> None:
    """Restore terminal jobs and turn orphaned in-flight jobs into explicit failures."""
    cutoff = time.time() - _JOB_TTL_HOURS * 3600
    for path in _REQUIREMENTS_JOBS_DIR.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        job_id = str(data.get("job_id") or path.stem)
        if data.get("status") in {"running", "pending", "queued"}:
            data.update({
                "status": "failed",
                "phase": "interrupted",
                "message": "Requirements generation was interrupted",
                "error": "The backend restarted while generating this document. Please regenerate it.",
                "updated_at": datetime.now(UTC).isoformat(),
            })
        _REQUIREMENTS_JOBS[job_id] = data
        _persist_requirements_job(job_id)


_load_persisted_jobs()
_load_persisted_requirements_jobs()


# Function: _get_job
def _get_job(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


def _effective_job_target_stack(job: dict) -> tuple[str | None, str | None]:
    """Return the stack evidenced by the generated artifact when available.

    Older prompt jobs persisted the form's preset before prompt signals were
    applied inside generation. Their governed manifest records the effective
    language/framework actually used, so reports must prefer that evidence
    without rewriting the historical checkpoint.
    """
    requested = str(job.get("target_stack") or "").strip() or None
    stack = (
        ((job.get("validation") or {}).get("governance") or {})
        .get("reports", {}).get("requirements-manifest.json", {}).get("stack", {})
    )
    description = " ".join(str(stack.get(key) or "") for key in (
        "language", "backend", "frontend", "name",
    )).strip()
    effective = requested
    if description:
        resolved = _resolve_prompt_analysis_target(description, "prompt_inferred")
        if resolved != "custom":
            effective = resolved
    from services.modernizer.target_config import TARGET_STACKS
    name = str(TARGET_STACKS.get(effective or "", {}).get("name") or effective or "") or None
    return effective, name


# Function: _job_response
def _job_response(job: dict) -> dict:
    """Return job state without repeatedly transferring a growing partial project."""
    with _JOBS_LOCK:
        data = dict(job)
        effective_stack, effective_name = _effective_job_target_stack(job)
        data["requested_target_stack"] = job.get("target_stack")
        data["target_stack"] = effective_stack
        data["target_stack_name"] = effective_name
        data["events"] = list(job.get("events", []))[-_JOB_EVENT_LIMIT:]
        output = job.get("output") or {}
        data["generated_file_count"] = len(output)
        if _is_active_job(job):
            data["output"] = None
    return data


def _job_status_response(job: dict) -> dict:
    """Return the bounded fields required by the live progress indicator.

    The regular job response intentionally retains analysis, event history and
    completed output for API compatibility. Re-downloading that growing state
    every two seconds made the fallback poll unnecessarily expensive and more
    likely to be rejected by the upstream proxy while Ollama was busy.
    """
    fields = (
        "job_id", "status", "progress", "phase", "error", "updated_at",
    )
    with _JOBS_LOCK:
        data = {field: job.get(field) for field in fields}
        data["generated_file_count"] = len(job.get("output") or {})
    return data


def _job_list_summary(job: dict) -> dict:
    """Project the stable fields used by the jobs page.

    Persisted checkpoints predate some of the current job fields, and focused
    tests can also create deliberately minimal checkpoints.  A single legacy
    record must not make the complete jobs collection unavailable.
    """
    updated_at = job.get("updated_at")
    effective_stack, effective_name = _effective_job_target_stack(job)
    return {
        "job_id": job.get("job_id"),
        "project_id": job.get("project_id"),
        "folder_path": job.get("folder_path"),
        "prompt": job.get("prompt"),
        "status": job.get("status", "unknown"),
        "progress": job.get("progress", 0),
        "phase": job.get("phase"),
        "requested_target_stack": job.get("target_stack"),
        "target_stack": effective_stack,
        "target_stack_name": effective_name,
        "created_at": job.get("created_at") or updated_at,
        "updated_at": updated_at,
        "llm_model": job.get("llm_model"),
        "optimization_profile": job.get("optimization_profile"),
        "generated_file_count": len(job.get("output") or {}),
        "output_available": bool(job.get("output")) and job.get("status") in ("completed", "validation_failed", "failed"),
    }


def _generation_runtime_snapshot() -> dict:
    """Capture immutable execution settings for job audit and reporting."""
    try:
        from services.llm import check_status
        status = check_status()
        runtime = status.get("runtime") or {}
        return {
            "llm_model": runtime.get("selected_model") or status.get("active_model"),
            "optimization_profile": runtime.get("selected_profile") or "balanced",
            "parallelism": runtime.get("effective") or {},
        }
    except Exception:
        return {"llm_model": None, "optimization_profile": "balanced", "parallelism": {}}


# Function: _is_active_job
def _is_active_job(job: dict) -> bool:
    return job.get("status") not in ("completed", "validation_failed", "failed", "cancelled")


class _JobCancelled(Exception):
    """Internal signal raised by progress callbacks when an operator cancels a job."""


def _raise_if_job_cancelled(job_id: str) -> None:
    with _JOBS_LOCK:
        cancel_event = _JOB_CANCEL_EVENTS.get(job_id)
    if cancel_event and cancel_event.is_set():
        raise _JobCancelled("Generation cancelled by request")


def _finish_job_cancelled(job_id: str, message: str = "Generation cancelled by request") -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job or not _is_active_job(job):
            return
        job.update({"status": "cancelled", "phase": "cancelled", "error": message})
    _persist_job(job_id)
    _push(job_id, {"type": "cancelled", "phase": "cancelled", "message": message})


def _submit_generation_job(job_id: str, worker, *args) -> None:
    """Queue CPU/LLM-heavy work on a bounded executor instead of spawning an
    unbounded daemon thread for every HTTP request."""
    with _JOBS_LOCK:
        _JOBS[job_id]["status"] = "queued"
        _JOBS[job_id]["phase"] = "queued"
    _push(job_id, {
        "type": "queued", "phase": "queued", "progress": 0,
        "message": f"Generation queued ({_JOB_WORKERS} worker slots available)",
    })
    if not _JOB_ADMISSION.acquire(blocking=False):
        _record_worker_failure(
            job_id,
            RuntimeError(
                "Generation capacity is full. Wait for an active job to finish and retry."
            ),
        )
        return
    with _JOBS_LOCK:
        cancel_event = threading.Event()
        _JOB_CANCEL_EVENTS[job_id] = cancel_event
    try:
        future = _JOB_EXECUTOR.submit(worker, job_id, *args)
        with _JOBS_LOCK:
            _JOB_FUTURES[job_id] = future

        def release_capacity(done_future):
            worker_exception = None
            try:
                worker_exception = done_future.exception()
            except CancelledError:
                worker_exception = None
            except BaseException as callback_exc:
                worker_exception = callback_exc
            with _JOBS_LOCK:
                if _JOB_FUTURES.get(job_id) is done_future:
                    _JOB_FUTURES.pop(job_id, None)
                    _JOB_CANCEL_EVENTS.pop(job_id, None)
            _JOB_ADMISSION.release()
            if worker_exception is not None:
                _record_worker_failure(
                    job_id, worker_exception,
                    operation=f"background worker {getattr(worker, '__name__', 'unknown')}",
                )

        future.add_done_callback(release_capacity)
    except RuntimeError as exc:
        _JOB_ADMISSION.release()
        with _JOBS_LOCK:
            _JOB_CANCEL_EVENTS.pop(job_id, None)
        _record_worker_failure(job_id, exc)


def _resolve_quick_analysis_target(folder_path: str, requested_target: str) -> str:
    """Keep Quick Analysis from silently treating a Java repository as C#.

    The Quick Analysis screen historically sent its legacy ``aveva_mes``
    default without exposing a target selector. For a Java-only repository
    that is missing metadata, not an operator-selected Java-to-.NET migration.
    Resolve only that ambiguous default; explicit non-default and governed
    project targets remain untouched.
    """
    if requested_target != "aveva_mes":
        return requested_target

    java_files = 0
    dotnet_files = 0
    try:
        for path in Path(folder_path).rglob("*"):
            if not path.is_file():
                continue
            suffix = path.suffix.casefold()
            if suffix in {".java", ".kt", ".kts", ".groovy"}:
                java_files += 1
            elif suffix in {".cs", ".vb", ".aspx", ".ascx", ".cshtml", ".razor"}:
                dotnet_files += 1
    except OSError:
        return requested_target

    if java_files and dotnet_files == 0:
        logger.info(
            "Quick Analysis resolved legacy default target from source: "
            "java_files=%d target=spring_boot",
            java_files,
        )
        return "spring_boot"
    return requested_target


def _resolve_prompt_analysis_target(
    prompt: str, requested_target: str, custom_stack_desc: str = "",
) -> str:
    """Resolve a prompt form's non-selection sentinel to a real target stack.

    ``aveva_mes`` was historically sent by the prompt UI even when nobody
    selected it. That made unrelated requests inherit AVEVA's C#, SQL Server,
    and MES defaults. Only that legacy default and the explicit inference
    sentinel are resolved; a caller-selected target remains authoritative.
    """
    requested = str(requested_target or "").strip()
    description = "\n".join(
        part.strip() for part in (prompt, custom_stack_desc) if part and part.strip()
    )
    from services.modernizer.domain_generators.stack_signals import _detect_stack_signals
    from services.modernizer.target_config import _infer_target_language

    signals = _detect_stack_signals(description)
    backend = str(signals.get("backend") or "").casefold()
    frontend = str(signals.get("frontend") or "").casefold()
    description_low = description.casefold()

    if "aveva" in description_low or "manufacturing execution system" in description_low:
        return "aveva_mes"
    if "fastapi" in backend:
        return "python_fastapi"
    if "django" in backend:
        return "python_django"
    if "quarkus" in backend:
        return "java_quarkus"
    if "micronaut" in backend:
        return "java_micronaut"
    if "jakarta" in backend:
        return "java_jakarta"
    if "java ee" in backend or "struts" in backend:
        return "java_legacy"
    if "spring" in backend:
        return "spring_boot_react" if "react" in frontend else "spring_boot"
    if ".net" in backend or "asp.net" in backend:
        major = "10" if "10" in backend else "9" if "9" in backend else "8"
        if "angular" in frontend:
            return {"8": "dotnet_angular", "9": "dotnet9_angular", "10": "dotnet10_angular"}[major]
        if "react" in frontend:
            return "dotnet_react"
        if "blazor" in frontend:
            return "dotnet10_blazor" if major == "10" else "dotnet8_blazor"
        return "dotnet9_webapi" if major == "9" else "dotnet10_mvc" if major == "10" else "dotnet8_mvc"
    if "nestjs" in backend:
        return "node_nest_react"
    if "express" in backend:
        return "node_express_react"
    if "node.js" in backend:
        return "javascript_node"
    if backend:
        # The prompt named a backend for which there is no exact catalog
        # preset (for example Flask). Keep it custom so the named framework
        # survives instead of silently relabeling it as another framework.
        return "custom"

    if requested not in {"", "aveva_mes", "prompt_inferred"}:
        from services.modernizer.target_config import TARGET_STACKS
        requested_language = str(TARGET_STACKS.get(requested, {}).get("language") or "").casefold()
        inferred_language = _infer_target_language(description, default="")
        if not inferred_language or inferred_language == requested_language:
            return requested

    if frontend.startswith("angular"):
        version = re.search(r"\b(17|18|19|20|21|22)\b", frontend)
        return {
            "17": "angular17_ts", "18": "angular18_ts", "19": "angular19_ts",
            "20": "angular_ts", "21": "angular21_ts", "22": "angular22_ts",
        }.get(version.group(1) if version else "20", "angular_ts")
    if "react" in frontend:
        return "react_ts"
    if "vue" in frontend:
        return "vue3"

    language = _infer_target_language(description, default="")
    return {
        "python": "python_fastapi", "java": "spring_boot", "csharp": "dotnet8_mvc",
        "typescript": "react_ts", "javascript": "javascript_web", "go": "go_rest",
        "php": "php_laravel", "ruby": "ruby_rails", "rust": "rust_axum_react",
        "kotlin": "kotlin_ktor", "swift": "swift_vapor", "scala": "scala_play",
        "clojure": "clojure_ring", "haskell": "haskell_servant", "lisp": "common_lisp",
        "julia": "julia_application", "shell": "shell_automation", "r": "r_analytics",
        "c": "c_native", "cpp": "cpp_native",
    }.get(language, "custom")

# ─── Language / tech label maps (used by /api/fs/detect) ──────────────────────
_LANG_LABELS: dict[str, str] = {
    "csharp":           "C# (.NET)",
    "visualbasic":      "VB.NET",
    "aspnet-webforms":  "ASP.NET WebForms",
    "aspnet-razor":     "ASP.NET Razor",
    "java":             "Java",
    "python":           "Python",
    "javascript":       "JavaScript",
    "typescript":       "TypeScript",
    "php":              "PHP",
    "ruby":             "Ruby",
    "go":               "Go",
    "cobol":            "COBOL",
    "sql":              "SQL",
}

_TECH_LABELS: dict[str, str] = {
    "asp_net_webforms": "ASP.NET WebForms",
    "asp_net_mvc":      "ASP.NET MVC",
    "asp_net_core":     "ASP.NET Core",
    "java_ee":          "Java EE",
    "spring":           "Spring / Spring Boot",
    "oracle_db":        "Oracle Database",
    "sql_server":       "SQL Server",
    "entity_framework": "Entity Framework",
    "ado_net_raw":      "ADO.NET (raw SQL)",
    "jquery":           "jQuery",
    "react":            "React",
    "angular":          "Angular",
    "hibernate":        "Hibernate / JPA",
    "winforms":         "Windows Forms",
    "wpf":              "WPF",
}


# Function: _fs_list_no_path
def _fs_list_no_path(fs_root) -> dict:
    import string
    if fs_root is not None:
        target = fs_root
        try:
            subdirs = sorted(
                [p for p in target.iterdir() if p.is_dir() and not p.name.startswith(".")],
                key=lambda p: p.name.lower(),
            )
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied")
        return {
            "current": str(target),
            "parent": None,
            "dirs": [{"name": p.name, "path": str(p), "is_drive": False} for p in subdirs],
        }
    if os.name == "nt":
        drives = []
        for letter in string.ascii_uppercase:
            d = Path(f"{letter}:\\")
            if d.exists():
                drives.append({"name": str(d), "path": str(d), "is_drive": True})
        return {"current": "", "parent": None, "dirs": drives}
    root = Path("/")
    dirs = sorted(
        [{"name": p.name, "path": str(p), "is_drive": False}
         for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")],
        key=lambda x: x["name"].lower(),
    )
    return {"current": "/", "parent": None, "dirs": dirs}

# ─── Core API endpoints ───────────────────────────────────────────────────────

# Function: fs_list
@app.get("/api/fs/ls")
async def fs_list(path: str | None = None):
    """List subdirectories at *path*. Optionally restricted by MODERNIZATION_FS_ROOT."""
    fs_root = _fs_root_path()

    if not path:
        return _fs_list_no_path(fs_root)

    target = Path(path).resolve()
    if fs_root is not None and not _is_path_within(fs_root, target):
        raise HTTPException(status_code=403, detail="Path outside allowed root")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {path}")

    try:
        subdirs = sorted(
            [p for p in target.iterdir() if p.is_dir() and not p.name.startswith(".")],
            key=lambda p: p.name.lower(),
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    # Determine parent
    parent = str(target.parent) if target.parent != target else None
    if fs_root is not None and parent and not _is_path_within(fs_root, Path(parent)):
        parent = None

    return {
        "current": str(target),
        "parent": parent,
        "dirs": [{"name": p.name, "path": str(p), "is_drive": False} for p in subdirs],
    }


# ─── Upload-from-browser folder intake ─────────────────────────────────────
# The FolderBrowserModal ("Browse") only ever lists directories on the SERVER
# that runs this backend — a browser has no way to hand server-side code a
# path on the visitor's own machine. This endpoint is the other half: the
# frontend uses a native <input type="file" webkitdirectory> picker to read
# the user's chosen folder client-side, uploads every file (each UploadFile's
# `filename` carries its folder-relative path, e.g. "MyProject/src/Foo.cs" —
# see FormData.append(name, blob, file.webkitRelativePath) in the frontend),
# and this endpoint materializes them into a server-side temp directory. That
# temp path is then used exactly like a browsed path everywhere downstream
# (fs/detect, modernize/analyze) — zero changes needed to the analyze/
# modernize pipeline, which only ever consumed a folder_path string anyway.
_UPLOAD_SKIP_DIRS = {".git", ".vs", ".vscode", "bin", "obj", "node_modules",
                     "__pycache__", ".venv", "venv", "env", "dist", "build",
                     "target", "out", "packages", ".nuget", "TestResults",
                     ".gradle", ".idea", "coverage", ".next", ".nuxt",
                     ".mvn", ".svn", ".hg"}
_UPLOAD_MAX_FILES = 4000
_UPLOAD_MAX_BYTES = 200 * 1024 * 1024  # 200 MB — matches web.config maxAllowedContentLength
_UPLOAD_ROOT       = Path(tempfile.gettempdir()) / "modernization_uploads"


# Function: _sweep_old_uploads
def _sweep_old_uploads(max_age_hours: float = 6.0) -> None:
    """Best-effort cleanup of upload temp dirs older than max_age_hours."""
    if not _UPLOAD_ROOT.exists():
        return
    cutoff = time.time() - max_age_hours * 3600
    for child in _UPLOAD_ROOT.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


# Function: upload_folder
@app.post("/api/fs/upload-folder")
async def upload_folder(files: list[UploadFile] = File(...)):
    """Materialize a browser-uploaded folder into a server-side temp dir and
    return its path, for use exactly like a browsed folder_path."""
    if not files:
        raise HTTPException(status_code=400, detail="No files received")
    if len(files) > _UPLOAD_MAX_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"Too many files ({len(files)}); limit is {_UPLOAD_MAX_FILES}. "
                   "Exclude build output / dependency folders and try again.",
        )

    _sweep_old_uploads()
    _UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    dest_root = Path(tempfile.mkdtemp(dir=str(_UPLOAD_ROOT), prefix="upload_")).resolve()

    total_bytes = 0
    written = 0
    try:
        for uf in files:
            raw_name = (uf.filename or "").replace("\\", "/")
            parts = [p for p in raw_name.split("/") if p not in ("", ".", "..")]
            if not parts or any(part in _UPLOAD_SKIP_DIRS for part in parts[:-1]):
                await uf.read()  # drain so the multipart stream stays consistent
                continue

            dest_path = dest_root.joinpath(*parts)
            if not _is_path_within(dest_root, dest_path.resolve()):
                continue  # defense in depth against path traversal

            data = await uf.read()
            total_bytes += len(data)
            if total_bytes > _UPLOAD_MAX_BYTES:
                shutil.rmtree(dest_root, ignore_errors=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"Upload too large (limit {_UPLOAD_MAX_BYTES // (1024 * 1024)} MB). "
                           "Exclude build output / dependency folders and try again.",
                )

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(data)
            written += 1
    except HTTPException:
        raise
    except Exception as exc:
        shutil.rmtree(dest_root, ignore_errors=True)
        _raise_internal_error(exc, operation="upload project folder", artifact_path=str(dest_root))

    if written == 0:
        shutil.rmtree(dest_root, ignore_errors=True)
        raise HTTPException(status_code=400, detail="No usable files in upload after filtering")

    logger.info("Uploaded folder: %d files, %d bytes -> %s", written, total_bytes, dest_root)
    return {"path": str(dest_root), "file_count": written, "bytes": total_bytes}


# Function: health
@app.get("/api/health")
async def health():
    return {
        "status": "ok", "module": "Modernization", "port": int(os.getenv("PORT", "18084")),
        "deployment": "standalone",
        "python_runtime": _PYTHON_RUNTIME.public_dict(),
        "api_version": "2.0", "capabilities": [
            "governed_projects", "target_stack_catalog", "snapshot_governance",
            "toolchain_readiness", "resilient_generation_planning",
            "requirements_documentation", "requirements_knowledge_graph",
            "adaptive_llm_runtime", "jobs_analytics",
            "governed_generation_guidelines",
            "compiler_first_generation_repair",
            "record_inheritance_reconciliation",
            "generated_import_sanitization",
            "mutable_record_reconciliation",
            "java_source_seam_repair",
            "java_source_topology_preservation",
        ],
    }


# ── Governed, Git-independent projects ─────────────────────────────────────
# Function: _project_or_404
def _project_or_404(project_id: str) -> dict:
    try:
        return _PROJECT_STORE.get_project(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Modernization project not found")


# Function: _snapshot_or_404
def _snapshot_or_404(project_id: str, snapshot_id: str) -> dict:
    try:
        return _PROJECT_STORE.get_snapshot(project_id, snapshot_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Snapshot not found")


# Function: _latest
def _latest(project: dict, kind: str) -> dict | None:
    return next((item for item in project.get("snapshots", []) if item["kind"] == kind), None)


# Function: _artifact
def _artifact(snapshot: dict, filename: str = "artifact.json") -> dict:
    path = Path(snapshot["path"]) / filename
    if not path.exists():
        raise HTTPException(status_code=409, detail=f"Snapshot has no {filename}")
    return json.loads(path.read_text(encoding="utf-8"))


# Function: list_projects
@app.get("/api/projects")
async def list_projects():
    return {"projects": _PROJECT_STORE.list_projects()}


# Function: list_project_jobs
@app.get("/api/projects/{project_id}/jobs")
async def list_project_jobs(project_id: str):
    _project_or_404(project_id)
    jobs = [
        {"job_id": job["job_id"], "project_id": project_id, "status": job.get("status"),
         "phase": job.get("phase"), "progress": job.get("progress", 0), "error": job.get("error"),
         "created_at": job.get("created_at"), "updated_at": job.get("updated_at"),
         "target_stack": job.get("target_stack")}
        for job in _JOBS.values() if job.get("project_id") == project_id
    ]
    jobs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return {"jobs": jobs}


def _project_identity_payload(project: dict) -> dict:
    configuration = project.get("configuration") or {}
    return {
        "project_id": str(project.get("id") or ""),
        "project_name": str(project.get("name") or configuration.get("application_name") or ""),
        "application_key": str(configuration.get("application_key") or configuration.get("project_key") or ""),
        "application_name": str(configuration.get("application_name") or project.get("name") or ""),
        "client_name": str(configuration.get("client_name") or configuration.get("customer") or ""),
        "application_owner": str(configuration.get("application_owner") or ""),
        "business_unit": str(configuration.get("business_unit") or ""),
        "business_criticality": str(configuration.get("business_criticality") or ""),
    }


# Function: _project_creation_configuration
def _project_creation_configuration(body: dict) -> tuple[dict, str]:
    """Normalize creation metadata without requiring prompt-inferable stack fields."""
    configuration = dict(body.get("configuration") or {})
    client_name = str(configuration.get("client_name") or configuration.get("customer") or "").strip()
    application_name = str(configuration.get("application_name") or body.get("name") or "").strip()
    application_key = str(configuration.get("application_key") or configuration.get("project_key") or "").strip()
    if not application_key and application_name:
        application_key = re.sub(r"[^A-Za-z0-9]+", "-", application_name).strip("-").upper()[:40]
    configuration.update({
        "client_name": client_name,
        "customer": client_name,
        "application_name": application_name,
        "application_key": application_key,
        "project_key": application_key,
        "application_owner": str(configuration.get("application_owner") or "").strip(),
        "business_unit": str(configuration.get("business_unit") or "").strip(),
        "business_criticality": str(configuration.get("business_criticality") or "Medium").strip(),
    })
    origin_mode = str(configuration.get("origin_mode") or body.get("origin_mode") or "existing_source")
    configuration["origin_mode"] = origin_mode
    target = str(configuration.get("engine_target") or configuration.get("target_stack") or "")
    if origin_mode == "prompt" and target == "custom":
        configuration["custom_stack_desc"] = str(configuration.get("custom_stack_desc") or "").strip()
        if not str(configuration.get("target_stack_name") or "").strip():
            configuration["target_stack_name"] = "Inferred from project prompt"
    if origin_mode == "prompt":
        configuration["project_prompt"] = str(
            body.get("project_prompt") or configuration.get("project_prompt") or ""
        ).strip()
    guidelines = str(configuration.get("generation_guidelines") or "").strip()
    if len(guidelines) > 60_000:
        raise ValueError("generation_guidelines exceeds the 60,000 character governed limit")
    configuration["generation_guidelines"] = guidelines
    configuration["guideline_documents"] = list(configuration.get("guideline_documents") or [])
    configuration["guidelines_sha256"] = str(configuration.get("guidelines_sha256") or "")
    return configuration, origin_mode


def _snapshot_project_guidelines(project: dict, actor: str) -> None:
    configuration = project.get("configuration") or {}
    text = str(configuration.get("generation_guidelines") or "").strip()
    if not text:
        return
    source = _latest(project, "source")
    _PROJECT_STORE.add_json_snapshot(
        project["id"], "guidelines",
        {
            "documents": configuration.get("guideline_documents") or [],
            "sha256": configuration.get("guidelines_sha256") or hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "combined_text": text,
        },
        actor,
        {"purpose": "Authoritative coding, security, and delivery requirements"},
        source["id"] if source else None,
        "generation-guidelines.json",
    )


# Function: create_project
@app.post("/api/projects")
async def create_project(request: Request):
    body = await request.json()
    try:
        configuration, origin_mode = _project_creation_configuration(body)
        if origin_mode == "prompt":
            project = _PROJECT_STORE.create_prompt_project(
                str(body.get("name") or "New application"), str(body.get("project_prompt") or ""),
                _actor(request), configuration, max(1, int(body.get("retention_days", 365))),
            )
            source = _latest(project, "source")
            index = semantic_index(Path(source["path"]))
            analysis = {
                "summary": {
                    "project_origin": "Greenfield application generated from a governed prompt",
                    "business_context": configuration.get("description") or "Not provided",
                    "current_architecture": "No existing architecture; the approved project brief is the baseline.",
                    "architecture_constraint": configuration.get("architecture") or "To be defined during plan review",
                    "deployment_constraint": configuration.get("deployment") or "Not specified",
                },
                "project_type": "greenfield", "source": "prompt",
                "project_prompt": str(body.get("project_prompt") or ""),
                "generation_guidelines": configuration.get("generation_guidelines") or "",
                "guideline_documents": configuration.get("guideline_documents") or [],
                "tech_stack": ["Greenfield – no legacy source technology stack"],
                "requested_target": {
                    "name": configuration.get("target_stack_name"), "language": configuration.get("language"),
                    "framework": configuration.get("framework"), "runtime": configuration.get("runtime"),
                    "frontend": configuration.get("frontend"), "database": configuration.get("database"),
                    "architecture": configuration.get("architecture"), "deployment": configuration.get("deployment"),
                },
            }
            _PROJECT_STORE.add_json_snapshot(project["id"], "analysis", {"analysis": analysis, "semantic_index": index},
                _actor(request), {"target_stack": configuration.get("engine_target"),
                "custom_stack_desc": configuration.get("custom_stack_desc", ""), "parent_source": source["id"]}, source["id"])
            _PROJECT_STORE.set_status(project["id"], "Analyzed")
            project = _PROJECT_STORE.get_project(project["id"])
            _snapshot_project_guidelines(project, _actor(request))
            return _PROJECT_STORE.get_project(project["id"])
        source = Path(str(body.get("source_path") or "")).resolve()
        project = _PROJECT_STORE.create_project(str(body.get("name") or source.name or "Modernization project"), source,
            _actor(request), configuration, max(1, int(body.get("retention_days", 365))))
        _snapshot_project_guidelines(project, _actor(request))
        return _PROJECT_STORE.get_project(project["id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# Function: get_project
@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    return _project_or_404(project_id)


def _wire_requirement_identity(artifact: dict, project: dict) -> dict:
    """Attach canonical identity to current and legacy requirement artifacts."""
    value = copy.deepcopy(artifact)
    identity = _project_identity_payload(project)
    value["project_identity"] = identity
    if value.get("document_type") != "knowledge_graph" and not (
        isinstance(value.get("nodes"), list) and isinstance(value.get("edges"), list)
    ):
        return value
    root_id = f"project:{identity['project_id']}"
    nodes = value.setdefault("nodes", [])
    if not any(str(node.get("id")) == root_id for node in nodes if isinstance(node, dict)):
        nodes.insert(0, {
            "id": root_id, "label": f"{identity['project_id']} · {identity['project_name']}",
            "type": "business", "description": (
                f"Governed project for {identity['client_name'] or 'unspecified client'}; "
                f"application key {identity['application_key'] or 'not specified'}."
            ),
        })
    for node in nodes:
        if isinstance(node, dict):
            node.update({key: identity[key] for key in ("project_id", "project_name", "application_key", "client_name")})
    edges = value.setdefault("edges", [])
    for edge in edges:
        if isinstance(edge, dict):
            edge["project_id"] = identity["project_id"]
    connected = {str(edge.get("target")) for edge in edges if isinstance(edge, dict) and str(edge.get("source")) == root_id}
    for node in nodes[1:]:
        node_id = str(node.get("id") or "") if isinstance(node, dict) else ""
        if node_id and node_id not in connected and node.get("type") in {"business", "feature", "functional"}:
            edges.append({"source": root_id, "target": node_id, "relationship": "governs", "project_id": identity["project_id"]})
    return value


@app.get("/api/projects/{project_id}/requirements/{document_type}")
async def get_requirement_document(project_id: str, document_type: str):
    project = _project_or_404(project_id)
    canonical_type = "fsd" if document_type == "frd" else document_type
    if canonical_type not in {"brd", "fsd", "knowledge_graph"}:
        raise HTTPException(status_code=404, detail="Unknown requirements document type")
    snapshot = _latest(project, canonical_type)
    if not snapshot and canonical_type == "fsd":
        snapshot = _latest(project, "frd")
    if not snapshot:
        return {"artifact": None, "snapshot": None}
    artifact = _wire_requirement_identity(_artifact(snapshot), project)
    if canonical_type == "fsd" and artifact.get("document_type") == "frd":
        artifact = {**artifact, "document_type": "fsd", "title": "Functional Specification Document"}
    return {"artifact": artifact, "snapshot": snapshot}


@app.get("/api/projects/{project_id}/requirements/{document_type}/export")
async def export_requirement_document(project_id: str, document_type: str):
    project = _project_or_404(project_id)
    canonical_type = "fsd" if document_type == "frd" else document_type
    if canonical_type not in {"brd", "fsd"}:
        raise HTTPException(status_code=400, detail="DOCX export is available only for BRD and FSD")
    snapshot = _latest(project, canonical_type)
    if not snapshot and canonical_type == "fsd":
        snapshot = _latest(project, "frd")
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Generate the {canonical_type.upper()} before exporting it")
    from services.requirements_documentation import build_requirement_docx
    try:
        artifact = _wire_requirement_identity(_artifact(snapshot), project)
        if canonical_type == "fsd" and artifact.get("document_type") == "frd":
            artifact = {**artifact, "document_type": "fsd", "title": "Functional Specification Document"}
        content = await asyncio.to_thread(build_requirement_docx, artifact, project)
    except (RuntimeError, ValueError) as exc:
        _raise_internal_error(
            exc,
            operation=f"export {canonical_type.upper()} requirements document",
            artifact_path=str(snapshot.get("path") or ""),
        )
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", project["name"]).strip("-") or project_id
    filename = f"{project_id}-{safe_name}-{canonical_type.upper()}.docx"
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _generate_requirement_worker(
    job_id: str, project: dict, document_type: str, source: dict, actor: str,
    llm_model: str,
) -> None:
    from services.requirements_documentation import generate_requirement_artifact
    with _JOBS_LOCK:
        _REQUIREMENTS_JOBS[job_id].update({
            "status": "running", "phase": "analyzing", "progress": 20,
            "message": "Ollama is analyzing the project source",
            "updated_at": datetime.now(UTC).isoformat(),
        })
    _persist_requirements_job(job_id)
    try:
        streamed_characters = 0
        last_job_update = 0.0

        def on_token(token: str) -> None:
            nonlocal streamed_characters, last_job_update
            streamed_characters += len(token)
            now = time.monotonic()
            if now - last_job_update < 1:
                return
            last_job_update = now
            with _JOBS_LOCK:
                _REQUIREMENTS_JOBS[job_id].update({
                    "message": f"Ollama is drafting the document · {streamed_characters:,} characters streamed",
                    "streamed_characters": streamed_characters,
                    "phase": "drafting", "progress": min(90, 55 + streamed_characters // 700),
                    "updated_at": datetime.now(UTC).isoformat(),
                })
            _persist_requirements_job(job_id, force=False)

        artifact = generate_requirement_artifact(
            document_type, project, source["path"], on_token, model=llm_model,
        )
        snapshot = _PROJECT_STORE.add_json_snapshot(
            project["id"], document_type, artifact, actor,
            {"model": artifact.get("model"), "parent_source": source["id"]}, source["id"],
        )
        with _JOBS_LOCK:
            _REQUIREMENTS_JOBS[job_id].update({
                "status": "completed", "phase": "completed", "progress": 100,
                "message": "Requirements artifact generated",
                "artifact": artifact, "snapshot": snapshot,
                "updated_at": datetime.now(UTC).isoformat(),
            })
        _persist_requirements_job(job_id)
    except Exception as exc:
        record = build_error_record(
            exc,
            operation=f"generate {document_type.upper()} requirements artifact",
            component="requirements-job",
            job_id=job_id,
            artifact_path=str(source.get("path") or "") or None,
        )
        log_error_record(logger, record, exc)
        with _JOBS_LOCK:
            _REQUIREMENTS_JOBS[job_id].update({
                "status": "failed", "phase": "failed", "message": "Requirements generation failed",
                "error": record["description"], "error_details": record,
                "updated_at": datetime.now(UTC).isoformat(),
            })
        _persist_requirements_job(job_id)


@app.post("/api/projects/{project_id}/requirements/{document_type}/generate")
async def generate_requirement_document(project_id: str, document_type: str, request: Request):
    project = _project_or_404(project_id)
    document_type = "fsd" if document_type == "frd" else document_type
    if document_type not in {"brd", "fsd", "knowledge_graph"}:
        raise HTTPException(status_code=404, detail="Unknown requirements document type")
    source = _latest(project, "source")
    if not source:
        raise HTTPException(status_code=409, detail="Upload project source before generating requirements")
    from services.llm import check_status
    llm_status = check_status()
    runtime = llm_status.get("runtime") or {}
    llm_model = runtime.get("selected_model") or llm_status.get("active_model")
    if not llm_status.get("available") or not llm_model:
        raise HTTPException(status_code=503, detail="No selected LLM model is available")
    optimization_profile = runtime.get("selected_profile") or "balanced"
    with _JOBS_LOCK:
        active = next((job for job in _REQUIREMENTS_JOBS.values()
                       if job["project_id"] == project_id and job["document_type"] == document_type
                       and job["status"] in {"queued", "running"}), None)
        if active:
            return active
        job_id = f"req-{uuid.uuid4().hex[:12]}"
        job = {
            "job_id": job_id, "project_id": project_id, "document_type": document_type,
            "status": "queued", "phase": "queued", "progress": 5,
            "message": "Requirements generation queued",
            "llm_model": llm_model, "optimization_profile": optimization_profile,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        _REQUIREMENTS_JOBS[job_id] = job
    _persist_requirements_job(job_id)
    try:
        _JOB_EXECUTOR.submit(
            _generate_requirement_worker, job_id, project, document_type, source,
            _actor(request), llm_model,
        )
    except RuntimeError as exc:
        with _JOBS_LOCK:
            job.update({
                "status": "failed", "phase": "failed", "error": str(exc),
                "updated_at": datetime.now(UTC).isoformat(),
            })
        _persist_requirements_job(job_id)
        raise HTTPException(status_code=503, detail="Requirements generation queue is unavailable")
    return job


@app.get("/api/requirements/jobs/{job_id}")
async def get_requirement_job(job_id: str):
    with _JOBS_LOCK:
        job = _REQUIREMENTS_JOBS.get(job_id)
        if not job:
            path = _requirements_job_file(job_id)
            try:
                restored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                restored = None
            if isinstance(restored, dict):
                _REQUIREMENTS_JOBS[job_id] = restored
                job = restored
        if not job:
            raise HTTPException(status_code=404, detail="Requirements generation job not found")
        return dict(job)


# Function: _delete_project_action
def _delete_project_action(project_id: str, request: Request):
    _require_admin(request, "delete governed projects")
    _project_or_404(project_id)
    with _JOBS_LOCK:
        active = [
            job.get("job_id") for job in _JOBS.values()
            if job.get("project_id") == project_id and _is_active_job(job)
        ]
        active_requirements = [
            job.get("job_id") for job in _REQUIREMENTS_JOBS.values()
            if job.get("project_id") == project_id and job.get("status") in {"queued", "running"}
        ]
    active.extend(active_requirements)
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Project has an active job and cannot be deleted: {active[0]}",
        )
    result = _PROJECT_STORE.delete_project(project_id, _actor(request))
    for job_id in [
        key for key, job in _JOBS.items() if job.get("project_id") == project_id
    ]:
        _JOBS.pop(job_id, None)
        _JOB_QUEUES.pop(job_id, None)
    for job_id in [key for key, job in _REQUIREMENTS_JOBS.items() if job.get("project_id") == project_id]:
        _REQUIREMENTS_JOBS.pop(job_id, None)
        try:
            _requirements_job_file(job_id).unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove persisted requirements job %s", job_id, exc_info=True)
    return result


# Function: delete_project
@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    return _delete_project_action(project_id, request)


# Function: delete_project_via_action
@app.post("/api/projects/{project_id}/delete")
async def delete_project_via_action(project_id: str, request: Request):
    """Proxy-compatible destructive action for hosts that restrict DELETE."""
    return _delete_project_action(project_id, request)


# Function: _analysis_target_description
def _analysis_target_description(configuration: dict, body: dict) -> str:
    """Collect every user-controlled stack constraint used by project analysis."""
    custom_stack_desc = str(
        body.get("custom_stack_desc") or configuration.get("custom_stack_desc") or ""
    )
    return "\n".join(
        str(value).strip() for value in (
            custom_stack_desc,
            body.get("project_prompt"),
            configuration.get("project_prompt"),
            configuration.get("custom_stack_prompt"),
            configuration.get("custom_instructions"),
            configuration.get("framework"),
            configuration.get("runtime"),
            configuration.get("frontend"),
            configuration.get("database"),
            configuration.get("architecture"),
        ) if value and str(value).strip()
    )


# Function: analyze_governed_project
@app.post("/api/projects/{project_id}/analyze")
async def analyze_governed_project(project_id: str, request: Request):
    project = _project_or_404(project_id)
    source = _latest(project, "source")
    if not source:
        raise HTTPException(status_code=409, detail="Project has no source snapshot")
    body = await request.json()
    configured_target = project["configuration"].get("engine_target") or project["configuration"].get("target_stack")
    target_stack = str(body.get("target_stack") or configured_target or "").strip()
    if not target_stack:
        raise HTTPException(
            status_code=400,
            detail="Select a target technology stack before analyzing the project",
        )
    custom_stack_desc = str(body.get("custom_stack_desc") or project["configuration"].get("custom_stack_desc") or "")
    if target_stack == "custom" and not custom_stack_desc.strip():
        raise HTTPException(
            status_code=400,
            detail="Describe the custom target technology stack before analyzing the project",
        )
    configuration = project["configuration"]
    target_description = _analysis_target_description(configuration, body)
    from services.analyzer import analyze_project
    analysis = await asyncio.to_thread(
        analyze_project, source["path"], None, target_stack, target_description,
    )
    project_context = {
        key: project["configuration"].get(key) for key in (
            "application_name", "application_key", "client_name", "application_owner",
            "business_unit", "business_criticality", "description", "source_display_name",
            "target_stack_name", "language", "framework", "runtime", "frontend",
            "database", "architecture", "deployment", "dependency_versions", "custom_instructions",
            "custom_stack_prompt",
            "guideline_documents", "guidelines_sha256",
        ) if project["configuration"].get(key) not in (None, "", [], {})
    }
    analysis["project_context"] = project_context
    index = await asyncio.to_thread(semantic_index, Path(source["path"]))
    snapshot = _PROJECT_STORE.add_json_snapshot(
        project_id, "analysis", {"analysis": analysis, "semantic_index": index}, _actor(request),
        {"target_stack": target_stack, "custom_stack_desc": custom_stack_desc,
         "target_description": target_description, "project_context": project_context,
         "parent_source": source["id"]}, source["id"],
    )
    _PROJECT_STORE.set_status(project_id, "Analyzed")
    return {"snapshot": snapshot, "analysis": analysis, "semantic_index": index}


# Function: create_modernization_plan
@app.post("/api/projects/{project_id}/plans")
async def create_modernization_plan(project_id: str, request: Request):
    # A synchronous UI double-click, retry, or second browser tab must not create
    # competing plan/contract snapshot pairs for the same governed project.
    lock = _PROJECT_PLAN_LOCKS.setdefault(project_id, asyncio.Lock())
    async with lock:
        return await _create_modernization_plan_locked(project_id, request)


async def _create_modernization_plan_locked(project_id: str, request: Request):
    project = _project_or_404(project_id); analysis_snapshot = _latest(project, "analysis")
    if not analysis_snapshot:
        raise HTTPException(status_code=409, detail="Analyze the project before generating a plan")
    body = await request.json(); artifact = _artifact(analysis_snapshot)
    target = str(body.get("target_stack") or analysis_snapshot["metadata"].get("target_stack") or "").strip()
    if not target:
        raise HTTPException(
            status_code=400,
            detail="The analysis has no target technology stack; analyze the project again with an explicit target",
        )
    custom_desc = str(body.get("custom_stack_desc") or project["configuration"].get("custom_stack_desc") or analysis_snapshot["metadata"].get("custom_stack_desc") or "")
    if target == "custom" and not custom_desc.strip():
        raise HTTPException(
            status_code=400,
            detail="Describe the custom target technology stack before generating the plan",
        )

    # POST is retry-safe for an unchanged analysis and target. Only a complete,
    # linked plan/contract pair is reusable; historical orphan plans are ignored.
    existing_plan = _latest(project, "plans")
    existing_contract = _latest(project, "contracts")
    if (
        existing_plan
        and existing_contract
        and existing_plan.get("parent_id") == analysis_snapshot["id"]
        and existing_contract.get("parent_id") == existing_plan["id"]
        and existing_contract.get("metadata", {}).get("locked_for_plan") == existing_plan["id"]
        and existing_plan.get("metadata", {}).get("target_stack") == target
        and str(existing_plan.get("metadata", {}).get("custom_stack_desc") or "") == custom_desc
    ):
        plan = _artifact(existing_plan, "plan.json")
        contracts = _artifact(existing_contract, "contracts.json")
        return {
            "plan_snapshot": existing_plan, "plan": plan,
            "contract_snapshot": existing_contract, "contracts": contracts,
            "contract_validation": validate_contracts(contracts), "reused": True,
        }

    plan_analysis = dict(artifact["analysis"])
    if project["configuration"].get("origin_mode") == "prompt":
        plan_analysis["project_prompt"] = str(
            project["configuration"].get("project_prompt") or ""
        )
    plan_analysis["requested_target"] = {
        "name": project["configuration"].get("target_stack_name"),
        "language": project["configuration"].get("language"),
        "framework": project["configuration"].get("framework"),
        "runtime": project["configuration"].get("runtime"),
        "frontend": project["configuration"].get("frontend"),
        "database": project["configuration"].get("database"),
        "architecture": project["configuration"].get("architecture"),
        "deployment": project["configuration"].get("deployment"),
        "dependency_versions": project["configuration"].get("dependency_versions"),
        "custom_instructions": project["configuration"].get("custom_instructions"),
        "custom_stack_prompt": project["configuration"].get("custom_stack_prompt"),
    }
    plan_analysis["generation_guidelines"] = project["configuration"].get("generation_guidelines") or ""
    plan_analysis["guideline_documents"] = project["configuration"].get("guideline_documents") or []
    plan_analysis["project_context"] = {
        key: project["configuration"].get(key) for key in (
            "application_name", "application_key", "client_name", "application_owner",
            "business_unit", "business_criticality", "description", "source_display_name",
        ) if project["configuration"].get(key) not in (None, "", [], {})
    }
    if project["configuration"].get("origin_mode") == "prompt":
        plan_analysis["project_type"] = "greenfield"
        plan_analysis["source"] = "prompt"
        plan_analysis["summary"] = {
            "project_origin": "Greenfield application generated from a governed prompt",
            "business_context": project["configuration"].get("description") or "Not provided",
            "current_architecture": "No existing architecture; the approved project brief is the baseline.",
            "architecture_constraint": project["configuration"].get("architecture") or "To be defined during plan review",
            "deployment_constraint": project["configuration"].get("deployment") or "Not specified",
        }
        plan_analysis["tech_stack"] = ["Greenfield – no legacy source technology stack"]
    plan = generate_plan(plan_analysis, artifact["semantic_index"], custom_desc if target == "custom" and custom_desc else target, body.get("excluded_modules"))
    configured_architecture = {
        "name": project["configuration"].get("target_stack_name"),
        "language": project["configuration"].get("language"),
        "framework": project["configuration"].get("framework"),
        "runtime": project["configuration"].get("runtime"),
        "frontend": project["configuration"].get("frontend"),
        "database": project["configuration"].get("database"),
        "style": project["configuration"].get("architecture"),
        "deployment": project["configuration"].get("deployment"),
    }
    plan["target_architecture"] = {
        **(plan.get("target_architecture") or {}),
        **{
            key: value for key, value in configured_architecture.items()
            if value not in (None, "", [], {})
        },
    }
    effective_target = custom_desc if target == "custom" and custom_desc else target
    contract_prompt = ""
    if project["configuration"].get("origin_mode") == "prompt":
        source_snapshot = _latest(project, "source")
        source_brief = _artifact(source_snapshot, "project-brief.json") if source_snapshot else {}
        contract_prompt = "\n".join(dict.fromkeys(filter(None, (
            str(source_brief.get("project_prompt") or "").strip(),
            str(project["configuration"].get("project_prompt") or "").strip(),
            str(project["configuration"].get("custom_stack_prompt") or "").strip(),
            str(project["configuration"].get("generation_guidelines") or "").strip(),
            custom_desc.strip(),
        ))))
    contracts = generate_contracts(
        artifact["semantic_index"], effective_target, contract_prompt,
    )
    contract_validation = validate_contracts(contracts)
    if not contract_validation.get("valid"):
        validation_errors = "; ".join(str(item) for item in contract_validation.get("errors") or [])
        raise HTTPException(
            status_code=422,
            detail="Generated canonical contracts did not pass governance validation"
                   + (f": {validation_errors}" if validation_errors else ""),
        )

    # Persist only after both generated artifacts have passed preparation and
    # validation. This prevents a failed contract step from leaving an orphan
    # plan snapshot that makes subsequent retries ambiguous.
    snapshot, contract_snapshot = _PROJECT_STORE.add_plan_contract_snapshots(
        project_id, plan, contracts, _actor(request),
        {"target_stack": target, "custom_stack_desc": custom_desc},
        analysis_snapshot["id"], {"target_stack": target},
    )
    _PROJECT_STORE.set_status(project_id, "Plan Generated")
    return {"plan_snapshot": snapshot, "plan": plan, "contract_snapshot": contract_snapshot,
            "contracts": contracts, "contract_validation": contract_validation, "reused": False}


# Function: revise_modernization_plan
@app.patch("/api/projects/{project_id}/plans/{snapshot_id}")
async def revise_modernization_plan(project_id: str, snapshot_id: str, request: Request):
    _project_or_404(project_id)
    try:
        revised = _PROJECT_STORE.update_plan(project_id, snapshot_id, await request.json(), _actor(request))
        _PROJECT_STORE.set_status(project_id, "Plan Generated")
        return revised
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# Function: decide_snapshot
@app.post("/api/projects/{project_id}/snapshots/{snapshot_id}/decision")
async def decide_snapshot(project_id: str, snapshot_id: str, request: Request):
    body = await request.json()
    try:
        decision = str(body.get("decision"))
        snapshot = _snapshot_or_404(project_id, snapshot_id)
        if snapshot["kind"] == "plans" and decision == "approved":
            plan = _artifact(snapshot, "plan.json")
            if not plan.get("ready_for_approval"):
                unresolved = plan.get("unresolved_requirements") or ["Plan completeness has not been established"]
                raise HTTPException(
                    status_code=409,
                    detail="Plan has unresolved requirements: " + "; ".join(map(str, unresolved[:8])),
                )
        return _PROJECT_STORE.decide(project_id, snapshot_id, decision, _actor(request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# Function: restore_snapshot
@app.post("/api/projects/{project_id}/snapshots/{snapshot_id}/restore")
async def restore_snapshot(project_id: str, snapshot_id: str, request: Request):
    _snapshot_or_404(project_id, snapshot_id)
    return _PROJECT_STORE.restore(project_id, snapshot_id, _actor(request))


# Function: get_snapshot_artifact
@app.get("/api/projects/{project_id}/snapshots/{snapshot_id}/artifact")
async def get_snapshot_artifact(project_id: str, snapshot_id: str):
    """Return structured content used by the governed review workspace."""
    snapshot = _snapshot_or_404(project_id, snapshot_id)
    filenames = {
        "analysis": "artifact.json", "plans": "plan.json", "contracts": "contracts.json",
        "validation": "artifact.json", "exports": "manifest.json",
    }
    filename = filenames.get(snapshot["kind"])
    if not filename:
        raise HTTPException(status_code=400, detail="This snapshot contains files, not a structured artifact")
    return {"snapshot": snapshot, "artifact": _artifact(snapshot, filename)}


# Function: review_snapshot_file
@app.post("/api/projects/{project_id}/snapshots/{snapshot_id}/files/review")
async def review_snapshot_file(project_id: str, snapshot_id: str, request: Request):
    _snapshot_or_404(project_id, snapshot_id); body = await request.json()
    try:
        return _PROJECT_STORE.review_file(snapshot_id, str(body.get("file_path") or ""),
                                          str(body.get("decision") or "pending"), str(body.get("comment") or ""), _actor(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# Function: compare_project_snapshots
@app.get("/api/projects/{project_id}/compare")
async def compare_project_snapshots(project_id: str, left_snapshot_id: str, right_snapshot_id: str,
                                    search: str = "", change_type: str = ""):
    left = _snapshot_or_404(project_id, left_snapshot_id); right = _snapshot_or_404(project_id, right_snapshot_id)
    return await asyncio.to_thread(compare_directories, Path(left["path"]), Path(right["path"]), search, change_type)


# Function: export_project_comparison
@app.get("/api/projects/{project_id}/compare/export")
async def export_project_comparison(project_id: str, left_snapshot_id: str, right_snapshot_id: str,
                                    format: str = "html"):
    left = _snapshot_or_404(project_id, left_snapshot_id); right = _snapshot_or_404(project_id, right_snapshot_id)
    comparison = await asyncio.to_thread(compare_directories, Path(left["path"]), Path(right["path"]))
    if format.lower() == "pdf":
        data, media, suffix = comparison_pdf(comparison), "application/pdf", "pdf"
    elif format.lower() == "html":
        data, media, suffix = comparison_html(comparison).encode(), "text/html", "html"
    else:
        raise HTTPException(status_code=400, detail="format must be html or pdf")
    return StreamingResponse(io.BytesIO(data), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="comparison.{suffix}"'})


# Function: transform_governed_project
def _transformation_target_description(custom_stack_desc: str, configuration: dict, plan: dict) -> str:
    """Carry authoritative project and reviewed-plan database choices into generation."""
    target_architecture = plan.get("target_architecture") or {}
    database_conversion = plan.get("database_conversion_approach") or {}
    values = (
        custom_stack_desc,
        configuration.get("custom_stack_prompt"),
        configuration.get("database"),
        target_architecture.get("stack"),
        target_architecture.get("backend"),
        target_architecture.get("frontend"),
        target_architecture.get("runtime"),
        target_architecture.get("database"),
        target_architecture.get("deployment"),
        ", ".join(str(value) for value in (plan.get("target_technologies") or []) if value),
        database_conversion.get("target"),
    )
    return "\n".join(dict.fromkeys(
        str(value).strip() for value in values if value and str(value).strip()
    ))


def _normalize_governed_target_stack(target_stack: str, target_description: str) -> str:
    """Resolve engine IDs while treating reviewed human-readable stacks as custom specifications."""
    from services.modernizer.target_config import TARGET_STACKS

    requested = str(target_stack or "").strip()
    if requested in TARGET_STACKS or requested == "custom":
        return requested

    def comparable(value: object) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

    requested_key = comparable(requested)
    for stack_id, definition in TARGET_STACKS.items():
        if requested_key and requested_key in {
            comparable(stack_id),
            comparable(definition.get("name")),
            comparable(definition.get("backend_tech")),
        }:
            return stack_id
    if requested or str(target_description or "").strip():
        return "custom"
    raise HTTPException(
        status_code=409,
        detail="The approved plan has no executable target stack. Regenerate the architecture plan with an explicit target.",
    )


def _plan_target_metadata(project_id: str, plan_snapshot: dict) -> tuple[str, str]:
    """Recover target metadata across revised plan snapshots created before lineage preservation."""
    current = plan_snapshot
    visited: set[str] = set()
    target_stack = ""
    custom_stack_desc = ""
    while current and current.get("id") not in visited:
        visited.add(str(current.get("id")))
        metadata = current.get("metadata") or {}
        target_stack = target_stack or str(metadata.get("target_stack") or "").strip()
        custom_stack_desc = custom_stack_desc or str(metadata.get("custom_stack_desc") or "").strip()
        if target_stack and (target_stack != "custom" or custom_stack_desc):
            break
        parent_id = current.get("parent_id")
        if not parent_id:
            break
        try:
            current = _PROJECT_STORE.get_snapshot(project_id, parent_id)
        except KeyError:
            break
    return target_stack, custom_stack_desc


@app.post("/api/projects/{project_id}/transform")
async def transform_governed_project(project_id: str, request: Request):
    project = _project_or_404(project_id); plan_snapshot = _latest(project, "plans"); source = _latest(project, "source")
    if not plan_snapshot or plan_snapshot.get("approval_decision") != "approved":
        raise HTTPException(status_code=409, detail="An approved modernization plan is required")
    body = await request.json(); plan = _artifact(plan_snapshot, "plan.json")
    lineage_target, lineage_custom_desc = _plan_target_metadata(project_id, plan_snapshot)
    configured_target = project["configuration"].get("engine_target") or project["configuration"].get("target_stack")
    requested_target = str(body.get("target_stack") or lineage_target or configured_target or "")
    custom_stack_desc = str(body.get("custom_stack_desc") or lineage_custom_desc or project["configuration"].get("custom_stack_desc") or "")
    custom_stack_desc = _transformation_target_description(
        custom_stack_desc, project.get("configuration") or {}, plan,
    )
    if requested_target and requested_target not in custom_stack_desc:
        custom_stack_desc = f"{requested_target}\n{custom_stack_desc}".strip()
    target_stack = _normalize_governed_target_stack(requested_target, custom_stack_desc)
    from services.build_runner import toolchain_compatibility_error
    config = project.get("configuration") or {}
    compatibility_error = toolchain_compatibility_error(" ".join(str(value or "") for value in (
        custom_stack_desc, config.get("target_stack_name"), config.get("language"),
        config.get("framework"), config.get("runtime"), config.get("dependency_versions"),
    )))
    if compatibility_error:
        raise HTTPException(status_code=409, detail=compatibility_error)
    job_id = str(uuid.uuid4()); now = datetime.now(UTC).isoformat()
    _JOBS[job_id] = {"job_id": job_id, "project_id": project_id, "actor": _actor(request), "folder_path": source["path"],
        "target_stack": target_stack, "custom_stack_desc": custom_stack_desc, "output_mode": "project", "status": "queued", "progress": 0,
        "phase": "queued", "created_at": now, "updated_at": now, "analysis": None, "output": None,
        "validation": None, "error": None, "events": [], "plan_snapshot_id": plan_snapshot["id"],
        **_generation_runtime_snapshot()}
    _persist_job(job_id); _JOB_QUEUES[job_id] = queue.Queue(maxsize=512); _PROJECT_STORE.set_status(project_id, "Transformation Running")
    contract_snapshot = _latest(project, "contracts")
    contract_text = json.dumps(
        effective_contracts_for_validation(_artifact(contract_snapshot, "contracts.json")),
        indent=2,
    ) if contract_snapshot else "{}"
    guide = "LOCKED CANONICAL CONTRACTS (all generated files must conform):\n" + contract_text
    configured_details = {
        label: config.get(key) for key, label in (
            ("application_name", "Application"), ("application_key", "Application key"),
            ("client_name", "Client"), ("application_owner", "Owner"),
            ("business_unit", "Business unit"), ("business_criticality", "Business criticality"),
            ("description", "Business purpose and scope"), ("target_stack_name", "Approved target stack"),
            ("language", "Language"), ("framework", "Framework/backend"), ("runtime", "Runtime/version"),
            ("frontend", "Frontend technology"), ("database", "Database/data access"),
            ("architecture", "Architecture style"), ("deployment", "Deployment target"),
            ("dependency_versions", "Required dependency versions"), ("custom_instructions", "Additional requirements"),
            ("custom_stack_prompt", "Custom stack prompt"),
        ) if config.get(key) not in (None, "", [], {})
    }
    if configured_details:
        guide += "\n\nAUTHORITATIVE PROJECT DETAILS captured at project creation (preserve these in generated design, configuration, documentation, and code where applicable):\n"
        guide += "\n".join(f"- {label}: {value}" for label, value in configured_details.items())
    user_guidelines = str(config.get("generation_guidelines") or "").strip()
    if user_guidelines:
        guide += (
            "\n\nUSER-PROVIDED GENERATION GUIDELINES (mandatory and traceable):\n"
            + user_guidelines
        )
    correction_snapshot = _latest(project, "overrides")
    if correction_snapshot:
        correction = _artifact(correction_snapshot, "review.json")
        guide += "\n\nMANDATORY REVIEW CORRECTIONS FOR THIS RUN:\n" + str(correction.get("feedback") or "")
    if project["configuration"].get("origin_mode") == "prompt":
        user_prompt = str(project["configuration"].get("project_prompt") or "")
        _submit_generation_job(job_id, _prompt_worker, user_prompt, target_stack, [], custom_stack_desc, guide, "project")
    else:
        _submit_generation_job(job_id, _analysis_worker, source["path"], target_stack, custom_stack_desc, guide, "project")
    return {"job_id": job_id, "project_id": project_id, "status": "queued"}


# Function: get_transformation_context
@app.post("/api/projects/{project_id}/context")
async def get_transformation_context(project_id: str, request: Request):
    project = _project_or_404(project_id); analysis = _latest(project, "analysis"); contracts = _latest(project, "contracts")
    if not analysis or not contracts:
        raise HTTPException(status_code=409, detail="Analysis and canonical contracts are required")
    body = await request.json()
    return transformation_context(_artifact(analysis)["semantic_index"], str(body.get("current_file") or ""),
                                  effective_contracts_for_validation(
                                      _artifact(contracts, "contracts.json"),
                                  ), body.get("architecture_decisions"))


# Function: validate_project_contracts
@app.get("/api/projects/{project_id}/contracts/validate")
async def validate_project_contracts(project_id: str):
    contracts = _latest(_project_or_404(project_id), "contracts")
    if not contracts: raise HTTPException(status_code=404, detail="Canonical contracts not generated")
    effective = effective_contracts_for_validation(_artifact(contracts, "contracts.json"))
    return validate_contracts(effective)


# Function: purge_project_snapshots
@app.post("/api/projects/{project_id}/retention/purge")
async def purge_project_snapshots(project_id: str):
    _project_or_404(project_id)
    return _PROJECT_STORE.purge(project_id)


# Function: review_generated_output
@app.post("/api/projects/{project_id}/reviews")
async def review_generated_output(project_id: str, request: Request):
    project = _project_or_404(project_id); body = await request.json()
    decision = str(body.get("decision") or "").strip()
    if decision not in {"corrections_requested", "rejected"}:
        raise HTTPException(status_code=400, detail="decision must be corrections_requested or rejected")
    feedback = str(body.get("feedback") or "").strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="Review feedback is required")
    output_id = str(body.get("output_snapshot_id") or "")
    output = _snapshot_or_404(project_id, output_id) if output_id else _latest(project, "outputs")
    if not output or output["kind"] != "outputs":
        raise HTTPException(status_code=409, detail="A generated output snapshot is required")
    review = {"decision": decision, "feedback": feedback, "output_snapshot_id": output["id"],
              "file_feedback": body.get("file_feedback") or [], "reviewed_at": datetime.now(UTC).isoformat(),
              "reviewed_by": _actor(request)}
    snapshot = _PROJECT_STORE.add_json_snapshot(project_id, "overrides", review, _actor(request),
        {"output_snapshot_id": output["id"], "decision": decision}, output["id"], "review.json")
    _PROJECT_STORE.decide(project_id, output["id"], "rejected", _actor(request))
    if decision == "corrections_requested":
        _PROJECT_STORE.set_status(project_id, "Plan Approved")
    return {"review_snapshot": snapshot, "project": _PROJECT_STORE.get_project(project_id)}


# Function: approve_release
def _security_findings(root: Path) -> list[str]:
    findings = []
    patterns = [
        (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "Embedded private key"),
        (re.compile(r"(?i)(?:client_secret|api_key|access_key)\s*[:=]\s*[\"'][A-Za-z0-9_+/=-]{12,}[\"']"), "Embedded credential"),
        (re.compile(
            r"(?i)(?<![\w])[\"']?password[\"']?\s*[:=]\s*[\"']"
            r"(?!change|replace|example|\$\{|<)(?P<password_value>[^\"']{8,})[\"']"
        ), "Hard-coded password"),
    ]
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024: continue
        try: content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError: continue
        for pattern, label in patterns:
            match = pattern.search(content)
            if not match:
                continue
            if label == "Hard-coded password":
                line_start = content.rfind("\n", 0, match.start()) + 1
                prefix = content[line_start:match.start()]
                value = str(match.groupdict().get("password_value") or "").casefold()
                # UI form validation commonly assigns human-readable messages
                # to errors.password. That is neither a credential nor runtime
                # configuration and must not block an otherwise secure build.
                if re.search(r"(?:validationErrors|errors)\s*\.\s*$", prefix, re.IGNORECASE):
                    continue
                if any(phrase in value for phrase in (
                    "password is required", "password must", "invalid password",
                    "enter your password", "passwords do not match",
                )):
                    continue
            findings.append(f"{label}: {path.relative_to(root).as_posix()}")
    return findings


# Function: _evaluate_release_gate
def _generation_audit_has_unresolved_issues(path: Path) -> bool:
    """Distinguish retained retry history from current blocking failures."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return True
    return any(marker in text for marker in (
        "## Structural audit",
        "still FAILING",
        "## Real build",
    ))


def _evaluate_release_gate(project: dict, output: dict, validation_snapshot: dict) -> dict:
    validation = _artifact(validation_snapshot)
    blockers, warnings = [], []
    if validation.get("production_ready") is not True:
        warnings.append(
            "Code generation does not assert production readiness; final production acceptance "
            "remains human-owned and is recorded by release approval"
        )
    if validation.get("generation_validated") is False:
        blockers.append("Code generation validation did not pass")
    if int(validation.get("checked", 0)) <= 0: blockers.append("No generated files were validated")
    if int(validation.get("strict_checked", 0)) <= 0: blockers.append("No compiler or parser validation was completed")
    if int(validation.get("failed", 0)) > 0: blockers.append(f"{validation['failed']} file validation check(s) failed")
    build = validation.get("build")
    configuration = project.get("configuration", {})
    build_required = configuration.get("origin_mode") != "single_file"
    if build_required and not build: blockers.append("Required whole-project strict validation did not run")
    elif build and build.get("checker") in {"skipped", "unsupported-build-route"}:
        blockers.append("Required whole-project strict validation was not available")
    elif build and not build.get("passed"): blockers.append(f"Whole-project build failed ({build.get('checker')})")
    contracts = _latest(project, "contracts")
    contract_valid = False
    if not contracts: blockers.append("Canonical contracts are missing")
    else:
        effective_contracts = effective_contracts_for_validation(
            _artifact(contracts, "contracts.json"),
        )
        contract_result = validate_contracts(effective_contracts)
        contract_valid = bool(contract_result.get("valid"))
        if not contract_result.get("valid"): blockers.extend(contract_result.get("errors") or ["Canonical contract validation failed"])
        warnings.extend(contract_result.get("warnings") or [])
    security = _security_findings(Path(output["path"]))
    blockers.extend(security)
    if any(
        _generation_audit_has_unresolved_issues(path)
        for path in Path(output["path"]).rglob("_GENERATION_AUDIT.md")
    ):
        blockers.append("Generation audit reports unresolved issues")
    if not any("test" in path.name.lower() or "spec" in path.name.lower() for path in Path(output["path"]).rglob("*")):
        warnings.append("No generated automated tests were detected")
    return {"passed": not blockers, "blockers": blockers, "warnings": warnings,
            "checks": {"generation_validation": validation.get("generation_validated") is not False,
                       "file_validation": int(validation.get("failed", 0)) == 0,
                       "whole_project_build": bool(build and build.get("passed")),
                       "canonical_contracts": contract_valid, "security_scan": not security,
                       "production_readiness_asserted_by_generation":
                           validation.get("production_ready") is True}}


# Function: get_release_quality_gate
@app.get("/api/projects/{project_id}/quality-gate")
async def get_release_quality_gate(project_id: str, output_snapshot_id: str = ""):
    project = _project_or_404(project_id)
    output = _snapshot_or_404(project_id, output_snapshot_id) if output_snapshot_id else _latest(project, "outputs")
    if not output: raise HTTPException(status_code=404, detail="Generated output not found")
    validation = next((s for s in project["snapshots"] if s["kind"] == "validation" and s["metadata"].get("output_snapshot_id") == output["id"]), None)
    if not validation: raise HTTPException(status_code=409, detail="Output has no validation results")
    return _evaluate_release_gate(project, output, validation)


def _generated_asset_type(path: Path) -> str:
    """Classify a generated file for the post-generation asset catalog."""
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if name in {"dockerfile", "jenkinsfile"} or (suffix in {".yml", ".yaml"} and any(
        token in name for token in ("pipeline", "deploy", "docker", "k8s", "github")
    )):
        return "Pipeline / deployment"
    if suffix in {".sql", ".ddl", ".prisma"}:
        return "Database"
    if "openapi" in name or "swagger" in name or suffix in {".graphql", ".proto"}:
        return "API contract"
    if "test" in name or "spec" in name:
        return "Test"
    if suffix in {".md", ".rst", ".txt"}:
        return "Documentation"
    if suffix in {".json", ".xml", ".toml", ".properties", ".config", ".env"}:
        return "Configuration"
    return "Source code"


def _generated_asset_catalog(output: dict, project: dict | None = None) -> dict:
    root = Path(output["path"])
    files = []
    type_counts: dict[str, int] = {}
    modules: set[str] = set()
    total_lines = 0
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        kind = _generated_asset_type(path)
        lines = 0
        if size <= 2_000_000:
            try:
                lines = len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
            except OSError:
                pass
        top = relative.split("/", 1)[0]
        if "/" in relative:
            modules.add(top)
        total_bytes += size
        total_lines += lines
        type_counts[kind] = type_counts.get(kind, 0) + 1
        files.append({
            "path": relative, "name": path.name, "type": kind, "size": size, "lines": lines,
            "generated_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
        })
    return {
        "project_identity": _project_identity_payload(project or {}),
        "snapshot": {key: output.get(key) for key in ("id", "version", "checksum", "created_at", "status")},
        "summary": {"files": len(files), "lines": total_lines, "bytes": total_bytes,
                    "modules": len(modules), "type_counts": type_counts},
        "modules": sorted(modules), "files": files,
    }


@app.get("/api/projects/{project_id}/generated-assets")
async def get_generated_assets(project_id: str, output_snapshot_id: str = ""):
    project = _project_or_404(project_id)
    output = _snapshot_or_404(project_id, output_snapshot_id) if output_snapshot_id else _latest(project, "outputs")
    if not output or output.get("kind") != "outputs":
        raise HTTPException(status_code=404, detail="Generated output not found")
    return _generated_asset_catalog(output, project)


@app.get("/api/projects/{project_id}/generated-assets/export")
async def export_generated_assets(project_id: str, request: Request, output_snapshot_id: str = ""):
    project = _project_or_404(project_id)
    output = _snapshot_or_404(project_id, output_snapshot_id) if output_snapshot_id else _latest(project, "outputs")
    if not output or output.get("kind") != "outputs":
        raise HTTPException(status_code=404, detail="Generated output not found")
    catalog = _generated_asset_catalog(output, project)
    buf = io.BytesIO()
    root = Path(output["path"])
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in root.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(root).as_posix())
        archive.writestr("generated-assets-manifest.json", json.dumps({
            "project_id": project_id, "exported_at": datetime.now(UTC).isoformat(),
            "exported_by": _actor(request), **catalog,
        }, indent=2))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{project_id}-generated-assets.zip"',
    })


# Function: approve_release
@app.post("/api/projects/{project_id}/releases")
async def approve_release(project_id: str, request: Request):
    project = _project_or_404(project_id); body = await request.json()
    output_id = str(body.get("output_snapshot_id") or "")
    output = _snapshot_or_404(project_id, output_id) if output_id else _latest(project, "outputs")
    if not output or output["kind"] != "outputs":
        raise HTTPException(status_code=409, detail="A generated output snapshot is required")
    validation = next((s for s in project["snapshots"] if s["kind"] == "validation" and
                       s["metadata"].get("output_snapshot_id") == output["id"]), None)
    if not validation:
        raise HTTPException(status_code=409, detail="Output has no validation results")
    quality_gate = _evaluate_release_gate(project, output, validation)
    if not quality_gate["passed"]:
        raise HTTPException(status_code=409, detail={"message": "Release quality gate failed", **quality_gate})
    release = _PROJECT_STORE.add_directory_snapshot(project_id, "approved", Path(output["path"]), _actor(request),
        {"output_snapshot_id": output["id"], "validation_snapshot_id": validation["id"],
         "approval_comment": str(body.get("comment") or "")}, output["id"])
    release = _PROJECT_STORE.decide(project_id, release["id"], "approved", _actor(request))
    _PROJECT_STORE.set_status(project_id, "Approved")
    return release


# Function: export_release
@app.get("/api/projects/{project_id}/releases/{snapshot_id}/export")
async def export_release(project_id: str, snapshot_id: str, request: Request):
    release = _snapshot_or_404(project_id, snapshot_id)
    if release["kind"] != "approved" or not release["locked"]:
        raise HTTPException(status_code=409, detail="Only locked approved releases can be exported")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        root = Path(release["path"])
        for path in root.rglob("*"):
            if path.is_file(): archive.write(path, path.relative_to(root).as_posix())
        manifest = {"project_id": project_id, "release_snapshot_id": snapshot_id, "checksum": release["checksum"],
                    "exported_at": datetime.now(UTC).isoformat(), "exported_by": _actor(request),
                    "metadata": release["metadata"]}
        archive.writestr("modernization-release-manifest.json", json.dumps(manifest, indent=2))
    buf.seek(0)
    _PROJECT_STORE.add_json_snapshot(project_id, "exports", manifest, _actor(request),
                                     {"release_snapshot_id": snapshot_id}, snapshot_id, "manifest.json")
    _PROJECT_STORE.set_status(project_id, "Exported")
    return StreamingResponse(buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project_id}-{snapshot_id}.zip"'})


# Function: llm_status
@app.get("/api/llm/status")
async def llm_status():
    """Check Ollama availability and return the best available code model."""
    recommended = None
    try:
        from services.llm import DEVSTRAL_MODEL, check_status
        recommended = DEVSTRAL_MODEL
        return check_status()
    except Exception as exc:
        return {"available": False, "error": str(exc), "recommended": recommended}


@app.put("/api/llm/runtime")
async def update_llm_runtime(request: Request):
    """Select the deployment model/profile for subsequent generation jobs."""
    body = await request.json()
    with _JOBS_LOCK:
        generation_active = any(_is_active_job(job) for job in _JOBS.values())
        requirements_active = any(
            job.get("status") in {"queued", "running"}
            for job in _REQUIREMENTS_JOBS.values()
        )
        if generation_active or requirements_active:
            raise HTTPException(
                status_code=409,
                detail="Model and optimization settings can only change while generation is idle.",
            )
    try:
        from services.llm import check_status, configure_inference_slots
        from services.llm_runtime import update_runtime
        status = check_status()
        if not status.get("available"):
            raise HTTPException(status_code=503, detail=status.get("error") or "LLM service unavailable")
        runtime = update_runtime(body.get("model"), body.get("profile"), status.get("models") or [])
        configure_inference_slots(runtime["effective"]["inference_lanes"])
        return check_status()
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


_ADDITIONAL_STACK_PRESETS = [
    ("c_native", "C17 native application", "Native and systems", "C", "C17", "CLI / service", "Files / database"),
    ("cpp_native", "C++20 native application", "Native and systems", "C++", "C++20", "CLI / service", "Files / database"),
    ("dotnet_react", ".NET 8 Web API + React + PostgreSQL", "Microsoft", "C#", ".NET 8 Web API", "React + TypeScript", "PostgreSQL 16"),
    ("dotnet_angular", ".NET 8 Web API + Angular + SQL Server", "Microsoft", "C#", ".NET 8 Web API", "Angular 18", "SQL Server 2022"),
    ("dotnet_microservices", ".NET 8 Microservices + Kubernetes", "Microsoft", "C#", ".NET 8 + Aspire", "React", "PostgreSQL"),
    ("node_nest_react", "NestJS + React + PostgreSQL", "JavaScript", "TypeScript", "NestJS", "React + TypeScript", "PostgreSQL"),
    ("node_express_react", "Node.js Express + React + MongoDB", "JavaScript", "TypeScript", "Express.js", "React + TypeScript", "MongoDB"),
    ("nextjs_fullstack", "Next.js Full Stack + PostgreSQL", "JavaScript", "TypeScript", "Next.js API routes", "Next.js App Router", "PostgreSQL + Prisma"),
    ("java_quarkus", "Java 21 Quarkus + PostgreSQL", "Java", "Java", "Quarkus", "REST API", "PostgreSQL"),
    ("java_micronaut", "Java 21 Micronaut + PostgreSQL", "Java", "Java", "Micronaut", "REST API", "PostgreSQL"),
    ("kotlin_spring", "Kotlin + Spring Boot + PostgreSQL", "JVM", "Kotlin", "Spring Boot", "REST API", "PostgreSQL"),
    ("python_fastapi_react", "FastAPI + React + PostgreSQL", "Python", "Python", "FastAPI", "React + TypeScript", "PostgreSQL"),
    ("python_django_react", "Django DRF + React + PostgreSQL", "Python", "Python", "Django REST Framework", "React + TypeScript", "PostgreSQL"),
    ("go_gin_react", "Go Gin + React + PostgreSQL", "Cloud native", "Go", "Gin", "React + TypeScript", "PostgreSQL"),
    ("go_fiber_vue", "Go Fiber + Vue + PostgreSQL", "Cloud native", "Go", "Fiber", "Vue 3 + TypeScript", "PostgreSQL"),
    ("rust_axum_react", "Rust Axum + React + PostgreSQL", "Cloud native", "Rust", "Axum", "React + TypeScript", "PostgreSQL"),
    ("php_laravel_vue", "Laravel + Vue + MySQL", "PHP", "PHP", "Laravel", "Vue 3", "MySQL 8"),
    ("ruby_rails_react", "Ruby on Rails + React + PostgreSQL", "Ruby", "Ruby", "Rails", "React", "PostgreSQL"),
    ("flutter_dotnet", "Flutter + .NET 8 API", "Mobile", "Dart / C#", ".NET 8 Web API", "Flutter", "PostgreSQL"),
    ("react_native_node", "React Native + NestJS", "Mobile", "TypeScript", "NestJS", "React Native", "PostgreSQL"),
    ("cobol_java", "COBOL to Java Spring Boot", "Mainframe", "Java", "Spring Boot", "REST API", "PostgreSQL"),
    ("cobol_dotnet", "COBOL to .NET 8", "Mainframe", "C#", ".NET 8", "Blazor", "SQL Server"),
    ("javascript_node", "JavaScript + Node.js", "JavaScript / TypeScript", "JavaScript", "Node.js", "CLI / REST API", "Optional"),
    ("swift_vapor", "Swift + Vapor", "Native and systems", "Swift", "Vapor", "REST API", "PostgreSQL"),
    ("kotlin_ktor", "Kotlin + Ktor", "JVM languages", "Kotlin", "Ktor", "REST API", "PostgreSQL"),
    ("shell_automation", "Shell / Bash automation", "Scripting and analytics", "Shell", "Bash", "CLI", "Files / external services"),
    ("r_analytics", "R analytics application", "Scripting and analytics", "R", "R 4.x", "CLI / Shiny", "Files / database"),
    ("scala_play", "Scala + Play Framework", "JVM languages", "Scala", "Play Framework", "REST API", "PostgreSQL"),
    ("clojure_ring", "Clojure + Ring", "JVM languages", "Clojure", "Ring / Reitit", "REST API", "PostgreSQL"),
    ("haskell_servant", "Haskell + Servant", "Functional languages", "Haskell", "Servant", "REST API", "PostgreSQL"),
    ("common_lisp", "Common Lisp application", "Functional languages", "Common Lisp", "ANSI Common Lisp", "CLI / service", "Files / database"),
    ("elixir_phoenix", "Elixir + Phoenix", "BEAM languages", "Elixir", "Phoenix", "LiveView / REST API", "PostgreSQL"),
    ("erlang_otp", "Erlang/OTP application", "BEAM languages", "Erlang", "OTP", "Service / CLI", "Mnesia / external database"),
    ("dart_server", "Dart server application", "Mobile and Dart", "Dart", "Dart / Shelf", "REST API", "PostgreSQL"),
    ("julia_application", "Julia application", "Scripting and analytics", "Julia", "Julia 1.x", "CLI / HTTP service", "Files / database"),
    ("fortran_native", "Modernize Fortran → Java 21", "Legacy modernization", "Java", "Spring Boot 3", "REST / batch", "PostgreSQL"),
    ("ada_native", "Modernize Ada → Java 21", "Legacy modernization", "Java", "Spring Boot 3", "REST / service", "PostgreSQL"),
    ("pascal_delphi", "Modernize Object Pascal / Delphi → .NET 8", "Legacy modernization", "C#", ".NET 8 Web API", "React / desktop replacement", "SQL Server"),
    ("ocaml_application", "Modernize OCaml → Java 21", "Legacy modernization", "Java", "Spring Boot 3", "REST / service", "PostgreSQL"),
    ("prolog_application", "Modernize Prolog rules → Java 21", "Legacy modernization", "Java", "Spring Boot 3 / rules module", "REST / service", "PostgreSQL"),
    ("abap_application", "SAP ABAP application", "Vendor enterprise", "ABAP", "SAP ABAP", "SAP GUI / service", "SAP"),
    ("pli_batch", "Modernize Enterprise PL/I → Java 21", "Legacy modernization", "Java", "Spring Boot 3 / Spring Batch", "REST / batch", "PostgreSQL / DB2"),
    ("rpg_application", "Modernize IBM i RPG → Java 21", "Legacy modernization", "Java", "Spring Boot 3 / Spring Batch", "REST / batch", "PostgreSQL / Db2"),
    ("jcl_batch", "Modernize z/OS JCL → Java Spring Batch", "Legacy modernization", "Java", "Spring Boot 3 / Spring Batch", "Batch orchestration", "PostgreSQL / object storage"),
    ("mumps_application", "Modernize M/MUMPS → .NET 8", "Legacy modernization", "C#", ".NET 8 Web API", "REST / service", "PostgreSQL"),
    ("natural_application", "Modernize Software AG Natural → Java 21", "Legacy modernization", "Java", "Spring Boot 3", "REST / batch", "PostgreSQL"),
    ("progress_openedge", "Modernize OpenEdge ABL → .NET 8", "Legacy modernization", "C#", ".NET 8 Web API", "React / service", "PostgreSQL / SQL Server"),
    ("salesforce_apex", "Salesforce Apex application", "Vendor enterprise", "Apex", "Salesforce Apex", "Lightning / API", "Salesforce"),
    ("sql_generic", "ANSI SQL DDL/DML", "Data and schemas", "SQL", "ANSI SQL", "Database", "ANSI SQL"),
    ("postgresql_sql", "PostgreSQL and PL/pgSQL", "Data and schemas", "SQL", "PostgreSQL PL/pgSQL", "Database API", "PostgreSQL"),
    ("plsql_oracle", "Oracle PL/SQL", "Data and schemas", "PL/SQL", "Oracle PL/SQL", "Database API", "Oracle"),
    ("tsql_sqlserver", "Microsoft T-SQL", "Data and schemas", "T-SQL", "SQL Server T-SQL", "Database API", "SQL Server"),
    ("yaml_artifact", "YAML configuration", "Configuration formats", "YAML", "YAML 1.2", "Configuration", "N/A"),
    ("json_artifact", "JSON document", "Configuration formats", "JSON", "RFC 8259 JSON", "Configuration", "N/A"),
    ("toml_artifact", "TOML configuration", "Configuration formats", "TOML", "TOML 1.0", "Configuration", "N/A"),
    ("xml_artifact", "XML document", "Configuration formats", "XML", "XML 1.0", "Configuration", "N/A"),
    ("markdown_artifact", "Markdown document", "Documentation", "Markdown", "CommonMark", "Documentation", "N/A"),
    ("graphql_schema", "GraphQL schema or operations", "API contracts", "GraphQL", "GraphQL", "API contract", "N/A"),
    ("protobuf_schema", "Protocol Buffers schema", "API contracts", "Protobuf", "Protocol Buffers 3", "API contract", "N/A"),
    ("dockerfile_artifact", "Dockerfile", "DevOps and cloud", "Dockerfile", "Docker", "Container image", "N/A"),
    ("cloudformation_template", "AWS CloudFormation template", "DevOps and cloud", "CloudFormation", "AWS CloudFormation", "Infrastructure as code", "AWS"),
    ("kubernetes_manifest", "Kubernetes manifests", "DevOps and cloud", "Kubernetes manifests", "Kubernetes", "Deployment manifests", "Kubernetes"),
    ("helm_chart", "Helm chart", "DevOps and cloud", "Helm", "Helm 3", "Kubernetes package", "Kubernetes"),
    ("ansible_playbook", "Ansible playbook", "DevOps and cloud", "Ansible", "Ansible", "Automation", "Managed hosts"),
    ("jenkins_pipeline", "Jenkins pipeline", "CI/CD", "Jenkinsfile", "Jenkins Pipeline", "CI/CD", "Jenkins"),
    ("github_actions_workflow", "GitHub Actions workflow", "CI/CD", "GitHub Actions", "GitHub Actions", "CI/CD", "GitHub"),
]


_STACK_LANGUAGE_TOOL = {
    "c#": "dotnet", "csharp": "dotnet", "c": "c", "c++": "cpp", "cpp": "cpp",
    "java": "java+jvm_build", "typescript": "typescript",
    "javascript": "typescript", "python": "python", "go": "go", "php": "php+composer",
    "ruby": "ruby+bundler", "rust": "rust+rust_package_manager", "swift": "swift+swift_package_manager", "kotlin": "kotlin+gradle",
    "shell": "shell", "r": "r", "scala": "scala+sbt", "clojure": "java+maven",
    "haskell": "haskell+haskell_build", "common lisp": "lisp", "elixir": "elixir+mix",
    "erlang": "erlang", "dart": "dart", "julia": "julia", "cobol": "cobol",
    "fortran": "fortran", "ada": "ada", "pascal/delphi": "pascal",
    "ocaml": "ocaml", "prolog": "prolog", "abap": "abap", "pl/i": "pli",
    "rpg": "rpg", "jcl": "jcl", "mumps": "mumps", "natural": "natural",
    "progress 4gl": "progress4gl", "apex": "apex", "jenkinsfile": "jenkinsfile",
    "sql": "sql_parser", "pl/sql": "sql_parser", "t-sql": "sql_parser",
    "yaml": "yaml_parser", "json": "json_parser", "toml": "toml_parser",
    "xml": "xml_parser", "graphql": "graphql_parser", "protobuf": "protobuf",
    "terraform/hcl": "terraform", "cloudformation": "yaml_parser",
    "kubernetes manifests": "yaml_parser", "helm": "yaml_parser",
    "ansible": "yaml_parser", "github actions": "yaml_parser",
    "dart / c#": "flutter+dotnet", "dart": "dart",
}
_INTERNAL_PARSER_LANGUAGES = {"markdown", "dockerfile"}
_PROJECT_READY_LANGUAGES = {
    "c#", "csharp", "java", "typescript", "javascript", "python", "go",
    "sql", "pl/sql", "t-sql",
    # php/-l, ruby/-c, cobol/cobc, shell/bash -n already run across every file in
    # the generated project (see build_runner.py's _run_source_checks / dispatch),
    # the same whole-project rigor python's compileall/go's `go test ./...` get -
    # they were excluded here only by omission, not because the check is weaker.
    "php", "ruby", "cobol", "shell",
    # c/cpp now get a real compile-every-file + link-the-project build
    # (build_runner.py._run_c_family_build), not just a per-file syntax pass -
    # this is what makes them genuinely project-ready rather than single-file-only.
    "c", "c++", "cpp",
    # validators.py._EXTERNAL_VALIDATORS already runs a real compiler per file
    # across the whole project for all of these, same tier as php/ruby above.
    # NOTE: membership here must match the STACK's `language` string exactly as
    # used by _stack_readiness() below, not the _STACK_LANGUAGE_TOOL tool-key -
    # they differ for 4 of these (e.g. language "common lisp" -> tool key "lisp").
    "rust", "swift", "kotlin", "r", "scala", "haskell", "elixir", "dart",
    "julia", "fortran", "ada", "pascal/delphi", "erlang", "ocaml", "prolog",
    "common lisp", "progress 4gl", "pl/i", "dart / c#",
    # tree-sitter grammar-backed, not a compiler, but genuine whole-file syntax
    # parsing run across every file - clojure has no _EXTERNAL_VALIDATORS entry,
    # it dispatches straight to _TREE_SITTER_LANGUAGES.
    "clojure",
    # Heuristic structural validators (validators.py._LEGACY_HEURISTIC_VALIDATORS)
    # - no real compiler exists for these vendor-platform languages (see
    # _UNAVAILABLE_VENDOR_TOOLCHAINS's former contents) - honestly labeled
    # "heuristic" in validation_mode, but still run across every project file.
    # (pl/i and progress 4gl already added above with their correct language-
    # string form, not repeated here.)
    "abap", "rpg", "jcl", "mumps", "natural", "apex",
}
_HEURISTIC_ONLY_LANGUAGES = {
    # Must match the STACK's `language` string form (see _PROJECT_READY_LANGUAGES
    # note above) - "progress 4gl" and "pl/i", not the tool-key forms.
    "abap", "rpg", "jcl", "mumps", "natural", "progress 4gl", "apex", "pl/i",
}
_DISPLAY_LANGUAGE = {
    "csharp": "C#", "c": "C", "cpp": "C++", "java": "Java",
    "typescript": "TypeScript", "javascript": "JavaScript", "python": "Python",
    "go": "Go", "php": "PHP", "ruby": "Ruby", "cobol": "COBOL",
    "sql": "SQL", "lisp": "Common Lisp", "rpg": "RPG",
}
_ARTIFACT_LANGUAGES = {
    "YAML", "JSON", "TOML", "XML", "Markdown", "GraphQL", "Protobuf",
    "Dockerfile", "Terraform/HCL", "CloudFormation", "Kubernetes manifests",
    "Helm", "Ansible", "Jenkinsfile", "GitHub Actions",
}


# Function: _stack_readiness
def _stack_readiness(stack: dict, tools: dict) -> dict:
    language = str(stack.get("language") or "").strip().casefold()
    if language in _INTERNAL_PARSER_LANGUAGES:
        from services.build_runner import PRODUCTION_PROJECT_BUILD_LANGUAGES
        return {
            "available": True, "project_ready": True,
            "full_generation": language in PRODUCTION_PROJECT_BUILD_LANGUAGES,
            "validation_mode": "parser", "blocked_reason": None,
        }
    tool_key = _STACK_LANGUAGE_TOOL.get(language)
    if not tool_key:
        return {
            "available": False,
            "project_ready": False,
            "full_generation": False,
            "validation_mode": "unsupported",
            "blocked_reason": f"No strict validator is registered for {stack.get('language') or 'this target'}",
        }
    required = tool_key.split("+")
    stack_text = " ".join(
        str(stack.get(key) or "")
        for key in ("name", "backend", "frontend", "backend_tech", "frontend_tech")
    ).casefold()
    if language in {"dart", "dart / c#"} and "flutter" in stack_text:
        # Full Flutter host setup is optional for modernization generation;
        # Dart analyzer + .NET backend toolchain are the strict requirements.
        required = ["dart", "dotnet"]
    missing = [key for key in required if not tools.get(key, {}).get("ready")]
    if language == "typescript" or language == "javascript":
        missing = [
            key for key in ("npm", "typescript_validator")
            if not tools.get(key, {}).get("ready")
        ]
    from services.build_runner import node_compatibility_error
    node_versions = [
        int(version) for version in tools.get("node", {}).get("versions", [])
        if str(version).isdigit()
    ]
    node_error = node_compatibility_error(stack_text, node_versions)
    if node_error:
        missing.append(node_error)
    if "db2" in " ".join(str(stack.get(key) or "") for key in ("name", "database")).casefold():
        if not tools.get("db2_sql_parser", {}).get("ready"):
            missing.append("db2_sql_parser")
    artifact = any(language == value.casefold() for value in _ARTIFACT_LANGUAGES)
    is_heuristic = language in _HEURISTIC_ONLY_LANGUAGES
    # Structural heuristics are useful preflight checks, but cannot establish
    # vendor-platform build readiness. Keep generation selectable while making
    # the external compiler/platform prerequisite explicit.
    if is_heuristic:
        missing.append(f"{stack.get('backend') or stack.get('language')} vendor toolchain")
    missing = list(dict.fromkeys(missing))
    available = not missing
    # No open-source compiler exists for these languages (proprietary vendor
    # platforms - SAP, IBM i/z-OS, Software AG, Progress, Salesforce); validated
    # via validators.py._LEGACY_HEURISTIC_VALIDATORS structural checks instead of
    # a real compiler. Distinct validation_mode so this is never conflated with
    # genuine compiler/parser-backed validation elsewhere in the catalog.
    from services.build_runner import PRODUCTION_PROJECT_BUILD_LANGUAGES
    # PRODUCTION_PROJECT_BUILD_LANGUAGES is keyed by the internal language id
    # modernizer.py's target["language"] actually holds ("csharp"), not the
    # display string this function's `language` var is derived from ("C#" ->
    # casefold "c#") - _DISPLAY_LANGUAGE's "csharp"->"C#" mapping is the only
    # one of these that changes spelling on casefold, so it's the only alias
    # needed (java/typescript/javascript/python/go/sql all casefold back to
    # their own internal id unchanged).
    gate_language = {
        "c#": "csharp",
        "c++": "cpp",
        "common lisp": "lisp",
        "github actions": "github_actions",
        "kubernetes manifests": "kubernetes",
        "terraform/hcl": "hcl",
        "pl/sql": "sql",
        "t-sql": "sql",
    }.get(language, language)
    return {
        "available": available,
        "project_ready": available and (language in _PROJECT_READY_LANGUAGES or artifact),
        # Distinct from project_ready: project_ready is about *validation* rigor
        # (can a real compiler/parser check this language's code across the
        # whole project). full_generation is about *generation* capability (can
        # the engine actually write new domain code in this language, not just
        # validate it) - see services/build_runner.py's
        # PRODUCTION_PROJECT_BUILD_LANGUAGES, the single source of truth the
        # "project" output-mode gate itself uses (services/modernizer.py).
        "full_generation": available and gate_language in PRODUCTION_PROJECT_BUILD_LANGUAGES,
        "validation_mode": "heuristic" if is_heuristic else "compiler/parser",
        "blocked_reason": None if not missing else "Missing strict prerequisite(s): " + ", ".join(missing),
    }


# Function: target_stacks
@app.get("/api/modernize/target-stacks")
async def target_stacks():
    """Return engine-native and guided presets; custom is always supported."""
    from services.modernizer import TARGET_STACKS
    from services.build_runner import toolchain_status
    readiness = await asyncio.to_thread(toolchain_status)
    tools = readiness["tools"]
    native = []
    for stack_id, value in TARGET_STACKS.items():
        category = "Database migration" if value.get("language") == "sql" else value.get("language", "Other").title()
        display_language = _DISPLAY_LANGUAGE.get(value.get("language"), value.get("language"))
        native.append({"id": stack_id, "engine_target": stack_id, "name": value["name"], "category": category,
                       "language": display_language, "backend": value.get("backend_tech"),
                       "frontend": value.get("frontend_tech"), "database": value.get("db_tech"), "native": True})
    native_ids = {item["id"] for item in native}
    guided = [{"id": item[0], "engine_target": "custom", "name": item[1], "category": item[2],
               "language": item[3], "backend": item[4], "frontend": item[5], "database": item[6], "native": False}
              for item in _ADDITIONAL_STACK_PRESETS if item[0] not in native_ids]
    stacks = [{**stack, **_stack_readiness(stack, tools)} for stack in native + guided]
    production_ready = sorted({stack["language"] for stack in stacks if stack["available"] and stack.get("language")})
    externally_gated = sorted({stack["language"] for stack in stacks if not stack["available"] and stack.get("language")})
    ready_artifacts = sorted(
        language for language in production_ready if language in _ARTIFACT_LANGUAGES
    )
    gated_artifacts = sorted(
        language for language in externally_gated if language in _ARTIFACT_LANGUAGES
    )
    production_project_languages = sorted({
        stack["language"] for stack in stacks
        if stack.get("project_ready") and stack.get("language") not in _ARTIFACT_LANGUAGES
    })
    return {
        "stacks": stacks,
        "custom_supported": True,
        "supported_languages": [value for value in production_ready if value not in _ARTIFACT_LANGUAGES],
        "production_project_languages": production_project_languages,
        "externally_gated_languages": [value for value in externally_gated if value not in _ARTIFACT_LANGUAGES],
        "supported_artifacts": ready_artifacts,
        "externally_gated_artifacts": gated_artifacts,
        "validation_policy": (
            "Compiler/parser backed and fail-closed; vendor toolchains must be configured."
        ),
    }


# Function: modernization_toolchains
@app.get("/api/modernize/toolchains")
async def modernization_toolchains():
    """Report build-host prerequisites before a governed transformation starts."""
    from services.build_runner import toolchain_status
    return await asyncio.to_thread(toolchain_status)


# Function: _install_toolchain_job
def _install_toolchain_job(job_id: str, tool_id: str, package_id: str, actor: str) -> None:
    job = _TOOLCHAIN_INSTALL_JOBS[job_id]
    try:
        job.update(status="running", message=f"Installing {tool_id}")
        proc = subprocess.run([
            "winget", "install", "--id", package_id, "--exact", "--silent",
            "--accept-package-agreements", "--accept-source-agreements",
        ], capture_output=True, text=True, timeout=900)
        output = (proc.stdout + "\n" + proc.stderr).strip()[-4000:]
        if proc.returncode != 0:
            raise RuntimeError(output or f"Installer exited with code {proc.returncode}")
        job.update(status="completed", progress=100, message="Installation completed", output=output)
        logger.info("Toolchain %s installed by %s", tool_id, actor)
    except Exception as exc:
        record = build_error_record(
            exc,
            operation=f"install toolchain {tool_id}",
            component="toolchain-installer",
            job_id=job_id,
            artifact_path=package_id,
        )
        job.update(
            status="failed", message=record["description"],
            output=record["description"], error_details=record,
        )
        log_error_record(logger, record, exc)
    finally:
        job["updated_at"] = datetime.now(UTC).isoformat()


# Function: install_modernization_toolchain
@app.post("/api/modernize/toolchains/install")
async def install_modernization_toolchain(request: Request):
    _require_admin(request)
    body = await request.json()
    tool_id = str(body.get("tool_id") or "")
    package_id = _TOOLCHAIN_PACKAGES.get(tool_id)
    if not package_id:
        raise HTTPException(status_code=400, detail="This prerequisite is not in the approved installation catalog")
    if os.name != "nt" or not shutil.which("winget"):
        raise HTTPException(status_code=409, detail="Automated installation requires winget on the Windows build host")
    from services.build_runner import toolchain_status
    current = next((item for item in toolchain_status()["catalog"] if item["id"] == tool_id), None)
    if current and current["installed"]:
        return {"status": "completed", "message": f"{current['name']} is already installed"}
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    _TOOLCHAIN_INSTALL_JOBS[job_id] = {"job_id": job_id, "tool_id": tool_id, "status": "queued", "progress": 0,
        "message": "Installation queued", "created_at": now, "updated_at": now, "actor": _actor(request)}
    threading.Thread(target=_install_toolchain_job, args=(job_id, tool_id, package_id, _actor(request)), daemon=True).start()
    return _TOOLCHAIN_INSTALL_JOBS[job_id]


# Function: modernization_toolchain_install_status
@app.get("/api/modernize/toolchains/install/{job_id}")
async def modernization_toolchain_install_status(job_id: str, request: Request):
    _require_admin(request)
    if job_id not in _TOOLCHAIN_INSTALL_JOBS:
        raise HTTPException(status_code=404, detail="Installation job not found")
    return _TOOLCHAIN_INSTALL_JOBS[job_id]


# Function: detect_language
@app.get("/api/fs/detect")
async def detect_language(path: str):
    """Quick scan of a folder to detect primary language and suggest modernization targets."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from services.analyzer import _enumerate_files, _language_distribution, _detect_tech_stack

    target_path = Path(path)
    if not target_path.exists() or not target_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")

    try:
        files    = _enumerate_files(target_path)[:300]   # cap for speed
        lang_dist = _language_distribution(files)
        tech     = _detect_tech_stack(files)

        # Primary = highest file count among real code languages
        primary_lang, primary_meta = max(
            lang_dist.items(),
            key=lambda x: x[1].get("files", 0),
            default=("unknown", {}),
        )

        detected_techs = list(tech.keys())
        suggested    = _suggest_targets(detected_techs, primary_lang)

        return {
            "primary_language": primary_lang,
            "primary_label":    _LANG_LABELS.get(primary_lang, primary_lang or "Unknown"),
            "languages":        lang_dist,
            "detected_techs":   detected_techs,
            "tech_labels":      {k: _TECH_LABELS.get(k, k) for k in detected_techs},
            "suggested_targets": suggested,
            "file_count":       len(files),
        }
    except Exception as exc:
        _raise_internal_error(exc, operation="analyze source folder", artifact_path=str(target_path))


# Function: _suggest_targets
def _suggest_targets(techs: list, primary_lang: str | None) -> list:
    """Map detected tech stack to suggested modernization target IDs."""
    s: list = []
    if any(t in techs for t in ("asp_net_webforms", "asp_net_mvc", "asp_net_core", "winforms", "wpf")):
        s.extend(["aveva_mes", "dotnet8_blazor", "dotnet8_mvc", "react_ts"])
    if any(t in techs for t in ("java_ee", "spring")) or primary_lang == "java":
        s.extend(["spring_boot", "spring_boot_react"])
    if any(t in techs for t in ("jquery", "angular", "react")) or primary_lang in ("javascript", "typescript"):
        s.extend(["react_ts", "angular_ts", "vue3"])
    if "oracle_db" in techs:
        s.extend(["oracle_to_mssql", "oracle_to_postgres", "oracle_to_mongodb"])
    if "sql_server" in techs:
        s.append("mssql_to_postgres")
    if primary_lang == "cobol":
        s.extend(["spring_boot", "dotnet8_blazor"])
    if not s:
        s = ["aveva_mes", "dotnet8_blazor", "spring_boot", "react_ts"]
    # deduplicate while preserving order
    seen: set = set()
    return [x for x in s if not (x in seen or seen.add(x))]  # type: ignore[func-returns-value]


# Function: get_session
@app.get("/api/auth/session")
async def get_session(request: Request):
    payload = getattr(request.state, "auth", None)
    if not payload:
        return JSONResponse(status_code=401, content={"error": "No validated session"})
    return {
        "authenticated": True,
        "user": {
            "username": payload.get("sub"),
            "role": payload.get("role"),
            "apps": payload.get("apps", []),
            "credential_expires_at": payload.get("credential_expires_at"),
        },
    }


@app.post("/api/auth/login")
async def login(request: Request):
    try:
        body = await request.json()
        challenge = _ACCESS_STORE.begin_login(
            str(body.get("username") or "").strip(), str(body.get("password") or "")
        )
        return {"credential_required": True, "challenge": challenge, "expires_in_seconds": 300}
    except ValueError as exc:
        return JSONResponse(status_code=401, content={"error": str(exc)})
    except RuntimeError as exc:
        return JSONResponse(status_code=503, content={"error": str(exc)})


@app.post("/api/auth/credential")
async def complete_login(challenge: str = Form(...), credential: UploadFile = File(...)):
    content = await credential.read()
    if len(content) > 64 * 1024:
        raise HTTPException(status_code=413, detail="Credential file is too large")
    try:
        token, payload = _ACCESS_STORE.complete_login(challenge, content)
        _ACCESS_STORE.sync_projects([
            _PROJECT_STORE.get_project(project["id"]) for project in _PROJECT_STORE.list_projects()
        ])
        return {
            "token": token,
            "idle_timeout_seconds": None,
            "user": {"username": payload["sub"], "role": payload["role"], "apps": payload["apps"]},
            "credential": payload["credential"],
        }
    except ValueError as exc:
        return JSONResponse(status_code=401, content={"error": str(exc)})
    except RuntimeError as exc:
        return JSONResponse(status_code=503, content={"error": str(exc)})


# Function: _active_session_job_ids
def _active_session_job_ids() -> list[str]:
    """Return all server-side work that must retain the operator session."""
    with _JOBS_LOCK:
        active = [
            str(job_id) for job_id, job in _JOBS.items()
            if _is_active_job(job)
        ]
    active.extend(
        str(job_id) for job_id, job in _REQUIREMENTS_JOBS.items()
        if job.get("status") in {"queued", "pending", "running"}
    )
    active.extend(
        str(job_id) for job_id, job in _TOOLCHAIN_INSTALL_JOBS.items()
        if job.get("status") in {"queued", "pending", "running"}
    )
    return list(dict.fromkeys(active))


@app.post("/api/auth/logout")
async def logout(request: Request):
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    # Automatic idle logout is the safe default, including for older cached
    # clients that sent an empty body. Explicit UI sign-out sends force=true.
    force = bool((body or {}).get("force", False))
    active_jobs = _active_session_job_ids()
    if active_jobs and not force:
        return {
            "logged_out": False,
            "deferred": True,
            "reason": "active_jobs",
            "active_job_count": len(active_jobs),
        }
    token = _extract_bearer_token(request.headers.get("Authorization", ""))
    if token:
        _ACCESS_STORE.logout(token)
    return {"logged_out": True, "deferred": False, "active_job_count": 0}


# Function: refresh_access_token
@app.post("/api/auth/refresh")
async def refresh_access_token(request: Request):
    return JSONResponse(status_code=410, content={"error": "Session refresh is disabled; sign in and upload the credential file again"})
    """Exchange a token that's expired (or about to) for a fresh one.

    Exempted from enforce_auth (see public_paths) because the whole point is
    to accept a token the strict path would already reject on `exp` — the
    signature check here is what proves the caller actually held a
    legitimately-issued session, not just any client.
    """
    token = _extract_bearer_token(request.headers.get("Authorization", ""))
    if not token:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        token = (body or {}).get("token")
    if not token:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    try:
        payload = _decode_token_for_refresh(token)
    except (ValueError, RuntimeError) as exc:
        return JSONResponse(status_code=401, content={"error": str(exc)})
    new_token, exp = _issue_access_token(payload)
    return {"token": new_token, "exp": exp}


# ─── Modernization jobs ───────────────────────────────────────────────────────

# Function: list_jobs
@app.get("/api/modernize/jobs")
async def list_jobs():
    with _JOBS_LOCK:
        jobs = [_job_list_summary(job) for job in _JOBS.values()]
    return {"jobs": jobs}


def _report_datetime(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _jobs_report(date_from: str = "", date_to: str = "", tech_stack: str = "", status: str = "") -> dict:
    start = _report_datetime(f"{date_from}T00:00:00+00:00") if date_from else None
    end = _report_datetime(f"{date_to}T23:59:59.999999+00:00") if date_to else None
    rows = []
    with _JOBS_LOCK:
        source = list(_JOBS.values())
    all_stacks = sorted({str(_effective_job_target_stack(job)[0] or "Unspecified") for job in source})
    all_statuses = sorted({str(job.get("status") or "unknown") for job in source})
    for job in source:
        created = _report_datetime(job.get("created_at") or job.get("updated_at"))
        if start and (not created or created < start):
            continue
        if end and (not created or created > end):
            continue
        if tech_stack and str(_effective_job_target_stack(job)[0] or "") != tech_stack:
            continue
        if status and str(job.get("status") or "") != status:
            continue
        updated = _report_datetime(job.get("updated_at")) or created
        duration = max(0, (updated - created).total_seconds()) if created and updated else 0
        summary = _job_list_summary(job)
        summary.update({
            "duration_seconds": round(duration, 1),
            "created_date": created.date().isoformat() if created else "unknown",
        })
        rows.append(summary)
    rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)

    status_counts: dict[str, int] = {}
    stack_counts: dict[str, int] = {}
    daily: dict[str, dict] = {}
    durations = []
    for row in rows:
        state = row["status"]
        stack = row.get("target_stack") or "Unspecified"
        date = row["created_date"]
        status_counts[state] = status_counts.get(state, 0) + 1
        stack_counts[stack] = stack_counts.get(stack, 0) + 1
        bucket = daily.setdefault(date, {"date": date, "total": 0, "completed": 0, "failed": 0})
        bucket["total"] += 1
        if state == "completed":
            bucket["completed"] += 1
        if state in ("failed", "validation_failed"):
            bucket["failed"] += 1
        if state in ("completed", "validation_failed", "failed", "cancelled"):
            durations.append(row["duration_seconds"])
    completed = status_counts.get("completed", 0)
    failed = status_counts.get("failed", 0) + status_counts.get("validation_failed", 0)
    terminal = completed + failed
    return {
        "summary": {
            "total": len(rows),
            "completed": completed,
            "failed": status_counts.get("failed", 0),
            "validation_failed": status_counts.get("validation_failed", 0),
            "running": sum(status_counts.get(key, 0) for key in ("running", "queued", "pending")),
            "success_rate": round((completed / terminal * 100), 1) if terminal else 0,
            "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else 0,
            "output_count": sum(1 for row in rows if row["output_available"]),
        },
        "daily": sorted(daily.values(), key=lambda item: item["date"]),
        "tech_stacks": [{"name": key, "count": value} for key, value in sorted(stack_counts.items(), key=lambda item: (-item[1], item[0]))],
        "statuses": [{"name": key, "count": value} for key, value in sorted(status_counts.items())],
        "filter_options": {"tech_stacks": all_stacks, "statuses": all_statuses},
        "jobs": rows,
    }


@app.get("/api/modernize/jobs/report/export")
async def export_jobs_report(request: Request):
    params = request.query_params
    report = _jobs_report(params.get("date_from", ""), params.get("date_to", ""), params.get("tech_stack", ""), params.get("status", ""))
    export_format = params.get("format", "csv").lower()
    if export_format == "json":
        data = json.dumps(report, indent=2, default=str).encode("utf-8")
        media, extension = "application/json", "json"
    else:
        buffer = io.StringIO()
        fields = ["job_id", "status", "target_stack", "created_at", "updated_at", "duration_seconds", "generated_file_count", "llm_model", "optimization_profile", "output_available"]
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["jobs"])
        data, media, extension = buffer.getvalue().encode("utf-8-sig"), "text/csv", "csv"
    return StreamingResponse(
        io.BytesIO(data), media_type=media,
        headers={"Content-Disposition": f'attachment; filename="modernization-jobs-report.{extension}"'},
    )


@app.get("/api/modernize/jobs/report")
async def jobs_report(request: Request):
    params = request.query_params
    return _jobs_report(params.get("date_from", ""), params.get("date_to", ""), params.get("tech_stack", ""), params.get("status", ""))


# Function: active_job_count
@app.get("/api/modernize/jobs/active-count")
async def active_job_count(request: Request):
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(status_code=403, detail="Active-job probe is loopback-only")
    active = [job for job in _JOBS.values() if _is_active_job(job)]
    return {
        "active_jobs": len(_active_session_job_ids()),
        "running": sum(1 for job in active if job.get("status") == "running"),
        "pending": sum(1 for job in active if job.get("status") in ("pending", "queued")),
    }


# Function: start_analysis
@app.post("/api/modernize/analyze")
async def start_analysis(request: Request):
    body = await request.json()
    folder_path       = (body.get("folder_path") or "").strip()
    target_stack      = (body.get("target_stack") or "aveva_mes").strip()
    custom_stack_desc = (body.get("custom_stack_desc") or "").strip()
    output_mode       = _validated_generation_output_mode(
        body.get("output_mode") or "project"
    )
    if not folder_path:
        raise HTTPException(status_code=400, detail="folder_path is required")

    p = Path(folder_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {folder_path}")
    if not p.is_dir():
        raise HTTPException(status_code=400, detail="folder_path must be a directory")

    target_stack = await asyncio.to_thread(
        _resolve_quick_analysis_target, str(p), target_stack,
    )

    job_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    _JOBS[job_id] = {
        "job_id":            job_id,
        "folder_path":       str(p.resolve()),
        "target_stack":      target_stack,
        "custom_stack_desc": custom_stack_desc,
        "output_mode":       output_mode,
        "status":       "queued",
        "progress":     0,
        "phase":       "queued",
        "created_at":  now,
        "updated_at":  now,
        "analysis":    None,
        "output":      None,
        "validation":  None,
        "error":       None,
        "events":      [],
        **_generation_runtime_snapshot(),
    }
    _persist_job(job_id)
    _JOB_QUEUES[job_id] = queue.Queue(maxsize=512)

    _submit_generation_job(
        job_id, _analysis_worker,
        str(p.resolve()), target_stack, custom_stack_desc, "", output_mode,
    )

    return {"job_id": job_id, "status": "queued"}


# Function: get_job
@app.get("/api/modernize/jobs/{job_id}")
async def get_job(job_id: str):
    return _job_response(_get_job(job_id))


@app.get("/api/modernize/jobs/{job_id}/status")
async def get_job_status(job_id: str):
    return _job_status_response(_get_job(job_id))


# Function: stream_job
@app.get("/api/modernize/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    _get_job(job_id)  # 404 if not found
    q = _JOB_QUEUES.get(job_id) or queue.Queue()

    # Function: event_generator
    async def event_generator():
        try:
            while True:
                deadline = asyncio.get_running_loop().time() + _JOB_STREAM_KEEPALIVE_SECONDS
                while True:
                    try:
                        event = q.get_nowait()
                        break
                    except queue.Empty:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            event = None
                            break
                        # Avoid consuming the shared asyncio thread pool for every
                        # open SSE connection. A short cooperative poll keeps both
                        # event delivery and unrelated requests responsive even
                        # while compiler repair workers are saturated.
                        await asyncio.sleep(min(0.05, remaining))
                if event is not None:
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") in ("complete", "validation_failed", "error", "cancelled"):
                        break
                else:
                    job = _JOBS.get(job_id, {})
                    if job.get("status") in ("completed", "validation_failed", "failed", "cancelled"):
                        break
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record = build_error_record(
                exc,
                operation="stream job progress",
                component="sse",
                job_id=job_id,
            )
            log_error_record(logger, record, exc)
            yield "data: " + json.dumps({
                "type": "error",
                "message": "Progress stream failed safely; reconnect or poll job status.",
                "error_id": record["error_id"],
            }) + "\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# Function: download_output
@app.get("/api/modernize/jobs/{job_id}/output")
async def download_output(job_id: str):
    job = _get_job(job_id)
    output = job.get("output")
    # A "failed" job that was interrupted mid-run (backend restart) can still
    # have partial output worth downloading — see on_file in _prompt_worker.
    # A job that failed with no output at all (e.g. the LLM was unreachable
    # from the first call) has nothing to serve.
    if job["status"] not in ("completed", "validation_failed", "failed") or not output:
        raise HTTPException(status_code=400, detail="Job not yet completed")

    # Build a ZIP in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, content in output.items():
            zf.writestr(filename, content)
        if job["status"] == "validation_failed":
            zf.writestr(
                "REVIEW_REQUIRED.md",
                "# Review required\n\n"
                "This project did not pass strict build and semantic acceptance. "
                "It is provided for diagnosis and remediation, not as a production-ready release.\n\n"
                "See `validation-report.json` and `_GENERATION_AUDIT.md` for details.\n",
            )
            zf.writestr(
                "validation-report.json",
                json.dumps(job.get("validation") or {}, indent=2, default=str),
            )
    buf.seek(0)

    folder_path = job.get("folder_path")
    if folder_path:
        raw_name = Path(folder_path).name
    else:
        # Prompt-driven jobs have no folder_path — every output key is
        # "<ProjectName>/relative/path" (see generate_from_prompt's
        # project_name derivation), so recover the name from that common
        # prefix instead of falling back to the opaque job_id hash.
        first_key = next(iter(output), "")
        raw_name = first_key.split("/", 1)[0] if "/" in first_key else job_id
    safe_name = re.sub(r"[^\w-]", "_", raw_name) or job_id
    archive_prefix = {
        "completed": "modernized",
        "validation_failed": "review_required",
        "failed": "partial",
    }.get(job["status"], "generated")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{archive_prefix}_{safe_name}.zip"',
            "X-Artifact-Status": job["status"],
        },
    )


# Function: delete_job
@app.delete("/api/modernize/jobs/{job_id}")
async def delete_job(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        if _is_active_job(job):
            cancel_event = _JOB_CANCEL_EVENTS.get(job_id)
            future = _JOB_FUTURES.get(job_id)
            if cancel_event:
                cancel_event.set()
            cancelled_before_start = bool(future and future.cancel())
            if cancelled_before_start:
                job.update({
                    "status": "cancelled", "phase": "cancelled",
                    "error": "Generation cancelled before execution began.",
                })
            else:
                job["cancel_requested"] = True
        else:
            cancelled_before_start = False
        if _is_active_job(job) and not cancelled_before_start:
            _persist_job(job_id)
            _push(job_id, {
                "type": "progress", "phase": "cancelling", "progress": job.get("progress", 0),
                "message": "Cancellation requested; stopping after the current generation step…",
            })
            return {"cancellation_requested": job_id}
        _JOBS.pop(job_id, None)
        _JOB_QUEUES.pop(job_id, None)
        _LAST_JOB_PERSIST.pop(job_id, None)
        _JOB_CANCEL_EVENTS.pop(job_id, None)
        _JOB_FUTURES.pop(job_id, None)
    try:
        _job_file(job_id).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        _ACCESS_STORE.delete_job(job_id)
    except Exception:
        logger.warning("Could not delete persisted job checkpoint %s", job_id, exc_info=True)
    return {"deleted": job_id}


# ─── Guide/document text extraction helper ───────────────────────────────────

# Function: _extract_guide_text
# Function: _extract_pdf_text
def _extract_pdf_text(fname: str, content: bytes) -> str:
    try:
        import pymupdf
        doc = pymupdf.open(stream=content, filetype="pdf")
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        if not text.strip():
            raise ValueError(f"{fname}: PDF contains no extractable text; supply an OCR/text PDF")
        return f"=== {fname} [PDF] ===\n{text}"
    except ImportError as exc:
        raise ValueError(f"{fname}: PDF extraction requires PyMuPDF") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"{fname}: PDF extraction failed: {exc}") from exc


# Function: _extract_docx_text
def _extract_docx_text(fname: str, content: bytes) -> str:
    try:
        import docx  # python-docx
        import io as _io
        document = docx.Document(_io.BytesIO(content))
        blocks = [p.text for p in document.paragraphs]
        for table in document.tables:
            blocks.extend("\t".join(cell.text for cell in row.cells) for row in table.rows)
        text = "\n".join(blocks)
        if not text.strip():
            raise ValueError(f"{fname}: DOCX contains no extractable text")
        return f"=== {fname} [DOCX] ===\n{text}"
    except ImportError as exc:
        raise ValueError(f"{fname}: DOCX extraction requires python-docx") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"{fname}: DOCX extraction failed: {exc}") from exc


# Function: _extract_guide_text
async def _extract_guide_text(files: list[UploadFile]) -> tuple[list, str]:
    """
    Split uploaded files into image blobs + plain guide text.
    Supported text types include source, Markdown, .editorconfig, and common
    linter/formatter configuration formats.
    Supported doc types:  .pdf (if fitz/PyMuPDF available), .docx (if python-docx available)
    Returns (images_data, guide_text)
    """
    images_data: list = []
    guide_parts: list = []

    text_exts = {
        ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".json",
        ".yaml", ".yml", ".csv", ".xml", ".html", ".htm", ".cs",
        ".java", ".sql", ".sh", ".bat", ".ps1", ".go", ".rs", ".toml",
        ".ini", ".cfg", ".conf", ".properties", ".gradle", ".kts", ".neon",
    }
    config_names = {
        ".editorconfig", ".prettierrc", ".eslintrc", ".stylelintrc",
        ".clang-format", "dockerfile", "makefile", "ruff.toml", "pyproject.toml",
        "mypy.ini", "setup.cfg", "checkstyle.xml", "spotbugs.xml", "stylecop.json",
        "eslint.config.js", "eslint.config.mjs", "detekt.yml", ".golangci.yml",
    }
    max_file_bytes = 10 * 1024 * 1024
    max_total_bytes = 40 * 1024 * 1024
    total_bytes = 0

    for f in files:
        if not f or not f.filename:
            continue
        content = await f.read()
        fname   = f.filename
        ct      = (f.content_type or "").lower()
        ext     = Path(fname).suffix.lower()
        total_bytes += len(content)
        if len(content) > max_file_bytes:
            raise HTTPException(status_code=413, detail=f"{fname}: attachment exceeds 10 MiB")
        if total_bytes > max_total_bytes:
            raise HTTPException(status_code=413, detail="Attachments exceed the 40 MiB request limit")

        if ct.startswith("image/") or ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
            images_data.append({"filename": fname, "content_type": ct or "image/png", "data": content})

        elif ext in text_exts or Path(fname).name.casefold() in config_names or ct.startswith("text/"):
            try:
                try:
                    text = content.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = content.decode("utf-16")
                guide_parts.append(f"=== {fname} ===\n{text}")
            except UnicodeError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"{fname}: text guidelines must be UTF-8 or UTF-16; decoding failed",
                ) from exc

        elif ext == ".pdf":
            try:
                guide_parts.append(_extract_pdf_text(fname, content))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        elif ext == ".docx":
            try:
                guide_parts.append(_extract_docx_text(fname, content))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        elif ext == ".doc":
            raise HTTPException(status_code=400, detail=f"{fname}: legacy .doc is unsupported; convert it to .docx")
        else:
            raise HTTPException(status_code=400, detail=f"{fname}: unsupported reference/guideline file type")

    guide_text = "\n\n".join(guide_parts)
    if len(guide_text) > 60_000:
        raise HTTPException(
            status_code=413,
            detail=(
                "Extracted guideline text exceeds 60,000 characters. Split or reduce the "
                "documents so every accepted rule can fit in generation context without truncation."
            ),
        )
    return images_data, guide_text


@app.post("/api/generation-guidelines/extract")
async def extract_generation_guidelines(files: list[UploadFile] = File(default=[])):
    """Normalize uploaded standards into auditable generation instructions."""
    if not files:
        raise HTTPException(status_code=400, detail="Select at least one guideline document")
    images, guide_text = await _extract_guide_text(files)
    if images:
        raise HTTPException(
            status_code=400,
            detail="Image-only guidelines are unsupported for governed projects; upload an OCR/text PDF, DOCX, or text/configuration file.",
        )
    if not guide_text.strip():
        raise HTTPException(status_code=400, detail="The uploaded documents contain no extractable guideline text")
    documents = [
        {"name": str(item.filename), "content_type": str(item.content_type or "application/octet-stream")}
        for item in files if item and item.filename
    ]
    return {
        "guide_text": guide_text,
        "documents": documents,
        "sha256": hashlib.sha256(guide_text.encode("utf-8")).hexdigest(),
        "character_count": len(guide_text),
    }


# ─── Prompt-based analysis endpoint ─────────────────────────────────────────

# Function: analyze_from_prompt
@app.post("/api/modernize/analyze-prompt")
async def analyze_from_prompt(
    prompt: str = Form(...),
    target_stack: str = Form("prompt_inferred"),
    custom_stack_desc: str = Form(""),
    output_mode: str = Form("project"),
    files: list[UploadFile] = File(default=[]),
    # Legacy field alias kept for backward compat
    images: list[UploadFile] = File(default=[]),
):
    """Start code generation from a natural-language prompt with optional file attachments
    (images, PDFs, text docs, source files, CSV, etc.)."""
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    output_mode = _validated_generation_output_mode(output_mode)
    target_stack = _resolve_prompt_analysis_target(prompt, target_stack, custom_stack_desc)
    from services.modernizer.target_config import TARGET_STACKS
    target_stack_name = str(TARGET_STACKS.get(target_stack, {}).get("name") or target_stack)
    from services.modernizer import _unresolved_requirement_placeholders
    unresolved = _unresolved_requirement_placeholders(prompt)
    if unresolved:
        raise HTTPException(
            status_code=400,
            detail=(
                "Replace unresolved requirement placeholders before generation: "
                + ", ".join(unresolved[:8])
            ),
        )

    all_uploads = list(files or []) + list(images or [])
    images_data, guide_text = await _extract_guide_text(all_uploads)
    guideline_documents = [
        {"name": str(item.filename), "content_type": str(item.content_type or "application/octet-stream")}
        for item in all_uploads if item and item.filename and not str(item.content_type or "").startswith("image/")
    ]

    job_id = str(uuid.uuid4())[:8]
    now = datetime.now(UTC).isoformat()
    _JOBS[job_id] = {
        "job_id":            job_id,
        "folder_path":       None,
        "prompt":            prompt,
        "target_stack":      target_stack,
        "target_stack_name": target_stack_name,
        "custom_stack_desc": custom_stack_desc.strip(),
        "output_mode":       output_mode,
        "attached_files":    len(all_uploads),
        "guideline_documents": guideline_documents,
        "guidelines_sha256": hashlib.sha256(guide_text.encode("utf-8")).hexdigest() if guide_text else None,
        "status":       "queued",
        "progress":     0,
        "phase":        "queued",
        "analysis":     None,
        "output":       None,
        "validation":   None,
        "error":        None,
        "created_at":   now,
        "updated_at":   now,
        "events":       [],
        **_generation_runtime_snapshot(),
    }
    _persist_job(job_id)
    _JOB_QUEUES[job_id] = queue.Queue(maxsize=512)

    _submit_generation_job(
        job_id, _prompt_worker,
        prompt, target_stack, images_data, custom_stack_desc.strip(), guide_text, output_mode,
    )

    return {"job_id": job_id, "status": "queued"}


# Function: analyze_folder_with_guides
@app.post("/api/modernize/analyze-with-guides")
async def analyze_folder_with_guides(
    folder_path: str = Form(...),
    target_stack: str = Form("aveva_mes"),
    custom_stack_desc: str = Form(""),
    output_mode: str = Form("project"),
    files: list[UploadFile] = File(default=[]),
):
    """Start folder analysis with optional reference guide file attachments."""
    folder_path = folder_path.strip()
    output_mode = _validated_generation_output_mode(output_mode)
    if not folder_path:
        raise HTTPException(status_code=400, detail="folder_path is required")

    p = Path(folder_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {folder_path}")
    if not p.is_dir():
        raise HTTPException(status_code=400, detail="folder_path must be a directory")

    target_stack = await asyncio.to_thread(
        _resolve_quick_analysis_target, str(p), target_stack,
    )

    _, guide_text = await _extract_guide_text(files or [])

    job_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    _JOBS[job_id] = {
        "job_id":            job_id,
        "folder_path":       str(p.resolve()),
        "target_stack":      target_stack,
        "custom_stack_desc": custom_stack_desc.strip(),
        "output_mode":       output_mode,
        "attached_files":    len(files or []),
        "status":       "queued",
        "progress":     0,
        "phase":        "queued",
        "created_at":   now,
        "updated_at":   now,
        "analysis":     None,
        "output":       None,
        "validation":   None,
        "error":        None,
        "events":       [],
        **_generation_runtime_snapshot(),
    }
    _persist_job(job_id)
    _JOB_QUEUES[job_id] = queue.Queue(maxsize=512)

    _submit_generation_job(
        job_id, _analysis_worker,
        str(p.resolve()), target_stack, custom_stack_desc.strip(), guide_text, output_mode,
    )

    return {"job_id": job_id, "status": "queued"}


# ─── Analysis worker (runs in background thread) ──────────────────────────────

# Function: _push
def _push(job_id: str, event: dict, *, record: bool = True):
    """Send an SSE event and update job state."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        enriched_event = {
            **event,
            "ts": datetime.now(UTC).isoformat(),
        }
        if record:
            events = job.setdefault("events", [])
            events.append(enriched_event)
            if len(events) > _JOB_EVENT_LIMIT:
                del events[:-_JOB_EVENT_LIMIT]
        job["updated_at"] = datetime.now(UTC).isoformat()

    q = _JOB_QUEUES.get(job_id)
    if q:
        try:
            q.put_nowait(enriched_event)
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(enriched_event)
            except queue.Full:
                logger.warning("Dropped SSE event for job %s because its queue remained full", job_id)
    terminal = enriched_event.get("type") in {"complete", "validation_failed", "error", "cancelled"}
    _persist_job(job_id, force=terminal)


def _mark_job_running(job_id: str) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["phase"] = "starting"
        job["started_at"] = datetime.now(UTC).isoformat()
    _push(job_id, {
        "type": "progress", "phase": "starting", "progress": 1,
        "message": "Background generation worker started",
    })


# Function: _analysis_worker
def _analysis_worker(job_id: str, folder_path: str, target_stack: str = "aveva_mes", custom_stack_desc: str = "", guide_text: str = "", output_mode: str = "project"):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        logger.warning("Analysis worker skipped because job %s is no longer registered", job_id)
        return
    try:
        from services.analyzer   import analyze_project
        from services.modernizer import modernize_project
    except ImportError as exc:
        _record_worker_failure(job_id, exc, operation="load analysis services")
        return

    try:
        _raise_if_job_cancelled(job_id)
        _mark_job_running(job_id)

        # Function: on_progress
        def on_progress(phase: str, pct: int, message: str):
            _raise_if_job_cancelled(job_id)
            with _JOBS_LOCK:
                if not _is_active_job(job):
                    return
                job["status"]   = "running"
                job["phase"]    = phase
                job["progress"] = pct
            _push(job_id, {
                "type":     "progress",
                "phase":    phase,
                "progress": pct,
                "message":  message,
            })

        # Phase 1: deep analysis
        analysis = analyze_project(
            folder_path, on_progress, target_stack, custom_stack_desc,
        )
        _raise_if_job_cancelled(job_id)
        job["analysis"] = analysis

        _push(job_id, {
            "type":     "analysis_complete",
            "progress": 50,
            "analysis": analysis,
        })

        # Phase 2: generate modernized code
        output, validation = modernize_project(folder_path, analysis, target_stack, on_progress, custom_stack_desc, guide_text=guide_text, output_mode=output_mode)
        _raise_if_job_cancelled(job_id)
        job["output"]     = output
        job["validation"] = validation
        validation_failed = _failed_strict_validation(validation, output_mode == "project")
        job["status"]   = "validation_failed" if validation_failed else "completed"
        job["progress"] = 100
        job["phase"] = "validation_failed" if validation_failed else "complete"

        project_id = job.get("project_id")
        if project_id:
            actor = job.get("actor", "local-operator")
            parent = job.get("plan_snapshot_id")
            output_snapshot = _PROJECT_STORE.add_output_snapshot(
                project_id, output, actor,
                {"target_stack": target_stack, "job_id": job_id, "model": _JOBS.get(job_id, {}).get("llm_model"),
                 "prompt_template_version": "governed-contracts-v1"}, parent,
            )
            _PROJECT_STORE.set_status(project_id, "Validation Running")
            validation_snapshot = _PROJECT_STORE.add_json_snapshot(
                project_id, "validation", validation, actor,
                {"job_id": job_id, "output_snapshot_id": output_snapshot["id"]}, output_snapshot["id"],
            )
            job["output_snapshot_id"] = output_snapshot["id"]
            job["validation_snapshot_id"] = validation_snapshot["id"]
            _PROJECT_STORE.set_status(project_id, "Review Required")

        _push(job_id, {
            "type":     "validation_failed" if validation_failed else "complete",
            "progress": 100,
            "message":  (
                _validation_failure_message(validation, output_mode == "project")
                if validation_failed else "Modernization complete"
            ),
            "output":   output if output.get("__single_file__") else None,
            "validation": validation,
        })

    except Exception as exc:
        cancel_event = _JOB_CANCEL_EVENTS.get(job_id)
        if isinstance(exc, _JobCancelled) or (cancel_event and cancel_event.is_set()):
            _finish_job_cancelled(job_id)
            return
        _record_worker_failure(job_id, exc, operation="legacy analysis and modernization")


# Function: _prompt_worker
def _single_file_failed_validation(output: dict, validation: dict) -> bool:
    """Backward-compatible helper retained for callers outside this module."""
    return bool(output.get("__single_file__")) and int((validation or {}).get("failed", 0)) > 0


# Function: _failed_strict_validation
def _failed_strict_validation(validation: dict, require_project_build: bool) -> bool:
    """A job may complete only after strict checks, and projects also require
    their registered whole-output validation route."""
    result = validation or {}
    if result.get("generation_validated", result.get("production_ready")) is False:
        return True
    if int(result.get("failed", 0)) > 0 or int(result.get("strict_checked", 0)) <= 0:
        return True
    build = result.get("build")
    return bool(require_project_build and (not build or not build.get("passed")))


def _validation_failure_message(validation: dict, require_project_build: bool = False) -> str:
    """Describe the gate that failed instead of mislabeling every failure."""
    result = validation or {}
    governance = result.get("governance") or {}
    if governance.get("passed") is False:
        diagnostics = [str(item) for item in governance.get("diagnostics") or []]
        detail = "; ".join(diagnostics[:3]) or "accountability or traceability gate failed"
        return f"Generated code passed source validation but failed governance: {detail}"[:800]
    files = result.get("files") or []
    failed_file = next((item for item in files if item.get("passed") is False), None)
    if failed_file:
        checker = str(failed_file.get("checker") or "strict source")
        diagnostics = [str(item) for item in failed_file.get("diagnostics") or []]
        detail = "; ".join(diagnostics[:3]) or "no validator diagnostic was returned"
        return f"Generated code failed {checker} validation: {detail}"[:800]
    build = result.get("build") or {}
    if require_project_build and not build.get("passed"):
        checker = str(build.get("checker") or "project build")
        diagnostics = [str(item) for item in build.get("diagnostics") or []]
        if not diagnostics:
            remaining_errors = build.get("remaining_errors") or {}
            if isinstance(remaining_errors, dict):
                for path, errors in remaining_errors.items():
                    values = errors if isinstance(errors, list) else [errors]
                    diagnostics.extend(
                        f"{path}: {error}" for error in values if str(error).strip()
                    )
                    if len(diagnostics) >= 3:
                        break
        detail = "; ".join(diagnostics[:3]) or "whole-project validation did not pass"
        return f"Generated project failed {checker}: {detail}"[:800]
    if int(result.get("strict_checked", 0)) <= 0:
        return "Generated code was not accepted because no strict compiler/parser check ran"
    return "Generated code failed strict validation; inspect the validation report for diagnostics"


# Function: _record_worker_failure
def _record_worker_failure(
    job_id: str,
    exc: BaseException,
    *,
    operation: str = "generation worker",
) -> None:
    """Persist terminal failure and make an approved governed plan retryable."""
    artifact_path = str(getattr(exc, "path", "") or "") or None
    record = build_error_record(
        exc,
        operation=operation,
        component="background-job",
        job_id=job_id,
        artifact_path=artifact_path,
    )
    log_error_record(logger, record, exc)
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            logger.warning(
                "Could not persist worker failure %s because job %s is missing",
                record["error_id"], job_id,
            )
            return
        job["status"] = "failed"
        job["phase"] = "failed"
        job["error"] = record["description"]
        job["error_details"] = record
    project_id = job.get("project_id")
    if project_id:
        try:
            project = _PROJECT_STORE.get_project(project_id)
            if project and project.get("status") == "Transformation Running":
                _PROJECT_STORE.set_status(project_id, "Plan Approved")
        except (KeyError, ValueError, OSError):
            logger.warning("Could not restore project %s after job failure", project_id, exc_info=True)
    _persist_job(job_id)
    _push(job_id, {
        "type": "error",
        "message": record["description"],
        "error_id": record["error_id"],
        "details": record,
    })


# Function: _prompt_worker
def _prompt_worker(job_id: str, user_prompt: str, target_stack: str, images_data: list, custom_stack_desc: str = "", guide_text: str = "", output_mode: str = "project"):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        logger.warning("Prompt worker skipped because job %s is no longer registered", job_id)
        return
    try:
        from services.modernizer import generate_from_prompt
    except ImportError as exc:
        _record_worker_failure(job_id, exc, operation="load prompt generation services")
        return

    try:
        _raise_if_job_cancelled(job_id)
        _mark_job_running(job_id)
        job["output"] = {}
        if output_mode == "single_file":
            job["live_output"] = ""
            job["live_output_phase"] = "drafting"

        stream_state = {
            "emitted_chars": 0,
            "last_emit": 0.0,
            "pending_chunks": [],
            "pending_chars": 0,
        }

        # Function: on_progress
        def on_progress(phase: str, pct: int, message: str):
            _raise_if_job_cancelled(job_id)
            with _JOBS_LOCK:
                # A wave timeout can leave an already-running LLM future alive
                # briefly. Its late callback must never resurrect a terminal
                # job and indefinitely block session cleanup or deployment.
                if not _is_active_job(job):
                    return
                job["status"]   = "running"
                job["phase"]    = phase
                job["progress"] = pct
            _push(job_id, {
                "type":     "progress",
                "phase":    phase,
                "progress": pct,
                "message":  message,
            })

        # Function: on_single_file_stream
        def on_single_file_stream(token: str, reset: bool = False):
            """Checkpoint an explicitly non-final preview and stream bounded deltas.

            The preview is never placed in ``output`` and therefore cannot be
            downloaded or accepted as an artifact. A token-limit retry resets it
            before the replacement draft starts.
            """
            _raise_if_job_cancelled(job_id)
            now = time.monotonic()
            with _JOBS_LOCK:
                if not _is_active_job(job):
                    return
                if reset:
                    job["live_output"] = ""
                    job["live_output_phase"] = "drafting"
                    stream_state["emitted_chars"] = 0
                    stream_state["pending_chunks"].clear()
                    stream_state["pending_chars"] = 0
                if token:
                    stream_state["pending_chunks"].append(token)
                    stream_state["pending_chars"] += len(token)
                should_emit = bool(
                    reset
                    or stream_state["pending_chars"] >= 512
                    or now - stream_state["last_emit"] >= 0.5
                )
                if not should_emit:
                    return
                offset = stream_state["emitted_chars"]
                pending = "".join(stream_state["pending_chunks"])
                content = str(job.get("live_output") or "") + pending
                job["live_output"] = content
                stream_state["pending_chunks"].clear()
                stream_state["pending_chars"] = 0
                stream_state["emitted_chars"] = len(content)
                stream_state["last_emit"] = now
            _push(job_id, {
                "type": "code_delta",
                "offset": 0 if reset else offset,
                "content": content if reset else pending,
                "reset": reset,
                "phase": "drafting",
            }, record=False)

        if output_mode == "single_file":
            on_progress.on_single_file_stream = on_single_file_stream

        # Function: on_file
        def on_file(path: str, content: str):
            """Persist each file the instant it's generated. This backend
            gets killed by something outside the app roughly every 3-5
            minutes under load — often shorter than a full multi-file
            generation — so without this, a job interrupted mid-run loses
            every file it had already finished, not just the ones in flight."""
            _raise_if_job_cancelled(job_id)
            with _JOBS_LOCK:
                if not _is_active_job(job):
                    return
                job["output"][path] = content
            _persist_job(job_id, force=False)

        output, validation = generate_from_prompt(
            user_prompt, target_stack, images_data, on_progress, custom_stack_desc,
            guide_text=guide_text, output_mode=output_mode, on_file=on_file,
        )
        _raise_if_job_cancelled(job_id)
        job["output"]     = output
        job["validation"] = validation
        effective_output_mode = str(validation.get("output_mode") or output_mode)
        job["effective_output_mode"] = effective_output_mode
        if output.get("__single_file__"):
            job["live_output"] = output["__single_file__"]
            job["live_output_phase"] = "final"
        validation_failed = _failed_strict_validation(
            validation, effective_output_mode == "project",
        )
        job["status"]   = "validation_failed" if validation_failed else "completed"
        job["progress"] = 100
        job["phase"] = "validation_failed" if validation_failed else "complete"

        project_id = job.get("project_id")
        if project_id:
            actor = job.get("actor", "local-operator")
            parent = job.get("plan_snapshot_id")
            output_snapshot = _PROJECT_STORE.add_output_snapshot(project_id, output, actor,
                {"target_stack": target_stack, "job_id": job_id, "model": _JOBS.get(job_id, {}).get("llm_model"),
                 "prompt_template_version": "governed-prompt-contracts-v1",
                 "effective_output_mode": effective_output_mode}, parent)
            _PROJECT_STORE.set_status(project_id, "Validation Running")
            validation_snapshot = _PROJECT_STORE.add_json_snapshot(project_id, "validation", validation, actor,
                {"job_id": job_id, "output_snapshot_id": output_snapshot["id"]}, output_snapshot["id"])
            job["output_snapshot_id"] = output_snapshot["id"]
            job["validation_snapshot_id"] = validation_snapshot["id"]
            _PROJECT_STORE.set_status(project_id, "Review Required")

        _push(job_id, {
            "type":     "validation_failed" if validation_failed else "complete",
            "progress": 100,
            "message":  (
                _validation_failure_message(validation, effective_output_mode == "project")
                if validation_failed else "Code generation complete"
            ),
            "output":   output if output.get("__single_file__") else None,
            "validation": validation,
        })

    except Exception as exc:
        cancel_event = _JOB_CANCEL_EVENTS.get(job_id)
        if isinstance(exc, _JobCancelled) or (cancel_event and cancel_event.is_set()):
            _finish_job_cancelled(job_id)
            return
        _record_worker_failure(job_id, exc, operation="prompt-based code generation")


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os
    uvicorn.run(
        "api.server:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "18084")),
        reload=False,
        log_level="info",
    )
