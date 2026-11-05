"""Bounded media decoding and immutable sample storage, independent of API/review orchestration."""

import asyncio
import hashlib
import io
import os
import time
import warnings
from uuid import NAMESPACE_URL, uuid5

import httpx
from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

from .quality import difference_hash

ANIMATED_EXTENSIONS = {"gif", "webm", "mp4", "webp", "avif"}
MAX_ANIMATION_BYTES = 32 * 1024 * 1024
MAX_ANIMATION_FRAMES = 2000
MAX_DECODE_PIXELS = 200_000_000


def normalize_image(raw, maximum_pixels):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > maximum_pixels or getattr(image, "is_animated", False):
                    raise HTTPException(413, "Choose one still frame within the image pixel limit")
                image.load()
                image = image.convert("RGB")
                image.thumbnail((4096, 4096))
                out = io.BytesIO()
                image.save(out, "PNG")
                return out.getvalue(), image.width, image.height
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(415, "Sample could not be decoded safely") from None


def store_image(root, raw, maximum_pixels):
    png, width, height = normalize_image(raw, maximum_pixels)
    folder = root / "samples"
    if folder.is_symlink():
        raise HTTPException(409, "Sample storage is unsafe")
    folder.mkdir(exist_ok=True)
    digest = hashlib.md5(png).hexdigest()
    name = hashlib.sha256(png).hexdigest() + ".png"
    path = folder / name
    if path.is_symlink():
        raise HTTPException(409, "Sample destination is unsafe")
    # Content-addressed immutable files: concurrent saves publish identical bytes.
    if not path.exists():
        import tempfile

        descriptor, temporary = tempfile.mkstemp(dir=folder, suffix=".part")
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(png)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {"image_path": name, "image_md5": digest, "dhash": difference_hash(png), "width": width, "height": height}


async def insert_sample(conn, project_id, post_id, source_md5, kind, timestamp_ms, image):
    sample_id = str(uuid5(NAMESPACE_URL, f"hydra:{project_id}:{post_id}:{source_md5}:{kind}:{timestamp_ms}"))
    now = time.time()
    await conn.execute(
        "INSERT OR IGNORE INTO media_samples(id,project_id,post_id,source_md5,kind,timestamp_ms,"
        "image_path,image_md5,dhash,width,height,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            sample_id,
            project_id,
            post_id,
            source_md5,
            kind,
            timestamp_ms,
            image["image_path"],
            image["image_md5"],
            image["dhash"],
            image["width"],
            image["height"],
            now,
            now,
        ),
    )
    return dict(await (await conn.execute("SELECT * FROM media_samples WHERE id=?", (sample_id,))).fetchone())


async def checked_post(state, post_id, source_md5):
    post = await state.feed.raw(post_id, fresh=True)
    source = post.get("file") or {}
    if post.get("flags", {}).get("deleted") or not source.get("url"):
        raise HTTPException(404, "Source media is unavailable")
    if source.get("md5") != source_md5:
        raise HTTPException(409, "Source media changed; reopen it before sampling")
    if source.get("ext") not in ANIMATED_EXTENSIONS:
        raise HTTPException(415, "Frame sampling is for animated media")
    return post


def sample_path(db, row):
    root = db.root / "samples"
    path = root / row["image_path"]
    if root.is_symlink() or path.is_symlink() or not path.is_file() or path.resolve().parent != root.resolve():
        raise HTTPException(404, "Saved sample image is unavailable")
    return path


async def resolve_dataset_sample(db, sample_id, post_id, source_md5, sample_md5):
    row = await db.one(
        "SELECT s.* FROM media_samples s JOIN posts p ON p.post_id=s.post_id AND p.md5=s.source_md5 WHERE s.id=? AND s.post_id=? AND s.source_md5=? AND s.image_md5=?",
        (sample_id, post_id, source_md5, sample_md5),
    )
    if not row:
        return None
    try:
        return sample_path(db, row)
    except HTTPException:
        return None


async def animation_bytes(state, post_id, source_md5):
    media = state.media
    key = (post_id, source_md5)
    # Bound compressed originals globally, not per artwork. Scrubbing and GIF
    # playback reuse one source download rather than fetching it for every frame.
    async with media.animation_lock:
        now = time.monotonic()
        cached = media.animation_cache.get(key)
        if cached and now - cached[0] < 300:
            media.animation_cache.move_to_end(key)
            return cached[1]
        raw = await _download_animation(state, post_id, source_md5)
        media.animation_cache[key] = (now, raw)
        while sum(len(item[1]) for item in media.animation_cache.values()) > 64 * 1024 * 1024:
            media.animation_cache.popitem(last=False)
        return raw


async def _download_animation(state, post_id, source_md5):
    post = await checked_post(state, post_id, source_md5)
    if post["file"]["ext"] not in {"gif", "webp", "avif"}:
        raise HTTPException(415, "Use native video playback for this format")
    media = state.media
    response = None
    await media.acquire_slot("original")
    try:
        response = await media._open(post["file"]["url"])
        if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
            raise HTTPException(502, "Animation download failed")
        raw = bytearray()
        async for chunk in response.aiter_raw(65536):
            if len(raw) + len(chunk) > MAX_ANIMATION_BYTES:
                raise HTTPException(413, "Animation exceeds the 32 MiB sampling limit")
            raw.extend(chunk)
        if hashlib.md5(raw).hexdigest() != source_md5:
            raise HTTPException(409, "Animation identity changed; reopen the artwork")
        return bytes(raw)
    except httpx.TransportError:
        raise HTTPException(502, "Animation server could not be reached") from None
    finally:
        if response:
            await response.aclose()
        media.release_slot("original")


async def uses_thumbnail_review(state, post):
    extension = (post.get("file") or {}).get("ext")
    if extension in {"gif", "mp4", "webm"}:
        return True
    if extension not in {"webp", "avif"}:
        return False
    # These containers can also be still images. Preserve full-image tagging
    # for stills; only animated containers become thumbnail training samples.
    raw = await animation_bytes(state, post["id"], post["file"]["md5"])
    async with state.media.animation_decode_slots:
        metadata = await asyncio.to_thread(decode_animation, raw)
    return metadata["animated"]


def decode_animation(raw, selected=None):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                frames = getattr(image, "n_frames", 1)
                if frames > MAX_ANIMATION_FRAMES or image.width * image.height * frames > MAX_DECODE_PIXELS:
                    raise HTTPException(413, "Animation exceeds the frame decoding budget")
                if selected is not None:
                    if not 0 <= selected < frames:
                        raise HTTPException(404, "Animation frame not found")
                    image.seek(selected)
                    still = image.convert("RGB")
                    still.thumbnail((4096, 4096))
                    out = io.BytesIO()
                    still.save(out, "PNG")
                    return out.getvalue()
                timeline, position = [], 0
                for index in range(frames):
                    image.seek(index)
                    duration = max(20, int(image.info.get("duration", 100)))
                    timeline.append({"index": index, "timestamp_ms": position, "duration_ms": duration})
                    position += duration
                return {
                    "animated": frames > 1,
                    "duration_ms": position,
                    "frames": timeline,
                    "width": image.width,
                    "height": image.height,
                }
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(415, "Animation could not be decoded safely") from None
