import asyncio
import hashlib
import io
import json
import os
import random
import sqlite3
import time
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from backend.app.db import SCHEMA, SCHEMA_V2, Database
from backend.app.main import create_app
from backend.app.mock import post
from backend.tests.conftest import PASSWORD
from backend.tests.test_transient_review import create, label, save
from backend.tests.test_workflow import run


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        for offset in range(0, len(self.data), 16384):
            await asyncio.sleep(0.03)
            yield self.data[offset : offset + 16384]


def slow_originals(client, monkeypatch):
    out = io.BytesIO()
    Image.frombytes("RGB", (256, 256), random.Random(42).randbytes(256 * 256 * 3)).save(out, format="PNG")
    raw = out.getvalue()
    originals = {}
    for i in (997, 998, 999):
        item = post(i)
        item["file"].update(md5=hashlib.md5(raw).hexdigest(), size=len(raw), width=256, height=256)
        originals[i] = item
        client.app.state.feed.remember(item, "dragon", "query", 1)
    real = client.app.state.source.request

    async def source(path, params=None, background=False):
        if path.startswith("/posts/"):
            return {"post": originals[int(path.rsplit("/", 1)[-1].split(".")[0])]}
        return await real(path, params, background=background)

    monkeypatch.setattr(client.app.state.source, "request", source)
    old = client.app.state.media.client
    starts = []

    async def respond(request):
        starts.append(time.monotonic())
        return httpx.Response(
            200, headers={"Content-Type": "image/png", "Content-Length": str(len(raw))}, stream=SlowStream(raw)
        )

    client.app.state.media.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return old, starts, raw


def test_real_workers_overlap_but_keep_interactive_capacity(client, monkeypatch):
    pid = create(client)
    old, starts, raw = slow_originals(client, monkeypatch)
    for i in (997, 998, 999):
        save(client, pid, i)
    db, workers = client.app.state.db, client.app.state.workers

    async def exercise():
        await workers.start(1)
        try:
            for _ in range(300):
                if client.app.state.transfers.metrics["active"] == 2:
                    break
                await asyncio.sleep(0.01)
            assert client.app.state.transfers.metrics["active"] == 2
            # Two downloads leave capacity for an interactive original and a preview.
            await asyncio.wait_for(client.app.state.media.acquire_slot("preview"), 0.2)
            client.app.state.media.release_slot("preview")
            await asyncio.wait_for(client.app.state.media.acquire_slot("original"), 0.2)
            client.app.state.media.release_slot("original")
            for _ in range(400):
                rows = await db.rows("SELECT status FROM jobs WHERE type='library'")
                if all(r["status"] == "done" for r in rows):
                    break
                await asyncio.sleep(0.02)
            assert all(r["status"] == "done" for r in rows)
        finally:
            await workers.close()

    try:
        run(client, exercise())
        assert len(starts) == 3 and starts[1] - starts[0] < 0.3
        metrics = client.app.state.transfers.metrics
        assert metrics["peak_active"] == 2 and metrics["bytes"] == 3 * len(raw)
        assert client.app.state.media.metrics["peak_active"] <= 4
        assert len(list(client.app.state.library.root.glob("e621_*"))) == 12
        assert not list((client.app.state.library.root / ".hydra-studio-tmp").iterdir())
        assert not list(db.root.glob("cache/*"))
        assert not run(client, db.rows("SELECT * FROM labels"))
    finally:
        run(client, client.app.state.media.client.aclose())
        client.app.state.media.client = old


def test_cancel_mid_transfer_then_retry_never_publishes_partial(client, monkeypatch):
    pid = create(client)
    old, _, _ = slow_originals(client, monkeypatch)
    save(client, pid)
    db, workers = client.app.state.db, client.app.state.workers
    job = run(client, db.one("SELECT * FROM jobs WHERE type='library'"))
    run(client, workers.start(1))
    try:
        for _ in range(200):
            row = run(client, db.one("SELECT * FROM jobs WHERE id=?", (job["id"],)))
            if row["progress_bytes"] > 0:
                break
            time.sleep(0.01)
        assert row["progress_bytes"] > 0 and row["status"] == "running"
        response = client.post(f"/api/downloads/{job['id']}/cancel", json={"request_id": str(uuid4())})
        assert response.status_code == 200, response.text
        for _ in range(200):
            if job["id"] not in workers.active_downloads:
                break
            time.sleep(0.01)
        assert not list(client.app.state.library.root.glob("e621_*"))
        assert not list((client.app.state.library.root / ".hydra-studio-tmp").iterdir())
        assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "cancelled"
        response = client.post(f"/api/downloads/{job['id']}/retry", json={"request_id": str(uuid4())})
        assert response.status_code == 200, response.text
        for _ in range(300):
            if client.get(f"/api/jobs/{job['id']}").json()["status"] == "done":
                break
            time.sleep(0.02)
        assert client.get("/api/library/posts/999").json()["status"] == "saved"
        assert len(list(client.app.state.library.root.glob("e621_*"))) == 4
    finally:
        run(client, workers.close())
        run(client, client.app.state.media.client.aclose())
        client.app.state.media.client = old


def test_restart_recovers_journalled_partial_and_preserves_foreign_file(settings):
    app = create_app(settings, start_workers=False)
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        client.headers["X-CSRF-Token"] = client.post("/api/auth/login", json={"password": PASSWORD}).json()[
            "csrf_token"
        ]
        pid = create(client)
        client.get(f"/api/projects/{pid}/query")
        label(client, pid, 999, "unsure")
        save(client, pid)
        db = app.state.db
        job = run(client, db.one("SELECT * FROM jobs WHERE type='library'"))
        stage = settings.library_root / ".hydra-studio-tmp" / uuid4().hex
        stage.mkdir()
        partial = stage / "original.part"
        partial.write_bytes(b"owned interrupted transfer")
        foreign = settings.library_root / "manual.part"
        foreign.write_bytes(b"manual data")

        async def crash():
            async with db.transaction() as conn:
                await conn.execute("UPDATE jobs SET status='running',lease_until=0 WHERE id=?", (job["id"],))
                await conn.execute(
                    "INSERT INTO download_partials VALUES(?,?,?,?)",
                    (job["id"], str(partial.absolute()), partial.stat().st_ino, partial.stat().st_dev),
                )

        run(client, crash())
    with TestClient(create_app(settings), headers={"Origin": "http://testserver"}) as client:
        client.headers["X-CSRF-Token"] = client.post("/api/auth/login", json={"password": PASSWORD}).json()[
            "csrf_token"
        ]
        for _ in range(300):
            if client.get("/api/library/posts/999").json()["status"] == "saved":
                break
            time.sleep(0.02)
        assert client.get("/api/library/posts/999").json()["status"] == "saved"
        assert not partial.exists() and not stage.exists()
        assert foreign.read_bytes() == b"manual data"
        assert client.get(f"/api/projects/{pid}/candidates/999").json()["state"] == "unsure"
        assert client.get(f"/api/projects/{pid}/candidates").json()["total"] == 1


def test_export_publication_recovers_after_link_before_ack(client, monkeypatch):
    pid = create(client)
    client.get(f"/api/projects/{pid}/query")
    label(client, pid, 999, "positive")
    run(client, client.app.state.media.download_preview(999))
    did = client.post(f"/api/projects/{pid}/datasets", json={"request_id": str(uuid4()), "name": "recovery"}).json()[
        "id"
    ]
    client.post(f"/api/datasets/{did}/media", json={"request_id": str(uuid4())})
    downloads, db = client.app.state.downloads, client.app.state.db
    job = run(client, db.one("SELECT * FROM jobs WHERE type='dataset_media'"))
    real = downloads.publish

    def interrupted(partial, destination, record):
        os.link(partial, destination)
        raise OSError("crash before acknowledgement")

    monkeypatch.setattr(downloads, "publish", interrupted)
    with pytest.raises(OSError):
        run(client, downloads.process(job, json.loads(job["payload_json"])))
    assert client.get(f"/api/datasets/{did}/media/manifest").status_code == 409
    monkeypatch.setattr(downloads, "publish", real)
    job = run(client, db.one("SELECT * FROM jobs WHERE id=?", (job["id"],)))
    run(client, downloads.process(job, json.loads(job["payload_json"])))
    assert client.get(f"/api/datasets/{did}/media/manifest").status_code == 200
    assert len(list((db.root / "exports" / did).iterdir())) == 1


def test_deleted_upstream_keeps_review_but_cannot_save_media(client, monkeypatch):
    pid = create(client)
    client.get(f"/api/projects/{pid}/query")
    label(client, pid, 999, "positive")
    real = client.app.state.source.request

    async def deleted(path, params=None, background=False):
        if path == "/posts/999.json":
            raise HTTPException(404, "Deleted")
        return await real(path, params, background=background)

    monkeypatch.setattr(client.app.state.source, "request", deleted)
    result = client.get(f"/api/projects/{pid}/candidates/999")
    assert result.status_code == 200 and result.json()["state"] == "positive"
    assert result.json()["unavailable"] and not result.json()["has_original"]
    assert client.get("/api/media/999/original").status_code == 404
    assert client.get(f"/api/projects/{pid}/candidates").json()["total"] == 1


async def test_migration_preserves_conflicting_duplicate_reviews_and_events(tmp_path):
    with sqlite3.connect(tmp_path / "studio.sqlite3") as conn:
        conn.executescript(SCHEMA + SCHEMA_V2 + "PRAGMA user_version=2;")
        conn.execute("INSERT INTO projects VALUES(1,'e621',1,'dragon','dragon',1)")
        conn.execute("INSERT INTO projects VALUES(2,'e621',1,'dragon','dragon',2)")
        conn.execute(
            "INSERT INTO posts VALUES(999,'s','md5','{}',0,NULL,'png',256,256,'https://static1.e621.net/p',NULL,'https://static1.e621.net/f',NULL,0,0)"
        )
        for pid, state in ((1, "positive"), (2, "negative")):
            conn.execute("INSERT INTO project_candidates VALUES(?,999,1,0)", (pid,))
            conn.execute("INSERT INTO labels VALUES(?,999,?,0)", (pid, state))
            conn.execute(
                "INSERT INTO label_events(project_id,post_id,new_state,version,reviewed_at,source) VALUES(?,999,?,1,0,'review')",
                (pid, state),
            )
    db = Database(tmp_path)
    try:
        await db.open()
        assert len(await db.rows("SELECT * FROM labels")) == len(await db.rows("SELECT * FROM label_events")) == 2
        assert len(await db.rows("SELECT * FROM canonical_projects")) == 1
        assert (await db.one("SELECT * FROM project_conflicts"))["states"] == 2
        from backend.app.datasets import snapshot
        from backend.app.quality import eligible
        from backend.app.reviews import edit_labels

        async with db.transaction() as conn:
            project, rows = await snapshot(conn, 2)
        assert project["id"] == 1 and len(rows) == 1 and not eligible(rows[0])
        with pytest.raises(HTTPException, match="409"):
            await edit_labels(db, 1, uuid4(), [{"post_id": 999, "state": "unsure", "expected_version": 1}])
        assert {r["state"] for r in await db.rows("SELECT * FROM labels")} == {"positive", "negative"}
        assert not await db.rows("PRAGMA foreign_key_check")
        assert len(list((tmp_path / "backups").glob("pre-schema-v4-*"))) == 1
    finally:
        await db.close()


def test_truncated_original_never_publishes_and_can_retry(client):
    pid = create(client)
    client.get(f"/api/projects/{pid}/query")
    save(client, pid)
    library, db = client.app.state.library, client.app.state.db
    job = run(client, db.one("SELECT * FROM jobs WHERE type='library'"))
    old = client.app.state.media.client
    client.app.state.media.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "image/png", "Content-Length": "100"}, stream=httpx.ByteStream(b"short")
            )
        )
    )
    try:
        with pytest.raises(HTTPException, match="502"):
            run(client, library.process(job, json.loads(job["payload_json"])))
        assert not list(library.root.glob("e621_*"))
        assert not list((library.root / ".hydra-studio-tmp").iterdir())
    finally:
        run(client, client.app.state.media.client.aclose())
        client.app.state.media.client = old
    run(client, library.process(job, json.loads(job["payload_json"])))
    assert client.get("/api/library/posts/999").json()["status"] == "saved"


def test_save_pins_current_view_without_rewriting_review_snapshot(client, monkeypatch):
    pid = create(client)
    client.get(f"/api/projects/{pid}/query")
    label(client, pid, 999, "positive")
    db = client.app.state.db
    before = run(client, db.one("SELECT * FROM posts WHERE post_id=999"))
    changed = post(998)
    changed["id"] = 999
    real = client.app.state.source.request

    async def source(path, params=None, background=False):
        if path == "/posts/999.json":
            return {"post": changed}
        return await real(path, params, background=background)

    monkeypatch.setattr(client.app.state.source, "request", source)
    view = client.get(f"/api/projects/{pid}/candidates/999").json()
    assert view["md5"] != before["md5"]
    path = f"/api/projects/{pid}/library/999"
    payload = {"request_id": str(uuid4()), "wanted": True, "expected_version": 0, "source_md5": before["md5"]}
    assert client.put(path, json=payload).status_code == 409
    payload.update(request_id=str(uuid4()), source_md5=view["md5"])
    assert client.put(path, json=payload).status_code == 200
    job = run(client, db.one("SELECT * FROM jobs WHERE type='library'"))
    run(client, client.app.state.library.process(job, json.loads(job["payload_json"])))
    assert client.get("/api/library/posts/999").json()["status"] == "saved"
    assert run(client, db.one("SELECT * FROM posts WHERE post_id=999")) == before


def test_restart_cleans_owned_original_and_sidecars_before_publication_journal(client, monkeypatch):
    pid = create(client)
    client.get(f"/api/projects/{pid}/query")
    save(client, pid)
    library, db = client.app.state.library, client.app.state.db
    job = run(client, db.one("SELECT * FROM jobs WHERE type='library'"))
    write, clean = library.write_staged_file, library.clean_staging

    async def interrupted(*args):
        await write(*args)
        raise RuntimeError("simulated hard interruption before publication reservation")

    async def no_cleanup(*_):
        pass

    monkeypatch.setattr(library, "write_staged_file", interrupted)
    monkeypatch.setattr(library, "clean_staging", no_cleanup)
    with pytest.raises(RuntimeError):
        run(client, library.process(job, json.loads(job["payload_json"])))
    stages = list((library.root / ".hydra-studio-tmp").iterdir())
    assert len(stages) == 1 and len(list(stages[0].iterdir())) == 2
    assert run(client, db.one("SELECT record_json FROM saved_media WHERE post_id=999"))["record_json"] is None
    monkeypatch.setattr(library, "write_staged_file", write)
    monkeypatch.setattr(library, "clean_staging", clean)
    run(client, library.recover_staging())
    assert not list((library.root / ".hydra-studio-tmp").iterdir())
    assert not run(client, db.rows("SELECT * FROM library_staging"))
    run(client, library.process(job, json.loads(job["payload_json"])))
    assert client.get("/api/library/posts/999").json()["status"] == "saved"


def test_recovery_preserves_replaced_staging_file_and_its_ownership_journal(client, monkeypatch):
    pid = create(client)
    client.get(f"/api/projects/{pid}/query")
    save(client, pid)
    library, db = client.app.state.library, client.app.state.db
    job = run(client, db.one("SELECT * FROM jobs WHERE type='library'"))
    write, clean = library.write_staged_file, library.clean_staging

    async def interrupted(*args):
        await write(*args)
        raise RuntimeError("crash")

    async def no_cleanup(*_):
        pass

    monkeypatch.setattr(library, "write_staged_file", interrupted)
    monkeypatch.setattr(library, "clean_staging", no_cleanup)
    with pytest.raises(RuntimeError):
        run(client, library.process(job, json.loads(job["payload_json"])))
    stage = next((library.root / ".hydra-studio-tmp").iterdir())
    sidecar = next(stage.glob("*.json"))
    replacement = stage / "replacement"
    replacement.write_bytes(b"manually replaced metadata")
    os.replace(replacement, sidecar)
    monkeypatch.setattr(library, "clean_staging", clean)
    run(client, client.app.state.downloads.recover_partials(library))
    assert sidecar.read_bytes() == b"manually replaced metadata"
    assert len(list(stage.iterdir())) == 2
    assert run(client, db.one("SELECT status FROM jobs WHERE id=?", (job["id"],)))["status"] == "failed"
    assert run(client, db.one("SELECT * FROM download_partials WHERE job_id=?", (job["id"],)))
    assert run(client, db.one("SELECT * FROM library_staging WHERE job_id=?", (job["id"],)))


def test_cross_project_review_cannot_replace_an_earlier_review_source(client, monkeypatch):
    pid = create(client)
    client.get(f"/api/projects/{pid}/query")
    label(client, pid, 999, "positive")
    db = client.app.state.db
    before = run(client, db.one("SELECT * FROM posts WHERE post_id=999"))
    other = client.post(
        "/api/projects",
        json={"request_id": str(uuid4()), "target_kind": "custom", "name": "another_target", "query": "dragon"},
    ).json()["id"]
    changed = post(998)
    changed["id"] = 999
    client.app.state.feed.remember(changed, "dragon", "query", other)
    response = client.patch(
        f"/api/projects/{other}/labels/999",
        json={"request_id": str(uuid4()), "expected_version": 0, "state": "negative"},
    )
    assert response.status_code == 409
    assert run(client, db.one("SELECT * FROM posts WHERE post_id=999")) == before
    assert client.get(f"/api/projects/{pid}/candidates/999").json()["state"] == "positive"
    assert client.get(f"/api/projects/{other}/candidates").json()["total"] == 0


def test_media_and_existing_review_are_bound_to_displayed_identity(client):
    pid = create(client)
    client.get(f"/api/projects/{pid}/query")
    label(client, pid, 999, "positive")
    changed = post(998)
    changed["id"] = 999
    client.app.state.feed.remember(changed, "dragon", "query", pid)
    original = post(999)["file"]["md5"]
    assert client.get(f"/api/media/999/preview?source_md5={original}").status_code == 409
    assert client.get(f"/api/media/999/original?source_md5={original}").status_code == 409
    response = client.patch(
        f"/api/projects/{pid}/labels/999",
        json={
            "request_id": str(uuid4()),
            "expected_version": 1,
            "state": "negative",
            "source_md5": changed["file"]["md5"],
        },
    )
    assert response.status_code == 409
    assert (
        client.patch(
            f"/api/projects/{pid}/labels/999", json={"request_id": str(uuid4()), "expected_version": 1, "state": None}
        ).status_code
        == 200
    )


def test_first_human_review_refreshes_unreviewed_legacy_metadata(client):
    from backend.app.jobs import persist_post

    pid = create(client)
    client.get(f"/api/projects/{pid}/query")
    db = client.app.state.db
    old = post(999)
    old["file"]["md5"] = "0" * 32

    async def legacy():
        async with db.transaction() as conn:
            await persist_post(conn, old)
            await conn.execute("INSERT INTO project_candidates VALUES(?,999,0,0)", (pid,))

    run(client, legacy())
    label(client, pid, 999, "positive")
    assert run(client, db.one("SELECT md5 FROM posts WHERE post_id=999"))["md5"] == post(999)["file"]["md5"]
