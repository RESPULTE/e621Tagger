import json
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from . import auth, schemas
from .jobs import enqueue
from .reviews import edit_labels, fingerprint, receipt, save_receipt, undo_label

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_session)])
public = APIRouter(prefix="/api/auth")


@public.post("/login")
async def login(body: schemas.Login, request: Request, response: Response):
    token, csrf = await auth.login(request, body.password)
    return session_response(request, response, token, csrf)


@public.get("/options")
async def auth_options(request: Request):
    return {"password_required": not request.app.state.settings.passwordless}


@public.post("/connect")
async def connect(request: Request, response: Response):
    # Only an explicit private-network deployment may create sessions without a password.
    if not request.app.state.settings.passwordless:
        raise HTTPException(403, "Passwordless access is disabled")
    token, csrf = await auth.new_session(request)
    return session_response(request, response, token, csrf)


def session_response(request, response, token, csrf):
    settings = request.app.state.settings
    response.set_cookie(
        settings.session_cookie_name,
        token,
        httponly=True,
        secure=settings.secure_cookie,
        samesite="strict",
        max_age=settings.session_hours * 3600,
        path="/",
    )
    return {"csrf_token": csrf}


@router.get("/auth/session")
async def session(request: Request, session=Depends(auth.require_session)):
    return {"csrf_token": session["csrf_token"], "mock": request.app.state.settings.mock_e621}


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    cookie_name = request.app.state.settings.session_cookie_name
    async with request.app.state.db.transaction() as conn:
        await conn.execute(
            "DELETE FROM sessions WHERE token_hash=?", (auth.token_hash(request.cookies.get(cookie_name, "")),)
        )
    response.delete_cookie(cookie_name, path="/")
    return {"ok": True}


@router.get("/tags", response_model=list[schemas.TagResult])
async def tags(
    request: Request,
    q: str = Query(min_length=1, max_length=100, pattern=r"^[^\s\x00-\x1f*]+$"),
    category: int | None = Query(default=None, ge=0, le=8),
    minimum: int = Query(default=0, ge=0),
    exact: bool = False,
):
    return await request.app.state.source.tags(q.lower(), category, minimum, exact)


async def require_project(db, project_id):
    project = await db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def clean_query(query):
    query = query.strip()
    if any(part.startswith("order:") for part in query.split()):
        raise HTTPException(422, "Discovery uses descending post IDs; remove the order: term")
    return query


async def create_discovery(conn, project_id, query, source_kind, seed_ids=None):
    await conn.execute(
        "INSERT OR IGNORE INTO search_runs(project_id,query,source_kind) VALUES(?,?,?)",
        (project_id, query, source_kind),
    )
    row = await (
        await conn.execute(
            "SELECT * FROM search_runs WHERE project_id=? AND query=? AND source_kind=?",
            (project_id, query, source_kind),
        )
    ).fetchone()
    if row["exhausted"]:
        return {"job_id": None, "exhausted": True}
    payload = {"run_id": row["id"]}
    if seed_ids:
        payload["seed_ids"] = seed_ids
    job_id = await enqueue(conn, "discovery", f"discovery:{row['id']}:{row['cursor'] or 0}", payload)
    return {"job_id": job_id, "exhausted": False}


@router.post("/projects")
async def create_project(body: schemas.ProjectCreate, request: Request):
    db = request.app.state.db
    signature = fingerprint("project", body.model_dump(mode="json"))
    async with db.transaction() as conn:
        existing = await receipt(conn, body.request_id, signature)
        if existing is not None:
            return existing
    name = body.name.strip().lower()
    canonical = await request.app.state.source.canonical_tag(name) if body.target_kind == "e621" else None
    query = canonical["canonical_name"] if canonical else clean_query(body.query)
    async with db.transaction() as conn:
        existing = await receipt(conn, body.request_id, signature)
        if existing is not None:
            return existing
        if canonical:
            target_id, name = canonical["tag_id"], canonical["canonical_name"]
        else:
            await conn.execute(
                "INSERT OR IGNORE INTO custom_tags(name,description) VALUES(?,?)", (name, body.description)
            )
            row = await (await conn.execute("SELECT id FROM custom_tags WHERE name=?", (name,))).fetchone()
            target_id = row["id"]
        cursor = await conn.execute(
            "INSERT INTO projects(target_kind,target_id,display_name,query,created_at) VALUES(?,?,?,?,?)",
            (body.target_kind, target_id, name, query, time.time()),
        )
        project_id = cursor.lastrowid
        jobs = []
        if query:
            jobs.append(await create_discovery(conn, project_id, query, "target" if canonical else "query"))
        if body.seed_ids:
            jobs.append(
                await create_discovery(
                    conn,
                    project_id,
                    "seeds:" + ",".join(map(str, sorted(set(body.seed_ids)))),
                    "seed",
                    sorted(set(body.seed_ids)),
                )
            )
        result = {"id": project_id, "display_name": name, "jobs": jobs}
        await save_receipt(conn, body.request_id, signature, result)
        return result


@router.get("/projects")
async def list_projects(
    request: Request, cursor: int | None = Query(default=None, gt=0), limit: int = Query(default=50, ge=1, le=100)
):
    rows = await request.app.state.db.rows(
        "SELECT p.*, (SELECT count(*) FROM project_candidates c WHERE c.project_id=p.id) AS total "
        "FROM projects p WHERE (? IS NULL OR p.id<?) ORDER BY p.id DESC LIMIT ?",
        (cursor, cursor, limit + 1),
    )
    return {"items": rows[:limit], "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}


@router.get("/projects/{project_id}", response_model=schemas.ProjectResult)
async def project_detail(project_id: int, request: Request):
    db = request.app.state.db
    project = await require_project(db, project_id)
    rows = await db.rows(
        "SELECT COALESCE(l.state,'unreviewed') AS state,count(*) AS n FROM project_candidates c "
        "LEFT JOIN labels l USING(project_id,post_id) WHERE c.project_id=? GROUP BY l.state",
        (project_id,),
    )
    counts = {"positive": 0, "negative": 0, "unsure": 0, "unreviewed": 0}
    counts.update({row["state"]: row["n"] for row in rows})
    counts["total"] = sum(counts.values())
    searches = await db.rows("SELECT * FROM search_runs WHERE project_id=? ORDER BY id DESC", (project_id,))
    jobs = await db.rows(
        "SELECT j.id,j.status,j.error,j.result_json FROM jobs j JOIN search_runs s "
        "ON json_extract(j.payload_json,'$.run_id')=s.id WHERE j.type='discovery' AND s.project_id=? ORDER BY j.id DESC LIMIT 10",
        (project_id,),
    )
    return {**project, "counts": counts, "searches": searches, "jobs": jobs}


@router.post("/projects/{project_id}/discovery")
async def discover(project_id: int, body: schemas.DiscoveryCreate, request: Request):
    db = request.app.state.db
    project = await require_project(db, project_id)
    query = clean_query(body.query or project["query"])
    if not query:
        raise HTTPException(422, "Enter a discovery query for this seed-only project")
    source_kind = (
        body.source_kind if query != project["query"] else ("target" if project["target_kind"] == "e621" else "query")
    )
    if query != project["query"] and body.source_kind == "target":
        raise HTTPException(422, "Additional queries must use source_kind=query")
    signature = fingerprint(["discovery", project_id], body.model_dump(mode="json"))
    async with db.transaction() as conn:
        existing = await receipt(conn, body.request_id, signature)
        if existing is not None:
            return existing
        result = await create_discovery(conn, project_id, query, source_kind)
        await save_receipt(conn, body.request_id, signature, result)
        return result


def decode_candidate(row):
    return {
        "post_id": row["post_id"],
        "rating": row["rating"],
        "md5": row["md5"],
        "tags": json.loads(row["tags_json"]),
        "score": row["score"],
        "posted_at": row["posted_at"],
        "media_ext": row["media_ext"],
        "width": row["width"],
        "height": row["height"],
        "unavailable": bool(row["unavailable"]),
        "has_preview": bool(row["preview_url"]),
        "has_original": bool(row["file_url"]),
        "has_video_fallback": bool(row["video_url"]),
        "state": row["state"],
        "version": row["review_version"],
        "event_id": row["event_id"],
        "sources": json.loads(row["sources"]),
    }


CANDIDATE_SELECT = """SELECT p.*,c.review_version,l.state,
 (SELECT max(id) FROM label_events e WHERE e.project_id=c.project_id AND e.post_id=c.post_id) AS event_id,
 (SELECT json_group_array(DISTINCT s.source_kind || ': ' || s.query) FROM discoveries d JOIN search_runs s ON s.id=d.search_run_id
 WHERE d.project_id=c.project_id AND d.post_id=c.post_id) AS sources
 FROM project_candidates c JOIN posts p USING(post_id) LEFT JOIN labels l USING(project_id,post_id)"""


@router.get("/projects/{project_id}/candidates", response_model=schemas.CandidatePage)
async def candidates(
    project_id: int,
    request: Request,
    state: str = "all",
    cursor: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=100),
    rating: str | None = None,
    min_score: int | None = None,
    after: str | None = None,
    before: str | None = None,
    source: schemas.SourceKind | None = None,
):
    await require_project(request.app.state.db, project_id)
    if state not in {"all", "unreviewed", "reviewed", "positive", "negative", "unsure"}:
        raise HTTPException(422, "Invalid label filter")
    if rating is not None and rating not in {"s", "q", "e"}:
        raise HTTPException(422, "Invalid rating")
    where, args = ["c.project_id=?"], [project_id]
    if state == "unreviewed":
        where.append("l.state IS NULL")
    elif state == "reviewed":
        where.append("l.state IS NOT NULL")
    elif state != "all":
        where.append("l.state=?")
        args.append(state)
    for column, value, op in (
        ("p.rating", rating, "="),
        ("p.score", min_score, ">="),
        ("p.posted_at", after, ">="),
        ("p.posted_at", before, "<="),
    ):
        if value is not None and value != "":
            if column == "p.posted_at":
                try:
                    datetime.fromisoformat(value)
                except ValueError:
                    raise HTTPException(422, "Dates must use ISO format") from None
                if op == "<=":
                    value += "T23:59:59Z" if len(value) == 10 else ""
            where.append(f"{column}{op}?")
            args.append(value)
    if source:
        where.append(
            "EXISTS(SELECT 1 FROM discoveries d JOIN search_runs s ON s.id=d.search_run_id WHERE d.project_id=c.project_id AND d.post_id=c.post_id AND s.source_kind=?)"
        )
        args.append(source)
    clause = " AND ".join(where)
    total = await request.app.state.db.one(
        "SELECT count(*) AS n FROM project_candidates c JOIN posts p USING(post_id) LEFT JOIN labels l USING(project_id,post_id) WHERE "
        + clause,
        args,
    )
    if cursor:
        where.append("c.post_id<?")
        args.append(cursor)
    rows = await request.app.state.db.rows(
        CANDIDATE_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY c.post_id DESC LIMIT ?", (*args, limit + 1)
    )
    return {
        "items": [decode_candidate(row) for row in rows[:limit]],
        "next_cursor": str(rows[limit - 1]["post_id"]) if len(rows) > limit else None,
        "total": total["n"],
    }


@router.get("/projects/{project_id}/candidates/{post_id}", response_model=schemas.Candidate)
async def candidate_detail(project_id: int, post_id: int, request: Request):
    row = await request.app.state.db.one(
        CANDIDATE_SELECT + " WHERE c.project_id=? AND c.post_id=?", (project_id, post_id)
    )
    if not row:
        raise HTTPException(404, "Candidate not found")
    return decode_candidate(row)


@router.patch("/projects/{project_id}/labels/{post_id}", response_model=schemas.LabelResult)
async def label(project_id: int, post_id: int, body: schemas.LabelMutation, request: Request):
    return (
        await edit_labels(
            request.app.state.db,
            project_id,
            body.request_id,
            [{"post_id": post_id, "state": body.state, "expected_version": body.expected_version}],
        )
    )[0]


@router.post("/projects/{project_id}/labels/batch", response_model=list[schemas.LabelResult])
async def batch(project_id: int, body: schemas.BatchLabels, request: Request):
    return await edit_labels(
        request.app.state.db, project_id, body.request_id, [item.model_dump() for item in body.items]
    )


@router.post("/projects/{project_id}/labels/{post_id}/undo", response_model=schemas.LabelResult)
async def undo(project_id: int, post_id: int, body: schemas.UndoMutation, request: Request):
    return await undo_label(request.app.state.db, project_id, post_id, body)


@router.get("/jobs/{job_id}")
async def job(job_id: int, request: Request):
    row = await request.app.state.db.one(
        "SELECT id,type,status,attempts,error,result_json,updated_at FROM jobs WHERE id=?", (job_id,)
    )
    if not row:
        raise HTTPException(404, "Job not found")
    return row


@router.post("/media/prefetch")
async def prefetch(body: schemas.MediaPrefetch, request: Request):
    return {"job_ids": await request.app.state.media.schedule(body.post_ids)}


@router.get("/media/{post_id}/{variant}")
@router.head("/media/{post_id}/{variant}", include_in_schema=False)
async def media(post_id: int, variant: str, request: Request):
    if variant == "preview":
        return await request.app.state.media.preview_response(request, post_id)
    if variant not in {"sample", "original", "video_mp4"}:
        raise HTTPException(404, "Unknown media variant")
    return await request.app.state.media.stream_response(request, post_id, variant)


@router.get("/metrics")
async def metrics(request: Request):
    state = request.app.state
    queues = await state.db.rows("SELECT type,status,count(*) AS count FROM jobs GROUP BY type,status")
    cache = await state.db.one("SELECT count(*) AS files,COALESCE(sum(bytes),0) AS bytes FROM image_cache")
    return {
        "metadata": state.source.metrics,
        "media": state.media.metrics,
        "queues": queues,
        "cache": cache,
        "timings": state.telemetry.snapshot(),
    }


@router.post("/metrics/client")
async def client_metric(body: schemas.ClientMetric, request: Request):
    request.app.state.telemetry.record(body.name, body.value)
    return {"ok": True}
