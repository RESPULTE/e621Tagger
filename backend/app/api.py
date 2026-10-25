import json
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from . import auth, schemas
from .jobs import enqueue
from .reviews import fingerprint, receipt, review_request, save_receipt, undo_label

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


@router.get("/tags/autocomplete", response_model=list[schemas.TagResult])
async def autocomplete(
    request: Request,
    q: str = Query(min_length=1, max_length=100, pattern=r"^[^\s\x00-\x1f*]+$"),
):
    return await request.app.state.source.autocomplete(q.lower())


async def require_project(db, project_id):
    project = await db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if not project:
        raise HTTPException(404, "Project not found")
    identity = await db.one(
        "SELECT project_id FROM canonical_projects WHERE target_kind=? AND target_id=?",
        (project["target_kind"], project["target_id"]),
    )
    return await db.one("SELECT * FROM projects WHERE id=?", (identity["project_id"],)) if identity else project


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
        return {"job_id": None, "exhausted": True, "run_id": row["id"], "query": query}
    payload = {"run_id": row["id"]}
    if seed_ids:
        payload["seed_ids"] = seed_ids
    job_id = await enqueue(conn, "discovery", f"discovery:{row['id']}:{row['cursor'] or 0}", payload)
    return {"job_id": job_id, "exhausted": False, "run_id": row["id"], "query": query}


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
    query = (
        canonical["canonical_name"]
        if canonical
        else clean_query(body.query or "id:" + ",".join(map(str, body.seed_ids)))
    )
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
            row = await (await conn.execute("SELECT id,description FROM custom_tags WHERE name=?", (name,))).fetchone()
            if row["description"] != body.description:
                raise HTTPException(
                    409, "This custom target already has a different definition; reviews were preserved"
                )
            target_id = row["id"]
        identity = await (
            await conn.execute(
                "SELECT project_id FROM canonical_projects WHERE target_kind=? AND target_id=?",
                (body.target_kind, target_id),
            )
        ).fetchone()
        if identity:
            project_id = identity["project_id"]
        else:
            cursor = await conn.execute(
                "INSERT INTO projects(target_kind,target_id,display_name,query,created_at) VALUES(?,?,?,?,?)",
                (body.target_kind, target_id, name, query, time.time()),
            )
            project_id = cursor.lastrowid
            await conn.execute(
                "INSERT INTO canonical_projects VALUES(?,?,?)", (body.target_kind, target_id, project_id)
            )
        result = {"id": project_id, "display_name": name, "jobs": []}
        await save_receipt(conn, body.request_id, signature, result)
        return result


@router.get("/projects")
async def list_projects(
    request: Request, cursor: int | None = Query(default=None, gt=0), limit: int = Query(default=50, ge=1, le=100)
):
    rows = await request.app.state.db.rows(
        "SELECT p.*, (SELECT count(*) FROM canonical_candidates c JOIN labels l ON l.project_id=c.owner_project_id AND l.post_id=c.post_id WHERE c.project_id=p.id) AS total "
        "FROM projects p JOIN canonical_projects k ON k.project_id=p.id WHERE (? IS NULL OR p.id<?) ORDER BY p.id DESC LIMIT ?",
        (cursor, cursor, limit + 1),
    )
    return {"items": rows[:limit], "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}


@router.get("/projects/{project_id}", response_model=schemas.ProjectResult)
async def project_detail(project_id: int, request: Request):
    db = request.app.state.db
    project = await require_project(db, project_id)
    project_id = project["id"]
    rows = await db.rows(
        "SELECT l.state,count(*) AS n FROM canonical_candidates c JOIN labels l ON l.project_id=c.owner_project_id AND l.post_id=c.post_id WHERE c.project_id=? GROUP BY l.state",
        (project_id,),
    )
    counts = {"positive": 0, "negative": 0, "unsure": 0}
    counts.update({row["state"]: row["n"] for row in rows})
    counts["total"] = sum(counts.values())
    duplicates = await db.rows(
        "SELECT p.id,p.display_name,(SELECT count(*) FROM labels l WHERE l.project_id=p.id) AS reviews FROM projects p JOIN project_family f ON f.member_id=p.id WHERE f.project_id=? AND p.id!=?",
        (project_id, project_id),
    )
    conflicts = await db.one("SELECT count(*) AS n FROM project_conflicts WHERE project_id=?", (project_id,))
    return {
        **project,
        "counts": counts,
        "searches": [],
        "jobs": [],
        "duplicate_projects": duplicates,
        "conflicts": conflicts["n"],
    }


@router.get("/projects/{project_id}/query", response_model=schemas.CandidatePage)
async def query_feed(
    project_id: int,
    request: Request,
    query: str = Query(default="", max_length=500),
    cursor: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=50),
):
    project = await require_project(request.app.state.db, project_id)
    return await request.app.state.feed.page(project["id"], query or project["query"], cursor, limit)


@router.post("/projects/{project_id}/discovery")
async def discover(project_id: int, body: schemas.DiscoveryCreate, request: Request):
    db = request.app.state.db
    project = await require_project(db, project_id)
    if body.source_kind != "target" and not body.query.strip():
        raise HTTPException(422, "Enter an additional discovery query")
    query = clean_query(body.query or project["query"])
    if not query:
        raise HTTPException(422, "Enter a discovery query for this seed-only project")
    if query != project["query"] and body.source_kind == "target":
        raise HTTPException(422, "Additional queries must use source_kind=query")
    source_kind = (
        body.source_kind
        if body.source_kind in {"unrelated", "hard_negative"}
        else ("target" if project["target_kind"] == "e621" and query == project["query"] else "query")
    )
    page = await request.app.state.feed.page(project["id"], query, kind=source_kind)
    return {"job_id": None, "run_id": None, "exhausted": page["next_cursor"] is None, "query": query, **page}


def decode_candidate(row):
    return {
        "conflict": bool(row.get("conflict", False)),
        "conflicting_reviews": json.loads(row.get("conflicting_reviews") or "[]"),
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
        "favorite": {
            "post_id": row["post_id"],
            "version": row["save_version"] or 0,
            "wanted": bool(row["save_wanted"]),
            "present": bool(row["save_present"]),
            "status": row["save_status"] or "idle",
            "error": row["save_error"],
            "updated_at": row["save_updated_at"] or 0,
        },
    }


CANDIDATE_SELECT = """SELECT p.*,c.review_version,l.state,
 (SELECT json_group_array(json_object('project_id',x.project_id,'state',x.state,'version',pc.review_version))
 FROM labels x JOIN project_family pf ON pf.member_id=x.project_id JOIN project_candidates pc ON pc.project_id=x.project_id AND pc.post_id=x.post_id
 WHERE pf.project_id=c.project_id AND x.post_id=c.post_id) AS conflicting_reviews,
 EXISTS(SELECT 1 FROM project_conflicts f WHERE f.project_id=c.project_id AND f.post_id=c.post_id) AS conflict,
 m.version AS save_version,m.wanted AS save_wanted,m.present AS save_present,
 m.status AS save_status,m.error AS save_error,m.updated_at AS save_updated_at,
 (SELECT max(id) FROM label_events e WHERE e.project_id=c.owner_project_id AND e.post_id=c.post_id) AS event_id,
 (SELECT json_group_array(DISTINCT s.source_kind || ': ' || s.query) FROM discoveries d JOIN search_runs s ON s.id=d.search_run_id
 WHERE d.project_id=c.owner_project_id AND d.post_id=c.post_id) AS sources
 FROM canonical_candidates c JOIN posts p USING(post_id) LEFT JOIN labels l ON l.project_id=c.owner_project_id AND l.post_id=c.post_id
 LEFT JOIN saved_media m ON m.post_id=p.post_id"""


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
    run_id: int | None = Query(default=None, gt=0),
):
    project_id = (await require_project(request.app.state.db, project_id))["id"]
    if state not in {"all", "unreviewed", "reviewed", "positive", "negative", "unsure"}:
        raise HTTPException(422, "Invalid label filter")
    if rating is not None and rating not in {"s", "q", "e"}:
        raise HTTPException(422, "Invalid rating")
    where, args = ["c.project_id=?", "l.state IS NOT NULL"], [project_id]
    if run_id is not None:
        run = await request.app.state.db.one(
            "SELECT id FROM search_runs WHERE id=? AND project_id=?", (run_id, project_id)
        )
        if not run:
            raise HTTPException(404, "Search does not belong to this project")
        where.append(
            "EXISTS(SELECT 1 FROM discoveries d WHERE d.project_id=c.owner_project_id "
            "AND d.post_id=c.post_id AND d.search_run_id=?)"
        )
        args.append(run_id)
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
            "EXISTS(SELECT 1 FROM discoveries d JOIN search_runs s ON s.id=d.search_run_id WHERE d.project_id=c.owner_project_id AND d.post_id=c.post_id AND s.source_kind=?)"
        )
        args.append(source)
    clause = " AND ".join(where)
    total = await request.app.state.db.one(
        "SELECT count(*) AS n FROM canonical_candidates c JOIN posts p USING(post_id) LEFT JOIN labels l ON l.project_id=c.owner_project_id AND l.post_id=c.post_id WHERE "
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
    project = await require_project(request.app.state.db, project_id)
    post = await request.app.state.feed.raw(post_id, fresh=True)
    return await request.app.state.feed.candidate(project["id"], post)


@router.patch("/projects/{project_id}/labels/{post_id}", response_model=schemas.LabelResult)
async def label(project_id: int, post_id: int, body: schemas.LabelMutation, request: Request):
    project_id = (await require_project(request.app.state.db, project_id))["id"]
    items = [{**body.model_dump(exclude_none=True, exclude={"request_id"}), "post_id": post_id, "state": body.state}]
    return (await review_request(request.app.state, project_id, body.request_id, items))[0]


@router.post("/projects/{project_id}/labels/batch", response_model=list[schemas.LabelResult])
async def batch(project_id: int, body: schemas.BatchLabels, request: Request):
    project_id = (await require_project(request.app.state.db, project_id))["id"]
    return await review_request(
        request.app.state,
        project_id,
        body.request_id,
        [{**item.model_dump(exclude_none=True), "state": item.state} for item in body.items],
    )


@router.post("/projects/{project_id}/labels/{post_id}/undo", response_model=schemas.LabelResult)
async def undo(project_id: int, post_id: int, body: schemas.UndoMutation, request: Request):
    project_id = (await require_project(request.app.state.db, project_id))["id"]
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
async def media(
    post_id: int,
    variant: str,
    request: Request,
    source_md5: str | None = Query(default=None, pattern=r"^[a-f0-9]{32}$"),
):
    if variant == "preview":
        return await request.app.state.media.preview_response(request, post_id, source_md5)
    if variant not in {"sample", "original", "video_mp4"}:
        raise HTTPException(404, "Unknown media variant")
    return await request.app.state.media.stream_response(request, post_id, variant, source_md5)


@router.get("/metrics")
async def metrics(request: Request):
    state = request.app.state
    queues = await state.db.rows("SELECT type,status,count(*) AS count FROM jobs GROUP BY type,status")
    cache = await state.db.one("SELECT count(*) AS files,COALESCE(sum(bytes),0) AS bytes FROM image_cache")
    return {
        "metadata": state.source.metrics,
        "downloads": state.transfers.metrics,
        "transient_posts": len(state.feed.posts),
        "media": state.media.metrics,
        "media_timings": state.media.timings.snapshot(),
        "queues": queues,
        "cache": cache,
        "timings": state.telemetry.snapshot(),
    }


@router.post("/metrics/client")
async def client_metric(body: schemas.ClientMetric, request: Request):
    request.app.state.telemetry.record(body.name, body.value)
    return {"ok": True}
