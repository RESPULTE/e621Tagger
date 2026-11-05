"""Bounded temporary reference images; never candidates or training samples."""

import asyncio
import hashlib
import io
import warnings

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

FORMATS = {"JPEG", "PNG", "WEBP"}


def validate_image(raw, maximum_pixels):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in FORMATS or getattr(image, "is_animated", False):
                    raise HTTPException(415, "Use a still JPEG, PNG or WebP reference image")
                if image.width * image.height > maximum_pixels:
                    raise HTTPException(413, "Reference exceeds the decoded pixel limit")
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(415, "Reference image could not be decoded safely") from None
    return hashlib.sha256(raw).hexdigest()


async def read_upload(request):
    settings = request.app.state.settings
    if request.headers.get("content-encoding", "identity") != "identity":
        raise HTTPException(415, "Compressed upload bodies are unsupported")
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > settings.reference_max_bytes):
        raise HTTPException(413, "Reference exceeds the upload byte limit")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > settings.reference_max_bytes:
            raise HTTPException(413, "Reference exceeds the upload byte limit")
        raw.extend(chunk)
    if not raw:
        raise HTTPException(422, "Choose a reference image")
    raw = bytes(raw)
    digest = await asyncio.to_thread(validate_image, raw, settings.reference_max_pixels)
    return raw, digest


async def read_post(media, post):
    source = post.get("file") or {}
    if post.get("flags", {}).get("deleted") or not source.get("url"):
        raise HTTPException(404, "Source image is unavailable")
    if source.get("ext") not in {"jpg", "jpeg", "png", "webp"}:
        raise HTTPException(415, "Use a still JPEG, PNG or WebP post")
    maximum = media.settings.reference_max_bytes
    size = source.get("size")
    if isinstance(size, int) and not 0 < size <= maximum:
        raise HTTPException(413, "Source image exceeds the reference byte limit")
    response = None
    await media.acquire_slot("original")
    try:
        response = await media._open(source["url"])
        if response.status_code != 200:
            raise HTTPException(502, "Source image download failed; retry later")
        length = response.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > maximum):
            raise HTTPException(413, "Source image exceeds the reference byte limit")
        if response.headers.get("content-encoding", "identity") != "identity":
            raise HTTPException(502, "Source image encoding changed")
        raw = bytearray()
        async for chunk in response.aiter_raw(65536):
            if len(raw) + len(chunk) > maximum:
                raise HTTPException(413, "Source image exceeds the reference byte limit")
            raw.extend(chunk)
        if not raw or (length and len(raw) != int(length)) or (size and len(raw) != size):
            raise HTTPException(502, "Source image download is incomplete")
        if hashlib.md5(raw).hexdigest() != source.get("md5"):
            raise HTTPException(409, "Source image identity changed; refresh the post")
        raw = bytes(raw)
        digest = await asyncio.to_thread(validate_image, raw, media.settings.reference_max_pixels)
        return raw, digest
    finally:
        if response:
            await response.aclose()
        media.release_slot("original")
