from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from api import server


def _admin_request():
    return SimpleNamespace(
        state=SimpleNamespace(auth={"sub": "admin@example.test", "role": "admin"}),
        headers={},
    )


@pytest.mark.parametrize("status", ["completed", "validation_failed", "failed", "cancelled"])
def test_terminal_modernization_job_does_not_block_project_deletion(status):
    project_id = f"terminal-project-{status}"
    job_id = f"terminal-job-{status}"
    server._JOBS[job_id] = {
        "job_id": job_id,
        "project_id": project_id,
        "status": status,
    }
    try:
        with patch.object(server, "_project_or_404", return_value={"id": project_id}), patch.object(
            server._PROJECT_STORE,
            "delete_project",
            return_value={"deleted": project_id},
        ) as delete_project:
            result = server._delete_project_action(project_id, _admin_request())

        assert result == {"deleted": project_id}
        delete_project.assert_called_once_with(project_id, "admin@example.test")
        assert job_id not in server._JOBS
    finally:
        server._JOBS.pop(job_id, None)
        server._JOB_QUEUES.pop(job_id, None)


@pytest.mark.parametrize("status", ["queued", "pending", "running"])
def test_active_modernization_job_still_blocks_project_deletion(status):
    project_id = f"active-project-{status}"
    job_id = f"active-job-{status}"
    server._JOBS[job_id] = {
        "job_id": job_id,
        "project_id": project_id,
        "status": status,
    }
    try:
        with patch.object(server, "_project_or_404", return_value={"id": project_id}), pytest.raises(
            HTTPException,
        ) as caught:
            server._delete_project_action(project_id, _admin_request())

        assert caught.value.status_code == 409
        assert job_id in str(caught.value.detail)
    finally:
        server._JOBS.pop(job_id, None)
        server._JOB_QUEUES.pop(job_id, None)
