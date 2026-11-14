"""PostgreSQL-backed login, idle sessions, and signed credential verification."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, UTC
from pathlib import Path

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - reported as configuration failure at runtime
    psycopg = None
    Jsonb = None

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from services.trusted_clock import TrustedClock

PASSWORD_ITERATIONS = 600_000
CHALLENGE_MINUTES = 5
EXPECTED_PUBLIC_KEY_SHA256 = "26eeb5cfb494924a248bc3c2f220f3f85650cc61032d0a233f9c7d2cd608fe2f"
APPLICATION_EXPIRES_AT = datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_application_active(now: datetime) -> None:
    if now > APPLICATION_EXPIRES_AT:
        raise RuntimeError("Modernization authorization expired on 2026-11-30; startup denied")


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")


def verify_password(password: str, salt_text: str, digest_text: str) -> bool:
    salt = base64.b64decode(salt_text)
    _, candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, digest_text)


def verify_credential_file(content: bytes, public_key_path: Path, now: datetime | None = None) -> dict:
    """Verify the Ed25519 signature and expiry of a credential envelope."""
    try:
        envelope = json.loads(content.decode("utf-8"))
        payload = envelope["payload"]
        signature = base64.b64decode(envelope["signature"])
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("Credential verifier key is not Ed25519")
        public_key.verify(signature, canonical)
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
    except (KeyError, ValueError, TypeError, json.JSONDecodeError, InvalidSignature) as exc:
        raise ValueError("Credential file is invalid or has been modified") from exc
    if (now or _utcnow()) > expires_at:
        raise ValueError(f"Credential file expired on {expires_at.date().isoformat()}")
    if payload.get("application") != "Modernization":
        raise ValueError("Credential file is not valid for Modernization")
    return payload


class AccessStore:
    def __init__(self, database_url: str | None = None, public_key_path: Path | None = None,
                 clock: TrustedClock | None = None):
        self.database_url = database_url or os.getenv("MODERNIZATION_DATABASE_URL", "")
        self.public_key_path = public_key_path or Path(
            os.getenv("MODERNIZATION_CREDENTIAL_PUBLIC_KEY")
            or Path(__file__).resolve().parents[1] / "data" / "security" / "credential_public_key.pem"
        )
        self.clock = clock or TrustedClock()
        self._initialized = False

    def _connect(self):
        if not self.database_url:
            raise RuntimeError("MODERNIZATION_DATABASE_URL is required")
        if psycopg is None:
            raise RuntimeError("psycopg is not installed")
        return psycopg.connect(self.database_url, connect_timeout=5)

    def initialize(self) -> None:
        if self._initialized:
            return
        if not self.public_key_path.is_file():
            raise RuntimeError(f"Credential public key is missing: {self.public_key_path}")
        fingerprint = hashlib.sha256(self.public_key_path.read_bytes()).hexdigest()
        if fingerprint != EXPECTED_PUBLIC_KEY_SHA256:
            raise RuntimeError("Credential public key integrity check failed")
        trusted_now = self.clock.now(force=True)
        ensure_application_active(trusted_now)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS modernization_users (
                    username TEXT PRIMARY KEY, password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
                    active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL);
                CREATE TABLE IF NOT EXISTS modernization_login_challenges (
                    challenge_hash TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES modernization_users(username),
                    expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ);
                CREATE TABLE IF NOT EXISTS modernization_sessions (
                    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES modernization_users(username),
                    role TEXT NOT NULL, credential_id TEXT NOT NULL, credential_expires_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL, last_activity_at TIMESTAMPTZ NOT NULL,
                    revoked_at TIMESTAMPTZ);
                CREATE TABLE IF NOT EXISTS modernization_credential_audit (
                    id BIGSERIAL PRIMARY KEY, username TEXT NOT NULL, credential_id TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL, verified_at TIMESTAMPTZ NOT NULL);
                CREATE TABLE IF NOT EXISTS modernization_project_state (
                    project_id TEXT PRIMARY KEY, details JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL);
                CREATE TABLE IF NOT EXISTS modernization_job_state (
                    job_id TEXT PRIMARY KEY, project_id TEXT, job_type TEXT NOT NULL,
                    details JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL);
                CREATE TABLE IF NOT EXISTS modernization_auth_audit (
                    id BIGSERIAL PRIMARY KEY, username TEXT NOT NULL, event TEXT NOT NULL,
                    success BOOLEAN NOT NULL, details JSONB NOT NULL, occurred_at TIMESTAMPTZ NOT NULL);
                CREATE TABLE IF NOT EXISTS modernization_time_authority (
                    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                    trusted_utc TIMESTAMPTZ NOT NULL, sources JSONB NOT NULL, checked_at TIMESTAMPTZ NOT NULL);
            """)
            previous = db.execute(
                "SELECT trusted_utc FROM modernization_time_authority WHERE singleton=TRUE"
            ).fetchone()
            if previous and trusted_now < previous[0] - timedelta(minutes=3):
                raise RuntimeError("Trusted UTC rollback detected; startup denied")
            db.execute(
                "INSERT INTO modernization_time_authority VALUES (TRUE,%s,%s,%s) "
                "ON CONFLICT(singleton) DO UPDATE SET trusted_utc=EXCLUDED.trusted_utc,"
                "sources=EXCLUDED.sources,checked_at=EXCLUDED.checked_at",
                (trusted_now, Jsonb(self.clock.last_sources), trusted_now),
            )
            username = os.getenv("MODERNIZATION_BOOTSTRAP_ADMIN_USER", "admin")
            password = os.getenv("MODERNIZATION_BOOTSTRAP_ADMIN_PASSWORD", "")
            exists = db.execute(
                "SELECT 1 FROM modernization_users WHERE username=%s", (username,)
            ).fetchone()
            if not exists:
                if not password:
                    raise RuntimeError("MODERNIZATION_BOOTSTRAP_ADMIN_PASSWORD is required for first startup")
                salt, digest = hash_password(password)
                db.execute(
                    "INSERT INTO modernization_users VALUES (%s,%s,%s,'admin',TRUE,%s)",
                    (username, salt, digest, trusted_now),
                )
        self._initialized = True

    @staticmethod
    def _audit(db, username: str, event: str, success: bool, now: datetime, details: dict | None = None) -> None:
        db.execute(
            "INSERT INTO modernization_auth_audit(username,event,success,details,occurred_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (username or "unknown", event, success, Jsonb(details or {}), now),
        )

    def begin_login(self, username: str, password: str) -> str:
        self.initialize()
        now = self.clock.now()
        with self._connect() as db:
            row = db.execute(
                "SELECT password_salt,password_hash FROM modernization_users "
                "WHERE username=%s AND active=TRUE", (username,)
            ).fetchone()
            if not row or not verify_password(password, row[0], row[1]):
                self._audit(db, username, "PASSWORD_REJECTED", False, now)
                db.commit()
                raise ValueError("Invalid username or password")
            challenge = secrets.token_urlsafe(48)
            db.execute(
                "INSERT INTO modernization_login_challenges VALUES (%s,%s,%s,NULL)",
                (self._token_hash(challenge), username, now + timedelta(minutes=CHALLENGE_MINUTES)),
            )
            self._audit(db, username, "PASSWORD_VERIFIED", True, now)
        return challenge

    def complete_login(self, challenge: str, credential_content: bytes) -> tuple[str, dict]:
        self.initialize()
        now = self.clock.now()
        challenge_hash = self._token_hash(challenge)
        with self._connect() as db:
            row = db.execute(
                "SELECT c.username,u.role FROM modernization_login_challenges c "
                "JOIN modernization_users u ON u.username=c.username "
                "WHERE c.challenge_hash=%s AND c.used_at IS NULL AND c.expires_at>%s AND u.active=TRUE",
                (challenge_hash, now),
            ).fetchone()
            if not row:
                self._audit(db, "unknown", "CREDENTIAL_REJECTED", False, now, {"reason": "challenge"})
                db.commit()
                raise ValueError("Login challenge is invalid or expired")
            try:
                credential = verify_credential_file(credential_content, self.public_key_path, now)
            except ValueError as exc:
                self._audit(db, row[0], "CREDENTIAL_REJECTED", False, now, {"reason": str(exc)})
                db.commit()
                raise
            db.execute(
                "UPDATE modernization_login_challenges SET used_at=%s WHERE challenge_hash=%s",
                (now, challenge_hash),
            )
            token = secrets.token_urlsafe(64)
            expires_at = datetime.fromisoformat(str(credential["expires_at"]))
            db.execute(
                "INSERT INTO modernization_sessions VALUES (%s,%s,%s,%s,%s,%s,%s,NULL)",
                (self._token_hash(token), row[0], row[1], credential["credential_id"], expires_at, now, now),
            )
            db.execute(
                "INSERT INTO modernization_credential_audit(username,credential_id,expires_at,verified_at) "
                "VALUES (%s,%s,%s,%s)", (row[0], credential["credential_id"], expires_at, now),
            )
            self._audit(db, row[0], "LOGIN_COMPLETED", True, now,
                        {"credential_id": credential["credential_id"], "expires_at": credential["expires_at"]})
        return token, {"sub": row[0], "role": row[1], "apps": ["MODERNIZATION"], "credential": credential}

    def validate_session(self, token: str, *, touch: bool = True) -> dict:
        self.initialize()
        now = self.clock.now()
        if now > APPLICATION_EXPIRES_AT:
            raise ValueError("Modernization authorization expired on 2026-11-30")
        with self._connect() as db:
            row = db.execute(
                "SELECT username,role,credential_id,credential_expires_at,last_activity_at "
                "FROM modernization_sessions WHERE token_hash=%s AND revoked_at IS NULL",
                (self._token_hash(token),),
            ).fetchone()
            if not row or row[3] < now:
                if row:
                    db.execute(
                        "UPDATE modernization_sessions SET revoked_at=%s WHERE token_hash=%s",
                        (now, self._token_hash(token)),
                    )
                    self._audit(db, row[0], "SESSION_CREDENTIAL_EXPIRED", False, now)
                    db.commit()
                raise ValueError("Session credential expired; sign in and upload a valid credential file again")
            if touch:
                db.execute(
                    "UPDATE modernization_sessions SET last_activity_at=%s WHERE token_hash=%s",
                    (now, self._token_hash(token)),
                )
        return {"sub": row[0], "role": row[1], "apps": ["MODERNIZATION"],
                "credential_id": row[2], "credential_expires_at": row[3].isoformat()}

    def logout(self, token: str) -> None:
        self.initialize()
        now = self.clock.now()
        with self._connect() as db:
            row = db.execute(
                "SELECT username FROM modernization_sessions WHERE token_hash=%s", (self._token_hash(token),)
            ).fetchone()
            db.execute(
                "UPDATE modernization_sessions SET revoked_at=%s WHERE token_hash=%s",
                (now, self._token_hash(token)),
            )
            if row:
                self._audit(db, row[0], "LOGOUT", True, now)

    def sync_projects(self, projects: list[dict]) -> None:
        self.initialize()
        now = self.clock.now()
        with self._connect() as db:
            for project in projects:
                db.execute(
                    "INSERT INTO modernization_project_state VALUES (%s,%s,%s) "
                    "ON CONFLICT(project_id) DO UPDATE SET details=EXCLUDED.details,updated_at=EXCLUDED.updated_at",
                    (project["id"], Jsonb(project), now),
                )

    def sync_job(self, job: dict, job_type: str = "modernization") -> None:
        self.initialize()
        with self._connect() as db:
            db.execute(
                "INSERT INTO modernization_job_state VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT(job_id) DO UPDATE SET details=EXCLUDED.details,updated_at=EXCLUDED.updated_at",
                (job["job_id"], job.get("project_id"), job_type, Jsonb(job), self.clock.now()),
            )

    def load_jobs(self, job_type: str = "modernization", max_age_days: int = 7) -> list[dict]:
        """Load recent job checkpoints so job history survives container replacement."""
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                "SELECT details FROM modernization_job_state "
                "WHERE job_type=%s AND updated_at >= now() - (%s * interval '1 day') "
                "ORDER BY updated_at DESC",
                (job_type, max(1, int(max_age_days))),
            ).fetchall()
        return [row[0] for row in rows if isinstance(row[0], dict)]

    def delete_job(self, job_id: str) -> None:
        """Remove a job checkpoint after an explicit job deletion."""
        self.initialize()
        with self._connect() as db:
            db.execute("DELETE FROM modernization_job_state WHERE job_id=%s", (job_id,))

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
