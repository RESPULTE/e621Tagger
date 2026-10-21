import asyncio
import time

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.app.db import Database
from backend.app.e621 import E621, retry_delay
from backend.app.jobs import enqueue, persist_post
from backend.app.media import Media
from backend.app.mock import post, transport


async def test_scheduler_coalescing_and_global_spacing(settings):
    db = Database(settings.data_root)
    await db.open()
    starts = []

    def respond(request):
        starts.append(time.monotonic())
        return httpx.Response(200, json={"ok": True})

    source = E621(settings, db, httpx.MockTransport(respond))
    source.start()
    try:
        results = await asyncio.gather(
            *[source.request("/same.json") for _ in range(10)], source.request("/different.json", background=True)
        )
        assert len(results) == 11 and len(starts) == 2
        assert starts[1] - starts[0] >= 0.98
    finally:
        await source.close()
        await db.close()


async def test_scheduler_retry_after_and_cancellation(settings):
    db = Database(settings.data_root)
    await db.open()
    starts = []

    def respond(request):
        starts.append(time.monotonic())
        return httpx.Response(503, headers={"Retry-After": "1"}) if len(starts) == 1 else httpx.Response(200, json=[])

    source = E621(settings, db, httpx.MockTransport(respond))
    source.start()
    try:
        cancelled = asyncio.create_task(source.request("/tags.json"))
        await asyncio.sleep(0.05)
        survivor = asyncio.create_task(source.request("/tags.json"))
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        assert await survivor == []
        assert starts[1] - starts[0] >= 0.98
        assert source.metrics["throttled"] == 1
        assert retry_delay("2", 0) == 2
    finally:
        await source.close()
        await db.close()


async def test_preview_cache_integrity_quota_and_backpressure(settings):
    db = Database(settings.data_root)
    await db.open()
    media = Media(settings, db, httpx.MockTransport(transport))
    try:
        async with db.transaction() as conn:
            for i in range(1, 81):
                await persist_post(conn, post(i))
        assert len(await media.schedule(list(range(1, 81)))) == 64
        assert len(await media.schedule(list(range(1, 81)))) <= 64
        jobs = await db.one("SELECT count(*) AS n FROM jobs WHERE status='pending'")
        assert jobs["n"] == 64
        await media.download_preview(1)
        cached = await media.cached_preview(1)
        assert cached and cached[1].is_file()
        before = media.metrics["bytes"]
        await media.download_preview(1)
        assert media.metrics["bytes"] == before
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
        result = await media.preview_response(request, 1)
        assert result.status_code == 200 and result.headers.get("etag")
        cached[1].write_bytes(b"x" * cached[0]["bytes"])
        assert (await media.preview_response(request, 1)).status_code == 202
        assert await db.one("SELECT * FROM image_cache WHERE post_id=1") is None
        assert not list(media.cache_root.glob("*.part"))
    finally:
        await media.close()
        await db.close()


async def test_range_original_and_safe_redirects(settings):
    db = Database(settings.data_root)
    await db.open()
    media = Media(settings, db, httpx.MockTransport(transport))
    async with db.transaction() as conn:
        await persist_post(conn, post(1))
    try:
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"range", b"bytes=0-15")]})
        response = await media.stream_response(request, 1, "original")
        assert response.status_code == 206 and response.headers["content-range"].startswith("bytes 0-15/")
        data = b"".join([chunk async for chunk in response.body_iterator])
        assert len(data) == 16 and data.startswith(b"\x89PNG")
        assert media.metrics["active"] == 0
        for url in (
            "http://static1.e621.net/a",
            "https://evil.example/a",
            "https://static1.e621.net@127.0.0.1/a",
            "https://static1.e621.net:8443/a",
        ):
            with pytest.raises(HTTPException):
                media.validate_url(url)
        media.client._transport = httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
        )
        with pytest.raises(HTTPException) as exc:
            await media._open("https://static1.e621.net/a")
        assert exc.value.status_code == 403
    finally:
        await media.close()
        await db.close()


async def test_disk_full_never_marks_cache_valid(settings, monkeypatch):
    db = Database(settings.data_root)
    await db.open()
    media = Media(settings, db, httpx.MockTransport(transport))
    async with db.transaction() as conn:
        await persist_post(conn, post(1))
    from pathlib import Path

    original = Path.write_bytes

    def fail(path, body):
        if path.suffix == ".part":
            raise OSError("No space left on device")
        return original(path, body)

    monkeypatch.setattr(Path, "write_bytes", fail)
    try:
        with pytest.raises(OSError):
            await media.download_preview(1)
        assert await db.one("SELECT * FROM image_cache") is None
        assert not list(media.cache_root.glob("*.part"))
    finally:
        await media.close()
        await db.close()


async def test_failed_cache_registration_removes_completed_file(settings, monkeypatch):
    import sqlite3

    db = Database(settings.data_root)
    await db.open()
    media = Media(settings, db, httpx.MockTransport(transport))
    async with db.transaction() as conn:
        await persist_post(conn, post(1))
    execute = db.connection.execute

    async def fail_registration(sql, parameters=()):
        if sql.startswith("INSERT OR REPLACE INTO image_cache"):
            raise sqlite3.OperationalError("database or disk is full")
        return await execute(sql, parameters)

    monkeypatch.setattr(db.connection, "execute", fail_registration)
    try:
        with pytest.raises(sqlite3.OperationalError):
            await media.download_preview(1)
        assert await db.one("SELECT * FROM image_cache") is None
        assert not list(media.cache_root.iterdir())
    finally:
        await media.close()
        await db.close()


async def test_expired_lease_and_single_process_guard(settings):
    db = Database(settings.data_root)
    await db.open()
    from backend.app.jobs import Workers

    try:
        other = Database(settings.data_root)
        with pytest.raises(RuntimeError):
            await other.open()
        async with db.transaction() as conn:
            job_id = await enqueue(conn, "preview", "preview:1", {"post_id": 1})
            await conn.execute("UPDATE jobs SET status='running',lease_until=? WHERE id=?", (time.time() - 1, job_id))
        worker = Workers(db, None, None)
        claimed = await worker._claim("preview")
        assert claimed["id"] == job_id
        assert (await db.one("SELECT * FROM jobs WHERE id=?", (job_id,)))["attempts"] == 1
    finally:
        await db.close()


async def test_media_concurrency_and_cancelled_stream_cleanup(settings):
    db = Database(settings.data_root)
    await db.open()
    media = Media(settings, db, httpx.MockTransport(transport))
    try:
        async with db.transaction() as conn:
            for i in range(1, 11):
                await persist_post(conn, post(i))
        await asyncio.gather(*(media.download_preview(i) for i in range(1, 11)))
        assert 1 <= media.metrics["peak_active"] <= settings.media_concurrency
        assert media.metrics["active"] == 0
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
        response = await media.stream_response(request, 10, "original")
        assert response.headers["content-type"] == "video/webm"
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        assert media.metrics["active"] == 0
    finally:
        await media.close()
        await db.close()


@pytest.mark.parametrize("status", [403, 404, 429, 503])
async def test_unavailable_media_releases_permits(settings, status):
    db = Database(settings.data_root)
    await db.open()
    media = Media(settings, db, httpx.MockTransport(lambda request: httpx.Response(status)))
    async with db.transaction() as conn:
        await persist_post(conn, post(1))
    try:
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
        with pytest.raises(HTTPException) as exc:
            await media.stream_response(request, 1, "original")
        assert exc.value.status_code == status
        assert media.metrics["active"] == 0
        assert await db.one("SELECT * FROM image_cache") is None
    finally:
        await media.close()
        await db.close()


async def test_null_urls_corrupt_download_and_cache_eviction(settings):
    db = Database(settings.data_root)
    await db.open()
    media = Media(
        settings,
        db,
        httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"not an image", headers={"Content-Type": "image/png"})
        ),
    )
    async with db.transaction() as conn:
        await persist_post(conn, {"id": 1, "file": None, "sample": None, "preview": None})
        await persist_post(conn, post(2))
    try:
        assert await media.schedule([1]) == []
        with pytest.raises(HTTPException) as exc:
            await media.download_preview(2)
        assert exc.value.status_code == 415
        assert await db.one("SELECT * FROM image_cache") is None
        assert not list(media.cache_root.glob("*.part"))
        # The hard quota must be obeyed even if reviewed entries need eviction.
        settings.cache_bytes = 1024 * 1024
        path = media.cache_root / "2.webp"
        path.write_bytes(b"x" * 700_000)
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO image_cache VALUES(2,'preview','cache/2.webp',700000,'test',0)")
        await media._prune(700_000)
        assert not path.exists()
        assert await db.one("SELECT * FROM image_cache") is None
    finally:
        await media.close()
        await db.close()


async def test_approved_alias_processing_status(settings):
    db = Database(settings.data_root)
    await db.open()
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path == "/tag_aliases.json":
            return httpx.Response(
                200,
                json=[{"antecedent_name": "old_dragon", "consequent_name": "dragon", "status": "processing"}]
                if request.url.params["search[antecedent_name]"] == "old_dragon"
                else [],
            )
        return httpx.Response(200, json=[{"id": 1, "name": "dragon", "category": 5, "post_count": 123}])

    source = E621(settings, db, httpx.MockTransport(respond))
    source.start()
    try:
        resolved = await source.canonical_tag("old_dragon")
        assert resolved["canonical_name"] == "dragon" and resolved["matched_alias"] == "old_dragon"
        assert seen[0].url.params["search[status]"] == "approved"
    finally:
        await source.close()
        await db.close()
