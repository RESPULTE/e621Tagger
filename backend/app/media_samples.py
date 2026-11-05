"""Source-bound still samples, independent decisions, and bounded animation decoding."""

import asyncio
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import auth
from .feed import post_row
from .jobs import persist_post
from .media_sampling import animation_bytes, checked_post, decode_animation, insert_sample, sample_path, store_image
from .projects import require_active_project, require_project
from .reference_images import read_upload
from .reviews import fingerprint, receipt, save_receipt, transition

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_session)])
MAX_SAMPLES = 256


class Sample(BaseModel):
    id: str
    project_id: int
    post_id: int
    source_md5: str
    kind: Literal["thumbnail", "frame"]
    timestamp_ms: int | None
    image_url: str
    state: Literal["positive", "negative"] | None
    favorite: bool
    version: int


class SampleEdit(BaseModel):
    request_id: UUID
    expected_version: int = Field(ge=0)
    state: Literal["positive", "negative"] | None = None
    favorite: bool | None = None


def public_sample(row):
    return {key: row[key] for key in ("id", "project_id", "post_id", "source_md5", "kind", "state", "version")} | {
        "timestamp_ms": row["timestamp_ms"] if row["kind"] == "frame" else None,
        "favorite": bool(row["favorite"]),
        "image_url": f"/api/projects/{row['project_id']}/posts/{row['post_id']}/samples/{row['id']}/image",
    }


@router.get("/projects/{project_id}/posts/{post_id}/samples", response_model=list[Sample])
async def list_samples(project_id: int, post_id: int, request: Request):
    project = await require_project(request.app.state.db, project_id)
    post = await request.app.state.feed.raw(post_id)
    rows = await request.app.state.db.rows(
        "SELECT * FROM media_samples WHERE project_id=? AND post_id=? AND source_md5=? ORDER BY timestamp_ms,id",
        (project["id"], post_id, (post.get("file") or {}).get("md5")),
    )
    return [public_sample(row) for row in rows]


@router.post("/projects/{project_id}/posts/{post_id}/samples", response_model=Sample)
async def create_sample(
    project_id: int,
    post_id: int,
    request: Request,
    request_id: UUID,
    source_md5: str = Query(pattern=r"^[a-f0-9]{32}$"),
    kind: Literal["thumbnail", "frame"] = "frame",
    timestamp_ms: int | None = Query(default=None, ge=0, le=86_400_000),
):
    state = request.app.state
    project_id = (await require_project(state.db, project_id))["id"]
    if kind == "frame" and timestamp_ms is None:
        raise HTTPException(422, "A frame needs a playback position")
    position = timestamp_ms if kind == "frame" else -1
    raw, digest = await read_upload(request)
    signature = fingerprint(["sample", project_id, post_id], [source_md5, kind, position, digest])
    async with state.db.transaction() as conn:
        existing = await receipt(conn, request_id, signature)
    if existing is not None:
        return existing
    post = await checked_post(state, post_id, source_md5)
    if kind == "thumbnail":
        # A thumbnail is always the app's actual preview, never arbitrary upload bytes.
        raw = await state.media.thumbnail_bytes(post_row(post))
    async with state.db.transaction() as conn:
        await require_active_project(conn, project_id)
        existing = await receipt(conn, request_id, signature)
        if existing is not None:
            return existing
        prior = await (await conn.execute("SELECT md5 FROM posts WHERE post_id=?", (post_id,))).fetchone()
        if prior and prior["md5"] != source_md5:
            raise HTTPException(409, "Stored source identity changed; existing samples were preserved")
        count = await (
            await conn.execute(
                "SELECT count(*) FROM media_samples WHERE project_id=? AND post_id=? AND kind='frame'",
                (project_id, post_id),
            )
        ).fetchone()
        found = await (
            await conn.execute(
                "SELECT * FROM media_samples WHERE project_id=? AND post_id=? AND source_md5=? AND kind=? AND timestamp_ms=?",
                (project_id, post_id, source_md5, kind, position),
            )
        ).fetchone()
        if found:
            result = public_sample(dict(found))
            await save_receipt(conn, request_id, signature, result)
            return result
        if kind == "frame" and count[0] >= MAX_SAMPLES:
            raise HTTPException(413, "This artwork already has 256 saved samples")
        # Serialize publication after identity/cap checks. No network I/O occurs
        # in this transaction, and duplicate timestamps never create orphan files.
        image = await asyncio.to_thread(store_image, state.db.root, raw, state.settings.reference_max_pixels)
        await persist_post(conn, post)
        await conn.execute(
            "INSERT OR IGNORE INTO project_candidates(project_id,post_id,first_seen_at) VALUES(?,?,?)",
            (project_id, post_id, time.time()),
        )
        row = await insert_sample(conn, project_id, post_id, source_md5, kind, position, image)
        if kind == "thumbnail":
            label = await (
                await conn.execute(
                    "SELECT l.state FROM canonical_candidates c LEFT JOIN active_labels l ON l.project_id=c.owner_project_id AND l.post_id=c.post_id WHERE c.project_id=? AND c.post_id=?",
                    (project_id, post_id),
                )
            ).fetchone()
            await conn.execute(
                "UPDATE media_samples SET state=? WHERE id=?", (label["state"] if label else None, row["id"])
            )
            row["state"] = label["state"] if label else None
        result = public_sample(row)
        await save_receipt(conn, request_id, signature, result)
    return result


async def sample_row(db, project_id, post_id, sample_id):
    row = await db.one(
        "SELECT s.* FROM media_samples s JOIN posts p ON p.post_id=s.post_id AND p.md5=s.source_md5 WHERE s.id=? AND s.project_id=? AND s.post_id=?",
        (sample_id, project_id, post_id),
    )
    if not row:
        raise HTTPException(404, "Sample is unavailable or its source changed")
    return row


@router.get("/projects/{project_id}/posts/{post_id}/samples/{sample_id}/image")
async def sample_image(project_id: int, post_id: int, sample_id: UUID, request: Request):
    db = request.app.state.db
    project_id = (await require_project(db, project_id))["id"]
    row = await sample_row(db, project_id, post_id, str(sample_id))
    return FileResponse(sample_path(db, row), media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.patch("/projects/{project_id}/posts/{post_id}/samples/{sample_id}", response_model=Sample)
async def edit_sample(project_id: int, post_id: int, sample_id: UUID, body: SampleEdit, request: Request):
    async with request.app.state.library.operation_locks[post_id % 64]:
        return await _edit_sample(project_id, post_id, sample_id, body, request)


async def _edit_sample(project_id, post_id, sample_id, body, request):
    # Separate intents allow a favourite to merge with a concurrent thumbnail
    # decision without overwriting it after collection filesystem publication.
    if {"state", "favorite"}.issubset(body.model_fields_set):
        raise HTTPException(422, "Update the decision and favourite in separate requests")
    state = request.app.state
    project_id = (await require_project(state.db, project_id))["id"]
    signature = fingerprint(
        ["sample-edit", project_id, post_id, str(sample_id)], body.model_dump(mode="json", exclude_unset=True)
    )
    async with state.db.transaction() as conn:
        existing = await receipt(conn, body.request_id, signature)
    if existing is not None:
        return existing
    row = await sample_row(state.db, project_id, post_id, str(sample_id))
    await checked_post(state, post_id, row["source_md5"])
    if row["version"] != body.expected_version:
        raise HTTPException(409, "This sample changed in another tab; reload before saving")
    if "favorite" in body.model_fields_set:
        if body.favorite is None:
            raise HTTPException(422, "Favorite must be true or false")
        from .sample_library import change_favorite

        await change_favorite(state, row, body.favorite)
    async with state.db.transaction() as conn:
        await require_active_project(conn, project_id)
        existing = await receipt(conn, body.request_id, signature)
        if existing is not None:
            return existing
        row = dict(await (await conn.execute("SELECT * FROM media_samples WHERE id=?", (str(sample_id),))).fetchone())
        if row["version"] != body.expected_version and "favorite" not in body.model_fields_set:
            raise HTTPException(409, "This sample changed in another tab; reload before saving")
        # Other sample mutations share the operation lock. Only an ordinary
        # thumbnail label can advance the version during favourite I/O; preserve
        # that newer decision and acknowledge the completed independent save.
        if "state" in body.model_fields_set:
            row["state"] = body.state
            if row["kind"] == "thumbnail":
                parent = await (
                    await conn.execute(
                        "SELECT review_version FROM canonical_candidates WHERE project_id=? AND post_id=?",
                        (project_id, post_id),
                    )
                ).fetchone()
                await transition(
                    conn,
                    project_id,
                    post_id,
                    parent["review_version"],
                    body.state,
                    source="sample-thumbnail",
                    source_md5=row["source_md5"],
                )
        if "favorite" in body.model_fields_set:
            if body.favorite is None:
                raise HTTPException(422, "Favorite must be true or false")
            row["favorite"] = int(body.favorite)
        row["version"] += 1
        await conn.execute(
            "UPDATE media_samples SET state=?,favorite=?,version=?,updated_at=? WHERE id=?",
            (row["state"], row["favorite"], row["version"], time.time(), row["id"]),
        )
        await conn.execute(
            "INSERT INTO media_sample_events(sample_id,version,state,favorite,created_at) VALUES(?,?,?,?,?)",
            (row["id"], row["version"], row["state"], row["favorite"], time.time()),
        )
        result = public_sample(row)
        await save_receipt(conn, body.request_id, signature, result)
    return result


@router.get("/posts/{post_id}/animation")
async def animation_info(post_id: int, request: Request, source_md5: str = Query(pattern=r"^[a-f0-9]{32}$")):
    raw = await animation_bytes(request.app.state, post_id, source_md5)
    async with request.app.state.media.animation_decode_slots:
        return await asyncio.to_thread(decode_animation, raw)


@router.get("/posts/{post_id}/animation/frames/{index}")
async def animation_frame(
    post_id: int, index: int, request: Request, source_md5: str = Query(pattern=r"^[a-f0-9]{32}$")
):
    raw = await animation_bytes(request.app.state, post_id, source_md5)
    async with request.app.state.media.animation_decode_slots:
        png = await asyncio.to_thread(decode_animation, raw, index)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})
