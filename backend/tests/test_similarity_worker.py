import json
from uuid import uuid4

import httpx
import pytest

from backend import similarity_worker
from backend.app.hydra_knn import validate
from backend.app.neighbor_index import REVISION
from backend.tests.test_similarity import arrays


@pytest.mark.parametrize("result_status", [200, 409, 422, 503, 401])
def test_worker_submits_real_fixture_row_and_handles_result_errors(tmp_path, monkeypatch, result_status):
    arrays(tmp_path, monkeypatch)
    validate(tmp_path)
    job = {"id": str(uuid4()), "lease": str(uuid4()), "post_id": 20, "index_revision": REVISION}
    polls, results, waits = [], [], []

    def respond(request):
        assert request.headers["authorization"] == "Bearer " + "x" * 32
        assert request.headers["origin"] == "http://worker-test"
        body = json.loads(request.content)
        if request.url.path.endswith("/poll"):
            polls.append(body)
            assert body["ready"]
            return httpx.Response(200, json={"job": job if len(polls) == 1 else None})
        results.append(body)
        return httpx.Response(result_status if len(results) == 1 else 204)

    original_client = httpx.Client
    monkeypatch.setattr(
        similarity_worker.httpx,
        "Client",
        lambda **kwargs: original_client(
            **kwargs,
            transport=httpx.MockTransport(respond),
        ),
    )

    def sleep(seconds):
        waits.append(seconds)
        if len(polls) >= 2:
            raise KeyboardInterrupt()

    monkeypatch.setattr(similarity_worker.time, "sleep", sleep)
    expected = PermissionError if result_status == 401 else KeyboardInterrupt
    with pytest.raises(expected):
        similarity_worker.run("http://worker-test", tmp_path, "x" * 32)
    assert results[0]["post_id"] == 20
    assert results[0]["neighbors"][0] == {"post_id": 100, "rank": 2, "distance": 1 / 64}
    assert len(results) == (2 if result_status == 503 else 1)
    if result_status == 503:
        assert waits == [2, 2]  # Network/server retries back off even with a pending result.
    if result_status == 401:
        assert len(polls) == 1 and not waits  # Credential errors require operator action.


def test_restarting_worker_waits_for_predecessor_heartbeat(tmp_path, monkeypatch):
    arrays(tmp_path, monkeypatch)
    validate(tmp_path)
    polls, waits = [], []

    def respond(request):
        polls.append(request)
        return httpx.Response(409) if len(polls) == 1 else httpx.Response(200, json={"job": None})

    original_client = httpx.Client
    monkeypatch.setattr(similarity_worker.httpx, "Client", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(respond),
    ))

    def sleep(seconds):
        waits.append(seconds)
        if len(polls) == 2:
            raise KeyboardInterrupt()

    monkeypatch.setattr(similarity_worker.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        similarity_worker.run("http://worker-test", tmp_path, "x" * 32)
    assert len(polls) == 2 and waits == [2, 2]
