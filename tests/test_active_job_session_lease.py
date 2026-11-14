import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from api import server


class LogoutRequest:
    def __init__(self, body):
        self.headers = {"Authorization": "Bearer valid-session"}
        self._body = body

    async def json(self):
        return self._body


def test_idle_logout_is_deferred_while_generation_job_is_running():
    original = dict(server._JOBS)
    server._JOBS.clear()
    server._JOBS["job-running"] = {"job_id": "job-running", "status": "running"}
    try:
        with patch.object(server._ACCESS_STORE, "logout") as revoke:
            response = asyncio.run(server.logout(LogoutRequest({"force": False})))
        assert response == {
            "logged_out": False,
            "deferred": True,
            "reason": "active_jobs",
            "active_job_count": 1,
        }
        revoke.assert_not_called()
    finally:
        server._JOBS.clear()
        server._JOBS.update(original)


def test_explicit_sign_out_revokes_session_even_while_job_is_running():
    original = dict(server._JOBS)
    server._JOBS.clear()
    server._JOBS["job-running"] = {"job_id": "job-running", "status": "running"}
    try:
        with patch.object(server._ACCESS_STORE, "logout") as revoke:
            response = asyncio.run(server.logout(LogoutRequest({"force": True})))
        assert response["logged_out"] is True
        revoke.assert_called_once_with("valid-session")
    finally:
        server._JOBS.clear()
        server._JOBS.update(original)


def test_completed_jobs_do_not_defer_idle_logout():
    original = dict(server._JOBS)
    server._JOBS.clear()
    server._JOBS["job-complete"] = {"job_id": "job-complete", "status": "completed"}
    try:
        with patch.object(server._ACCESS_STORE, "logout") as revoke:
            response = asyncio.run(server.logout(LogoutRequest({"force": False})))
        assert response["logged_out"] is True
        revoke.assert_called_once_with("valid-session")
    finally:
        server._JOBS.clear()
        server._JOBS.update(original)
