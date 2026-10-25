import asyncio
import hashlib
import json
import time
from uuid import uuid4

import numpy as np
import pytest
from fastapi import HTTPException

from backend.app import hydra_knn
from backend.app.datasets import snapshot
from backend.app.hydra_knn import HydraIndex, validate
from backend.app.mock import post
from backend.app.similarity import Similarity
from backend.app.upstream import UpstreamGate
from backend.tests.test_workflow import browse, project, run


def arrays(tmp_path, monkeypatch, unsorted=False):
    posts = np.array([30, 10, 20] if unsorted else [10, 20, 30], dtype="<i8")
    ids = np.tile(np.arange(100, 164, dtype="<i8"), (3, 1))
    ids[:, :3] = np.column_stack((posts, np.full(3, 100), np.full(3, 100)))
    dists = np.tile(np.arange(64, dtype="<f2") / 64, (3, 1))
    files = {}
    for kind, array in zip(("postids", "ids", "dists"), (posts, ids, dists)):
        path = tmp_path / f"hydra35_embed_{kind}.npy"
        np.save(path, array)
        files[kind] = (array.dtype.str, array.shape, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setattr(hydra_knn, "FILES", files)
    return posts


@pytest.mark.parametrize("unsorted", [False, True])
def test_exact_mmap_lookup_validation_and_missing(tmp_path, monkeypatch, unsorted):
    arrays(tmp_path, monkeypatch, unsorted)
    validate(tmp_path)
    index = HydraIndex(tmp_path)
    index.open()
    assert index.error is None
    assert isinstance(index.arrays["ids"], np.memmap)
    result = index.lookup_sync(20)
    assert result["neighbors"][0] == {"post_id": 100, "rank": 2, "distance": 1 / 64}
    assert len(result["neighbors"]) == 62
    assert index.lookup_sync(21)["coverage"] == "not_indexed"
    path = tmp_path / "hydra35_embed_dists.npy"
    with path.open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"x")
    broken = HydraIndex(tmp_path)
    broken.open()
    assert broken.error
    with pytest.raises(ValueError, match="Checksum"):
        validate(tmp_path)


def test_invalid_shape_and_nan_are_rejected(tmp_path, monkeypatch):
    arrays(tmp_path, monkeypatch)
    path = tmp_path / "hydra35_embed_dists.npy"
    data = np.load(path)
    data[0, 1] = np.nan
    np.save(path, data)
    with pytest.raises(ValueError, match="distances"):
        validate(tmp_path, published=False)
    np.save(path, np.zeros((3, 63), dtype="<f2"))
    with pytest.raises(ValueError, match="matching"):
        validate(tmp_path, published=False)


async def finish(service):
    await asyncio.gather(*list(service.tasks.values()))


def test_browse_is_transient_review_is_verified_and_replay_survives_expiry(client):
    pid = project(client)[0]["id"]
    browse(client, pid)
    state = client.app.state
    body = {"post_id": 999, "request_id": str(uuid4())}
    url = f"/api/projects/{pid}/similar"
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    key = response.json()["id"]
    assert client.post(url, json=body).json()["id"] == key
    run(client, finish(state.similarity))
    details = client.get(url + "/" + key).json()
    assert details["status"] == "done"
    assert details["results"]
    for table in ("posts", "project_candidates", "labels", "similarity_provenance", "search_runs", "jobs"):
        assert run(client, state.db.one(f"SELECT count(*) AS n FROM {table}"))["n"] == 0
    entry = details["results"][0]
    context = entry["candidate"]["similarity"]
    label = {"request_id": str(uuid4()), "state": "negative", "expected_version": 0, "similarity": context}
    path = f"/api/projects/{pid}/labels/{entry['post_id']}"
    first = client.patch(path, json=label)
    assert first.status_code == 200, first.text
    evidence = run(client, state.db.one("SELECT * FROM similarity_provenance"))
    assert evidence["seed_post_id"] == 999 and evidence["neighbor_rank"] == entry["rank"]
    assert evidence["l2_distance"] == entry["distance"]
    _, samples = run(client, snapshot(state.db.connection, pid))
    assert any(p["kind"] == "hydra35_knn" for p in json.loads(samples[0]["provenance_json"]))
    invalid = {**label, "request_id": str(uuid4()), "expected_version": 1, "similarity": {**context, "seed_post_id": 1}}
    assert client.patch(path, json=invalid).status_code == 409
    assert client.get(url + "/" + key).json()["results"][0]["candidate"]["state"] == "negative"
    # Changing the label without KNN context drops active evidence; undo restores it.
    changed = client.patch(path, json={"request_id": str(uuid4()), "expected_version": 1, "state": "positive"}).json()
    _, samples = run(client, snapshot(state.db.connection, pid))
    assert not any(p["kind"] == "hydra35_knn" for p in json.loads(samples[0]["provenance_json"]))
    restored = client.post(
        path + "/undo", json={"request_id": str(uuid4()), "expected_version": 2, "event_id": changed["event_id"]}
    ).json()
    _, samples = run(client, snapshot(state.db.connection, pid))
    assert any(p["kind"] == "hydra35_knn" for p in json.loads(samples[0]["provenance_json"]))
    # Receipts are durable even though the search and its index service are not.
    run(client, state.similarity.close())

    class Offline:
        async def lookup(self, seed):
            raise AssertionError("Replay must not need the index")

    state.similarity.index = Offline()
    assert client.patch(path, json=label).json() == first.json()
    assert client.get(url + "/" + key).status_code == 410
    undo = client.post(
        path + "/undo", json={"request_id": str(uuid4()), "event_id": restored["event_id"], "expected_version": 3}
    )
    assert undo.status_code == 200 and undo.json()["state"] == "positive"
    assert run(client, state.db.one("SELECT count(*) AS n FROM similarity_provenance"))["n"] == 2


def test_backfill_preserves_rank_and_transient_errors_do_not_discard(client):
    pid = project(client)[0]["id"]
    service = client.app.state.similarity

    class Index:
        revision = "synthetic"

        async def lookup(self, seed):
            return {
                "revision": self.revision,
                "coverage": "available",
                "neighbors": [
                    {"post_id": n, "rank": i + 1, "distance": i / 10} for i, n in enumerate([500, 501, 502, 503])
                ],
            }

    service.index = Index()
    for n in range(500, 504):
        value = post(n)
        if n == 500:
            value["flags"] = {"deleted": True}
        run(client, remember(service.feed, value))

    async def search():
        item = service.enqueue("owner", pid, 999, 2, uuid4())
        await finish(service)
        return await service.view(item)

    result = run(client, search())
    assert [r["post_id"] for r in result["results"]] == [501, 502]
    assert [r["rank"] for r in result["results"]] == [2, 3]


async def test_failure_keeps_placeholders_and_cancellation_stops_hydration():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    class Index:
        async def lookup(self, seed):
            return {
                "revision": "test",
                "coverage": "available",
                "neighbors": [{"post_id": n, "rank": n, "distance": n / 10} for n in range(1, 4)],
            }

    class Feed:
        async def raw(self, post_id, **kwargs):
            calls.append(post_id)
            started.set()
            await release.wait()
            raise HTTPException(503, "Temporary upstream failure")

    service = Similarity(Index(), Feed())
    item = service.enqueue("one", 1, 10, 2, uuid4())
    await started.wait()
    assert len(item["results"]) == 2 and item["results"][0]["metadata_state"] == "pending"
    service.tasks[item["id"]].cancel()
    await finish(service)
    assert item["status"] == "cancelled" and calls == [1]
    service.start(item)
    release.set()
    await finish(service)
    assert item["status"] == "failed" and item["error"] == "Temporary upstream failure"
    assert [r["post_id"] for r in item["results"]] == [1, 2]
    await service.close()


async def test_memory_budget_evicts_finished_searches_and_never_exceeds_limit():
    class Index:
        async def lookup(self, seed):
            return {"revision": "test", "coverage": "not_indexed", "neighbors": []}

    service = Similarity(Index(), None)
    service.byte_limit = 30
    first = service.enqueue("one", 1, 10, 2, uuid4())
    await finish(service)
    service.retain(first, {"a": "1234567890"})
    second = service.enqueue("one", 1, 11, 2, uuid4())
    service.retain(second, {"a": "1234567890"})
    assert first["id"] not in service.searches
    with pytest.raises(HTTPException, match="503"):
        service.retain(second, {"a": "x" * 50})
    assert sum(i["bytes"] for i in service.searches.values()) <= 30
    await service.close()


async def remember(feed, value):
    feed.remember(value)


async def test_queue_bounds_cancel_expiry_and_session_isolation():
    gate = asyncio.Event()

    class Index:
        async def lookup(self, seed):
            await gate.wait()
            return {"revision": "test", "coverage": "not_indexed", "neighbors": []}

    service = Similarity(Index(), None)
    service.maximum = 1
    first = service.enqueue("one", 1, 10, 32, uuid4())
    assert service.enqueue("one", 1, 10, 32, uuid4())["id"] == first["id"]
    with pytest.raises(HTTPException) as full:
        service.enqueue("one", 1, 11, 32, uuid4())
    assert full.value.status_code == 429
    with pytest.raises(HTTPException):
        service.get("two", 1, first["id"])
    gate.set()
    await finish(service)
    assert first["status"] == "not_indexed"
    first["accessed"] = time.monotonic() - 3601
    service.prune()
    assert not service.searches
    await service.close()


async def test_shared_upstream_gate_spaces_independent_clients():
    gate = UpstreamGate(0.025)
    starts = []

    async def request():
        await gate.acquire()
        starts.append(time.monotonic())

    await asyncio.gather(*(request() for _ in range(4)))
    assert all(b - a >= 0.02 for a, b in zip(starts, starts[1:]))


async def test_shared_gate_rechecks_deadline_after_an_early_timer(monkeypatch):
    from types import SimpleNamespace

    from backend.app import upstream

    now, sleeps = [100.0], []
    gate = UpstreamGate(1)

    async def early_sleep(delay):
        sleeps.append(delay)
        now[0] += delay / 2 if len(sleeps) == 1 else delay

    monkeypatch.setattr(upstream, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(upstream, "asyncio", SimpleNamespace(sleep=early_sleep))
    await gate.acquire()
    await gate.acquire()
    assert now[0] == 101 and sleeps == [1, 0.5]
