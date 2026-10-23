"""Consistent snapshot reads, immutable manifests, and private metadata exports."""

import asyncio
import csv
import hashlib
import io
import json
import time
from collections import Counter
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.responses import Response

from . import auth, schemas
from .quality import MAX_CANDIDATES, eligible, manifest_samples, summarize
from .reviews import fingerprint, receipt, save_receipt

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_session)])

SNAPSHOT = """SELECT p.*,c.review_version,l.state,l.reviewed_at,
 (SELECT max(id) FROM label_events e WHERE e.project_id=c.project_id AND e.post_id=c.post_id) AS event_id,
 (SELECT json_group_array(json_object('run_id',s.id,'kind',s.source_kind,'query',s.query,'discovered_at',d.discovered_at))
 FROM discoveries d JOIN search_runs s ON s.id=d.search_run_id
 WHERE d.project_id=c.project_id AND d.post_id=c.post_id) AS provenance_json,f.dhash
 FROM project_candidates c JOIN posts p USING(post_id) LEFT JOIN labels l USING(project_id,post_id)
 LEFT JOIN image_fingerprints f ON f.post_id=p.post_id AND f.md5 IS p.md5 AND f.preview_url IS p.preview_url
 WHERE c.project_id=? ORDER BY c.post_id LIMIT ?"""


async def snapshot(conn, project_id):
    project = await (await conn.execute("SELECT * FROM projects WHERE id=?", (project_id,))).fetchone()
    if project is None:
        raise HTTPException(404, "Project not found")
    rows = await (await conn.execute(SNAPSHOT, (project_id, MAX_CANDIDATES + 1))).fetchall()
    if len(rows) > MAX_CANDIDATES:
        raise HTTPException(413, f"This dataset workflow supports at most {MAX_CANDIDATES} project candidates")
    return dict(project), [dict(row) for row in rows]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@router.get("/projects/{project_id}/quality")
async def quality(project_id: int, request: Request):
    async with request.app.state.db.transaction() as conn:
        _, rows = await snapshot(conn, project_id)
    summary = await asyncio.to_thread(summarize, rows)
    summary["preview_errors"] = await request.app.state.db.rows(
        "SELECT c.post_id,j.error FROM project_candidates c JOIN jobs j ON j.job_key='preview:' || c.post_id "
        "JOIN labels l USING(project_id,post_id) WHERE c.project_id=? AND j.status='failed' "
        "AND l.state IN ('positive','negative') ORDER BY j.updated_at DESC LIMIT 20",
        (project_id,),
    )
    return summary


@router.post("/projects/{project_id}/quality/prepare")
async def prepare(project_id: int, request: Request):
    async with request.app.state.db.transaction() as conn:
        _, rows = await snapshot(conn, project_id)
    missing = [row["post_id"] for row in rows if eligible(row) and not row.get("dhash")]
    jobs = await request.app.state.media.schedule(missing[:64], fingerprints=True)
    return {"remaining": len(missing), "scheduled": len(jobs)}


@router.post("/projects/{project_id}/datasets")
async def freeze(project_id: int, body: schemas.DatasetCreate, request: Request):
    db = request.app.state.db
    signature = fingerprint(["dataset", project_id], body.model_dump(mode="json"))
    async with db.transaction() as conn:
        existing = await receipt(conn, body.request_id, signature)
        if existing is not None:
            return existing
        project, rows = await snapshot(conn, project_id)
        captured = time.time()
    missing = sum(eligible(row) and not row.get("dhash") for row in rows)
    if missing:
        raise HTTPException(409, f"Prepare preview fingerprints before freezing: {missing} reviewed images remain")
    samples = await asyncio.to_thread(manifest_samples, rows, body)
    included = [sample for sample in samples if sample["target"] is not None]
    if not included:
        raise HTTPException(
            422, "No eligible reviewed positives or negatives; check availability and conflicting labels"
        )
    version_id = str(uuid4())
    split_counts = dict(Counter(sample["split"] for sample in included))
    warnings = [
        "dHash uses cached preview images; crops, color changes and other near-duplicates may escape detection.",
        "Availability is metadata at freeze time, not a guarantee that a remote URL will remain downloadable.",
    ]
    if set(split_counts) != {"train", "validation", "test"}:
        warnings.append("At least one split is empty. Collect more independent groups before training/evaluation.")
    if {sample["target"] for sample in included} != {0, 1}:
        warnings.append("Both verified positive and negative labels are required for supervised classification.")
    manifest = {
        "schema_version": 1,
        "dataset_id": version_id,
        "name": body.name.strip(),
        "created_at": captured,
        "project": project,
        "policy": {
            "version": "studio-split-v1",
            "split_seed": body.split_seed,
            "ratios": [80, 10, 10],
            "group_artists": body.group_artists,
            "group_series": body.group_series,
            "near_hash": "preview-dhash64-lanczos-v1",
            "near_hamming": body.near_hamming,
            "exact_duplicates": "smallest eligible post ID; conflicting exact labels excluded",
            "split": "sha256(seed:group), first 32 bits modulo 100; groups never cross splits",
        },
        "counts": {"included": len(included), "excluded": len(samples) - len(included), "splits": split_counts},
        "warnings": warnings,
        "samples": samples,
    }
    encoded = canonical(manifest)
    if len(encoded.encode()) > 16 * 1024 * 1024:
        raise HTTPException(413, "Manifest exceeds the 16 MiB limit; use a smaller project")
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    result = {
        "id": version_id,
        "name": manifest["name"],
        "manifest_sha256": digest,
        "created_at": captured,
        "counts": manifest["counts"],
        "warnings": warnings,
    }
    async with db.transaction() as conn:
        # Concurrent retries may finish analysis together; publish only one version.
        existing = await receipt(conn, body.request_id, signature)
        if existing is not None:
            return existing
        await conn.execute(
            "INSERT INTO datasets VALUES(?,?,?,?,?,?)",
            (version_id, project_id, manifest["name"], captured, digest, encoded),
        )
        await save_receipt(conn, body.request_id, signature, result)
    return result


@router.get("/projects/{project_id}/datasets")
async def versions(project_id: int, request: Request):
    db = request.app.state.db
    if not await db.one("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "Project not found")
    return await db.rows(
        "SELECT id,name,created_at,manifest_sha256,json_extract(manifest_json,'$.counts.included') AS included "
        "FROM datasets WHERE project_id=? ORDER BY created_at DESC LIMIT 100",
        (project_id,),
    )


@router.get("/datasets/{version_id}/export")
async def export(version_id: UUID, request: Request, format: Literal["json", "jsonl", "csv"] = Query(default="jsonl")):
    row = await request.app.state.db.one("SELECT * FROM datasets WHERE id=?", (str(version_id),))
    if not row:
        raise HTTPException(404, "Dataset not found")
    if format == "json":
        content, media_type = row["manifest_json"], "application/json"
    else:
        manifest = json.loads(row["manifest_json"])
        included = [sample for sample in manifest["samples"] if sample["target"] is not None]
        if format == "jsonl":
            metadata = {key: value for key, value in manifest.items() if key != "samples"}
            content = canonical({"type": "manifest", "manifest_sha256": row["manifest_sha256"], **metadata}) + "\n"
            content += "".join(canonical({"type": "sample", **sample}) + "\n" for sample in included)
            media_type = "application/x-ndjson"
        else:
            out = io.StringIO(newline="")
            fields = ["dataset_id", "manifest_sha256", *included[0].keys()]
            writer = csv.DictWriter(out, fieldnames=fields)
            writer.writeheader()
            for sample in included:
                values = {"dataset_id": str(version_id), "manifest_sha256": row["manifest_sha256"], **sample}
                values["provenance"] = canonical(values["provenance"])
                # No user-provided cell may execute as a spreadsheet formula.
                writer.writerow(
                    {
                        key: "'" + value
                        if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r"))
                        else value
                        for key, value in values.items()
                    }
                )
            content, media_type = out.getvalue(), "text/csv"
    return Response(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="dataset-{version_id}.{format}"',
            "X-Manifest-SHA256": row["manifest_sha256"],
            "Cache-Control": "private, no-store",
        },
    )
