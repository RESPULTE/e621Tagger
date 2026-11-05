import asyncio
import json
import time
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.image_jobs import OFFLINE
from backend.app.jobs import enqueue
from backend.app.processing_machine import ProcessingMachine, WorkerPoll
from backend.tests.test_image_retrieval import TOKEN, heartbeat, image_bytes, result
from backend.tests.test_workflow import project, run


async def test_only_live_image_capability_enables_search_and_queue_is_bounded():
    machine = ProcessingMachine(TOKEN)
    assert machine.error == OFFLINE
    worker = heartbeat()
    worker.image_capability = None
    machine.poll(worker)
    assert "not ready" in machine.error
    with pytest.raises(HTTPException) as unavailable:
        await machine.search_image({"query_type": "upload", "query_sha256": "a" * 64}, raw=image_bytes())
    assert unavailable.value.status_code == 503
    with pytest.raises(ValidationError):
        WorkerPoll(worker_id=uuid4(), ready=True, index_revision="old")
    ready = heartbeat()
    machine.seen = 0
    machine.poll(ready)
    tasks = [asyncio.create_task(machine.search_image(
        {"query_type": "upload", "query_sha256": "a" * 64}, raw=image_bytes())) for _ in range(32)]
    await asyncio.sleep(0)
    with pytest.raises(HTTPException) as full:
        await machine.search_image({"query_type": "upload", "query_sha256": "a" * 64}, raw=image_bytes())
    assert full.value.status_code == 429
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert not machine.images.requests
    await machine.close()


def test_worker_scoped_auth_offline_retained_image_evidence_and_no_historical_api(client):
    state = client.app.state
    state.settings.similarity_worker_token = state.processing_machine.token = TOKEN
    state.similarity.index = state.processing_machine
    pid = project(client)[0]["id"]
    base = f"/api/projects/{pid}/similar"
    assert client.get(base).json() == {"items": [], "available": False, "message": OFFLINE, "corpus_size": None}
    request = {"post_id": 999, "request_id": str(uuid4())}
    assert client.post(base, json=request).status_code == 503
    headers = {"Authorization": "Bearer " + TOKEN}
    worker = heartbeat()
    body = worker.model_dump(mode="json")
    poll = "/api/processing-machine/poll"
    assert client.post(poll, json=body).status_code == 401
    assert client.post(poll, json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    old = {"worker_id": str(worker.worker_id), "ready": True, "index_revision": "old"}
    assert client.post(poll, json=old, headers=headers).status_code == 422
    assert not client.get(base).json()["available"]
    assert client.post(poll, json=body, headers=headers).status_code == 200
    assert client.get(base).json()["available"]
    for mode in ("auto", "neighbors", "image"):
        assert client.post(base, json={**request, "mode": mode}).status_code == 422
    queued = client.post(base, json=request)
    assert queued.status_code == 200
    for _ in range(30):
        job = client.post(poll, json=body, headers=headers).json()["job"]
        if job:
            break
        time.sleep(0.01)
    from backend.app.mock import post

    response = result(job, worker.worker_id, neighbors=[{
        "post_id": 998, "md5": post(998)["file"]["md5"], "rank": 1, "distance": 0.25,
    }]).model_dump(mode="json")
    assert client.post("/api/processing-machine/results", json=response, headers=headers).status_code == 204

    async def finish():
        await asyncio.gather(*list(state.similarity.tasks.values()))
    run(client, finish())
    search = queued.json()["id"]
    detail = client.get(base + "/" + search).json()
    assert detail["status"] == "done" and detail["query_type"] == "post"
    state.processing_machine.seen = time.monotonic() - 16
    assert client.get(base).json()["message"] == OFFLINE
    assert client.post(base, json={**request, "request_id": str(uuid4())}).status_code == 503
    assert client.post(base + "/" + search + "/retry", json={"request_id": str(uuid4())}).status_code == 503
    entry = detail["results"][0]
    review = client.patch(f"/api/projects/{pid}/labels/998", json={
        "request_id": str(uuid4()), "state": "negative", "expected_version": 0,
        "similarity": entry["candidate"]["similarity"],
    })
    assert review.status_code == 200, review.text
    saved = run(client, state.db.one("SELECT * FROM retrieval_provenance"))
    assert saved["source_post_id"] == 999 and saved["distance"] == 0.25
    assert not run(client, state.db.rows("SELECT * FROM similarity_provenance"))
    for path in ("/api/embeddings/status", f"/api/projects/{pid}/neighbors/999"):
        assert client.get(path).status_code == 404
    assert client.post(f"/api/projects/{pid}/neighbors/999", json={"request_id": str(uuid4())}).status_code in {404, 405}
    assert not any("/neighbors/" in path or path.startswith("/api/embeddings") for path in client.app.openapi()["paths"])
    client.cookies.clear()
    assert client.get("/api/projects", headers=headers).status_code == 401


async def test_image_neighbor_bounds_and_heartbeat_ownership():
    machine = ProcessingMachine(TOKEN)
    worker = heartbeat()
    machine.poll(worker)
    with pytest.raises(HTTPException) as occupied:
        machine.poll(heartbeat())
    assert occupied.value.status_code == 409
    task = asyncio.create_task(machine.search_image({"query_type": "upload", "query_sha256": "a" * 64}, raw=image_bytes()))
    await asyncio.sleep(0)
    job = machine.poll(worker)["job"]
    for neighbors in ([{"post_id": 998, "md5": "e" * 32, "rank": 1, "distance": value}]
                      for value in (float("nan"), float("inf"), 100)):
        with pytest.raises(ValidationError):
            result(job, worker.worker_id, neighbors=neighbors)
    for neighbors in ([{"post_id": 998, "md5": "e" * 32, "rank": 1, "distance": 0.1}] * 65,
                      [{"post_id": 998, "md5": "e" * 32, "rank": 65, "distance": 0.1}]):
        with pytest.raises(ValidationError):
            result(job, worker.worker_id, neighbors=neighbors)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await machine.close()


def test_retired_neighbor_jobs_cancel_without_deleting_completed_evidence(client):
    state = client.app.state

    async def prepare():
        async with state.db.transaction() as conn:
            ids = [await enqueue(conn, "neighbors", "legacy:" + status, {"seed_id": 999})
                   for status in ("pending", "running", "done", "failed")]
            for job_id, status in zip(ids, ("pending", "running", "done", "failed")):
                await conn.execute("UPDATE jobs SET status=?,result_json=? WHERE id=?",
                                   (status, json.dumps({"legacy": status}), job_id))
        await state.workers.start(1)
        await state.workers.close()
        return ids

    ids = run(client, prepare())
    rows = run(client, state.db.rows("SELECT * FROM jobs ORDER BY id"))
    assert [r["id"] for r in rows] == ids
    assert [r["status"] for r in rows] == ["cancelled", "cancelled", "done", "failed"]
    assert [json.loads(r["result_json"])["legacy"] for r in rows] == ["pending", "running", "done", "failed"]
    assert all("retired" in r["error"] for r in rows[:2])
