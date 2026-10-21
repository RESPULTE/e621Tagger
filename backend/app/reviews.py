"""The only implementation of label transitions, version checks, and undo."""

import hashlib
import json
import time

from fastapi import HTTPException


def fingerprint(scope, payload):
    return hashlib.sha256(json.dumps([scope, payload], sort_keys=True, default=str).encode()).hexdigest()


async def receipt(conn, request_id, signature):
    row = await (
        await conn.execute("SELECT * FROM mutation_requests WHERE request_id=?", (str(request_id),))
    ).fetchone()
    if row:
        if row["fingerprint"] != signature:
            raise HTTPException(409, "Request ID was already used for a different operation")
        return json.loads(row["response_json"])
    return None


async def save_receipt(conn, request_id, signature, result):
    await conn.execute(
        "INSERT INTO mutation_requests VALUES(?,?,?,?)", (str(request_id), signature, json.dumps(result), time.time())
    )


async def transition(conn, project_id, post_id, expected_version, state, *, undo_of=None, source="review"):
    row = await (
        await conn.execute(
            "SELECT c.review_version,l.state FROM project_candidates c LEFT JOIN labels l USING(project_id,post_id) "
            "WHERE c.project_id=? AND c.post_id=?",
            (project_id, post_id),
        )
    ).fetchone()
    if not row:
        raise HTTPException(404, "Post is not a candidate in this project")
    if row["review_version"] != expected_version:
        raise HTTPException(
            409,
            {
                "message": "This post changed in another tab; reload before saving",
                "state": row["state"],
                "version": row["review_version"],
                "post_id": post_id,
            },
        )
    now, version = time.time(), expected_version + 1
    if state is None:
        await conn.execute("DELETE FROM labels WHERE project_id=? AND post_id=?", (project_id, post_id))
    else:
        await conn.execute(
            "INSERT INTO labels VALUES(?,?,?,?) ON CONFLICT(project_id,post_id) DO UPDATE SET "
            "state=excluded.state,reviewed_at=excluded.reviewed_at",
            (project_id, post_id, state, now),
        )
    await conn.execute(
        "UPDATE project_candidates SET review_version=? WHERE project_id=? AND post_id=?",
        (version, project_id, post_id),
    )
    cursor = await conn.execute(
        "INSERT INTO label_events(project_id,post_id,old_state,new_state,version,reviewed_at,source,undo_of) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (project_id, post_id, row["state"], state, version, now, source, undo_of),
    )
    return {"post_id": post_id, "state": state, "version": version, "event_id": cursor.lastrowid}


async def edit_labels(db, project_id, request_id, items):
    signature = fingerprint(["labels", project_id], items)
    async with db.transaction() as conn:
        existing = await receipt(conn, request_id, signature)
        if existing is not None:
            return existing
        results = [
            await transition(
                conn,
                project_id,
                item["post_id"],
                item["expected_version"],
                item["state"],
                source="batch" if len(items) > 1 else "review",
            )
            for item in items
        ]
        await save_receipt(conn, request_id, signature, results)
        return results


async def undo_label(db, project_id, post_id, mutation):
    signature = fingerprint(["undo", project_id, post_id], mutation.model_dump(mode="json"))
    async with db.transaction() as conn:
        existing = await receipt(conn, mutation.request_id, signature)
        if existing is not None:
            return existing
        event = await (
            await conn.execute(
                "SELECT * FROM label_events WHERE id=? AND project_id=? AND post_id=?",
                (mutation.event_id, project_id, post_id),
            )
        ).fetchone()
        if not event:
            raise HTTPException(404, "Review event not found")
        if event["version"] != mutation.expected_version:
            raise HTTPException(409, "Only the current review event can be undone")
        result = await transition(
            conn, project_id, post_id, mutation.expected_version, event["old_state"], undo_of=event["id"], source="undo"
        )
        await save_receipt(conn, mutation.request_id, signature, result)
        return result
