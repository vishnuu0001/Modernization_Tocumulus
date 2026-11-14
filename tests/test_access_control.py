import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services.access_control import (
    EXPECTED_PUBLIC_KEY_SHA256,
    AccessStore,
    ensure_application_active,
    hash_password,
    verify_credential_file,
    verify_password,
)


def _credential(tmp_path, expires_at="2026-11-30T23:59:59Z"):
    private_key = Ed25519PrivateKey.generate()
    public_path = tmp_path / "public.pem"
    public_path.write_bytes(private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    payload = {"application": "Modernization", "credential_id": "test-id", "expires_at": expires_at}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    content = json.dumps({
        "payload": payload,
        "signature": base64.b64encode(private_key.sign(canonical)).decode(),
    }).encode()
    return content, public_path


def test_password_is_salted_and_not_stored_as_plaintext():
    salt, digest = hash_password("Admin@1234")
    assert "Admin@1234" not in salt + digest
    assert verify_password("Admin@1234", salt, digest)
    assert not verify_password("wrong", salt, digest)


def test_signed_credential_accepts_valid_file(tmp_path):
    content, public_path = _credential(tmp_path)
    payload = verify_credential_file(content, public_path, datetime(2026, 11, 30, 12, tzinfo=timezone.utc))
    assert payload["credential_id"] == "test-id"


def test_signed_credential_rejects_tampering(tmp_path):
    content, public_path = _credential(tmp_path)
    envelope = json.loads(content)
    envelope["payload"]["expires_at"] = "2036-11-30T23:59:59Z"
    with pytest.raises(ValueError, match="modified"):
        verify_credential_file(json.dumps(envelope).encode(), public_path)


def test_signed_credential_rejects_expiry(tmp_path):
    content, public_path = _credential(tmp_path, "2026-11-30T23:59:59Z")
    with pytest.raises(ValueError, match="expired"):
        verify_credential_file(content, public_path, datetime(2026, 12, 1, tzinfo=timezone.utc))


def test_application_can_start_through_end_of_30_november_2026():
    ensure_application_active(datetime(2026, 11, 30, 23, 59, 59, tzinfo=timezone.utc))


def test_application_start_is_denied_beyond_30_november_2026():
    with pytest.raises(RuntimeError, match="startup denied"):
        ensure_application_active(datetime(2026, 12, 1, tzinfo=timezone.utc))


def test_issued_november_30_credential_matches_pinned_key_and_boundary():
    root = Path(__file__).resolve().parents[1]
    public_path = root / "data" / "security" / "credential_public_key.pem"
    credential_path = root / "Modernization-Credential-2026-11-30.mcred"

    assert hashlib.sha256(public_path.read_bytes()).hexdigest() == EXPECTED_PUBLIC_KEY_SHA256
    payload = verify_credential_file(
        credential_path.read_bytes(), public_path,
        datetime(2026, 11, 30, 23, 59, 59, tzinfo=timezone.utc),
    )
    assert payload["expires_at"] == "2026-11-30T23:59:59Z"
    with pytest.raises(ValueError, match="expired"):
        verify_credential_file(
            credential_path.read_bytes(), public_path,
            datetime(2026, 12, 1, tzinfo=timezone.utc),
        )


def test_session_does_not_expire_due_to_inactivity():
    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    executed = []

    class Result:
        def fetchone(self):
            return (
                "admin", "admin", "credential-id",
                now + timedelta(days=7), now - timedelta(days=30),
            )

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=()):
            executed.append((query, params))
            return Result()

    store = AccessStore()
    store.initialize = lambda: None
    store.clock.now = lambda *args, **kwargs: now
    store._connect = lambda: Database()

    session = store.validate_session("valid-token", touch=False)

    assert session["sub"] == "admin"
    assert not any(
        query.lstrip().startswith("UPDATE modernization_sessions SET revoked_at")
        for query, _params in executed
    )
