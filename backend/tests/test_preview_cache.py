import asyncio

import httpx

from backend.app.db import Database
from backend.app.feed import post_row
from backend.app.media import Media
from backend.app.mock import artwork, post, transport


async def test_preview_coalesces_reuses_identity_and_evicts_by_bytes(settings):
    db = Database(settings.data_root)
    await db.open()
    calls = []

    async def respond(request):
        calls.append(request.url.path)
        await asyncio.sleep(0.01)
        return transport(request)

    media = Media(settings, db, httpx.MockTransport(respond))
    first, second = post_row(post(1)), post_row(post(2))
    try:
        results = await asyncio.gather(*(media.thumbnail_bytes(first) for _ in range(8)))
        assert len(calls) == 1 and all(raw == results[0] for raw in results)
        assert await media.thumbnail_bytes(first) == results[0]
        assert len(calls) == 1 and media.metrics["hits"] == 1 and media.metrics["misses"] == 1
        settings.transient_preview_bytes = len(results[0]) + len(media._thumbnail(artwork(2, preview=True))) - 1
        await media.thumbnail_bytes(second)
        assert media.preview_bytes <= settings.transient_preview_bytes
        await media.thumbnail_bytes(first)
        assert len(calls) == 3
        await media.thumbnail_bytes({**first, "md5": "different"})
        assert len(calls) == 4
        assert not media.cache_root.exists()
        assert not await db.rows("SELECT * FROM image_cache")
        timings = media.timings.snapshot()
        assert timings["preview_fetch_ms"]["samples"] == 4
        assert timings["thumbnail_conversion_ms"]["samples"] == 4
    finally:
        await media.close()
        await db.close()


async def test_zero_budget_never_retains_previews(settings):
    settings.transient_preview_bytes = 0
    db = Database(settings.data_root)
    await db.open()
    media = Media(settings, db, httpx.MockTransport(transport))
    try:
        await media.thumbnail_bytes(post_row(post(1)))
        assert not media.previews and media.preview_bytes == 0
    finally:
        await media.close()
        await db.close()
