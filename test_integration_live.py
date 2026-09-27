"""Black-box integration test against a running Agent Relay instance.

Unlike ``test_agent_relay.py`` (in-process ``TestClient`` against a scratch
database), this test speaks real HTTP to whatever server is listening at
``RELAY_BASE_URL`` and exercises whatever database that server is configured
with. It requires a server to already be running (``uv run uvicorn main:app``
or ``docker compose up``) and skips itself if none answers, so it never
blocks a normal ``pytest`` run in CI.
"""

from __future__ import annotations

import os
import uuid
from typing import Generator

import httpx
import pytest

BASE_URL = os.getenv("RELAY_BASE_URL", "http://127.0.0.1:8000")


@pytest.fixture(scope="module")
def client() -> Generator[httpx.Client, None, None]:
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        try:
            response = client.get("/health")
        except httpx.HTTPError:
            pytest.skip(f"no live Agent Relay server at {BASE_URL}")
        if response.status_code != 200:
            pytest.skip(f"live server at {BASE_URL} is not healthy")
        yield client


def register(client: httpx.Client, name: str) -> tuple[dict, dict[str, str]]:
    response = client.post("/api/v1/agents", json={"name": name})
    assert response.status_code == 201, response.text
    data = response.json()
    return data, {"Authorization": f"Bearer {data['token']}"}


def test_two_agents_exchange_a_task_and_its_result(client: httpx.Client):
    # Unique names/keys per run: this hits the server's real, persistent
    # database, so nothing here assumes an empty table.
    run_id = uuid.uuid4().hex[:8]
    sender, sender_headers = register(client, f"sender-{run_id}")
    recipient, recipient_headers = register(client, f"uppercase-{run_id}")

    sent = client.post(
        "/api/v1/tasks",
        headers={**sender_headers, "Idempotency-Key": f"live-{run_id}"},
        json={"to": recipient["agent_id"], "input": "hello relay"},
    )
    assert sent.status_code == 201, sent.text
    task_id = sent.json()["task_id"]
    assert sent.json()["status"] == "queued"

    claim = client.post(
        "/api/v1/tasks/claim",
        headers=recipient_headers,
        json={"worker_id": f"live-worker-{run_id}", "wait_seconds": 5},
    )
    assert claim.status_code == 200, claim.text
    claim_data = claim.json()
    assert claim_data["task_id"] == task_id
    assert claim_data["from"] == sender["agent_id"]
    assert claim_data["input"] == "hello relay"

    complete = client.post(
        f"/api/v1/tasks/{task_id}/complete",
        headers=recipient_headers,
        json={"claim_token": claim_data["claim_token"], "output": claim_data["input"].upper()},
    )
    assert complete.status_code == 200, complete.text
    assert complete.json() == {"task_id": task_id, "status": "completed"}

    result = client.get(f"/api/v1/tasks/{task_id}", headers=sender_headers)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "completed"
    assert body["output"] == "HELLO RELAY"
    assert body["error"] is None
    assert body["from"] == sender["agent_id"]
    assert body["to"] == recipient["agent_id"]
