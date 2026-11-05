import json
import time
from uuid import uuid4

import pytest
from fastapi import HTTPException

from backend.tests.test_download_queue import slow_originals
from backend.tests.test_library import process, setup, toggle
from backend.tests.test_workflow import run


def delete(client, job_id, request_id=None):
    return client.post(f"/api/downloads/{job_id}/delete", json={"request_id": request_id or str(uuid4())})


def test_download_metadata_completed_delete_replay_and_stale_job_protection(client):
    pid = setup(client)
    toggle(client, pid)
    process(client)
    listing = client.get("/api/downloads").json()["items"]
    assert len(listing) == 1
    job = listing[0]
    assert job["status"] == "done" and job["can_delete"]
    assert job["thumbnail_url"] == "/api/media/999/preview"
    assert job["upvotes"] == 204 and job["search_tag"] == "dragon" and job["project_id"] == pid
    assert client.get(job["thumbnail_url"]).status_code == 200
    request_id = str(uuid4())
    result = delete(client, job["id"], request_id)
    assert result.status_code == 200 and not result.json()["favorite"]["wanted"]
    assert delete(client, job["id"], request_id).json() == result.json()
    removing = client.get("/api/downloads").json()["items"]
    assert len(removing) == 1 and not removing[0]["can_delete"]
    process(client)
    assert client.get("/api/library/posts/999").json()["status"] == "idle"
    assert not client.get("/api/downloads").json()["items"]
    assert not list(client.app.state.library.root.glob("e621_999_*"))
    toggle(client, pid, version=2)
    assert delete(client, job["id"]).status_code == 409
    assert client.get("/api/library/posts/999").json()["wanted"]


@pytest.mark.parametrize("via_heart", [False, True])
def test_deleting_stalled_favourite_joins_cleanup_and_keeps_scheduler_alive(client, monkeypatch, via_heart):
    pid = setup(client)
    old, _, _ = slow_originals(client, monkeypatch, stall_post=999)
    toggle(client, pid)
    db, workers = client.app.state.db, client.app.state.workers
    job = run(client, db.one("SELECT * FROM jobs WHERE type='library'"))
    run(client, workers.start(1))
    try:
        for _ in range(200):
            current = run(client, db.one("SELECT * FROM jobs WHERE id=?", (job["id"],)))
            if current["progress_bytes"] > 0:
                break
            time.sleep(0.01)
        assert current["progress_bytes"] > 0
        started = time.monotonic()
        response = toggle(client, pid, wanted=False, version=1)[0] if via_heart else delete(client, job["id"])
        assert response.status_code == 200, response.text
        assert time.monotonic() - started < 2, "Cancellation waited for the stalled socket"
        for _ in range(200):
            state = client.get("/api/library/posts/999").json()
            if state["status"] == "idle" and job["id"] not in workers.active_downloads:
                break
            time.sleep(0.01)
        assert not state["wanted"] and not state["present"] and state["status"] == "idle"
        assert not list(client.app.state.library.root.glob("e621_999_*"))
        assert not list((client.app.state.library.root / ".hydra-studio-tmp").iterdir())
        assert not run(client, db.rows("SELECT * FROM download_partials"))
        assert not run(client, db.rows("SELECT * FROM library_staging"))
        assert not list(db.root.glob("cache/*"))
        # A cancelled operation must not terminate its scheduler loop.
        toggle(client, pid, post=998)
        for _ in range(300):
            if client.get("/api/library/posts/998").json()["status"] == "saved":
                break
            time.sleep(0.02)
        assert client.get("/api/library/posts/998").json()["status"] == "saved"
        assert not run(client, db.rows("SELECT * FROM labels"))
    finally:
        run(client, workers.close())
        run(client, client.app.state.media.client.aclose())
        client.app.state.media.client = old


def test_delete_preserves_external_edits_and_legacy_upvotes_are_not_net_score(client):
    pid = setup(client)
    toggle(client, pid)
    process(client)
    db = client.app.state.db
    job = run(client, db.one("SELECT * FROM jobs WHERE type='library'"))
    payload = json.loads(job["payload_json"])
    payload.pop("display")
    payload.pop("project_id")

    async def legacy():
        async with db.transaction() as conn:
            await conn.execute("UPDATE jobs SET payload_json=? WHERE id=?", (json.dumps(payload), job["id"]))

    run(client, legacy())
    client.app.state.feed.posts.clear()
    listed = client.get("/api/downloads").json()["items"][0]
    assert listed["upvotes"] is None and listed["search_tag"] is None
    original = next(client.app.state.library.root.glob("*.png"))
    original.write_bytes(b"externally edited original")
    assert delete(client, job["id"]).status_code == 200
    with pytest.raises(HTTPException, match="409"):
        process(client)
    assert original.read_bytes() == b"externally edited original"
    assert run(client, db.one("SELECT record_json FROM saved_media WHERE post_id=999"))["record_json"]


def test_delete_queued_favourite_never_downloads_or_publishes(client):
    pid = setup(client)
    toggle(client, pid)
    job = client.get("/api/downloads").json()["items"][0]
    assert delete(client, job["id"]).status_code == 200
    process(client)
    assert not client.get("/api/downloads").json()["items"]
    assert not client.get("/api/library/posts/999").json()["wanted"]
    assert not list(client.app.state.library.root.glob("e621_*"))


def test_stale_retry_cannot_replace_a_later_favourite_intent(client):
    pid = setup(client)
    toggle(client, pid)
    db = client.app.state.db
    job = client.get("/api/downloads").json()["items"][0]

    async def fail():
        async with db.transaction() as conn:
            await conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (job["id"],))
            await conn.execute("UPDATE saved_media SET status='failed' WHERE post_id=999")

    run(client, fail())
    toggle(client, pid, wanted=False, version=1)
    process(client)
    toggle(client, pid, version=2)
    before = client.get("/api/library/posts/999").json()
    response = client.post(f"/api/downloads/{job['id']}/retry", json={"request_id": str(uuid4())})
    assert response.status_code == 409
    assert client.get("/api/library/posts/999").json() == before


def test_inbox_saves_and_deletes_preserve_legacy_top_level_files(client):
    pid = setup(client)
    toggle(client, pid)
    process(client)
    library = client.app.state.library
    legacy = list(library.root.glob("e621_999_*"))
    assert len(legacy) == 1 and legacy[0].suffix == ".png"
    inbox = library.root / "Inbox"
    inbox.mkdir()
    manual = inbox / "manual.txt"
    manual.write_bytes(b"not owned by Studio")
    client.app.state.settings.library_subdirectory = "Inbox"
    toggle(client, pid, post=998)
    process(client, post=998)
    saved_inbox = list(inbox.glob("e621_998_*"))
    assert len(saved_inbox) == 1 and saved_inbox[0].suffix == ".png"
    assert not list(library.root.glob("e621_998_*"))
    record = json.loads(run(client, library.db.one("SELECT record_json FROM saved_media WHERE post_id=998"))["record_json"])
    assert record["directory"] == "Inbox"
    toggle(client, pid, post=998, wanted=False, version=1)
    process(client, post=998)
    assert not list(inbox.glob("e621_998_*")) and all(path.exists() for path in legacy)
    toggle(client, pid, wanted=False, version=1)
    process(client)
    assert not any(path.exists() for path in legacy)
    assert manual.read_bytes() == b"not owned by Studio"


def test_interrupted_inbox_publication_recovers_to_its_recorded_destination(client, monkeypatch):
    import os

    pid = setup(client)
    library = client.app.state.library
    inbox = library.root / "Inbox"
    inbox.mkdir()
    client.app.state.settings.library_subdirectory = "Inbox"
    toggle(client, pid)
    publish = library.publish_files

    def interrupted(record):
        entry = record["files"][0]
        assert entry["name"].endswith(".png")
        os.link(library.paths(record) / entry["name"], inbox / entry["name"])
        raise RuntimeError("interrupted Inbox publication")

    monkeypatch.setattr(library, "publish_files", interrupted)
    with pytest.raises(RuntimeError):
        process(client)
    # Configuration changes never redirect an existing ownership journal.
    client.app.state.settings.library_subdirectory = ""
    monkeypatch.setattr(library, "publish_files", publish)
    process(client)
    saved = list(inbox.glob("e621_999_*"))
    assert len(saved) == 1 and saved[0].suffix == ".png"
    assert not list(library.root.glob("e621_999_*"))
    assert not list((library.root / ".hydra-studio-tmp").iterdir())


def test_missing_or_unsafe_inbox_fails_without_creating_a_destination(client):
    pid = setup(client)
    library = client.app.state.library
    client.app.state.settings.library_subdirectory = "Inbox"
    assert not library.capability()["available"]
    toggle(client, pid)
    with pytest.raises(HTTPException, match="503"):
        process(client)
    assert not (library.root / "Inbox").exists()
    with pytest.raises(HTTPException, match="409"):
        library.destination({"directory": "../outside"})
