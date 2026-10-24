"""One bounded, integrity-checked transfer primitive for every explicit output."""

import asyncio
import hashlib
import os
import shutil
import time

from fastapi import HTTPException

from backend.filesystem import disk


class Transfers:
    def __init__(self, db, media, settings):
        self.db, self.media, self.settings = db, media, settings
        self.slots = asyncio.Semaphore(min(settings.download_concurrency, max(1, settings.media_concurrency - 2)))
        self.metrics = {"active": 0, "peak_active": 0, "completed": 0, "bytes": 0}

    async def save(self, path, url, md5, expected_size, root, *, job_id, current):
        async with self.slots:
            self.metrics["active"] += 1
            self.metrics["peak_active"] = max(self.metrics["peak_active"], self.metrics["active"])
            try:
                return await self._save(path, url, md5, expected_size, root, job_id, current)
            finally:
                self.metrics["active"] -= 1

    async def _save(self, path, url, md5, expected_size, root, job_id, current):
        maximum = self.settings.library_max_bytes
        if isinstance(expected_size, int) and not 0 < expected_size <= maximum:
            raise HTTPException(413, "Original exceeds the configured download limit")
        if (await disk(shutil.disk_usage, root)).free < (
            expected_size or maximum
        ) + self.settings.library_reserve_bytes:
            raise HTTPException(507, "Download storage has insufficient free space")
        response = None
        await self.media.acquire_slot("original")
        count, checked_at = 0, 0
        md5_hash, sha256 = hashlib.md5(), hashlib.sha256()
        try:
            response = await self.media._open(url)
            if response.status_code != 200:
                raise HTTPException(
                    response.status_code if response.status_code in {403, 404, 429, 503} else 502,
                    "CDN could not supply the original",
                )
            kind = response.headers.get("content-type", "").split(";")[0]
            if not (kind.startswith("image/") or kind.startswith("video/")) or kind == "image/svg+xml":
                raise HTTPException(415, "Original response is not supported media")
            length = response.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > maximum):
                raise HTTPException(413, "Original exceeds the configured download limit")
            if response.headers.get("content-encoding", "identity") != "identity":
                raise HTTPException(502, "Original response unexpectedly changed encoding")
            if not await current():
                raise HTTPException(409, "Download was cancelled or superseded")
            with path.open("xb") as output:
                inode = os.fstat(output.fileno()).st_ino
                if job_id:
                    async with self.db.transaction() as conn:
                        await conn.execute(
                            "INSERT OR REPLACE INTO download_partials VALUES(?,?,?,?)",
                            (job_id, str(path.absolute()), inode, os.fstat(output.fileno()).st_dev),
                        )
                        await conn.execute(
                            "UPDATE jobs SET progress_bytes=0,total_bytes=?,updated_at=? WHERE id=?",
                            (int(length) if length else expected_size, time.time(), job_id),
                        )
                async for chunk in response.aiter_raw(65536):
                    count += len(chunk)
                    if count > maximum:
                        raise HTTPException(413, "Original exceeds the configured download limit")
                    if time.monotonic() - checked_at >= 0.15:
                        if not await current():
                            raise HTTPException(409, "Download was cancelled or superseded")
                        if job_id:
                            async with self.db.transaction() as conn:
                                await conn.execute(
                                    "UPDATE jobs SET progress_bytes=?,updated_at=? WHERE id=?",
                                    (count, time.time(), job_id),
                                )
                        checked_at = time.monotonic()
                    await disk(output.write, chunk)
                    md5_hash.update(chunk)
                    sha256.update(chunk)
                    self.media.metrics["bytes"] += len(chunk)
                await disk(output.flush)
                await disk(os.fsync, output.fileno())
            if (
                not count
                or (length and count != int(length))
                or (isinstance(expected_size, int) and count != expected_size)
            ):
                raise HTTPException(502, "Original download was incomplete")
            if md5_hash.hexdigest() != md5:
                raise HTTPException(502, "Original checksum did not match e621; nothing was saved")
            if not await current():
                raise HTTPException(409, "Download was cancelled or superseded")
            if job_id:
                async with self.db.transaction() as conn:
                    await conn.execute(
                        "UPDATE jobs SET progress_bytes=?,total_bytes=?,updated_at=? WHERE id=?",
                        (count, count, time.time(), job_id),
                    )
            self.metrics["completed"] += 1
            self.metrics["bytes"] += count
            return {"bytes": count, "sha256": sha256.hexdigest(), "md5": md5}
        finally:
            try:
                if response:
                    await response.aclose()
            finally:
                self.media.release_slot("original")
