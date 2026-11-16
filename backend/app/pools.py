"""Bulk favourites share the versioned library queue with individual saves."""

import json

from fastapi import APIRouter, Depends, Request

from . import auth, schemas
from .jobs import queue_album
from .library import ACTIVE_SAVE_STATUSES, PositiveId, queue_intent

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_session)])


@router.get("/pools/{pool_id}", response_model=schemas.Pool)
async def pool_detail(pool_id: PositiveId, request: Request):
    return await request.app.state.source.pool(pool_id)


@router.post("/pools/{pool_id}/favorite", response_model=schemas.PoolFavoriteResult)
async def favorite_pool(pool_id: PositiveId, request: Request):
    pool = await request.app.state.source.pool(pool_id)
    library = request.app.state.library
    await library.prepare_root()
    saved = 0
    async with library.db.transaction() as conn:
        for position, post_id in enumerate(pool["post_ids"], start=1):
            # Pool metadata contains only IDs. The worker replaces this expired
            # placeholder with checksummed metadata before publishing any file.
            await conn.execute(
                "INSERT OR IGNORE INTO posts(post_id,tags_json,score,fetched_at) VALUES(?,'{}',0,0)", (post_id,)
            )
            old = await (await conn.execute(
                "SELECT m.version,m.wanted,m.status,j.payload_json FROM saved_media m LEFT JOIN jobs j "
                "ON j.job_key='library:' || m.post_id || ':' || m.version WHERE m.post_id=?", (post_id,)
            )).fetchone()
            previous = json.loads(old["payload_json"]) if old and old["wanted"] and old["payload_json"] else {}
            albums = previous.get("albums", {})
            if str(pool_id) in albums and old["status"] in ACTIVE_SAVE_STATUSES:
                saved += old["status"] == "saved"
                continue
            albums[str(pool_id)] = position
            context = {"albums": albums, "display": {"search_tag": pool["name"]}}
            if previous.get("project_id"):
                context["project_id"] = previous["project_id"]
            await queue_intent(conn, post_id, old["version"] + 1 if old else 1, True,
                               source=previous.get("source"), context=context)
        if pool["post_ids"] and library.settings.stash_url:
            await queue_album(conn, pool_id, pool["name"])
    total = len(pool["post_ids"])
    return {"total": total, "queued": total - saved, "saved": saved}
