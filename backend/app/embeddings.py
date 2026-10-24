"""Neighbor evidence, explicit candidate imports and descriptive tag suggestions.

These routes never assign labels, modify public tags or mutate frozen datasets.
The existing job tables retain bounded evidence without a schema migration.
"""

import json
from collections import Counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from . import auth, schemas
from .api import create_discovery, require_project
from .jobs import enqueue
from .neighbor_index import REVISION
from .reviews import attach_neighbor_provenance, fingerprint, receipt, save_receipt

PositiveId = Annotated[int, Path(gt=0, le=2**63 - 1)]

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_session)])


@router.get("/embeddings/status", response_model=schemas.EmbeddingStatus)
async def status(request: Request):
    enabled = request.app.state.settings.published_neighbors
    if request.app.state.settings.mock_e621:
        return {
            "enabled": enabled,
            "revision": "synthetic",
            "mode": "synthetic",
            "message": "Synthetic neighbor fixtures for isolated tests; no published index requests.",
        }
    return {
        "enabled": enabled,
        "revision": REVISION,
        "mode": "published_known_id_ranges",
        "message": "On-demand published neighbors; at most 128 KiB of array data per attempt. No images sent."
        if enabled
        else "Published neighbor lookup is disabled; review and datasets remain available.",
    }


@router.post("/projects/{project_id}/neighbors/{post_id}", response_model=schemas.NeighborJob)
async def lookup(project_id: PositiveId, post_id: PositiveId, body: schemas.Mutation, request: Request):
    db = request.app.state.db
    project_id = (await require_project(db, project_id))["id"]
    signature = fingerprint(["neighbors", project_id, post_id, REVISION], body.model_dump(mode="json"))
    async with db.transaction() as conn:
        existing = await receipt(conn, body.request_id, signature)
        if existing is not None:
            return existing
        if not request.app.state.settings.published_neighbors:
            raise HTTPException(409, "Published neighbor lookup is disabled; review remains available")
        candidate = await (
            await conn.execute(
                "SELECT 1 FROM project_candidates WHERE project_id=? AND post_id=?", (project_id, post_id)
            )
        ).fetchone()
        transient = request.app.state.feed.context(project_id, post_id)
        if not candidate and not transient:
            raise HTTPException(404, "Candidate not found")
        key = f"neighbors:{project_id}:{post_id}:{REVISION}"
        old = await (await conn.execute("SELECT id,status FROM jobs WHERE job_key=?", (key,))).fetchone()
        if not old or old["status"] not in {"done", "pending", "running"}:
            queued = (
                await (
                    await conn.execute(
                        "SELECT count(*) AS n FROM jobs WHERE type='neighbors' AND status IN ('pending','running')"
                    )
                ).fetchone()
            )["n"]
            if queued >= 16:
                raise HTTPException(429, "Neighbor queue is full; review remains available, retry later")
        # Completed evidence is stable. Explicit retry can restart a failed job.
        job_id = (
            old["id"]
            if old and old["status"] == "done"
            else await enqueue(conn, "neighbors", key, {"project_id": project_id, "seed_id": post_id})
        )
        result = {"job_id": job_id}
        await save_receipt(conn, body.request_id, signature, result)
        return result


async def evidence(db, project_id, post_id):
    project_id = (await require_project(db, project_id))["id"]
    return await db.one("SELECT * FROM jobs WHERE job_key=?", (f"neighbors:{project_id}:{post_id}:{REVISION}",))


@router.get("/projects/{project_id}/neighbors/{post_id}", response_model=schemas.NeighborEvidence)
async def get_evidence(project_id: PositiveId, post_id: PositiveId, request: Request):
    db = request.app.state.db
    project_id = (await require_project(db, project_id))["id"]
    job = await evidence(db, project_id, post_id)
    if not job:
        return {"status": "idle", "neighbors": [], "suggested_tags": [], "known_metadata": 0}
    result = json.loads(job["result_json"]) if job["status"] == "done" else {}
    neighbors = result.get("neighbors", [])
    ids = [item["post_id"] for item in neighbors]
    metadata = {}
    if ids:
        rows = await db.rows(
            "SELECT p.post_id,p.tags_json,p.unavailable,c.post_id AS candidate_id,l.state FROM posts p "
            "LEFT JOIN canonical_candidates c ON c.post_id=p.post_id AND c.project_id=? "
            "LEFT JOIN labels l ON l.project_id=c.owner_project_id AND l.post_id=p.post_id "
            "WHERE p.post_id IN (" + ",".join("?" for _ in ids) + ")",
            (project_id, *ids),
        )
        metadata = {row["post_id"]: row for row in rows}
    tags = Counter()
    known = 0
    for item in neighbors:
        row = metadata.get(item["post_id"])
        transient = request.app.state.feed.peek(item["post_id"])
        if not row and transient:
            from .feed import post_row

            row = {**post_row(transient[1]), "candidate_id": None, "state": None}
        item.update(
            {
                "in_project": bool(row and row["candidate_id"]),
                "state": row["state"] if row else None,
                "unavailable": bool(row["unavailable"]) if row else None,
            }
        )
        if row and not row["unavailable"]:
            known += 1
            # Count each general tag once per neighbor. This is source metadata,
            # not calibrated classifier output or a vote assigning ground truth.
            tags.update(set(json.loads(row["tags_json"]).get("general", [])))
    return {
        "status": job["status"],
        "job_id": job["id"],
        "error": job["error"],
        "result": result or None,
        "neighbors": neighbors,
        "known_metadata": known,
        "suggested_tags": [
            {"name": tag, "count": count}
            for tag, count in sorted(tags.items(), key=lambda pair: (-pair[1], pair[0]))[:20]
            if count >= 2
        ],
    }


@router.post("/projects/{project_id}/neighbors/{post_id}/import", response_model=schemas.NeighborImportResult)
async def import_neighbors(project_id: PositiveId, post_id: PositiveId, body: schemas.NeighborImport, request: Request):
    db = request.app.state.db
    project_id = (await require_project(db, project_id))["id"]
    signature = fingerprint(["neighbor-import", project_id, post_id], body.model_dump(mode="json"))
    async with db.transaction() as conn:
        existing = await receipt(conn, body.request_id, signature)
        if existing is not None:
            return existing
        job = await (
            await conn.execute("SELECT * FROM jobs WHERE id=? AND type='neighbors'", (body.job_id,))
        ).fetchone()
        if not job or json.loads(job["payload_json"]) != {"project_id": project_id, "seed_id": post_id}:
            raise HTTPException(404, "Neighbor evidence does not belong to this candidate/project")
        if job["status"] != "done":
            raise HTTPException(409, "Neighbor evidence is not ready")
        result = json.loads(job["result_json"])
        allowed = {item["post_id"] for item in result["neighbors"]}
        if not set(body.post_ids) <= allowed:
            raise HTTPException(422, "Select only IDs from this neighbor result")
        query = f"hydra35:{result['revision']}:seed:{post_id}:row:{result['row']}:sha256:{result['artifact_sha256']}"
        # Selection participates in run identity: another selection cannot reuse
        # an exhausted seed run and silently omit requested IDs.
        query += ":ids:" + ",".join(map(str, sorted(body.post_ids)))
        outcome = await create_discovery(conn, project_id, query, "embedding", sorted(body.post_ids))
        if outcome["job_id"] is not None:
            await conn.execute(
                "UPDATE jobs SET payload_json=json_set(payload_json,'$.neighbor_evidence',json(?)) WHERE id=?",
                (
                    json.dumps(
                        {
                            "seed_id": post_id,
                            "revision": result["revision"],
                            "row": result["row"],
                            "artifact_sha256": result["artifact_sha256"],
                            "metric": result.get("metric", "synthetic fixture"),
                            "neighbors": [item for item in result["neighbors"] if item["post_id"] in body.post_ids],
                        }
                    ),
                    outcome["job_id"],
                ),
            )
        for selected_id in body.post_ids:
            reviewed = await (
                await conn.execute(
                    "SELECT 1 FROM canonical_candidates c JOIN labels l ON l.project_id=c.owner_project_id AND l.post_id=c.post_id WHERE c.project_id=? AND c.post_id=?",
                    (project_id, selected_id),
                )
            ).fetchone()
            if reviewed:
                await attach_neighbor_provenance(conn, project_id, selected_id)
        outcome["query"] = "id:" + ",".join(map(str, sorted(body.post_ids)))
        await save_receipt(conn, body.request_id, signature, outcome)
        return outcome
