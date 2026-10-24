from fastapi import HTTPException


def test_autocomplete_alias_categories_cache_and_validation(client):
    response = client.get("/api/tags/autocomplete?q=DRAK")
    assert response.status_code == 200
    assert response.json() == [{
        "tag_id": 1, "canonical_name": "dragon", "category": 5,
        "post_count": 400000, "matched_alias": "drake",
    }]
    requests = client.app.state.source.metrics["requests"]
    assert client.get("/api/tags/autocomplete?q=drak").json() == response.json()
    assert client.app.state.source.metrics["requests"] == requests
    assert client.get("/api/tags/autocomplete?q=no_such_tag").json() == []
    requests = client.app.state.source.metrics["requests"]
    assert client.get("/api/tags/autocomplete?q=no_such_tag").json() == []
    assert client.app.state.source.metrics["requests"] == requests
    for term in ["", "dragon solo", "*", "x" * 101]:
        assert client.get("/api/tags/autocomplete", params={"q": term}).status_code == 422


def test_autocomplete_invalid_upstream_is_not_cached(client, monkeypatch):
    async def invalid(*args, **kwargs):
        return {"error": "invalid"}
    monkeypatch.setattr(client.app.state.source, "request", invalid)
    response = client.get("/api/tags/autocomplete?q=dragon")
    assert response.status_code == 502
    assert "dragon" not in client.app.state.source.autocomplete_cache


def test_autocomplete_failure_does_not_block_other_requests(client, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise HTTPException(503, "e621 is busy")
    monkeypatch.setattr(client.app.state.source, "request", unavailable)
    assert client.get("/api/tags/autocomplete?q=dragon").status_code == 503
    assert client.get("/api/projects").status_code == 200
