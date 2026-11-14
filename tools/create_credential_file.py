"""Create the signed Modernization credential required at each login."""
from __future__ import annotations

import argparse
import base64
import json
import os
import uuid
from datetime import datetime, UTC
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="Modernization-Credential-2026-11-30.mcred")
    parser.add_argument("--key-dir", default="data/security")
    parser.add_argument("--expires-at", default="2026-11-30T23:59:59Z")
    parser.add_argument("--rotate", action="store_true")
    args = parser.parse_args()
    key_dir = Path(args.key_dir).resolve()
    key_dir.mkdir(parents=True, exist_ok=True)
    private_path = key_dir / "credential_signing_private_key.pem"
    public_path = key_dir / "credential_public_key.pem"
    if public_path.exists() and not private_path.exists() and not args.rotate:
        raise SystemExit(
            "The pinned credential was already issued and its private signing key was removed. "
            "A replacement requires an explicit key-rotation release."
        )
    if private_path.exists():
        private_key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    else:
        private_key = Ed25519PrivateKey.generate()
        private_path.write_bytes(private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    public_path.write_bytes(private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    payload = {
        "application": "Modernization",
        "credential_id": str(uuid.uuid4()),
        "issued_at": datetime.now(UTC).isoformat(),
        "expires_at": args.expires_at,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    envelope = {"payload": payload, "signature": base64.b64encode(private_key.sign(canonical)).decode("ascii")}
    Path(args.output).resolve().write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    try:
        os.chmod(private_path, 0o600)
    except OSError:
        pass
    print(f"Created signed credential: {Path(args.output).resolve()}")
    print(f"Expires: {payload['expires_at']}")


if __name__ == "__main__":
    main()
