"""Bounded transient previews and authenticated, range-aware media streaming."""

import asyncio
import io
import re
import time
import warnings
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
            kind: asyncio.Semaphore(max(1, settings.media_concurrency - 1)) for kind in ("preview", "original")
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
        self.feed = None

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
                cooldown = await self.db.one("SELECT value FROM runtime_state WHERE key=?", ("cdn_next:" + host,))
                if cooldown:
                    self.host_next[host] = max(
                        self.host_next.get(host, 0), time.monotonic() + max(0, cooldown["value"] - time.time())
                    )
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
                delay = retry_delay(response.headers.get("Retry-After"), 2)
                self.host_next[host] = max(
                    self.host_next.get(host, 0),
                    time.monotonic() + delay,
                )
                async with self.db.transaction() as conn:
                    await conn.execute(
                        "INSERT INTO runtime_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=max(value,excluded.value)",
                        ("cdn_next:" + host, time.time() + delay),
                    )
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise HTTPException(502, "Invalid CDN redirect")
            url = urljoin(url, location)
        raise HTTPException(502, "Too many CDN redirects")

    async def resolve(self, post_id, expected_md5=None):
        if self.feed:
            post = await self.feed.row(post_id)
        else:
            post = await self.db.one("SELECT * FROM posts WHERE post_id=?", (post_id,))
        if expected_md5 and post and post["md5"] != expected_md5:
            raise HTTPException(409, "Media identity changed; refresh this view")
        return post

    async def schedule(self, post_ids, *, fingerprints=False):
        # Browsing prefetch never creates disk jobs. Only explicit quality work
        # retains a source-bound numeric fingerprint, never preview bytes.
        if not fingerprints:
            return []
        result = []
        async with self.db.transaction() as conn:
            for post_id in dict.fromkeys(post_ids[:64]):
                row = await (
                    await conn.execute(
                        "SELECT 1 FROM labels WHERE post_id=? AND state IN ('positive','negative')", (post_id,)
                    )
                ).fetchone()
                if row:
                    result.append(await enqueue(conn, "preview", f"preview:{post_id}", {"post_id": post_id}))
        return result

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

    async def thumbnail_bytes(self, post):
        if not post or post["unavailable"] or not post["preview_url"]:
            raise HTTPException(404, "Thumbnail unavailable")
        response = None
        await self.acquire_slot()
        try:
            response = await self._open(post["preview_url"])
            if response.status_code != 200:
                raise HTTPException(
                    response.status_code if response.status_code in {403, 404, 429, 503} else 502,
                    "Thumbnail unavailable",
                )
            if not response.headers.get("content-type", "").startswith("image/"):
                raise HTTPException(415, "Preview is not an image")
            raw = bytearray()
            async for chunk in response.aiter_bytes(65536):
                if len(raw) + len(chunk) > self.settings.preview_max_bytes:
                    raise HTTPException(413, "Preview exceeds the download limit")
                raw.extend(chunk)
                self.metrics["bytes"] += len(chunk)
            length = response.headers.get("content-length")
            if (
                length
                and response.headers.get("content-encoding", "identity") == "identity"
                and (not length.isdigit() or len(raw) != int(length))
            ):
                raise HTTPException(502, "Incomplete preview")
            try:
                return await asyncio.to_thread(self._thumbnail, raw)
            except (
                ValueError,
                OSError,
                UnidentifiedImageError,
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
            ):
                raise HTTPException(415, "Corrupt or unsupported thumbnail") from None
        finally:
            try:
                if response:
                    await response.aclose()
            finally:
                self.release_slot()

    async def download_preview(self, post_id):
        post = await self.db.one("SELECT * FROM posts WHERE post_id=?", (post_id,))
        raw = await self.thumbnail_bytes(post)
        await self.record_fingerprint(post_id, post, raw)

    async def record_fingerprint(self, post_id, post, raw):
        value = await asyncio.to_thread(difference_hash, raw)
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO image_fingerprints SELECT post_id,md5,preview_url,? FROM posts "
                "WHERE post_id=? AND md5 IS ? AND preview_url IS ? AND EXISTS(SELECT 1 FROM labels WHERE post_id=posts.post_id)",
                (value, post_id, post["md5"], post["preview_url"]),
            )

    async def preview_response(self, request: Request, post_id, source_md5=None):
        post = await self.resolve(post_id, source_md5)
        # HEAD does not fetch or render image bytes.
        headers = {"Cache-Control": "private, no-store"}
        if request.method == "HEAD":
            if not post or post["unavailable"] or not post["preview_url"]:
                raise HTTPException(404, "Thumbnail unavailable")
            return Response(media_type="image/webp", headers=headers)
        raw = await self.thumbnail_bytes(post)
        return Response(raw, media_type="image/webp", headers=headers)

    async def stream_response(self, request: Request, post_id, variant, source_md5=None):
        column = {"sample": "sample_url", "original": "file_url", "video_mp4": "video_url"}[variant]
        post = await self.resolve(post_id, source_md5)
        if not post or post["unavailable"] or not post[column]:
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
            response = await self._open(post[column], headers, method=request.method)
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
            if response.headers.get("content-encoding", "identity") != "identity":
                raise HTTPException(502, "Original response unexpectedly changed encoding")
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
