"""The only implementation of label transitions, version checks, and undo."""

import hashlib
import json
import time

from fastapi import HTTPException

from .projects import require_active_project


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


async def attach_legacy_discovery_provenance(conn, project_id, post_id):
    """Preserve already imported historical evidence when an old candidate is reviewed."""
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
    await require_active_project(conn, project_id)
    if state not in {None, "positive", "negative"}:
        raise HTTPException(422, "Only positive, negative or null reviews are supported")
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
                "state": row["state"] if row["state"] in {"positive", "negative"} else None,
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
        await conn.execute(
            "INSERT INTO retrieval_provenance SELECT ?,query_type,query_sha256,source_post_id,source_md5,"
            "candidate_md5,model_revision,embedding_space,index_revision,metric,neighbor_rank,distance "
            "FROM retrieval_provenance WHERE event_id=(SELECT id FROM label_events "
            "WHERE project_id=? AND post_id=? AND version<(SELECT version FROM label_events WHERE id=?) "
            "ORDER BY version DESC LIMIT 1)",
            (cursor.lastrowid, project_id, post_id, undo_of),
        )
    # The ordinary review of animated content labels its thumbnail. Sample edits
    # use this same transition, so gallery and focused-thumbnail decisions agree.
    await conn.execute(
        "UPDATE media_samples SET state=?,version=version+1,updated_at=? WHERE post_id=? "
        "AND project_id IN (SELECT project_id FROM project_family WHERE member_id=?) "
        "AND kind='thumbnail' AND source_md5=?",
        (state, now, post_id, project_id, row["md5"]),
    )
    return {"post_id": post_id, "state": state, "version": version, "event_id": cursor.lastrowid}


async def edit_labels(db, project_id, request_id, items, *, posts=(), evidence=None, thumbnails=None):
    signature = fingerprint(["labels", project_id], items)
    async with db.transaction() as conn:
        await require_active_project(conn, project_id)
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
        if thumbnails:
            from .media_sampling import insert_sample
            for post_id, (source_md5, image) in thumbnails.items():
                await insert_sample(conn, project_id, post_id, source_md5, "thumbnail", -1, image)
        for item in items:
            if item["state"] is not None:
                await attach_legacy_discovery_provenance(conn, project_id, item["post_id"])
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
                    "INSERT INTO retrieval_provenance VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (result["event_id"], context["query_type"], context["query_sha256"], context["source_post_id"],
                     context["source_md5"], context["md5"], context["model_revision"], context["embedding_space"],
                     context["revision"], context["metric"], context["rank"], context["distance"]),
                )
        await save_receipt(conn, request_id, signature, results)
        return results


async def review_request(state, project_id, request_id, items, *, owner=None):
    """Replay receipts before network access; commit retained image evidence with the label."""
    signature = fingerprint(["labels", project_id], items)
    async with state.db.transaction() as conn:
        await require_active_project(conn, project_id)
        existing = await receipt(conn, request_id, signature)
    if existing is not None:
        return existing
    evidence = {}
    for item in items:
        context = item.get("similarity")
        if context and item["state"] is not None:
            row = await state.similarity.review_evidence(project_id, search_id=context["search_id"], owner=owner)
            neighbor = next((n for n in row["neighbors"] if n["post_id"] == item["post_id"]), None)
            if row["revision"] != context["index_revision"] or neighbor is None:
                raise HTTPException(409, "Similarity evidence is invalid or changed; reopen the source search")
            if context.get("seed_post_id") is not None and context["seed_post_id"] != row["source_post_id"]:
                raise HTTPException(409, "Similarity source identity changed")
            current = await state.feed.raw(item["post_id"], fresh=True)
            if current.get("flags", {}).get("deleted") or (current.get("file") or {}).get("md5") != neighbor["md5"]:
                raise HTTPException(409, "Indexed candidate image changed; refresh and re-index it before review")
            evidence[item["post_id"]] = {**row, **neighbor}
    posts = await state.feed.review_posts(project_id, [i["post_id"] for i in items if i["state"] is not None])
    import asyncio

    from .feed import post_row
    from .media_sampling import ANIMATED_EXTENSIONS, store_image, uses_thumbnail_review
    thumbnails = {}
    thumbnail_posts = {post["id"]: post for post, _, _ in posts}
    # Existing reviewed candidates are intentionally absent from review_posts.
    # Materialize their missing animation thumbnail on the next explicit review.
    for item in items:
        if item["state"] is None or item["post_id"] in thumbnail_posts:
            continue
        stored = await state.db.one(
            "SELECT p.md5,p.media_ext FROM canonical_candidates c JOIN posts p USING(post_id) "
            "WHERE c.project_id=? AND c.post_id=? AND NOT EXISTS(SELECT 1 FROM media_samples ms "
            "WHERE ms.project_id=c.project_id AND ms.post_id=p.post_id AND ms.source_md5=p.md5 AND ms.kind='thumbnail')",
            (project_id, item["post_id"]),
        )
        if stored and stored["media_ext"] in ANIMATED_EXTENSIONS:
            raw = await state.feed.raw(item["post_id"], fresh=True)
            if (raw.get("file") or {}).get("md5") != stored["md5"]:
                raise HTTPException(409, "Source changed; the existing review was preserved")
            thumbnail_posts[item["post_id"]] = raw
    for post in thumbnail_posts.values():
        source = post.get("file") or {}
        if await uses_thumbnail_review(state, post):
            existing = await state.db.one("SELECT id FROM media_samples WHERE project_id=? AND post_id=? AND source_md5=? AND kind='thumbnail'", (project_id, post["id"], source.get("md5")))
            if not existing:
                raw = await state.media.thumbnail_bytes(post_row(post))
                image = await asyncio.to_thread(store_image, state.db.root, raw, state.settings.reference_max_pixels)
                thumbnails[post["id"]] = (source["md5"], image)
    return await edit_labels(state.db, project_id, request_id, items, posts=posts, evidence=evidence, thumbnails=thumbnails)


async def undo_label(db, project_id, post_id, mutation):
    signature = fingerprint(["undo", project_id, post_id], mutation.model_dump(mode="json"))
    async with db.transaction() as conn:
        await require_active_project(conn, project_id)
        existing = await receipt(conn, mutation.request_id, signature)
        if existing is not None:
            return {**existing, "state": existing["state"] if existing["state"] in {"positive", "negative"} else None}
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
            conn, project_id, post_id, mutation.expected_version,
            event["old_state"] if event["old_state"] in {"positive", "negative"} else None,
            undo_of=event["id"], source="undo"
        )
        await save_receipt(conn, mutation.request_id, signature, result)
        return result
