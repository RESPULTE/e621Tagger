import asyncio
import hashlib
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from backend.app import mock
from backend.app.db import SCHEMA, SCHEMA_V2, Database
from backend.filesystem import disk
from backend.tests.test_datasets import review
from backend.tests.test_workflow import browse, project, run


def setup(client):
    created, _ = project(client)
    browse(client, created["id"])
    return created["id"]


def toggle(client, pid, post=999, wanted=True, version=0, request_id=None):
    body = {"request_id": request_id or str(uuid4()), "wanted": wanted, "expected_version": version}
    result = client.put(f"/api/projects/{pid}/library/{post}", json=body)
    return result, body


def process(client, post=999, version=None):
    db = client.app.state.db
    row = run(client, db.one("SELECT * FROM saved_media WHERE post_id=?", (post,)))
    version = version or row["version"]
    job = run(client, db.one("SELECT * FROM jobs WHERE job_key=?", (f"library:{post}:{version}",)))
    run(client, client.app.state.library.process(job, json.loads(job["payload_json"])))


def test_save_tags_checksum_idempotency_delete_and_preserve_human_data(client):
    pid = setup(client)
    review(client, pid, 999, "negative")
    run(client, client.app.state.media.download_preview(999))
    frozen = client.post(
        f"/api/projects/{pid}/datasets", json={"request_id": str(uuid4()), "name": "Before favorite"}
    ).json()
    export = f"/api/datasets/{frozen['id']}/export?format=json"
    before = client.get(export).content
    request_id = str(uuid4())
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: toggle(client, pid, request_id=request_id)[0], range(2)))
    assert all(r.status_code == 200 for r in results)
    assert results[0].json() == results[1].json()
    process(client)
    saved = client.get("/api/library/posts/999").json()
    assert saved["status"] == "saved" and saved["wanted"] and saved["present"]
    root = client.app.state.settings.library_root
    files = sorted(root.glob("e621_999_*"))
    assert {p.suffix for p in files} == {".png", ".json", ".txt", ".xmp"}
    image = next(p for p in files if p.suffix == ".png")
    source = mock.post(999)
    assert hashlib.md5(image.read_bytes()).hexdigest() == source["file"]["md5"]
    metadata = json.loads(next(p for p in files if p.suffix == ".json").read_text(encoding="utf-8"))
    assert metadata["tags"] == source["tags"] and metadata["id"] == 999
    assert metadata["_hydra_studio"]["post_url"] == "https://e621.net/posts/999"
    assert "studio_demo" in next(p for p in files if p.suffix == ".txt").read_text()
    assert b"http://purl.org/dc/elements/1.1/" in next(p for p in files if p.suffix == ".xmp").read_bytes()
    assert not list((root / ".hydra-studio-tmp").iterdir())
    # The saved state is global across projects and cannot become a human label.
    second = project(client, custom=True)[0]["id"]
    assert client.get(f"/api/projects/{second}/candidates/999").json()["favorite"]["wanted"]
    assert client.get(f"/api/projects/{second}/candidates/999").json()["state"] is None
    assert client.get(f"/api/projects/{pid}/candidates/999").json()["state"] == "negative"
    assert client.get(export).content == before
    result, _ = toggle(client, pid, wanted=False, version=1)
    assert result.status_code == 200
    process(client)
    assert client.get("/api/library/posts/999").json()["status"] == "idle"
    assert not list(root.glob("e621_999_*"))
    assert client.get(export).content == before
    assert len(run(client, client.app.state.db.rows("SELECT * FROM label_events"))) == 1


def test_existing_files_edited_files_and_symlinks_are_preserved(client):
    pid = setup(client)
    root = client.app.state.settings.library_root
    original = root / f"e621_999_{mock.post(999)['file']['md5']}.png"
    original.write_bytes(b"pre-existing collection file")
    assert toggle(client, pid)[0].status_code == 200
    with pytest.raises(HTTPException, match="409"):
        process(client)
    assert original.read_bytes() == b"pre-existing collection file"
    assert not list(root.glob("*.json"))
    original.unlink()
    process(client)
    original.write_bytes(b"manually edited original")
    toggle(client, pid, wanted=False, version=1)
    with pytest.raises(HTTPException, match="409"):
        process(client)
    assert original.read_bytes() == b"manually edited original"
    assert len(list(root.glob("e621_999_*"))) == 4


def test_superseded_add_never_publishes_and_offline_identity_never_recreates(client):
    pid = setup(client)
    root = client.app.state.settings.library_root
    toggle(client, pid)
    toggle(client, pid, wanted=False, version=1)
    process(client, version=1)
    process(client, version=2)
    assert not list(root.glob("e621_999_*"))
    assert client.get("/api/library/posts/999").json()["status"] == "idle"
    marker = root / ".hydra-studio-library-id"
    marker.unlink()
    toggle(client, pid, wanted=True, version=2)
    with pytest.raises(HTTPException, match="503"):
        process(client)
    assert not marker.exists() and not list(root.glob("e621_999_*"))


def test_partial_publication_recovers_using_inode_manifest(client, monkeypatch):
    pid = setup(client)
    toggle(client, pid)
    library = client.app.state.library
    real = library.publish_files

    def interrupted(record):
        # Simulate a crash after the first no-clobber publication, before state
        # acknowledgement. The journal retains the staging inode and hashes.
        stage = library.paths(record)
        entry = record["files"][1]
        import os

        os.link(stage / entry["name"], library.root / entry["name"])
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(library, "publish_files", interrupted)
    with pytest.raises(RuntimeError):
        process(client)
    assert client.get("/api/library/posts/999").json()["status"] == "publishing"
    monkeypatch.setattr(library, "publish_files", real)
    process(client)
    assert client.get("/api/library/posts/999").json()["status"] == "saved"
    assert len(list(library.root.glob("e621_999_*"))) == 4


def test_wrong_original_checksum_size_and_scope_fail_without_publication(client):
    pid = setup(client)
    result, _ = toggle(client, pid, post=1)
    assert result.status_code == 404
    assert toggle(client, pid, post=2**80)[0].status_code == 422
    toggle(client, pid)
    assert toggle(client, pid, wanted=False, version=0)[0].status_code == 409
    media = client.app.state.media
    original = media.client

    def invalid(request):
        return httpx.Response(200, headers={"Content-Type": "image/png"}, stream=httpx.ByteStream(b"wrong original"))

    media.client = httpx.AsyncClient(transport=httpx.MockTransport(invalid))
    try:
        with pytest.raises(HTTPException, match="502"):
            process(client)
        assert not list(client.app.state.settings.library_root.glob("e621_999_*"))
    finally:
        run(client, media.client.aclose())
        media.client = original
    client.app.state.settings.library_max_bytes = 1024
    with pytest.raises(HTTPException, match="413"):
        process(client)
    assert not list(client.app.state.settings.library_root.glob("e621_999_*"))


def test_worker_restart_recovers_queued_collection_save(client):
    pid = setup(client)
    toggle(client, pid)

    async def work():
        workers = client.app.state.workers
        async with client.app.state.db.transaction() as conn:
            await conn.execute("UPDATE jobs SET status='running',lease_until=0 WHERE type='library'")
        await workers.start(1)
        try:
            for _ in range(200):
                row = await client.app.state.db.one("SELECT status FROM saved_media WHERE post_id=999")
                if row["status"] == "saved":
                    return
                await asyncio.sleep(0.03)
            pytest.fail("save worker did not recover")
        finally:
            await workers.close()

    run(client, work())
    assert client.get("/api/library/posts/999").json()["present"]


async def test_v2_upgrade_backs_up_and_preserves_review_tables(tmp_path):
    with sqlite3.connect(tmp_path / "studio.sqlite3") as connection:
        connection.executescript(SCHEMA + SCHEMA_V2 + "PRAGMA user_version=2;")
        connection.execute("INSERT INTO custom_tags(name) VALUES('preserved')")
        connection.execute("INSERT INTO projects VALUES(1,'custom',1,'preserved','dragon',0)")
    db = Database(tmp_path)
    try:
        await db.open()
        assert (await db.one("PRAGMA user_version"))["user_version"] == 5
        assert (await db.one("SELECT count(*) AS n FROM projects"))["n"] == 1
        backup = list((tmp_path / "backups").glob("pre-schema-v5-*.sqlite3"))
        assert len(backup) == 1
        with sqlite3.connect(backup[0]) as old:
            assert old.execute("PRAGMA user_version").fetchone()[0] == 2
            assert old.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
        assert not await db.rows("PRAGMA foreign_key_check")
    finally:
        await db.close()


async def test_cancellation_joins_disk_work_before_releasing_writer_lock(client, monkeypatch):
    library = client.app.state.library
    started, release, ended = threading.Event(), threading.Event(), threading.Event()

    def slow_write():
        started.set()
        release.wait(5)
        ended.set()

    async def operation(*_):
        await disk(slow_write)

    monkeypatch.setattr(library, "_process", operation)
    task = asyncio.create_task(library.process({}, {"post_id": 999, "version": 1}))
    try:
        await asyncio.to_thread(started.wait, 2)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0.03)
        task.cancel()  # shutdown can cancel a task already handling a timeout
        await asyncio.sleep(0.03)
        assert library.operation_locks[999 % 64].locked() and not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended.is_set() and not library.operation_locks[999 % 64].locked()


def test_replacement_during_delete_is_restored_without_deleting_it(client, monkeypatch):
    pid = setup(client)
    toggle(client, pid)
    process(client)
    library = client.app.state.library
    original = next(library.root.glob("*.png"))
    real_rename = os.rename

    def replace_before_move(source, destination):
        if source == original:
            replacement = library.root / "external-replacement"
            replacement.write_bytes(b"external file must survive")
            os.replace(replacement, original)
        return real_rename(source, destination)

    monkeypatch.setattr(os, "rename", replace_before_move)
    toggle(client, pid, wanted=False, version=1)
    with pytest.raises(HTTPException, match="409"):
        process(client)
    assert original.read_bytes() == b"external file must survive"
    assert run(client, library.db.one("SELECT record_json FROM saved_media WHERE post_id=999"))["record_json"]


def test_delete_durability_failure_retains_manifest_and_can_resume(client, monkeypatch):
    from backend.app import library as module

    pid = setup(client)
    toggle(client, pid)
    process(client)
    library = client.app.state.library
    toggle(client, pid, wanted=False, version=1)
    real_sync = module.sync_directory
    failed = False

    def fail_once(path):
        nonlocal failed
        if path == library.root and not failed:
            failed = True
            raise OSError("simulated directory flush failure")
        real_sync(path)

    monkeypatch.setattr(module, "sync_directory", fail_once)
    with pytest.raises(HTTPException, match="507"):
        process(client)
    assert run(client, library.db.one("SELECT record_json FROM saved_media WHERE post_id=999"))["record_json"]
    process(client)
    assert not list(library.root.glob("e621_999_*"))
    assert not list((library.root / ".hydra-studio-tmp").iterdir())
    assert client.get("/api/library/posts/999").json()["status"] == "idle"


def test_symlink_replacement_is_preserved(client):
    pid = setup(client)
    toggle(client, pid)
    process(client)
    root = client.app.state.library.root
    original = next(root.glob("*.png"))
    target = root / "external-file"
    target.write_bytes(b"unrelated")
    original.unlink()
    try:
        original.symlink_to(target)
    except OSError:
        pytest.skip("This Windows account cannot create symbolic links")
    toggle(client, pid, wanted=False, version=1)
    with pytest.raises(HTTPException, match="409"):
        process(client)
    assert original.is_symlink() and target.read_bytes() == b"unrelated"
    assert len(list(root.glob("e621_999_*"))) == 4


def test_on_after_interrupted_removal_redownloads_missing_sources(client, monkeypatch):
    pid = setup(client)
    toggle(client, pid)
    process(client)
    library = client.app.state.library
    toggle(client, pid, wanted=False, version=1)
    real_remove = library.remove_files

    def interrupted(record):
        real_remove(record)
        raise RuntimeError("crash before clearing journal")

    monkeypatch.setattr(library, "remove_files", interrupted)
    with pytest.raises(RuntimeError):
        process(client)
    assert not list(library.root.glob("e621_999_*"))
    monkeypatch.setattr(library, "remove_files", real_remove)
    toggle(client, pid, wanted=True, version=2)
    process(client)
    assert client.get("/api/library/posts/999").json()["status"] == "saved"
    assert len(list(library.root.glob("e621_999_*"))) == 4


def test_empty_mock_search_does_not_render_excluded_originals(monkeypatch):
    def unexpected_render(*_):
        pytest.fail("A metadata-only empty search rendered an excluded original")

    monkeypatch.setattr(mock, "artwork", unexpected_render)
    response = mock.transport(httpx.Request("GET", "https://e621.net/posts.json?tags=no_such_mock_tag"))
    assert response.status_code == 200 and response.json() == {"posts": []}
