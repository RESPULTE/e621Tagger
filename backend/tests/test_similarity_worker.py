import json
from concurrent.futures import Future
from uuid import uuid4

import httpx
import pytest

from backend import similarity_worker
from backend.tests.test_image_retrieval import INDEX, MODEL, SPACE


class ImmediateExecutor:
    """Make protocol tests independent of host thread scheduling."""

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def submit(self, fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future


class Engine:
    capability = {"model_revision": MODEL, "embedding_space": SPACE, "index_revision": INDEX, "metric": "unit_l2"}

    def process(self, client, job, worker_id):
        return {**job, "worker_id": worker_id, "coverage": "available", "index_revision": INDEX,
                "metric": "unit_l2", "neighbors": []}


@pytest.mark.parametrize("result_status", [200, 409, 422, 503, 401])
def test_worker_sends_only_image_protocol_and_handles_result_errors(monkeypatch, result_status):
    job = {"kind": "image_search", "protocol_version": 2, "id": str(uuid4()), "lease": str(uuid4()),
           "query_type": "upload", "query_sha256": "d" * 64, "model_revision": MODEL, "embedding_space": SPACE}
    polls, results, waits = [], [], []

    def respond(request):
        assert request.headers["authorization"] == "Bearer " + "x" * 32
        assert request.headers["origin"] == "http://worker-test"
        body = json.loads(request.content)
        if request.url.path.endswith("/poll"):
            polls.append(body)
            assert body["protocol_version"] == 2 and body["image_capability"] == Engine.capability
            assert "ready" not in body and "index_revision" not in body
            return httpx.Response(200, json={"job": job if len(polls) == 1 else None})
        results.append(body)
        return httpx.Response(result_status if len(results) == 1 else 204)

    original_client = httpx.Client
    monkeypatch.setattr(similarity_worker.httpx, "Client", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(similarity_worker, "ThreadPoolExecutor", ImmediateExecutor)

    def sleep(seconds):
        waits.append(seconds)
        if len(polls) >= 2:
            raise KeyboardInterrupt()

    monkeypatch.setattr(similarity_worker.time, "sleep", sleep)
    with pytest.raises(PermissionError if result_status == 401 else KeyboardInterrupt):
        similarity_worker.run("http://worker-test", "x" * 32, Engine())
    assert results[0]["kind"] == "image_search" and results[0]["protocol_version"] == 2
    assert len(results) == (2 if result_status == 503 else 1)
    assert all(wait == 2 for wait in waits)
    if result_status == 503:
        assert len(waits) == 3
    if result_status == 401:
        assert len(polls) == 1 and len(waits) == 1


def test_restarting_worker_waits_for_predecessor_heartbeat(monkeypatch):
    polls, waits = [], []

    def respond(request):
        polls.append(request)
        return httpx.Response(409) if len(polls) == 1 else httpx.Response(200, json={"job": None})

    original_client = httpx.Client
    monkeypatch.setattr(similarity_worker.httpx, "Client", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(respond)))

    def sleep(seconds):
        waits.append(seconds)
        if len(polls) == 2:
            raise KeyboardInterrupt()

    monkeypatch.setattr(similarity_worker.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        similarity_worker.run("http://worker-test", "x" * 32, Engine())
    assert len(polls) == 2 and waits == [2, 2]


@pytest.mark.parametrize("server", ["file:///tmp/a", "https://user:secret@example.com", "http://host/path"])
def test_worker_rejects_invalid_server_origin_before_network(server):
    with pytest.raises(ValueError, match="exact HTTP"):
        similarity_worker.run(server, "x" * 32, Engine())
