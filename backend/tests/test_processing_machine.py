import asyncio
import time
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.neighbor_index import REVISION
from backend.app.processing_machine import OFFLINE, ProcessingMachine, WorkerPoll, WorkerResult
from backend.tests.test_workflow import project, run

TOKEN = "test-worker-credential-32-characters-minimum"


def heartbeat(worker_id=None, ready=True):
    return WorkerPoll(worker_id=worker_id or uuid4(), ready=ready, index_revision=REVISION)


def evidence(job, worker, **overrides):
    return WorkerResult.model_validate(
        {
            **job,
            "worker_id": worker,
            "coverage": "available",
            "neighbors": [{"post_id": 998, "rank": 2, "distance": 0.25}],
            **overrides,
        }
    )


async def test_offline_heartbeat_lookup_and_retained_evidence():
    machine = ProcessingMachine(TOKEN)
    assert machine.error == OFFLINE
    with pytest.raises(HTTPException) as offline:
        await machine.lookup(999)
    assert offline.value.status_code == 503
    worker = heartbeat()
    assert machine.poll(worker) == {"job": None}
    assert machine.error is None
    first = asyncio.create_task(machine.lookup(999))
    second = asyncio.create_task(machine.lookup(999))
    await asyncio.sleep(0)
    job = machine.poll(worker)["job"]
    assert len(machine.requests) == 1
    assert machine.poll(worker)["job"] is None  # A lease is claimed once.
    machine.complete(evidence(job, worker.worker_id))
    one, two = await asyncio.gather(first, second)
    assert one == two and one["neighbors"][0]["rank"] == 2
    machine.seen = time.monotonic() - 16
    assert machine.error == OFFLINE
    assert await machine.lookup(999) == one  # Existing results can still be reviewed.
    with pytest.raises(HTTPException):
        await machine.lookup(1000)
    machine.poll(heartbeat(ready=False))
    assert "index unavailable" in machine.error
    await machine.close()


async def test_cancellation_stale_lease_timeout_and_worker_ownership():
    machine = ProcessingMachine(TOKEN)
    worker = heartbeat()
    machine.poll(worker)
    with pytest.raises(HTTPException, match="409"):
        machine.poll(heartbeat())
    first = asyncio.create_task(machine.lookup(999))
    second = asyncio.create_task(machine.lookup(999))
    await asyncio.sleep(0)
    job = machine.poll(worker)["job"]
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert len(machine.requests) == 1  # Cancelling one consumer preserves another.
    second.cancel()
    await asyncio.gather(second, return_exceptions=True)
    assert not machine.requests
    with pytest.raises(HTTPException, match="409"):
        machine.complete(evidence(job, worker.worker_id))
    machine.lookup_timeout = 0.01
    with pytest.raises(HTTPException, match="503"):
        await machine.lookup(1001)
    assert not machine.requests
    await machine.close()


async def test_results_are_bounded_and_validated_before_trusting_membership():
    machine = ProcessingMachine(TOKEN)
    worker = heartbeat()
    machine.poll(worker)
    task = asyncio.create_task(machine.lookup(999))
    await asyncio.sleep(0)
    job = machine.poll(worker)["job"]
    for overrides in [
        {"lease": uuid4()},
        {"worker_id": uuid4()},
        {"post_id": 1000},
        {"id": uuid4()},
        {"neighbors": [{"post_id": 999, "rank": 1, "distance": 0}]},
        {"coverage": "not_indexed"},
        {"neighbors": [{"post_id": 998, "rank": 2, "distance": 0.25}] * 2},
    ]:
        with pytest.raises(HTTPException):
            machine.complete(evidence(job, worker.worker_id, **overrides))
    for neighbors in [
        [{"post_id": 998, "rank": 2, "distance": float("nan")}],
        [{"post_id": 998, "rank": 2, "distance": 1000000}],
        [{"post_id": 998, "rank": 65, "distance": 0.25}],
        [{"post_id": 998, "rank": 2, "distance": 0.25}] * 65,
    ]:
        with pytest.raises(ValidationError):
            evidence(job, worker.worker_id, neighbors=neighbors)
    assert not task.done()
    machine.complete(evidence(job, worker.worker_id, coverage="not_indexed", neighbors=[]))
    assert (await task)["coverage"] == "not_indexed"
    await machine.close()


def test_worker_scoped_auth_offline_ui_and_review_integration(client):
    state = client.app.state
    state.settings.similarity_worker_token = TOKEN
    state.processing_machine.token = TOKEN
    state.similarity.index = state.processing_machine
    pid = project(client)[0]["id"]
    base = f"/api/projects/{pid}/similar"
    listing = client.get(base).json()
    assert not listing["available"] and listing["message"] == OFFLINE
    body = heartbeat().model_dump(mode="json")
    worker_path = "/api/processing-machine/poll"
    assert client.post(worker_path, json=body).status_code == 401  # Browser session has no worker rights.
    headers = {"Authorization": "Bearer " + TOKEN}
    assert client.post(worker_path, json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post(worker_path, json=body, headers=headers).status_code == 200
    assert client.get(base).json()["available"]
    response = client.post(base, json={"post_id": 999, "request_id": str(uuid4())})
    assert response.status_code == 200
    search = response.json()["id"]
    job = None
    for _ in range(30):
        job = client.post(worker_path, json=body, headers=headers).json()["job"]
        if job:
            break
        time.sleep(0.01)
    assert job
    result = evidence(job, body["worker_id"]).model_dump(mode="json")
    assert client.post("/api/processing-machine/results", json=result, headers=headers).status_code == 204

    async def hydrated():
        await asyncio.gather(*list(state.similarity.tasks.values()))

    run(client, hydrated())
    detail = client.get(base + "/" + search).json()
    assert detail["status"] == "done"
    state.processing_machine.seen = time.monotonic() - 16
    # Retained searches outlive a fixed bridge-cache TTL (and cache eviction).
    state.processing_machine.cache.clear()
    assert client.get(base).json()["message"] == OFFLINE
    rejected = client.post(base, json={"post_id": 1000, "request_id": str(uuid4())})
    assert rejected.status_code == 503 and rejected.json()["detail"] == OFFLINE
    entry = detail["results"][0]
    review = client.patch(
        f"/api/projects/{pid}/labels/998",
        json={
            "request_id": str(uuid4()),
            "state": "negative",
            "expected_version": 0,
            "similarity": entry["candidate"]["similarity"],
        },
    )
    assert review.status_code == 200, review.text
    saved = run(client, state.db.one("SELECT * FROM similarity_provenance"))
    assert saved["seed_post_id"] == 999 and saved["l2_distance"] == 0.25
    # The worker token grants no browser/API session.
    client.cookies.clear()
    assert client.get("/api/projects", headers=headers).status_code == 401
