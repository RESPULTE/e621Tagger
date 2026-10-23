import asyncio
import hashlib
import json
import struct
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from backend.app.neighbor_index import FILES, REVISION, ROWS, PublishedNeighbors
from backend.tests.test_datasets import review
from backend.tests.test_workflow import import_job, project, run


def header(kind):
    dtype, shape, _, _ = FILES[kind]
    text = repr({"descr": dtype, "fortran_order": False, "shape": shape}).encode()
    return b"\x93NUMPY\x01\x00v\x00" + text + b" " * (117 - len(text)) + b"\n"


class IndexTransport:
    """Virtual 4 GiB arrays: generate requested ranges, never allocate the index."""

    def __init__(self, failure=None):
        self.requests = []
        self.failure = failure

    def __call__(self, request):
        self.requests.append(request)
        kind = request.url.path.split("_")[-1].split(".")[0]
        start, end = map(int, request.headers["range"].split("=")[1].split("-"))
        length = end - start + 1
        if start == 0:
            data = header(kind)
            if self.failure == "header":
                data = data.replace(b"<i8", b"<f8")
        elif kind == "postids":
            row, count = (start - 128) // 8, length // 8
            values = [14 + 2 * r for r in range(row, row + count)]
            if self.failure == "unsorted":
                values[1] = values[0]
            data = struct.pack(f"<{count}q", *values)
        elif kind == "ids":
            seed = 14 + 2 * ((start - 128) // 512)
            data = struct.pack("<64q", seed, *range(900, 963))
        else:
            distances = [i / 64 for i in range(64)]
            if self.failure == "nan":
                distances[1] = float("nan")
            data = struct.pack("<64e", *distances)
        if self.failure == "truncated":
            data = data[:-1]
        if self.failure == "oversized":
            data += b"x"
        headers = {"Content-Range": f"bytes {start}-{end}/{FILES[kind][2]}"}
        if self.failure == "wrong_range":
            headers["Content-Range"] = f"bytes 0-127/{FILES[kind][2]}"
        return httpx.Response(
            200 if self.failure == "full_response" else 206, headers=headers, stream=httpx.ByteStream(data)
        )


async def test_published_row_identity_id_mapping_bounds_and_missing_coverage():
    transport = IndexTransport()
    reader = PublishedNeighbors(transport=httpx.MockTransport(transport))
    seed = 14 + 2 * (ROWS // 3)
    result = await reader.lookup(seed)
    assert result["seed_id"] == seed and result["row"] == ROWS // 3
    assert result["neighbors"][0] == {"post_id": 900, "rank": 2, "distance": 1 / 64}
    assert result["revision"] == REVISION and len(result["artifact_sha256"]) == 64
    assert result["bytes_read"] <= 128 * 1024 and result["range_requests"] <= 32
    assert len(transport.requests) == result["range_requests"]
    # There is no row for an odd ID. Never substitute its closest row.
    missing = await reader.lookup(seed + 1)
    assert missing["coverage"] == "not_found" and missing["neighbors"] == []


@pytest.mark.parametrize(
    "failure", ["header", "unsorted", "nan", "truncated", "oversized", "wrong_range", "full_response"]
)
async def test_index_rejects_bad_data_and_full_downloads(failure):
    transport = IndexTransport(failure)
    reader = PublishedNeighbors(transport=httpx.MockTransport(transport))
    with pytest.raises(HTTPException) as error:
        await reader.lookup(14)
    assert error.value.status_code == 502


async def test_index_network_failure_does_not_leak_signed_urls():
    def fail(request):
        raise httpx.ConnectError("secret signed URL", request=request)

    with pytest.raises(HTTPException) as error:
        await PublishedNeighbors(transport=httpx.MockTransport(fail)).lookup(14)
    assert error.value.status_code == 503 and "secret" not in error.value.detail


def completed_evidence(client, pid, seed=999):
    path = f"/api/projects/{pid}/neighbors/{seed}"
    body = {"request_id": str(uuid4())}
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    assert client.post(path, json=body).json() == response.json()
    result = run(client, client.app.state.workers.neighbors.lookup(seed))

    async def finish():
        async with client.app.state.db.transaction() as conn:
            await conn.execute("UPDATE jobs SET status='done',result_json=? WHERE id=?", (json.dumps(result), job_id))

    run(client, finish())
    return path, job_id, result


def test_neighbor_import_preserves_reviews_provenance_and_frozen_data(client):
    created, _ = project(client)
    pid = created["id"]
    import_job(client, created["jobs"][0]["job_id"])
    review(client, pid, 998, "positive")
    run(client, client.app.state.media.download_preview(998))
    frozen = client.post(
        f"/api/projects/{pid}/datasets", json={"request_id": str(uuid4()), "name": "Before neighbors"}
    ).json()
    export = f"/api/datasets/{frozen['id']}/export?format=json"
    before = client.get(export).content
    path, job_id, result = completed_evidence(client, pid)
    evidence = client.get(path).json()
    assert evidence["status"] == "done" and evidence["neighbors"][0]["state"] == "positive"
    assert evidence["known_metadata"] == 8 and evidence["suggested_tags"]
    body = {"request_id": str(uuid4()), "job_id": job_id, "post_ids": [998, 997]}
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: client.post(path + "/import", json=body), range(2)))
    assert all(r.status_code == 200 for r in responses)
    assert responses[0].json() == responses[1].json()
    imported = responses[0].json()
    import_job(client, imported["job_id"])
    rows = client.get(f"/api/projects/{pid}/candidates?run_id={imported['run_id']}").json()["items"]
    assert len(rows) == 2 and rows[0]["state"] == "positive" and rows[1]["state"] is None
    assert any("embedding:" in s and result["artifact_sha256"] in s for s in rows[0]["sources"])
    assert client.get(f"/api/projects/{pid}/candidates?source=embedding").json()["total"] == 2
    after = client.post(
        f"/api/projects/{pid}/datasets", json={"request_id": str(uuid4()), "name": "After neighbors"}
    ).json()
    samples = client.get(f"/api/datasets/{after['id']}/export?format=json").json()["samples"]
    source = next(s for s in next(x for x in samples if x["post_id"] == 998)["provenance"] if s["kind"] == "embedding")
    assert source["embedding"]["seed_id"] == 999 and source["embedding"]["rank"] == 2
    assert source["embedding"]["distance"] == 0.1
    assert client.get(export).content == before
    assert hashlib.sha256(before).hexdigest() == frozen["manifest_sha256"]
    assert len(run(client, client.app.state.db.rows("SELECT * FROM label_events"))) == 1
    assert client.post(path + "/import", json={**body, "post_ids": [996]}).status_code == 409
    assert (
        client.post(path + "/import", json={**body, "request_id": str(uuid4()), "post_ids": [123]}).status_code == 422
    )
    assert (
        client.post(path + "/import", json={**body, "request_id": str(uuid4()), "post_ids": [998, 998]}).status_code
        == 422
    )
    # Another selection is a different run, even after the first is exhausted.
    other = client.post(path + "/import", json={**body, "request_id": str(uuid4()), "post_ids": [996]}).json()
    assert other["run_id"] != imported["run_id"] and other["job_id"]


def test_neighbor_scope_disabled_queue_limits_and_no_automatic_labels(client):
    created, _ = project(client)
    pid = created["id"]
    import_job(client, created["jobs"][0]["job_id"])
    path, job_id, _ = completed_evidence(client, pid)
    second, _ = project(client)
    import_job(client, second["jobs"][0]["job_id"])
    bad = client.post(
        f"/api/projects/{second['id']}/neighbors/999/import",
        json={"request_id": str(uuid4()), "job_id": job_id, "post_ids": [998]},
    )
    assert bad.status_code == 404
    assert client.post(f"/api/projects/{pid}/neighbors/1", json={"request_id": str(uuid4())}).status_code == 404
    for seed in range(960, 976):
        assert (
            client.post(f"/api/projects/{pid}/neighbors/{seed}", json={"request_id": str(uuid4())}).status_code == 200
        )
    assert client.post(f"/api/projects/{pid}/neighbors/976", json={"request_id": str(uuid4())}).status_code == 429
    client.app.state.settings.published_neighbors = False
    assert client.get("/api/embeddings/status").json()["enabled"] is False
    assert client.post(path, json={"request_id": str(uuid4())}).status_code == 409
    assert client.get(path).json()["status"] == "done"
    assert client.get(f"/api/projects/{pid}").json()["counts"]["unreviewed"] == 50


def test_neighbor_worker_restart_recovery_and_failure_isolation(client):
    created, _ = project(client)
    pid = created["id"]
    import_job(client, created["jobs"][0]["job_id"])
    path = f"/api/projects/{pid}/neighbors/999"
    job_id = client.post(path, json={"request_id": str(uuid4())}).json()["job_id"]

    async def work():
        workers = client.app.state.workers
        async with client.app.state.db.transaction() as conn:
            await conn.execute("UPDATE jobs SET status='running',lease_until=0 WHERE id=?", (job_id,))
        await workers.start(1)
        try:
            for _ in range(100):
                row = await client.app.state.db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
                if row["status"] == "done":
                    return
                await asyncio.sleep(0.03)
            pytest.fail("neighbor worker did not finish")
        finally:
            await workers.close()

    run(client, work())
    assert client.get(path).json()["status"] == "done"
    assert client.get("/health").status_code == 200
    assert client.get(f"/api/projects/{pid}").json()["counts"]["unreviewed"] == 50


def test_neighbor_input_bounds_and_offline_failure_leave_review_available(client):
    created, _ = project(client)
    pid = created["id"]
    import_job(client, created["jobs"][0]["job_id"])
    path = f"/api/projects/{pid}/neighbors/999"
    for bad in (0, -1, 2**80):
        assert client.post(f"/api/projects/{pid}/neighbors/{bad}", json={"request_id": str(uuid4())}).status_code == 422
        assert client.get(f"/api/projects/{pid}/neighbors/{bad}").status_code == 422
    assert (
        client.post(path + "/import", json={"request_id": str(uuid4()), "job_id": 2**80, "post_ids": [998]}).status_code
        == 422
    )
    job_id = client.post(path, json={"request_id": str(uuid4())}).json()["job_id"]

    class Offline:
        async def lookup(self, seed_id):
            raise HTTPException(503, "Published neighbor index is offline")

    async def failure():
        workers = client.app.state.workers
        workers.neighbors = Offline()
        async with client.app.state.db.transaction() as conn:
            await conn.execute("UPDATE jobs SET attempts=3 WHERE id=?", (job_id,))
        await workers.start(1)
        try:
            for _ in range(100):
                row = await client.app.state.db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
                if row["status"] == "failed":
                    return
                await asyncio.sleep(0.03)
            pytest.fail("failed job did not finish")
        finally:
            await workers.close()

    run(client, failure())
    assert client.get(path).json()["status"] == "failed"
    review(client, pid, 998, "negative")
    assert client.get(f"/api/projects/{pid}").json()["counts"]["negative"] == 1
    assert client.get("/health").status_code == 200
    retried = client.post(path, json={"request_id": str(uuid4())})
    assert retried.json()["job_id"] == job_id
    assert client.get(path).json()["status"] == "pending"
