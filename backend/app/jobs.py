"""Persistent jobs; discovery commits page results and its cursor atomically."""

import asyncio
import json
import logging
import time

from fastapi import HTTPException

logger = logging.getLogger(__name__)
MAX_JOB_ATTEMPTS = 4


async def enqueue(conn, job_type, key, payload):
    now = time.time()
    row = await (await conn.execute("SELECT * FROM jobs WHERE job_key=?", (key,))).fetchone()
    if row and row["status"] in {"pending", "running"}:
        return row["id"]
    if row:
        await conn.execute(
            "UPDATE jobs SET status='pending',attempts=0,payload_json=?,available_at=?,lease_until=NULL,"
            "error=NULL,result_json=NULL,updated_at=? WHERE id=?",
            (json.dumps(payload), now, now, row["id"]),
        )
        return row["id"]
    cursor = await conn.execute(
        "INSERT INTO jobs(type,job_key,payload_json,status,available_at,updated_at) VALUES(?,?,?,'pending',?,?)",
        (job_type, key, json.dumps(payload), now, now),
    )
    return cursor.lastrowid


async def persist_post(conn, post):
    file, preview, sample = post.get("file") or {}, post.get("preview") or {}, post.get("sample") or {}
    values = (
        post["id"],
        post.get("rating"),
        file.get("md5"),
        json.dumps(post.get("tags", {})),
        post.get("score", {}).get("total", 0),
        post.get("created_at"),
        file.get("ext"),
        file.get("width"),
        file.get("height"),
        preview.get("url"),
        sample.get("url"),
        file.get("url"),
        sample.get("alternates", {}).get("variants", {}).get("mp4", {}).get("url"),
        int(post.get("flags", {}).get("deleted", False)),
        time.time(),
    )
    await conn.execute(
        "INSERT INTO posts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET "
        "rating=excluded.rating,md5=excluded.md5,tags_json=excluded.tags_json,score=excluded.score,"
        "posted_at=excluded.posted_at,media_ext=excluded.media_ext,width=excluded.width,height=excluded.height,"
        "preview_url=excluded.preview_url,sample_url=excluded.sample_url,file_url=excluded.file_url,"
        "video_url=excluded.video_url,unavailable=excluded.unavailable,fetched_at=excluded.fetched_at",
        values,
    )


class Workers:
    def __init__(self, db, source, media):
        self.db, self.source, self.media = db, source, media
        self.tasks = []

    async def start(self, media_count):
        # No other process can own these leases because Database.open takes a process lock.
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE jobs SET status='pending',lease_until=NULL WHERE status='running'")
        self.tasks = [asyncio.create_task(self._loop("discovery"), name="discovery")]
        self.tasks += [asyncio.create_task(self._loop("preview"), name=f"preview-{i}") for i in range(media_count)]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _claim(self, job_type):
        async with self.db.transaction() as conn:
            now = time.time()
            row = await (
                await conn.execute(
                    "SELECT * FROM jobs WHERE type=? AND ((status='pending' AND available_at<=?) "
                    "OR (status='running' AND lease_until<?)) ORDER BY available_at,id LIMIT 1",
                    (job_type, now, now),
                )
            ).fetchone()
            if not row:
                return None
            await conn.execute(
                "UPDATE jobs SET status='running',attempts=attempts+1,lease_until=?,updated_at=? WHERE id=?",
                (now + 120, now, row["id"]),
            )
            return {**dict(row), "attempts": row["attempts"] + 1}

    async def _heartbeat(self, job_id):
        while True:
            await asyncio.sleep(20)
            async with self.db.transaction() as conn:
                await conn.execute(
                    "UPDATE jobs SET lease_until=? WHERE id=? AND status='running'", (time.time() + 120, job_id)
                )

    async def _loop(self, job_type):
        while True:
            try:
                job = await self._claim(job_type)
                if not job:
                    await asyncio.sleep(0.25)
                    continue
                heartbeat = asyncio.create_task(self._heartbeat(job["id"]))
                try:
                    payload = json.loads(job["payload_json"])
                    if job_type == "discovery":
                        await self.discover(job, payload)
                    else:
                        await self.media.download_preview(payload["post_id"])
                        async with self.db.transaction() as conn:
                            await conn.execute(
                                "UPDATE jobs SET status='done',lease_until=NULL,result_json='{}',updated_at=? WHERE id=?",
                                (time.time(), job["id"]),
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Never persist exception strings containing request URLs or credentials.
                    status = exc.status_code if isinstance(exc, HTTPException) else None
                    permanent = status in {400, 403, 404, 409, 413, 415, 422}
                    failed = permanent or job["attempts"] >= MAX_JOB_ATTEMPTS
                    message = (
                        str(exc.detail) if isinstance(exc, HTTPException) else f"{type(exc).__name__}: operation failed"
                    )
                    async with self.db.transaction() as conn:
                        await conn.execute(
                            "UPDATE jobs SET status=?,error=?,available_at=?,lease_until=NULL,updated_at=? WHERE id=?",
                            (
                                "failed" if failed else "pending",
                                message[:500],
                                time.time() + 2 ** (job["attempts"] + 1),
                                time.time(),
                                job["id"],
                            ),
                        )
                    logger.warning("Job %s (%s): %s", job["id"], job_type, message)
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Job scheduler failed; retrying")
                await asyncio.sleep(1)

    async def discover(self, job, payload):
        run = await self.db.one("SELECT * FROM search_runs WHERE id=?", (payload["run_id"],))
        if not run:
            raise HTTPException(404, "Discovery run not found")
        missing = []
        if run["source_kind"] == "seed":
            posts = []
            for post_id in payload["seed_ids"]:
                try:
                    data = await self.source.request(f"/posts/{post_id}.json", background=True)
                    posts.append(data["post"])
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
                    missing.append(post_id)
                    posts.append({"id": post_id, "flags": {"deleted": True}})
        else:
            # b<ID> pagination is valid only in descending ID order.
            query = run["query"].strip()
            if any(part.startswith("order:") for part in query.split()):
                raise HTTPException(422, "Discovery queries cannot override ID ordering")
            params = {"tags": (query + " order:id_desc").strip(), "limit": 50}
            if run["cursor"]:
                params["page"] = f"b{run['cursor']}"
            data = await self.source.request("/posts.json", params, background=True)
            posts = data["posts"]
        now = time.time()
        async with self.db.transaction() as conn:
            for post in posts:
                await persist_post(conn, post)
                await conn.execute(
                    "INSERT OR IGNORE INTO project_candidates(project_id,post_id,first_seen_at) VALUES(?,?,?)",
                    (run["project_id"], post["id"], now),
                )
                await conn.execute(
                    "INSERT OR IGNORE INTO discoveries VALUES(?,?,?,?)", (run["project_id"], post["id"], run["id"], now)
                )
            exhausted = run["source_kind"] == "seed" or len(posts) < 50
            cursor = min((p["id"] for p in posts), default=run["cursor"])
            await conn.execute(
                "UPDATE search_runs SET cursor=?,exhausted=? WHERE id=?", (cursor, int(exhausted), run["id"])
            )
            result = {"imported": len(posts), "unavailable_ids": missing, "exhausted": exhausted}
            await conn.execute(
                "UPDATE jobs SET status='done',lease_until=NULL,result_json=?,error=NULL,updated_at=? WHERE id=?",
                (json.dumps(result), now, job["id"]),
            )
