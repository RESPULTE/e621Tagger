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


async def attach_neighbor_provenance(conn, project_id, post_id):
    """Only explicitly selected neighbor evidence can accompany a human review."""
    await conn.execute(
        "INSERT OR IGNORE INTO discoveries(project_id,post_id,search_run_id,discovered_at) "
        "SELECT c.owner_project_id,c.post_id,s.id,j.updated_at FROM canonical_candidates c "
        "JOIN search_runs s ON s.project_id=c.project_id AND s.source_kind='embedding' "
        "JOIN jobs j ON j.type='discovery' AND json_extract(j.payload_json,'$.run_id')=s.id "
        "WHERE c.project_id=? AND c.post_id=? AND EXISTS(SELECT 1 FROM json_each(j.payload_json,'$.seed_ids') ids WHERE ids.value=c.post_id)",
        (project_id, post_id),
    )


async def transition(
    conn, project_id, post_id, expected_version, state, *, undo_of=None, source="review", source_md5=None
):
    conflict = await (
        await conn.execute("SELECT 1 FROM project_conflicts WHERE project_id=? AND post_id=?", (project_id, post_id))
    ).fetchone()
    if conflict:
        raise HTTPException(409, "Duplicate projects contain conflicting reviews; explicit resolution is required")
    owner = await (
        await conn.execute(
            "SELECT owner_project_id FROM canonical_candidates WHERE project_id=? AND post_id=?", (project_id, post_id)
        )
    ).fetchone()
    if owner:
        project_id = owner["owner_project_id"]
    row = await (
        await conn.execute(
            "SELECT c.review_version,l.state,p.md5 FROM project_candidates c JOIN posts p USING(post_id) LEFT JOIN labels l USING(project_id,post_id) "
            "WHERE c.project_id=? AND c.post_id=?",
            (project_id, post_id),
        )
    ).fetchone()
    if not row:
        raise HTTPException(404, "Post is not a candidate in this project")
    if state is not None and source_md5 and source_md5 != row["md5"]:
        raise HTTPException(409, "Original identity changed; existing review snapshots were preserved")
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
    if undo_of is not None and state is not None:
        # Each undo event carries the restored decision's evidence. Snapshot reads
        # then remain bounded without walking arbitrarily long undo histories.
        await conn.execute(
            "INSERT INTO similarity_provenance SELECT ?,seed_post_id,neighbor_rank,l2_distance,index_revision "
            "FROM similarity_provenance WHERE event_id=(SELECT id FROM label_events "
            "WHERE project_id=? AND post_id=? AND version<(SELECT version FROM label_events WHERE id=?) "
            "ORDER BY version DESC LIMIT 1)",
            (cursor.lastrowid, project_id, post_id, undo_of),
        )
    return {"post_id": post_id, "state": state, "version": version, "event_id": cursor.lastrowid}


async def edit_labels(db, project_id, request_id, items, *, posts=(), evidence=None):
    signature = fingerprint(["labels", project_id], items)
    async with db.transaction() as conn:
        existing = await receipt(conn, request_id, signature)
        if existing is not None:
            return existing
        from .jobs import persist_post

        for post, query, kind in posts:
            prior = await (
                await conn.execute(
                    "SELECT md5,(EXISTS(SELECT 1 FROM labels WHERE post_id=posts.post_id) OR EXISTS(SELECT 1 FROM label_events WHERE post_id=posts.post_id)) AS reviewed FROM posts WHERE post_id=?",
                    (post["id"],),
                )
            ).fetchone()
            if prior and prior["reviewed"] and prior["md5"] != (post.get("file") or {}).get("md5"):
                raise HTTPException(
                    409, "Original identity changed since an earlier review/save; existing snapshots were preserved"
                )
            if not prior or not prior["reviewed"]:
                await persist_post(conn, post)
            await conn.execute(
                "INSERT OR IGNORE INTO project_candidates(project_id,post_id,first_seen_at) VALUES(?,?,?)",
                (project_id, post["id"], time.time()),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO search_runs(project_id,query,source_kind) VALUES(?,?,?)",
                (project_id, query, kind),
            )
            run = await (
                await conn.execute(
                    "SELECT id FROM search_runs WHERE project_id=? AND query=? AND source_kind=?",
                    (project_id, query, kind),
                )
            ).fetchone()
            await conn.execute(
                "INSERT OR IGNORE INTO discoveries VALUES(?,?,?,?)", (project_id, post["id"], run["id"], time.time())
            )
        for item in items:
            if item["state"] is not None:
                await attach_neighbor_provenance(conn, project_id, item["post_id"])
        results = [
            await transition(
                conn,
                project_id,
                item["post_id"],
                item["expected_version"],
                item["state"],
                source="batch" if len(items) > 1 else "review",
                source_md5=item.get("source_md5"),
            )
            for item in items
        ]
        for result in results:
            context = (evidence or {}).get(result["post_id"])
            if context and result["state"] is not None:
                await conn.execute(
                    "INSERT INTO similarity_provenance(event_id,seed_post_id,neighbor_rank,l2_distance,index_revision) "
                    "VALUES(?,?,?,?,?)",
                    (
                        result["event_id"],
                        context["seed_post_id"],
                        context["rank"],
                        context["distance"],
                        context["revision"],
                    ),
                )
        await save_receipt(conn, request_id, signature, results)
        return results


async def review_request(state, project_id, request_id, items):
    """Replay receipts before network/index access; commit evidence with the label."""
    signature = fingerprint(["labels", project_id], items)
    async with state.db.transaction() as conn:
        existing = await receipt(conn, request_id, signature)
    if existing is not None:
        return existing
    evidence = {}
    for item in items:
        context = item.get("similarity")
        if context and item["state"] is not None:
            row = await state.similarity.review_evidence(project_id, context["seed_post_id"])
            neighbor = next((n for n in row["neighbors"] if n["post_id"] == item["post_id"]), None)
            if row["revision"] != context["index_revision"] or neighbor is None:
                raise HTTPException(409, "Similarity evidence is invalid or changed; reopen the source search")
            evidence[item["post_id"]] = dict(neighbor, seed_post_id=context["seed_post_id"], revision=row["revision"])
    posts = await state.feed.review_posts(project_id, [i["post_id"] for i in items if i["state"] is not None])
    return await edit_labels(state.db, project_id, request_id, items, posts=posts, evidence=evidence)


async def undo_label(db, project_id, post_id, mutation):
    signature = fingerprint(["undo", project_id, post_id], mutation.model_dump(mode="json"))
    async with db.transaction() as conn:
        existing = await receipt(conn, mutation.request_id, signature)
        if existing is not None:
            return existing
        event = await (
            await conn.execute(
                "SELECT e.* FROM label_events e JOIN project_family f ON f.member_id=e.project_id WHERE e.id=? AND f.project_id=? AND e.post_id=?",
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
