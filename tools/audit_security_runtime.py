"""Print non-secret evidence for the standalone security data store."""
import os
from datetime import UTC

import psycopg
from dotenv import load_dotenv

load_dotenv()
with psycopg.connect(os.environ["MODERNIZATION_DATABASE_URL"]) as db:
    names = [
        "modernization_users", "modernization_login_challenges", "modernization_sessions",
        "modernization_credential_audit", "modernization_project_state", "modernization_job_state",
        "modernization_auth_audit", "modernization_time_authority",
    ]
    print({name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in names})
    user = db.execute(
        "SELECT username,role,length(password_salt),length(password_hash) "
        "FROM modernization_users WHERE username=%s", ("admin",),
    ).fetchone()
    revoked = db.execute(
        "SELECT revoked_at IS NOT NULL FROM modernization_sessions WHERE username=%s "
        "ORDER BY created_at DESC LIMIT 1", ("admin",),
    ).fetchone()
    print({
        "admin": user[0], "role": user[1], "salt_present": user[2] > 0,
        "password_hash_present": user[3] > 0, "tested_session_revoked": revoked[0],
    })
    events = db.execute(
        "SELECT event,success FROM modernization_auth_audit WHERE username=%s ORDER BY id DESC LIMIT 4",
        ("admin",),
    ).fetchall()
    authority = db.execute(
        "SELECT trusted_utc,sources FROM modernization_time_authority WHERE singleton=TRUE"
    ).fetchone()
    print({"recent_admin_events": events, "trusted_utc": authority[0].astimezone(UTC).isoformat(), "time_sources": authority[1]})
