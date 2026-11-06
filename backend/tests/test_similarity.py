import asyncio
import json
import time
from uuid import uuid4

import pytest
from fastapi import HTTPException

from backend.app.datasets import snapshot
from backend.app.mock import artwork, post
from backend.app.similarity import Similarity
from backend.app.upstream import UpstreamGate
from backend.tests.test_workflow import browse, project, run


def image_evidence(query, neighbors=()):
    return {"revision": "test", "coverage": "available", "query_type": query["query_type"],
            "query_sha256": query.get("query_sha256") or "a" * 64,
            "model_revision": "b" * 64, "embedding_space": "c" * 64, "metric": "unit_l2",
            "source_post_id": query.get("post_id"), "source_md5": query.get("source_md5"),
            "neighbors": [{**n, "md5": post(n["post_id"])["file"]["md5"]} for n in neighbors]}


class ImageProvider:
    error = None
    revision = "test"
    corpus_size = 123

    async def search_image(self, query, **kwargs):
        return image_evidence(query)


def test_selected_image_ranking_is_independent_of_project_tags_and_previous_queries(client):
    first_project = project(client)[0]["id"]
    second_project = project(client, custom=True)[0]["id"]
    service = client.app.state.similarity
    requests = []

    class Provider(ImageProvider):
        async def search_image(self, query, **kwargs):
            # The worker receives only image identity, never project/target/tags.
            assert set(query) == {"query_type", "post_id", "source_md5", "query_sha256"}
            requests.append(dict(query))
            ids = [998, 997] if query["post_id"] == 999 else [997, 998]
            return {**image_evidence(query, [
                {"post_id": n, "rank": i + 1, "distance": 0.1 + i / 10} for i, n in enumerate(ids)
            ]), "corpus_size": self.corpus_size}

    service.index = Provider()

    def search(pid, source):
        response = client.post(f"/api/projects/{pid}/similar", json={"post_id": source, "request_id": str(uuid4())})
        assert response.status_code == 200, response.text
        run(client, finish(service))
        return client.get(f"/api/projects/{pid}/similar/{response.json()['id']}").json()

    first = search(first_project, 999)
    assert first["source_artists"] == ["studio_demo"]
    assert client.get(f"/api/projects/{first_project}/similar").json()["items"][0]["source_artists"] == ["studio_demo"]
    other_image = search(first_project, 1001)
    other_project = search(second_project, 999)
    assert [r["post_id"] for r in first["results"]] == [998, 997]
    assert [r["post_id"] for r in other_image["results"]] == [997, 998]
    assert [(r["post_id"], r["distance"], r["rank"]) for r in other_project["results"]] == [
        (r["post_id"], r["distance"], r["rank"]) for r in first["results"]]
    assert first["duplicates_hidden"] == other_image["duplicates_hidden"] == other_project["duplicates_hidden"] == 0
    assert len(requests) == 3
    # Revisiting an older folder cannot alter either query's nearest matches.
    assert client.get(f"/api/projects/{first_project}/similar/{first['id']}").json()["results"] == first["results"]
    assert client.get(f"/api/projects/{second_project}/similar").json()["corpus_size"] == 123
    assert other_image["corpus_size"] == 123


def test_load_more_appends_unique_ranked_matches_and_refresh_recomputes(client):
    pid = project(client)[0]["id"]
    service, calls = client.app.state.similarity, []

    class Provider(ImageProvider):
        async def search_image(self, query, **kwargs):
            calls.append(dict(query))
            return {**image_evidence(query, [{"post_id": n, "rank": i + 1, "distance": i / 10}
                                            for i, n in enumerate(range(990, 996))]), "corpus_size": 7}

    service.index = Provider()
    url = f"/api/projects/{pid}/similar"
    response = client.post(url, json={"post_id": 999, "limit": 2, "request_id": str(uuid4())})
    assert response.status_code == 200, response.text
    detail_url = url + "/" + response.json()["id"]

    def detail():
        run(client, finish(service))
        return client.get(detail_url).json()

    assert [r["post_id"] for r in detail()["results"]] == [990, 991]
    assert detail()["has_more"]
    # Retained image rankings can be paged even if the GPU worker is now offline.
    service.index.error = "offline"
    body = {"request_id": str(uuid4())}
    assert client.post(detail_url + "/more", json=body).status_code == 200
    assert [r["post_id"] for r in detail()["results"]] == [990, 991, 992, 993]
    assert client.post(detail_url + "/more", json=body).status_code == 200
    assert len(detail()["results"]) == 4  # idempotent replay does not advance the cursor
    assert client.post(detail_url + "/more", json={"request_id": str(uuid4())}).status_code == 200
    final = detail()
    assert [r["rank"] for r in final["results"]] == list(range(1, 7))
    assert len({r["post_id"] for r in final["results"]}) == 6 and not final["has_more"]
    assert client.post(detail_url + "/more", json={"request_id": str(uuid4())}).status_code == 200
    assert detail()["results"] == final["results"] and len(calls) == 1
    service.index.error = None
    assert client.post(detail_url + "/retry", json={"request_id": str(uuid4())}).status_code == 200
    refreshed = detail()
    assert [r["post_id"] for r in refreshed["results"]] == [990, 991] and refreshed["has_more"]
    assert len(calls) == 2  # refresh checks the index; it never randomizes the ranking


def test_published_image_ids_bind_current_hashes_and_pages_skip_self_and_duplicate_media(client):
    pid = project(client)[0]["id"]
    service = client.app.state.similarity

    class Published(ImageProvider):
        async def search_image(self, query, **kwargs):
            evidence = image_evidence(query, [{"post_id": n, "rank": i + 1, "distance": i / 10}
                                             for i, n in enumerate([999, 998, 997, 996, 995, 994])])
            for neighbor in evidence["neighbors"]:
                neighbor["md5"] = None
            return {**evidence, "corpus_size": 6_122_692}

    service.index = Published()
    duplicate = post(996)
    duplicate["file"]["md5"] = post(998)["file"]["md5"]
    service.feed.remember(duplicate)
    response = client.post(f"/api/projects/{pid}/similar/reference?request_id={uuid4()}&limit=2",
                           content=artwork(999, False))
    assert response.status_code == 200, response.text
    url = f"/api/projects/{pid}/similar/{response.json()['id']}"
    run(client, finish(service))
    first = client.get(url).json()
    assert [r["post_id"] for r in first["results"]] == [998, 997]
    assert [r["candidate"]["md5"] for r in first["results"]] == [post(n)["file"]["md5"] for n in (998, 997)]
    assert client.post(url + "/more", json={"request_id": str(uuid4())}).status_code == 200
    run(client, finish(service))
    final = client.get(url).json()
    assert [r["post_id"] for r in final["results"]] == [998, 997, 995, 994]
    assert [r["rank"] for r in final["results"]] == [2, 3, 5, 6]
    assert not final["has_more"]
    evidence = service.searches[first["id"]]["evidence"]
    assert next(r for r in evidence["neighbors"] if r["post_id"] == 998)["md5"] == post(998)["file"]["md5"]
    # Hydrated current identities support the ordinary audited review path.
    saved = client.patch(f"/api/projects/{pid}/labels/998", json={"request_id": str(uuid4()),
        "state": "positive", "expected_version": 0, "similarity": first["results"][0]["candidate"]["similarity"]})
    assert saved.status_code == 200, saved.text


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
    assert details["status"] == "done" and details["query_type"] == "post"
    for table in ("posts", "project_candidates", "labels", "retrieval_provenance", "search_runs", "jobs"):
        assert run(client, state.db.one(f"SELECT count(*) AS n FROM {table}"))["n"] == 0
    entry = details["results"][0]
    context = entry["candidate"]["similarity"]
    assert context["search_id"] == key
    label = {"request_id": str(uuid4()), "state": "negative", "expected_version": 0, "similarity": context}
    path = f"/api/projects/{pid}/labels/{entry['post_id']}"
    first = client.patch(path, json=label)
    assert first.status_code == 200, first.text
    evidence = run(client, state.db.one("SELECT * FROM retrieval_provenance"))
    assert evidence["source_post_id"] == 999 and evidence["neighbor_rank"] == entry["rank"]
    assert evidence["distance"] == entry["distance"]
    _, samples = run(client, snapshot(state.db.connection, pid))
    assert any(p["kind"] == "hydra_image" for p in json.loads(samples[0]["provenance_json"]))
    invalid = {**label, "request_id": str(uuid4()), "expected_version": 1,
               "similarity": {**context, "seed_post_id": 1}}
    assert client.patch(path, json=invalid).status_code == 409
    assert client.get(url + "/" + key).json()["results"][0]["candidate"]["state"] == "negative"
    changed = client.patch(path, json={"request_id": str(uuid4()), "expected_version": 1, "state": "positive"}).json()
    _, samples = run(client, snapshot(state.db.connection, pid))
    assert not any(p["kind"] == "hydra_image" for p in json.loads(samples[0]["provenance_json"]))
    restored = client.post(path + "/undo", json={
        "request_id": str(uuid4()), "expected_version": 2, "event_id": changed["event_id"],
    }).json()
    _, samples = run(client, snapshot(state.db.connection, pid))
    assert any(p["kind"] == "hydra_image" for p in json.loads(samples[0]["provenance_json"]))
    run(client, state.similarity.close())
    assert client.patch(path, json=label).json() == first.json()
    assert client.get(url + "/" + key).status_code == 410
    undo = client.post(path + "/undo", json={
        "request_id": str(uuid4()), "event_id": restored["event_id"], "expected_version": 3,
    })
    assert undo.status_code == 200 and undo.json()["state"] == "positive"


def test_backfill_preserves_rank_and_transient_errors_do_not_discard(client):
    pid = project(client)[0]["id"]
    service = client.app.state.similarity

    class Provider(ImageProvider):
        async def search_image(self, query, **kwargs):
            return image_evidence(query, [{"post_id": n, "rank": i + 1, "distance": i / 10}
                                          for i, n in enumerate([500, 501, 502, 503])])

    service.index = Provider()
    for n in range(500, 504):
        value = post(n)
        if n == 500:
            value["flags"] = {"deleted": True}
        service.feed.remember(value)

    async def search():
        item = service.enqueue("owner", pid, 999, 2, uuid4())
        await finish(service)
        return await service.view(item)

    result = run(client, search())
    assert [r["post_id"] for r in result["results"]] == [501, 502]
    assert [r["rank"] for r in result["results"]] == [2, 3]


async def test_failure_keeps_placeholders_and_cancellation_stops_hydration():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Provider(ImageProvider):
        async def search_image(self, query, **kwargs):
            return image_evidence(query, [{"post_id": n, "rank": n, "distance": n / 10} for n in range(1, 4)])

    class Feed:
        async def raw(self, post_id, **kwargs):
            calls.append(post_id)
            started.set()
            await release.wait()
            raise HTTPException(503, "Temporary upstream failure")

    service = Similarity(Provider(), Feed())
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
    service = Similarity(ImageProvider(), None)
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


@pytest.mark.parametrize("failure", [True, False])
async def test_pending_earlier_folder_does_not_permanently_hide_later_valid_result(failure):
    started, release = asyncio.Event(), asyncio.Event()

    class Provider(ImageProvider):
        async def search_image(self, query, **kwargs):
            return image_evidence(query, [{"post_id": 998, "rank": 1, "distance": 0.25}])

    class Feed:
        calls = 0

        async def raw(self, post_id, **kwargs):
            self.calls += 1
            if self.calls == 1:
                started.set()
                await release.wait()
                if failure:
                    raise HTTPException(503, "Temporary metadata failure")
                return {"id": post_id, "flags": {"deleted": True}}
            return post(post_id)

        def remember(self, *args):
            pass

        async def candidate(self, *args):
            return {}

    service = Similarity(Provider(), Feed())
    first = service.enqueue("owner", 1, 10, 32, uuid4())
    await started.wait()
    second = service.enqueue("owner", 1, 20, 32, uuid4())
    await service.tasks[second["id"]]
    assert [r["post_id"] for r in second["results"]] == [998]
    release.set()
    await finish(service)
    assert first["status"] == ("failed" if failure else "done")
    assert [r["post_id"] for r in (await service.view(second))["results"]] == [998]
    await service.close()


async def test_queue_bounds_cancel_expiry_and_session_isolation():
    gate = asyncio.Event()

    class Provider(ImageProvider):
        async def search_image(self, query, **kwargs):
            await gate.wait()
            return image_evidence(query)

    service = Similarity(Provider(), None)
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
    assert first["status"] == "done" and not first["results"]
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

    async def early_wait(waiter, timeout):
        waiter.close()
        delay = timeout
        sleeps.append(delay)
        now[0] += delay / 2 if len(sleeps) == 1 else delay
        raise asyncio.TimeoutError

    monkeypatch.setattr(upstream, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(upstream, "asyncio", SimpleNamespace(wait_for=early_wait, TimeoutError=asyncio.TimeoutError))
    await gate.acquire()
    await gate.acquire()
    assert now[0] == 101 and sleeps == [1, 0.5]


def test_official_query_filters_loaded_matches_without_changing_ranking_or_evidence(client, monkeypatch):
    pid = project(client)[0]["id"]
    service = client.app.state.similarity

    class Provider(ImageProvider):
        async def search_image(self, query, **kwargs):
            return image_evidence(query, [{"post_id": n, "rank": i + 1, "distance": i / 10}
                                          for i, n in enumerate([997, 998, 996, 995])])

    service.index = Provider()
    response = client.post(f"/api/projects/{pid}/similar", json={
        "post_id": 999, "limit": 2, "request_id": str(uuid4())})
    assert response.status_code == 200
    url = f"/api/projects/{pid}/similar/{response.json()['id']}"
    run(client, finish(service))
    original = client.get(url).json()
    evidence = json.dumps(service.searches[original["id"]]["evidence"], sort_keys=True)
    calls = []
    request = service.feed.source.request

    async def record(path, params=None, **kwargs):
        if path == "/posts.json":
            calls.append(dict(params))
        return await request(path, params, **kwargs)

    monkeypatch.setattr(service.feed.source, "request", record)
    filtered = client.get(url, params={"query": "dragon -comic rating:e"})
    assert filtered.status_code == 200, filtered.text
    assert [r["post_id"] for r in filtered.json()["results"]] == [998]
    assert filtered.json()["results"][0] == original["results"][1]
    assert filtered.json()["unfiltered_count"] == 2 and filtered.json()["has_more"]
    assert calls == [{"tags": "( dragon -comic rating:e ) id:997,998 order:id_desc", "limit": 2}]
    # Repeated polling does not repeat upstream searches; reviews still stay fresh.
    assert client.get(url, params={"query": "dragon -comic rating:e"}).json() == filtered.json()
    assert len(calls) == 1
    # A user's own id constraint must intersect, never replace the loaded-ID set.
    assert client.get(url, params={"query": "id:999"}).json()["results"] == []
    assert client.get(url).json()["results"] == original["results"]
    assert client.get(url, params={"query": "id:997,998"}).json()["results"] == original["results"]
    assert client.post(url + "/more", json={"request_id": str(uuid4())}).status_code == 200
    run(client, finish(service))
    expanded = client.get(url, params={"query": "rating:e"}).json()
    assert [r["post_id"] for r in expanded["results"]] == [998, 995]
    assert [r["rank"] for r in expanded["results"]] == [2, 4]
    assert expanded["unfiltered_count"] == 4 and not expanded["has_more"]
    assert "id:997,998,996,995" in calls[-1]["tags"]
    assert json.dumps(service.searches[original["id"]]["evidence"], sort_keys=True) == evidence
    for table in ("posts", "project_candidates", "labels", "retrieval_provenance"):
        assert run(client, service.feed.db.one(f"SELECT count(*) AS n FROM {table}"))["n"] == 0
    assert client.get(url, params={"query": "order:score"}).status_code == 422
    assert client.get(url, params={"query": "x" * 501}).status_code == 422
    other = project(client, custom=True)[0]["id"]
    assert client.get(url.replace(f"/{pid}/", f"/{other}/"), params={"query": "dragon"}).status_code == 410


def test_filter_completion_state_matches_the_results_snapshot(client, monkeypatch):
    pid = project(client)[0]["id"]
    service = client.app.state.similarity
    response = client.post(f"/api/projects/{pid}/similar", json={
        "post_id": 999, "limit": 2, "request_id": str(uuid4())})
    run(client, finish(service))
    item = service.searches[response.json()["id"]]
    second = item["results"][1]
    candidate = second["candidate"]
    second.update(candidate=None, metadata_state="pending")
    item["status"] = "running"
    search_posts = service.feed.search_posts

    async def finish_during_filter(*args, **kwargs):
        posts = await search_posts(*args, **kwargs)
        second.update(candidate=candidate, metadata_state="ready")
        item["status"] = "done"
        return posts

    monkeypatch.setattr(service.feed, "search_posts", finish_during_filter)
    url = f"/api/projects/{pid}/similar/{item['id']}"
    first = client.get(url, params={"query": "dragon"}).json()
    assert first["status"] == "running"  # continue polling the incomplete snapshot
    assert len(first["results"]) == 1
    complete = client.get(url, params={"query": "dragon"}).json()
    assert complete["status"] == "done" and len(complete["results"]) == 2


def test_similarity_filter_upstream_errors_are_not_empty_results_or_cached(client, monkeypatch):
    pid = project(client)[0]["id"]
    service = client.app.state.similarity
    response = client.post(f"/api/projects/{pid}/similar", json={"post_id": 999, "request_id": str(uuid4())})
    run(client, finish(service))
    url = f"/api/projects/{pid}/similar/{response.json()['id']}"
    original = client.get(url).json()["results"]
    assert original
    request = service.feed.source.request

    async def unavailable(*args, **kwargs):
        raise HTTPException(503, "e621 is busy; retry this operation later")

    monkeypatch.setattr(service.feed.source, "request", unavailable)
    assert client.get(url, params={"query": "dragon"}).status_code == 503
    assert client.get(url).json()["results"] == original
    monkeypatch.setattr(service.feed.source, "request", request)
    assert client.get(url, params={"query": "dragon"}).json()["results"] == original


async def test_restricted_search_rejects_upstream_posts_outside_the_candidate_set():
    from backend.app.feed import Feed

    class Source:
        async def request(self, *args, **kwargs):
            return {"posts": [post(999)]}

    feed = Feed(Source(), None)
    with pytest.raises(HTTPException, match="502"):
        await feed.search_posts("dragon", post_ids=(998,))
    assert await feed.search_posts("dragon", post_ids=()) == []
