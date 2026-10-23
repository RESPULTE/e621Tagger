"""Bounded thumbnail cache and authenticated, range-aware original streaming."""

import asyncio
import hashlib
import io
import re
import time
import warnings
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import HTTPException, Request
from PIL import Image, UnidentifiedImageError
from starlette.responses import Response, StreamingResponse

from .e621 import retry_delay
from .jobs import enqueue
from .quality import difference_hash
from .telemetry import Telemetry


class Media:
    def __init__(self, settings, db, transport=None):
        self.settings, self.db = settings, db
        self.client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent, "Accept-Encoding": "identity"},
            transport=transport,
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=settings.media_concurrency),
        )
        self.permits = asyncio.Semaphore(settings.media_concurrency)
        # Keep the total budget unchanged. Neither long originals nor prefetch may
        # monopolize it; with one configured slot this degrades to serial service.
        self.kind_permits = {
            kind: asyncio.Semaphore(max(1, settings.media_concurrency - 1))
            for kind in ("preview", "original")
        }
        self.timings = Telemetry()
        self.host_locks = {}
        self.host_next = {}
        self.cache_lock = asyncio.Lock()
        self.metrics = {
            "hits": 0,
            "misses": 0,
            "bytes": 0,
            "active": 0,
            "peak_active": 0,
            "throttled": 0,
            "last_download_ms": 0.0,
        }
        self.cache_root = db.root / "cache"
        self.cache_root.mkdir(exist_ok=True)

    async def close(self):
        await self.client.aclose()

    async def acquire_slot(self, kind="preview"):
        started = time.monotonic()
        acquired_kind = False
        try:
            await asyncio.wait_for(self.kind_permits[kind].acquire(), timeout=15)
            acquired_kind = True
            await asyncio.wait_for(self.permits.acquire(), timeout=max(0.01, 15 - (time.monotonic() - started)))
        except asyncio.TimeoutError:
            if acquired_kind:
                self.kind_permits[kind].release()
            raise HTTPException(503, "Media slots are busy", headers={"Retry-After": "2"}) from None
        except BaseException:
            if acquired_kind:
                self.kind_permits[kind].release()
            raise
        self.timings.record(f"{kind}_slot_wait_ms", (time.monotonic() - started) * 1000)
        self.metrics["active"] += 1
        self.metrics["peak_active"] = max(self.metrics["peak_active"], self.metrics["active"])

    def release_slot(self, kind="preview"):
        self.metrics["active"] -= 1
        self.permits.release()
        self.kind_permits[kind].release()

    def validate_url(self, url):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.settings.allowed_hosts
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.fragment
        ):
            raise HTTPException(403, "Media URL is outside the configured e621 CDN allowlist")

    async def _open(self, url, headers=None, method="GET"):
        for redirect in range(5):
            self.validate_url(url)
            host = urlsplit(url).hostname
            lock = self.host_locks.setdefault(host, asyncio.Lock())
            started = time.monotonic()
            async with lock:
                # A response may extend cooldown while this task sleeps.
                while (delay := self.host_next.get(host, 0) - time.monotonic()) > 0:
                    await asyncio.sleep(delay)
                self.host_next[host] = time.monotonic() + self.settings.media_interval
            self.timings.record("host_wait_ms", (time.monotonic() - started) * 1000)
            started = time.monotonic()
            response = await self.client.send(self.client.build_request(method, url, headers=headers), stream=True)
            self.timings.record("upstream_headers_ms", (time.monotonic() - started) * 1000)
            if response.status_code in {429, 503}:
                self.metrics["throttled"] += 1
                self.host_next[host] = max(
                    self.host_next.get(host, 0),
                    time.monotonic() + retry_delay(response.headers.get("Retry-After"), 2),
                )
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise HTTPException(502, "Invalid CDN redirect")
            url = urljoin(url, location)
        raise HTTPException(502, "Too many CDN redirects")

    async def cached_preview(self, post_id):
        row = await self.db.one(
            "SELECT i.*,p.preview_url AS current_url,p.md5 AS current_md5 FROM image_cache i JOIN posts p USING(post_id) "
            "WHERE i.post_id=? AND variant='preview'", (post_id,)
        )
        if not row:
            return None
        path = (self.db.root / row["relative_path"]).resolve()
        # V1 entries have no source identity; rebuild lazily instead of attaching
        # a new source MD5 to bytes whose origin cannot be established.
        stale = row["source_url"] is None or (
            row["source_url"] != row["current_url"] or row["source_md5"] != row["current_md5"]
        )
        if stale or not path.is_relative_to(self.cache_root) or not path.is_file() or path.stat().st_size != row["bytes"]:
            async with self.db.transaction() as conn:
                await conn.execute("DELETE FROM image_cache WHERE post_id=? AND variant='preview'", (post_id,))
            if path.is_relative_to(self.cache_root):
                path.unlink(missing_ok=True)
            return None
        # Minute-resolution LRU avoids a WAL write on every warm gallery request.
        if row["last_access_at"] < time.time() - 60:
            async with self.db.transaction() as conn:
                await conn.execute(
                    "UPDATE image_cache SET last_access_at=? WHERE post_id=? AND variant='preview'", (time.time(), post_id)
                )
        return row, path

    async def schedule(self, post_ids, *, fingerprints=False):
        scheduled = []
        async with self.db.transaction() as conn:
            count = await (
                await conn.execute("SELECT count(*) FROM jobs WHERE type='preview' AND status IN ('pending','running')")
            ).fetchone()
            remaining = self.settings.queue_limit - count[0]
            for post_id in dict.fromkeys(post_ids):
                if remaining <= 0:
                    break
                post = await (
                    await conn.execute("SELECT preview_url FROM posts WHERE post_id=?", (post_id,))
                ).fetchone()
                if not post or not post["preview_url"]:
                    continue
                cache = await (
                    await conn.execute("SELECT 1 FROM image_cache WHERE post_id=? AND variant='preview'", (post_id,))
                ).fetchone()
                fingerprint = await (
                    await conn.execute(
                        "SELECT 1 FROM image_fingerprints f JOIN posts p USING(post_id) "
                        "WHERE f.post_id=? AND f.md5 IS p.md5 AND f.preview_url IS p.preview_url", (post_id,)
                    )
                ).fetchone() if fingerprints else None
                prior = await (
                    await conn.execute("SELECT status,updated_at FROM jobs WHERE job_key=?", (f"preview:{post_id}",))
                ).fetchone()
                if (cache and (not fingerprints or fingerprint)) or (
                    prior and prior["status"] == "failed" and prior["updated_at"] > time.time() - 300
                ):
                    continue
                job_id = await enqueue(conn, "preview", f"preview:{post_id}", {"post_id": post_id})
                scheduled.append(job_id)
                if not prior or prior["status"] not in {"pending", "running"}:
                    remaining -= 1
        return scheduled

    def _thumbnail(self, raw):
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > self.settings.preview_max_pixels:
                    raise ValueError("Preview exceeds the pixel limit")
                image.seek(0)
                image.thumbnail((320, 320))
                out = io.BytesIO()
                image.convert("RGB").save(out, "WEBP", quality=82)
                return out.getvalue()

    async def download_preview(self, post_id):
        async with self.cache_lock:
            cached = await self.cached_preview(post_id)
            if cached:
                row, path = cached
                raw = await asyncio.to_thread(path.read_bytes)
                if row["source_url"] is not None and hashlib.sha256(raw).hexdigest() == row["etag"]:
                    post = {"md5": row["source_md5"], "preview_url": row["source_url"]}
                    await self.record_fingerprint(post_id, post, raw)
                    return
                async with self.db.transaction() as conn:
                    await conn.execute("DELETE FROM image_cache WHERE post_id=? AND variant='preview'", (post_id,))
                path.unlink(missing_ok=True)
        post = await self.db.one("SELECT md5,preview_url FROM posts WHERE post_id=?", (post_id,))
        if not post or not post["preview_url"]:
            raise HTTPException(404, "Thumbnail unavailable")
        start = time.monotonic()
        await self.acquire_slot()
        response = None
        try:
            response = await self._open(post["preview_url"])
            try:
                if response.status_code != 200:
                    raise HTTPException(
                        response.status_code if response.status_code in {403, 404, 429, 503} else 502,
                        "CDN could not supply thumbnail",
                    )
                if not response.headers.get("content-type", "").startswith("image/"):
                    raise HTTPException(415, "Preview is not an image")
                raw = bytearray()
                async for chunk in response.aiter_bytes(65536):
                    raw.extend(chunk)
                    self.metrics["bytes"] += len(chunk)
                    if len(raw) > self.settings.preview_max_bytes:
                        raise HTTPException(413, "Preview exceeds the download limit")
            finally:
                await response.aclose()
        finally:
            self.release_slot()
        try:
            thumb = await asyncio.to_thread(self._thumbnail, raw)
        except (
            ValueError,
            OSError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            raise HTTPException(415, "Corrupt or unsupported thumbnail") from None
        async with self.cache_lock:
            await self._prune(len(thumb))
            relative = Path("cache") / f"{post_id}.webp"
            path = self.db.root / relative
            temporary = path.with_suffix(".part")
            try:
                await asyncio.to_thread(temporary.write_bytes, thumb)
                temporary.replace(path)
                try:
                    async with self.db.transaction() as conn:
                        await conn.execute(
                            "INSERT OR REPLACE INTO image_cache VALUES(?,'preview',?,?,?,?,?,?)",
                            (post_id, relative.as_posix(), len(thumb), hashlib.sha256(thumb).hexdigest(), time.time(),
                             post["preview_url"], post["md5"]),
                        )
                except BaseException:
                    # A disk-full database must not leave unaccounted files outside the LRU quota.
                    path.unlink(missing_ok=True)
                    raise
            finally:
                temporary.unlink(missing_ok=True)
        self.metrics["last_download_ms"] = round((time.monotonic() - start) * 1000, 1)
        self.timings.record("preview_ready_ms", (time.monotonic() - start) * 1000)
        await self.record_fingerprint(post_id, post, thumb)

    async def record_fingerprint(self, post_id, post, raw):
        value = await asyncio.to_thread(difference_hash, raw)
        async with self.db.transaction() as conn:
            # A discovery refresh may replace source metadata during the download.
            await conn.execute(
                "INSERT OR REPLACE INTO image_fingerprints SELECT post_id,md5,preview_url,? FROM posts "
                "WHERE post_id=? AND md5 IS ? AND preview_url IS ?",
                (value, post_id, post["md5"], post["preview_url"]),
            )

    async def _prune(self, incoming):
        total = await self.db.one("SELECT COALESCE(sum(bytes),0) AS bytes FROM image_cache")
        required = total["bytes"] + incoming - self.settings.cache_bytes
        if required <= 0:
            return
        # Labeled thumbnails are retained longer, but never override the hard disk quota.
        while required > 0:
            rows = await self.db.rows(
                "SELECT i.*,EXISTS(SELECT 1 FROM labels l WHERE l.post_id=i.post_id) AS labeled "
                "FROM image_cache i ORDER BY labeled,last_access_at LIMIT 64"
            )
            if not rows:
                break
            for row in rows:
                path = (self.db.root / row["relative_path"]).resolve()
                if path.is_relative_to(self.cache_root):
                    path.unlink(missing_ok=True)
                async with self.db.transaction() as conn:
                    await conn.execute(
                        "DELETE FROM image_cache WHERE post_id=? AND variant=?", (row["post_id"], row["variant"])
                    )
                required -= row["bytes"]
                if required <= 0:
                    return
        if required > 0:
            raise HTTPException(507, "Thumbnail cache quota is too small")

    async def preview_response(self, request: Request, post_id):
        async with self.cache_lock:
            cached = await self.cached_preview(post_id)
            if cached:
                row, path = cached
                self.metrics["hits"] += 1
                raw = await asyncio.to_thread(path.read_bytes)
                if hashlib.sha256(raw).hexdigest() != row["etag"]:
                    async with self.db.transaction() as conn:
                        await conn.execute("DELETE FROM image_cache WHERE post_id=? AND variant='preview'", (post_id,))
                    path.unlink(missing_ok=True)
                    cached = None
                else:
                    headers = {"ETag": f'"{row["etag"]}"', "Cache-Control": "private, max-age=86400"}
                    if request.headers.get("if-none-match") == headers["ETag"]:
                        return Response(status_code=304, headers=headers)
                    return Response(raw, media_type="image/webp", headers=headers)
        self.metrics["misses"] += 1
        post = await self.db.one("SELECT preview_url FROM posts WHERE post_id=?", (post_id,))
        if not post or not post["preview_url"]:
            raise HTTPException(404, "Thumbnail unavailable")
        job = await self.db.one("SELECT status,error,updated_at FROM jobs WHERE job_key=?", (f"preview:{post_id}",))
        if job and job["status"] == "failed" and job["updated_at"] > time.time() - 300:
            raise HTTPException(415, job["error"])
        await self.schedule([post_id])
        return Response(status_code=202, headers={"Retry-After": "1", "Cache-Control": "no-store"})

    async def stream_response(self, request: Request, post_id, variant):
        column = {"sample": "sample_url", "original": "file_url", "video_mp4": "video_url"}[variant]
        post = await self.db.one(f"SELECT {column} AS url FROM posts WHERE post_id=?", (post_id,))
        if not post or not post["url"]:
            raise HTTPException(404, "Media unavailable")
        headers = {}
        if request.headers.get("range"):
            if not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", request.headers["range"]):
                raise HTTPException(416, "Only a single byte range is supported")
            headers["Range"] = request.headers["range"]
        if request.headers.get("if-range"):
            headers["If-Range"] = request.headers["if-range"]
        await self.acquire_slot("original")
        response = None
        handed_off = False
        try:
            response = await self._open(post["url"], headers, method=request.method)
            forwarded = {
                key: response.headers[key]
                for key in ("content-type", "content-length", "content-range", "accept-ranges", "etag", "last-modified")
                if key in response.headers
            }
            forwarded["Cache-Control"] = "private, no-store"
            if response.status_code == 416:
                return Response(status_code=416, headers=forwarded)
            if response.status_code not in {200, 206}:
                raise HTTPException(
                    response.status_code if response.status_code in {403, 404, 429, 503} else 502,
                    "Original media unavailable",
                )
            content_type = response.headers.get("content-type", "").split(";")[0]
            if (
                not (content_type.startswith("image/") or content_type.startswith("video/"))
                or content_type == "image/svg+xml"
            ):
                raise HTTPException(415, "Unsupported media type")
            if request.method == "HEAD":
                return Response(status_code=response.status_code, headers=forwarded)

            async def chunks():
                try:
                    async for chunk in response.aiter_raw(65536):
                        self.metrics["bytes"] += len(chunk)
                        yield chunk
                finally:
                    try:
                        await response.aclose()
                    finally:
                        self.release_slot("original")

            handed_off = True
            return StreamingResponse(chunks(), status_code=response.status_code, headers=forwarded)
        except httpx.TransportError:
            raise HTTPException(502, "Media server could not be reached") from None
        finally:
            if not handed_off:
                try:
                    if response:
                        await response.aclose()
                finally:
                    self.release_slot("original")
