import pytest
from fastapi import HTTPException

from backend.tests.test_library import process, setup, toggle
from backend.tests.test_workflow import run


def test_pool_search_uses_membership_order(client):
    pid = setup(client)
    pool = client.get("/api/pools/1").json()
    assert client.get(f"/api/projects/{pid}/candidates/999").json()["pools"] == [1]
    assert client.get(f"/api/projects/{pid}/candidates/998").json()["pools"] == []
    query = f"/api/projects/{pid}/query?query=pool:1&limit=2"
    page = client.get(query).json()
    assert [p["post_id"] for p in page["items"]] == pool["post_ids"][:2]
    last = client.get(query + "&cursor=" + page["next_cursor"]).json()
    assert [p["post_id"] for p in last["items"]] == pool["post_ids"][2:]
    assert last["next_cursor"] is None
    named = client.get(f"/api/projects/{pid}/query", params={"query": "pool:" + pool["name"]}).json()
    assert named["pool_id"] == pool["id"]
    assert [item["post_id"] for item in named["items"]] == pool["post_ids"]


def test_pool_save_shares_existing_favourite_and_removes_owned_links(client):
    pid = setup(client)
    toggle(client, pid)
    process(client)
    root = client.app.state.library.root
    original = next(root.glob("e621_999_*"))
    url = "/api/pools/1/favorite"
    queued = {"total": 3, "queued": 3, "saved": 0}
    assert client.post(url).json() == queued
    assert client.post(url).json() == queued
    for post_id in (999, 997, 1000):
        process(client, post_id)
    folder = root / "pool" / "Pool-1"
    assert (folder / ".forcegallery").is_file()
    album = sorted(folder.glob("*_e621_*"))
    assert [p.name.split("_")[3] for p in album] == ["999", "997", "1000"]
    assert album[0].samefile(original)
    assert client.post(url).json() == {"total": 3, "queued": 0, "saved": 3}
    assert client.get("/api/library/posts/997").json()["wanted"]
    assert toggle(client, pid, wanted=False, version=2)[0].is_success
    process(client)
    assert not original.exists() and not album[0].exists()
    assert album[1].exists() and album[2].exists()


def test_overlapping_pool_saves_merge_intents(client, monkeypatch):
    async def pool(pool_id):
        return {"id": pool_id, "name": f"Pool {pool_id}", "post_ids": [999]}

    monkeypatch.setattr(client.app.state.source, "pool", pool)
    for pool_id in (2, 3):
        assert client.post(f"/api/pools/{pool_id}/favorite").json()["queued"] == 1
    process(client)
    root = client.app.state.library.root
    first, second = [next((root / "pool" / f"Pool-{i}").glob("*_e621_*")) for i in (2, 3)]
    assert first.samefile(second)
    for pool_id in (2, 3):
        assert client.post(f"/api/pools/{pool_id}/favorite").json()["saved"] == 1



def test_stash_scan_waits_for_downloads_then_claims_one_album(client, monkeypatch):
    library = client.app.state.library
    monkeypatch.setattr(library.settings, "stash_url", "http://test-stash")
    client.post("/api/pools/1/favorite").raise_for_status()
    workers = client.app.state.workers

    async def claim_now():
        async with library.db.transaction() as conn:
            await conn.execute("UPDATE jobs SET available_at=0 WHERE type='stash_pool'")
        return await workers._claim("stash_pool")

    assert run(client, claim_now()) is None
    for post_id in (999, 997, 1000):
        process(client, post_id)
    assert (library.root / "pool/Pool-1/.nogallery").is_file()
    assert run(client, claim_now())["type"] == "stash_pool"
    assert run(client, claim_now()) is None


def test_album_name_collision_preserves_external_file_and_single_favourite(client):
    pid = setup(client)
    toggle(client, pid)
    process(client)
    root = client.app.state.library.root
    original = next(root.glob("e621_999_*"))
    folder = root / "pool" / "Pool-1"
    folder.mkdir(parents=True)
    collision = folder / ("000001_p1_" + original.name)
    collision.write_bytes(b"existing file")
    client.post("/api/pools/1/favorite").raise_for_status()
    with pytest.raises(HTTPException, match="already exists"):
        process(client)
    assert collision.read_bytes() == b"existing file"
    toggle(client, pid, wanted=False, version=2)[0].raise_for_status()
    process(client)
    assert not original.exists()
    assert collision.read_bytes() == b"existing file"
