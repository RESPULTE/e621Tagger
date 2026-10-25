"""Expiring source metadata for browsing. Only explicit reviews/saves reach SQLite."""

import json
import time
from collections import OrderedDict

from fastapi import HTTPException


def post_row(post):
    file, preview, sample = post.get("file") or {}, post.get("preview") or {}, post.get("sample") or {}
    return dict(
        post_id=post["id"],
        rating=post.get("rating"),
        md5=file.get("md5"),
        tags_json=json.dumps(post.get("tags", {})),
        score=post.get("score", {}).get("total", 0),
        posted_at=post.get("created_at"),
        media_ext=file.get("ext"),
        width=file.get("width"),
        height=file.get("height"),
        preview_url=preview.get("url"),
        sample_url=sample.get("url"),
        file_url=file.get("url"),
        video_url=sample.get("alternates", {}).get("variants", {}).get("mp4", {}).get("url"),
        unavailable=int(post.get("flags", {}).get("deleted", False)),
        fetched_at=time.time(),
    )


class Feed:
    def __init__(self, source, db, maximum=2000, ttl=900):
        self.source, self.db, self.maximum, self.ttl = source, db, maximum, ttl
        self.posts = OrderedDict()
        self.sizes, self.bytes = {}, 0
        self.contexts = OrderedDict()

    def context(self, project_id, post_id):
        value = self.contexts.get((project_id, post_id))
        return value if value and value[0] >= time.monotonic() - self.ttl else None

    def remember(self, post, query="", kind="query", project_id=None):
        size = len(json.dumps(post).encode())
        if size > 256 * 1024:
            raise HTTPException(502, "Upstream post metadata exceeds the transient limit")
        self.bytes -= self.sizes.pop(post["id"], 0)
        self.sizes[post["id"]] = size
        self.bytes += size
        self.posts[post["id"]] = (time.monotonic(), post, query, kind, project_id)
        if project_id is not None:
            key = (project_id, post["id"])
            self.contexts[key] = (time.monotonic(), query, kind)
            self.contexts.move_to_end(key)
            while self.contexts and (
                len(self.contexts) > self.maximum or next(iter(self.contexts.values()))[0] < time.monotonic() - self.ttl
            ):
                self.contexts.popitem(last=False)
        self.posts.move_to_end(post["id"])
        while self.posts and (
            len(self.posts) > self.maximum
            or self.bytes > 16 * 1024**2
            or next(iter(self.posts.values()))[0] < time.monotonic() - self.ttl
        ):
            key, _ = self.posts.popitem(last=False)
            self.bytes -= self.sizes.pop(key)

    def peek(self, post_id):
        item = self.posts.get(post_id)
        return item if item and item[0] >= time.monotonic() - self.ttl else None

    async def raw(self, post_id, *, fresh=False, background=False):
        item = self.peek(post_id)
        if item and not fresh:
            return item[1]
        if background and not fresh:
            stored = await self.db.one(
                "SELECT * FROM posts WHERE post_id=? AND fetched_at>?", (post_id, time.time() - self.ttl)
            )
            if stored:
                post = {
                    "id": post_id,
                    "rating": stored["rating"],
                    "created_at": stored["posted_at"],
                    "tags": json.loads(stored["tags_json"]),
                    "score": {"total": stored["score"]},
                    "file": {
                        "url": stored["file_url"],
                        "md5": stored["md5"],
                        "ext": stored["media_ext"],
                        "width": stored["width"],
                        "height": stored["height"],
                    },
                    "preview": {"url": stored["preview_url"]},
                    "sample": {
                        "url": stored["sample_url"],
                        "alternates": {"variants": {"mp4": {"url": stored["video_url"]}}},
                    },
                    "flags": {"deleted": bool(stored["unavailable"])},
                }
                self.remember(post)
                return post
        try:
            post = (await self.source.request(f"/posts/{post_id}.json", background=background))["post"]
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            post = {"id": post_id, "flags": {"deleted": True}}
        if post.get("id") != post_id:
            raise HTTPException(502, "Upstream returned a different post")
        self.remember(post, *(item[2:] if item else ()))
        return post

    async def row(self, post_id, *, fresh=False):
        return post_row(await self.raw(post_id, fresh=fresh))

    async def candidate(self, project_id, post, query=""):
        from .api import CANDIDATE_SELECT, decode_candidate

        saved = await self.db.one(CANDIDATE_SELECT + " WHERE c.project_id=? AND c.post_id=?", (project_id, post["id"]))
        if not saved:
            saved = dict(
                review_version=0,
                state=None,
                event_id=None,
                sources=json.dumps(["query: " + query] if query else []),
                **{f"save_{key}": None for key in ("version", "wanted", "present", "status", "error", "updated_at")},
            )
            media = await self.db.one("SELECT * FROM saved_media WHERE post_id=?", (post["id"],))
            if media:
                saved.update(
                    {
                        "save_" + key: media[key]
                        for key in ("version", "wanted", "present", "status", "error", "updated_at")
                    }
                )
        return decode_candidate({**saved, **post_row(post)})

    async def page(self, project_id, query, cursor=None, limit=50, kind="query"):
        from .api import clean_query

        query = clean_query(query)
        if not query:
            raise HTTPException(422, "Enter an e621 discovery query")
        params = {"tags": query + " order:id_desc", "limit": limit}
        if cursor:
            params["page"] = f"b{cursor}"
        data = await self.source.request("/posts.json", params)
        posts = data["posts"]
        if len(posts) > limit or any(not isinstance(p.get("id"), int) or p["id"] <= 0 for p in posts):
            raise HTTPException(502, "Invalid upstream page")
        if cursor and any(p["id"] >= cursor for p in posts):
            raise HTTPException(502, "Upstream pagination did not advance")
        for post in posts:
            prior = self.context(project_id, post["id"])
            if prior and prior[2] == "embedding" and query.startswith("id:"):
                self.remember(post, prior[1], prior[2], project_id)
            else:
                self.remember(post, query, kind, project_id)
        return {
            "items": [await self.candidate(project_id, post, query) for post in posts],
            "next_cursor": str(min(p["id"] for p in posts)) if len(posts) == limit else None,
            "total": len(posts),
        }

    async def review_posts(self, project_id, ids):
        result = []
        for post_id in ids:
            existing = await self.db.one(
                "SELECT review_version FROM canonical_candidates WHERE project_id=? AND post_id=?",
                (project_id, post_id),
            )
            if existing and existing["review_version"] > 0:
                continue
            item = self.context(project_id, post_id)
            post = await self.raw(post_id)
            if post.get("flags", {}).get("deleted"):
                raise HTTPException(404, "Post unavailable; it cannot be newly reviewed")
            result.append(
                (
                    post,
                    item[1] if item else f"id:{post_id}",
                    item[2] if item else "query",
                )
            )
        return result
