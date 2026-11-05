import asyncio
import hashlib
import io
import json
import time
from uuid import UUID, uuid4

import httpx
import numpy as np
import pytest
from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError

from backend.app.datasets import snapshot
from backend.app.image_jobs import ImageResult
from backend.app.processing_machine import ProcessingMachine, WorkerPoll
from backend.app.reference_images import validate_image
from backend.image_retrieval_worker import ImageEngine
from backend.tests.test_workflow import project, run
from backend.vector_index import VectorIndex, unit_vector

SPACE, MODEL, INDEX, TOKEN = "a" * 64, "b" * 64, "c" * 64, "x" * 32


def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (24, 20), "blue").save(output, "PNG")
    return output.getvalue()


def heartbeat(**kwargs):
    return WorkerPoll(worker_id=uuid4(), protocol_version=2,
                      image_capability={"model_revision": MODEL, "embedding_space": SPACE, "index_revision": INDEX}, **kwargs)


def result(job, worker, **kwargs):
    return ImageResult(**{**job, "worker_id": worker, "coverage": "available", "index_revision": INDEX,
        "query_sha256": job["query_sha256"] or "d" * 64,
        "neighbors": [{"post_id": 998, "md5": "e" * 32, "rank": 1, "distance": 0.25}], **kwargs})


async def test_image_leases_checksums_revision_replacement_timeout_and_cancellation():
    machine = ProcessingMachine(TOKEN)
    worker = heartbeat()
    machine.poll(worker)
    raw = image_bytes()
    query = {"query_type": "upload", "query_sha256": hashlib.sha256(raw).hexdigest()}
    task = asyncio.create_task(machine.search_image(query, raw=raw))
    await asyncio.sleep(0)
    job = machine.poll(worker)["job"]
    assert job["kind"] == "image_search" and job["post_id"] is None
    assert await machine.images.image(UUID(job["id"]), UUID(job["lease"]), worker.worker_id) == (raw, query["query_sha256"])
    for overrides in ({"lease": uuid4()}, {"worker_id": uuid4()}, {"query_sha256": "f" * 64},
                      {"embedding_space": "f" * 64}, {"model_revision": "f" * 64}, {"corpus_size": 0},
                      {"neighbors": [{"post_id": 998, "md5": "e" * 32, "rank": 1, "distance": 0.25}] * 2}):
        with pytest.raises(HTTPException):
            machine.images.complete(result(job, worker.worker_id, **overrides))
    machine.images.complete(result(job, worker.worker_id))
    evidence = await task
    assert evidence["query_type"] == "upload" and evidence["source_post_id"] is None
    with pytest.raises(HTTPException):
        machine.images.complete(result(job, worker.worker_id))
    task = asyncio.create_task(machine.search_image(query, raw=raw))
    await asyncio.sleep(0)
    job = machine.poll(worker)["job"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not machine.images.requests
    with pytest.raises(HTTPException):
        await machine.images.image(UUID(job["id"]), UUID(job["lease"]), worker.worker_id)
    machine.images.timeout = 0.01
    with pytest.raises(HTTPException, match="503"):
        await machine.search_image(query, raw=raw)
    assert not machine.images.requests
    await machine.close()


async def test_image_busy_heartbeats_and_stale_worker_cannot_complete():
    machine = ProcessingMachine(TOKEN)
    worker = heartbeat()
    machine.poll(worker)
    query = {"query_type": "upload", "query_sha256": "a" * 64}
    task = asyncio.create_task(machine.search_image(query, raw=image_bytes()))
    await asyncio.sleep(0)
    worker.busy = True
    assert machine.poll(worker)["job"] is None and machine.error is None
    worker.busy = False
    job = machine.poll(worker)["job"]
    machine.seen = time.monotonic() - 16
    replacement = heartbeat()
    machine.poll(replacement)
    with pytest.raises(HTTPException, match="409"):
        machine.images.complete(result(job, worker.worker_id))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await machine.close()


def test_upload_validation_and_transient_binary_review_provenance(client):
    pid = project(client)[0]["id"]
    base = f"/api/projects/{pid}/similar"
    for raw in (b"not an image", b"<svg></svg>"):
        assert client.post(base + f"/reference?request_id={uuid4()}", content=raw).status_code == 415
    settings = client.app.state.settings
    settings.reference_max_bytes = 1024
    assert client.post(base + f"/reference?request_id={uuid4()}", content=b"x" * 1025).status_code == 413
    settings.reference_max_pixels = 100
    assert client.post(base + f"/reference?request_id={uuid4()}", content=image_bytes()).status_code == 413
    settings.reference_max_pixels = 20_000_000
    raw = image_bytes()
    request_id = uuid4()
    response = client.post(base + f"/reference?request_id={request_id}", content=raw)
    assert response.status_code == 200, response.text
    search = response.json()["id"]
    assert client.post(base + f"/reference?request_id={request_id}", content=raw).json()["id"] == search

    async def finish():
        await asyncio.gather(*list(client.app.state.similarity.tasks.values()))
    run(client, finish())
    detail = client.get(base + "/" + search).json()
    assert detail["query_type"] == "upload" and detail["source_post_id"] is None
    assert detail["query_sha256"] == hashlib.sha256(raw).hexdigest()
    assert detail["status"] == "done" and detail["results"]
    state = client.app.state
    for table in ("posts", "labels", "label_events", "retrieval_provenance", "jobs"):
        assert not run(client, state.db.rows(f"SELECT * FROM {table}"))
    entry = detail["results"][0]
    context = entry["candidate"]["similarity"]
    path = f"/api/projects/{pid}/labels/{entry['post_id']}"
    body = {"state": "positive", "request_id": str(uuid4()), "expected_version": 0, "similarity": context}
    saved = client.patch(path, json=body)
    assert saved.status_code == 200, saved.text
    evidence = run(client, state.db.one("SELECT * FROM retrieval_provenance"))
    assert evidence["source_post_id"] is None and evidence["query_sha256"] == hashlib.sha256(raw).hexdigest()
    _, rows = run(client, snapshot(state.db.connection, pid))
    assert any(p["kind"] == "hydra_image" for p in json.loads(rows[0]["provenance_json"]))
    changed = client.patch(path, json={"state": "negative", "request_id": str(uuid4()), "expected_version": 1}).json()
    undo = client.post(path + "/undo", json={"request_id": str(uuid4()), "expected_version": 2, "event_id": changed["event_id"]})
    assert undo.json()["state"] == "positive"
    _, rows = run(client, snapshot(state.db.connection, pid))
    assert any(p["kind"] == "hydra_image" for p in json.loads(rows[0]["provenance_json"]))
    forged = {**body, "request_id": str(uuid4()), "expected_version": 3,
              "similarity": {**context, "search_id": str(uuid4())}}
    assert client.patch(path, json=forged).status_code == 410
    run(client, state.similarity.close())
    assert client.patch(path, json=body).json() == saved.json()  # durable receipt, offline/expired search


def test_image_review_rejects_changed_indexed_candidate_identity_before_writes(client, monkeypatch):
    pid = project(client)[0]["id"]
    queued = client.post(f"/api/projects/{pid}/similar", json={
        "request_id": str(uuid4()), "post_id": 1001, "limit": 1,
    })
    assert queued.status_code == 200, queued.text
    search_id = queued.json()["id"]
    state = client.app.state

    async def finish():
        await asyncio.gather(*list(state.similarity.tasks.values()))

    run(client, finish())
    detail = client.get(f"/api/projects/{pid}/similar/{search_id}").json()
    assert detail["status"] == "done" and len(detail["results"]) == 1
    entry = detail["results"][0]
    post_id = entry["post_id"]
    indexed = next(n for n in state.similarity.searches[search_id]["evidence"]["neighbors"] if n["post_id"] == post_id)
    original_raw = state.feed.raw
    current = run(client, original_raw(post_id, fresh=True))
    assert indexed["md5"] == current["file"]["md5"]
    changed_md5 = "0" * 32 if indexed["md5"] != "0" * 32 else "1" * 32

    async def changed_source_identity(candidate_id, *, fresh=False, background=False):
        source = await original_raw(candidate_id, fresh=fresh, background=background)
        if candidate_id == post_id and fresh:
            return {**source, "file": {**source["file"], "md5": changed_md5}}
        return source

    monkeypatch.setattr(state.feed, "raw", changed_source_identity)
    tables = ("posts", "project_candidates", "labels", "label_events", "similarity_provenance",
              "retrieval_provenance", "mutation_requests")
    before = {table: run(client, state.db.rows(f"SELECT * FROM {table}")) for table in tables}
    body = {
        "request_id": str(uuid4()), "state": "positive", "expected_version": 0,
        "similarity": entry["candidate"]["similarity"],
    }

    response = client.patch(f"/api/projects/{pid}/labels/{post_id}", json=body)
    assert response.status_code == 409, response.text
    assert {table: run(client, state.db.rows(f"SELECT * FROM {table}")) for table in tables} == before


def test_exact_index_matches_numpy_deduplicates_restarts_and_isolates_spaces(tmp_path):
    rng = np.random.default_rng(7)
    vectors = rng.normal(size=(1030, 16)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    path = tmp_path / "index.sqlite3"
    index = VectorIndex(path, space=SPACE, model_revision=MODEL, dimension=16)
    for n, vector in enumerate(vectors, 1):
        index.upsert(n, f"{n:032x}", f"{n:064x}", vector)
    revision = index.revision
    assert not index.upsert(1, f"{1:032x}", f"{1:064x}", vectors[0])
    assert index.revision == revision
    query = unit_vector(rng.normal(size=16), 16)
    truth = np.argsort(np.linalg.norm(vectors - query, axis=1), kind="stable")[:32] + 1
    found = index.search(query, limit=32)
    assert [r["post_id"] for r in found["neighbors"]] == truth.tolist()
    assert found["revision"] == revision
    assert found["corpus_size"] == 1030
    index.upsert(2000, f"{1:032x}", f"{1:064x}", vectors[0])
    exact = index.search(vectors[0], limit=64)
    assert exact["neighbors"][0]["post_id"] == 1
    assert 2000 not in [r["post_id"] for r in exact["neighbors"]]
    assert all(r["md5"] != f"{1:032x}" for r in index.search(vectors[0], exclude_sha256=f"{1:064x}")["neighbors"])
    index.upsert(1, "f" * 32, "f" * 64, vectors[5])
    assert index.get(1, f"{1:032x}") is None  # changed source replaces current entry atomically
    index.close()
    reopened = VectorIndex(path, space=SPACE, model_revision=MODEL, dimension=16)
    assert reopened.get(1, "f" * 32) is not None
    reopened.close()
    with pytest.raises(ValueError, match="mismatch"):
        VectorIndex(path, space="f" * 64, model_revision=MODEL, dimension=16)
    with pytest.raises(ValueError, match="mismatch"):
        VectorIndex(path, space=SPACE, model_revision="f" * 64, dimension=16)


def test_index_invalid_vectors_capacity_and_failed_transaction_leave_valid_corpus(tmp_path):
    index = VectorIndex(tmp_path / "index.sqlite3", space=SPACE, model_revision=MODEL, dimension=3, maximum=1)
    for vector in ([0, 0, 0], [1, 2], [1, np.nan, 2]):
        with pytest.raises(ValueError):
            index.upsert(1, "a" * 32, "b" * 64, vector)
    index.upsert(1, "a" * 32, "b" * 64, [1, 0, 0])
    before = index.revision
    with pytest.raises(ValueError, match="full"):
        index.upsert(2, "b" * 32, "c" * 64, [0, 1, 0])
    assert index.revision == before and len(index.search([1, 0, 0])["neighbors"]) == 1
    index.close()


def test_image_http_transfer_auth_discriminator_and_stale_lease(client):
    from backend.app.mock import post

    state = client.app.state
    state.settings.similarity_worker_token = TOKEN
    state.processing_machine.token = TOKEN
    state.similarity.index = state.processing_machine
    worker = heartbeat()
    poll = "/api/processing-machine/poll"
    headers = {"Authorization": "Bearer " + TOKEN}
    body = worker.model_dump(mode="json")
    assert client.post(poll, json=body, headers=headers).status_code == 200
    pid = project(client)[0]["id"]
    raw = image_bytes()
    queued = client.post(f"/api/projects/{pid}/similar/reference?request_id={uuid4()}", content=raw)
    assert queued.status_code == 200
    job = None
    for _ in range(30):
        job = client.post(poll, json=body, headers=headers).json()["job"]
        if job:
            break
        time.sleep(0.01)
    assert job and job["kind"] == "image_search"
    path = f"/api/processing-machine/jobs/{job['id']}/image"
    params = {"lease": job["lease"], "worker_id": str(worker.worker_id)}
    assert client.get(path, params=params).status_code == 401
    assert client.get(path, params=params, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(path, params={**params, "lease": str(uuid4())}, headers=headers).status_code == 409
    assert client.get(path, params={**params, "worker_id": str(uuid4())}, headers=headers).status_code == 409
    transfer = client.get(path, params=params, headers=headers)
    assert transfer.content == raw and transfer.headers["x-image-sha256"] == hashlib.sha256(raw).hexdigest()
    response = result(job, worker.worker_id, neighbors=[{
        "post_id": 998, "md5": post(998)["file"]["md5"], "rank": 1, "distance": 0.25,
    }]).model_dump(mode="json")
    assert client.post("/api/processing-machine/results", json=response).status_code == 401
    assert client.post("/api/processing-machine/results", json=response, headers=headers).status_code == 204

    async def finish():
        await asyncio.gather(*list(state.similarity.tasks.values()))
    run(client, finish())
    detail = client.get(f"/api/projects/{pid}/similar/{queued.json()['id']}").json()
    assert detail["status"] == "done" and detail["results"][0]["post_id"] == 998
    assert client.get(path, params=params, headers=headers).status_code == 409

    cancelled_search = client.post(f"/api/projects/{pid}/similar", json={
        "request_id": str(uuid4()), "post_id": 1001, "limit": 1,
    })
    assert cancelled_search.status_code == 200, cancelled_search.text
    cancelled_id = cancelled_search.json()["id"]
    stale_job = None
    for _ in range(30):
        stale_job = client.post(poll, json=body, headers=headers).json()["job"]
        if stale_job:
            break
        time.sleep(0.01)
    assert stale_job and stale_job["kind"] == "image_search"
    cancel = client.post(f"/api/projects/{pid}/similar/{cancelled_id}/cancel", json={"request_id": str(uuid4())})
    assert cancel.status_code == 200 and cancel.json()["status"] == "cancelled"
    stale_path = f"/api/processing-machine/jobs/{stale_job['id']}/image"
    stale_params = {"lease": stale_job["lease"], "worker_id": str(worker.worker_id)}
    assert client.get(stale_path, params=stale_params, headers=headers).status_code == 409
    stale_result = result(stale_job, worker.worker_id, neighbors=[{
        "post_id": 998, "md5": post(998)["file"]["md5"], "rank": 1, "distance": 0.25,
    }]).model_dump(mode="json")
    assert client.post("/api/processing-machine/results", json=stale_result, headers=headers).status_code == 409
    for table in ("labels", "label_events", "similarity_provenance", "retrieval_provenance"):
        assert not run(client, state.db.rows(f"SELECT * FROM {table}"))


def test_worker_unseen_image_encoding_and_incremental_post_insertion(tmp_path):
    class Encoder:
        model_revision, space = MODEL, SPACE
        calls = 0

        def encode(self, raw):
            self.calls += 1
            assert raw == image_bytes()
            return np.array([1, 0, 0], dtype=np.float32)

    engine = object.__new__(ImageEngine)
    engine.encoder = Encoder()
    engine.space, engine.broad = SPACE, None
    engine.index = VectorIndex(tmp_path / "index.sqlite3", space=SPACE, model_revision=MODEL, dimension=3)
    engine.index.upsert(998, "e" * 32, "f" * 64, [0, 1, 0])
    engine.capability = engine._capability()
    raw, worker = image_bytes(), str(uuid4())
    md5, digest = hashlib.md5(raw).hexdigest(), hashlib.sha256(raw).hexdigest()
    job = {"kind": "image_search", "protocol_version": 2, "id": str(uuid4()), "lease": str(uuid4()),
           "query_type": "upload", "query_sha256": digest, "post_id": None, "source_md5": None,
           "model_revision": MODEL, "embedding_space": SPACE}
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(raw), headers={"X-Image-SHA256": digest})
    with httpx.Client(base_url="http://worker-test/", transport=httpx.MockTransport(respond)) as client:
        output = engine.process(client, job, worker)
        assert ImageResult.model_validate(output).coverage == "available"
        assert output["neighbors"][0]["post_id"] == 998
        assert output["corpus_size"] == 1
        assert engine.index.connection.execute("SELECT count(*) FROM vectors").fetchone()[0] == 1  # upload never ingested
        post_job = {**job, "id": str(uuid4()), "query_type": "post", "post_id": 999, "source_md5": md5, "query_sha256": None}
        assert engine.process(client, post_job, worker)["coverage"] == "available"
        assert engine.process(client, post_job, worker)["coverage"] == "available"
        assert len(requests) == 2 and engine.encoder.calls == 2  # indexed post avoids encoding and download
    engine.close()
    with pytest.raises(ValidationError):
        ImageResult.model_validate({**output, "model_revision": "wrong"})
    assert validate_image(raw, 1000) == digest


def test_broad_worker_uses_raw_image_features_and_keeps_the_old_corpus_separate(tmp_path, monkeypatch):
    from backend import image_retrieval_worker as module

    global_space = "9" * 64

    class Encoder:
        model_revision, space = MODEL, SPACE
        calls = 0

        def __init__(self, checkpoint):
            pass

        def encode_raw(self, raw):
            self.calls += 1
            assert raw == image_bytes()
            return np.eye(1, 2048, dtype=np.float32)[0]

        def encode(self, raw):
            raise AssertionError("Uncentered embeddings cannot query the broad index")

    class Broad:
        size, space, revision = 2, global_space, INDEX

        def __init__(self, root, *, model_revision, query_space):
            assert (model_revision, query_space) == (MODEL, SPACE)

        def transform(self, value):
            return unit_vector(value, 2048)

        def contains(self, ids):
            return np.isin(ids, [997, 998])

        def search(self, vector, *, exclude_post, override_ids):
            assert exclude_post == 999 and list(override_ids) == [999]
            return [{"post_id": n, "md5": None, "rank": i + 1, "distance": (i + 1) / 10}
                    for i, n in enumerate([997, 998])]

        def close(self):
            pass

    pilot_path = tmp_path / "pilot.sqlite3"
    old = VectorIndex(pilot_path, space=SPACE, model_revision=MODEL)
    old.upsert(123, "e" * 32, "f" * 64, np.eye(1, 2048, dtype=np.float32)[0])
    old.close()
    original_bytes = pilot_path.read_bytes()
    monkeypatch.setattr(module, "HydraEncoder", Encoder)
    monkeypatch.setattr(module, "BroadIndex", Broad)
    engine = ImageEngine("checkpoint", pilot_path, broad_index=tmp_path / "published")
    assert engine.index.path != pilot_path and engine.capability["corpus_size"] == 2
    raw = image_bytes()
    job = {"kind": "image_search", "protocol_version": 2, "id": str(uuid4()), "lease": str(uuid4()),
           "query_type": "post", "post_id": 999, "source_md5": hashlib.md5(raw).hexdigest(),
           "query_sha256": None, "model_revision": MODEL, "embedding_space": global_space}
    transfers = []

    def respond(request):
        transfers.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(raw), headers={"X-Image-SHA256": hashlib.sha256(raw).hexdigest()})

    with httpx.Client(base_url="http://worker-test/", transport=httpx.MockTransport(respond)) as client:
        output = engine.process(client, job, str(uuid4()))
        assert ImageResult.model_validate(output).coverage == "available"
        assert [n["post_id"] for n in output["neighbors"]] == [997, 998]
        assert output["corpus_size"] == 3 and output["embedding_space"] == global_space
        assert engine.process(client, job, str(uuid4()))["neighbors"] == output["neighbors"]
        assert engine.encoder.calls == len(transfers) == 1
        with pytest.raises(ValueError, match="embedding space"):
            engine.process(client, {**job, "embedding_space": SPACE}, str(uuid4()))
    engine.close()
    assert pilot_path.read_bytes() == original_bytes
