"""All metadata requests share one bounded, prioritized scheduler."""

import asyncio
import itertools
import random
import time
from collections import OrderedDict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from fastapi import HTTPException

CATEGORIES = {
    "general": 0,
    "artist": 1,
    "contributor": 2,
    "copyright": 3,
    "character": 4,
    "species": 5,
    "invalid": 6,
    "meta": 7,
    "lore": 8,
}


def retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            return max(0, float(value))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                pass
    return min(60, 2**attempt + random.random())


class E621:
    def __init__(self, settings, db, transport=None):
        self.settings, self.db = settings, db
        auth = (settings.e621_username, settings.e621_api_key) if settings.e621_api_key else None
        self.client = httpx.AsyncClient(
            base_url="https://e621.net",
            headers={"User-Agent": settings.user_agent},
            auth=auth,
            transport=transport,
            timeout=20,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=2),
        )
        self.queue = asyncio.PriorityQueue(maxsize=64)
        self.sequence = itertools.count()
        self.inflight = {}
        self.autocomplete_cache = OrderedDict()
        self.task = None
        self.metrics = {"requests": 0, "throttled": 0, "errors": 0, "last_latency_ms": 0.0}

    def start(self):
        self.task = asyncio.create_task(self._run(), name="e621-metadata")

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for future in self.inflight.values():
            if not future.done():
                future.cancel()
        await self.client.aclose()

    async def request(self, path: str, params=None, *, background=False):
        key = (path, tuple(sorted((params or {}).items())))
        if key in self.inflight:
            return await asyncio.shield(self.inflight[key])
        future = asyncio.get_running_loop().create_future()
        # Retrieve exceptions even if every waiting HTTP request disconnects.
        future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        self.inflight[key] = future
        try:
            self.queue.put_nowait((10 if background else 0, next(self.sequence), key, future))
        except asyncio.QueueFull:
            self.inflight.pop(key, None)
            raise HTTPException(503, "Metadata queue is full; retry shortly", headers={"Retry-After": "2"})
        return await asyncio.shield(future)

    async def _run(self):
        while True:
            _, _, key, future = await self.queue.get()
            path, params = key
            try:
                for attempt in range(4):
                    state = await self.db.one("SELECT value FROM runtime_state WHERE key='metadata_next'")
                    await asyncio.sleep(max(0, (state["value"] if state else 0) - time.time()))
                    async with self.db.transaction() as conn:
                        await conn.execute(
                            "INSERT OR REPLACE INTO runtime_state VALUES('metadata_next',?)",
                            (time.time() + self.settings.metadata_interval,),
                        )
                    start = time.monotonic()
                    try:
                        self.metrics["requests"] += 1
                        response = await self.client.get(path, params=dict(params))
                        self.metrics["last_latency_ms"] = round((time.monotonic() - start) * 1000, 1)
                    except httpx.TransportError:
                        if attempt == 3:
                            raise HTTPException(502, "e621 could not be reached") from None
                        await asyncio.sleep(retry_delay(None, attempt))
                        continue
                    if response.status_code in {429, 503, 502, 504}:
                        self.metrics["throttled"] += 1
                        delay = retry_delay(response.headers.get("Retry-After"), attempt)
                        async with self.db.transaction() as conn:
                            await conn.execute(
                                "INSERT OR REPLACE INTO runtime_state VALUES('metadata_next',?)",
                                (time.time() + max(delay, self.settings.metadata_interval),),
                            )
                        if attempt == 3:
                            raise HTTPException(503, "e621 is busy; retry this operation later")
                        continue
                    if response.status_code == 404:
                        raise HTTPException(404, "Post or tag unavailable on e621")
                    if response.status_code != 200:
                        raise HTTPException(502, f"e621 returned HTTP {response.status_code}")
                    try:
                        data = response.json()
                    except ValueError:
                        raise HTTPException(502, "e621 returned an invalid response") from None
                    if not future.done():
                        future.set_result(data)
                    break
            except asyncio.CancelledError:
                future.cancel()
                raise
            except Exception as exc:
                self.metrics["errors"] += 1
                if not future.done():
                    future.set_exception(exc)
            finally:
                self.inflight.pop(key, None)
                self.queue.task_done()

    async def cache_tags(self, tags):
        now = time.time()
        async with self.db.transaction() as conn:
            for tag in tags:
                await conn.execute(
                    "INSERT INTO tag_catalog VALUES(?,?,?,?,?) ON CONFLICT(tag_id) DO UPDATE SET "
                    "canonical_name=excluded.canonical_name,category=excluded.category,"
                    "post_count=excluded.post_count,updated_at=excluded.updated_at",
                    (tag["id"], tag["name"], tag["category"], tag["post_count"], now),
                )

    async def canonical_tag(self, name: str):
        name = name.strip().lower().replace(" ", "_")
        original = name
        seen = set()
        for _ in range(8):
            if name in seen:
                raise HTTPException(502, "Cyclic upstream tag alias")
            seen.add(name)
            alias = await self.db.one(
                "SELECT * FROM aliases WHERE name=? AND updated_at>?", (name, time.time() - 86400)
            )
            if alias:
                name = alias["canonical_name"]
                continue
            aliases = await self.request(
                "/tag_aliases.json", {"search[antecedent_name]": name, "search[status]": "approved", "limit": 10}
            )
            active = next(
                (
                    a
                    for a in aliases
                    if a.get("antecedent_name") == name and a.get("status") in {"active", "processing", "queued"}
                ),
                None,
            )
            if not active:
                break
            async with self.db.transaction() as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO aliases VALUES(?,?,?)", (name, active["consequent_name"], time.time())
                )
            name = active["consequent_name"]
        else:
            raise HTTPException(502, "Tag alias chain is too long")
        tag = await self.db.one(
            "SELECT * FROM tag_catalog WHERE canonical_name=? AND updated_at>?", (name, time.time() - 86400)
        )
        if not tag:
            tags = await self.request("/tags.json", {"search[name_matches]": name, "limit": 10})
            exact = next((t for t in tags if t["name"] == name), None)
            if not exact:
                raise HTTPException(404, "Tag not found; create a custom tag instead")
            await self.cache_tags([exact])
            tag = await self.db.one("SELECT * FROM tag_catalog WHERE canonical_name=?", (name,))
        return {**tag, "matched_alias": original if original != name else None}

    async def autocomplete(self, q):
        cached = self.autocomplete_cache.get(q)
        if cached and cached[0] > time.monotonic():
            self.autocomplete_cache.move_to_end(q)
            return cached[1]
        data = await self.request("/tags/autocomplete.json", {"search[name_matches]": q, "expiry": "7"})
        try:
            if not isinstance(data, list):
                raise ValueError
            result = [
                {
                    "tag_id": int(tag["id"]),
                    "canonical_name": str(tag["name"]),
                    "category": int(tag["category"]),
                    "post_count": int(tag["post_count"]),
                    "matched_alias": tag.get("antecedent_name"),
                }
                for tag in data[:20]
            ]
        except (KeyError, TypeError, ValueError):
            raise HTTPException(502, "e621 returned invalid autocomplete results") from None
        self.autocomplete_cache[q] = (time.monotonic() + 3600, result)
        self.autocomplete_cache.move_to_end(q)
        while len(self.autocomplete_cache) > 256:
            self.autocomplete_cache.popitem(last=False)
        return result

    async def tags(self, q, category=None, minimum=0, exact=False):
        if exact:
            try:
                tag = await self.canonical_tag(q)
            except HTTPException as exc:
                if exc.status_code == 404:
                    return []
                raise
            return [tag] if tag["post_count"] >= minimum and (category is None or tag["category"] == category) else []
        params = {"search[name_matches]": q + "*", "search[order]": "count", "limit": 20}
        if category is not None:
            params["search[category]"] = category
        # Query-result TTL also covers empty results and avoids re-fetching every keystroke revisited.
        cache_key = "tags:" + str(tuple(sorted(params.items())))
        cached = await self.db.one("SELECT value FROM runtime_state WHERE key=?", (cache_key,))
        if not cached or cached["value"] < time.time() - 3600:
            tags = await self.request("/tags.json", params)
            await self.cache_tags(tags)
            async with self.db.transaction() as conn:
                await conn.execute("INSERT OR REPLACE INTO runtime_state VALUES(?,?)", (cache_key, time.time()))
        args = [q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", minimum]
        where = "canonical_name LIKE ? ESCAPE '\\' AND post_count>=?"
        if category is not None:
            where += " AND category=?"
            args.append(category)
        return await self.db.rows(
            f"SELECT * FROM tag_catalog WHERE {where} ORDER BY (canonical_name=?) DESC,post_count DESC LIMIT 20",
            (*args, q),
        )
