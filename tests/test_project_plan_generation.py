import asyncio
import json

from fastapi import Request

from api import server
from services.governance import ProjectStore


def _json_request(payload: dict) -> Request:
    body = json.dumps(payload).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/api/projects/APP-001/plans",
        "raw_path": b"/api/projects/APP-001/plans", "query_string": b"",
        "headers": [], "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 18084),
    }, receive)


def _prompt_project(store: ProjectStore) -> str:
    prompt = "Create one complete Python FastAPI service with a GET /health endpoint."
    configuration = {
        "origin_mode": "prompt", "project_prompt": prompt,
        "engine_target": "python_fastapi", "target_stack": "python_fastapi",
        "target_stack_name": "FastAPI + SQLAlchemy + PostgreSQL",
    }
    project = store.create_prompt_project(
        "Availability service", prompt, "tester", configuration,
    )
    source = project["snapshots"][0]
    store.add_json_snapshot(
        project["id"], "analysis",
        {
            "analysis": {"project_type": "greenfield", "summary": {}},
            "semantic_index": {"hierarchy": {"modules": {}}},
        },
        "tester", {"target_stack": "python_fastapi"}, source["id"],
    )
    return project["id"]


def test_prompt_plan_reads_project_brief_and_retries_reuse_complete_pair(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "projects")
    project_id = _prompt_project(store)
    monkeypatch.setattr(server, "_PROJECT_STORE", store)
    server._PROJECT_PLAN_LOCKS.clear()

    async def run_twice():
        return await asyncio.gather(
            server.create_modernization_plan(
                project_id, _json_request({"target_stack": "python_fastapi"}),
            ),
            server.create_modernization_plan(
                project_id, _json_request({"target_stack": "python_fastapi"}),
            ),
        )

    first, second = asyncio.run(run_twice())
    project = store.get_project(project_id)

    assert sorted((first["reused"], second["reused"])) == [False, True]
    assert second["plan_snapshot"]["id"] == first["plan_snapshot"]["id"]
    assert len([item for item in project["snapshots"] if item["kind"] == "plans"]) == 1
    assert len([item for item in project["snapshots"] if item["kind"] == "contracts"]) == 1
    assert project["status"] == "Plan Generated"


def test_invalid_contracts_leave_no_plan_or_contract_snapshot(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "projects")
    project_id = _prompt_project(store)
    monkeypatch.setattr(server, "_PROJECT_STORE", store)
    monkeypatch.setattr(
        server, "validate_contracts",
        lambda _contracts: {"valid": False, "errors": ["missing governed contract"]},
    )
    server._PROJECT_PLAN_LOCKS.clear()

    async def run():
        try:
            await server.create_modernization_plan(
                project_id, _json_request({"target_stack": "python_fastapi"}),
            )
        except server.HTTPException as exc:
            return exc
        raise AssertionError("Expected contract validation to reject the plan")

    error = asyncio.run(run())
    project = store.get_project(project_id)

    assert error.status_code == 422
    assert "missing governed contract" in error.detail
    assert not [item for item in project["snapshots"] if item["kind"] == "plans"]
    assert not [item for item in project["snapshots"] if item["kind"] == "contracts"]
