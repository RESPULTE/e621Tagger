"""Exact local lookups in a checksum-verified publisher index. No model inference."""

import asyncio
import hashlib
import json
from pathlib import Path

import numpy as np
from fastapi import HTTPException

from .neighbor_index import FILES, REVISION


def file_sha256(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def validate(directory: Path, *, published=True):
    arrays, manifest = {}, {"revision": REVISION, "files": {}}
    for kind, (dtype, shape, size, digest) in FILES.items():
        path = directory / f"hydra35_embed_{kind}.npy"
        with path.open("rb") as stream:
            actual = file_sha256(stream)
        if published and (path.stat().st_size != size or actual != digest):
            raise ValueError(f"Checksum/size mismatch: {path.name}")
        value = np.load(path, mmap_mode="r", allow_pickle=False)
        if value.dtype.str != dtype or (published and value.shape != shape):
            raise ValueError(f"Invalid dtype/shape: {path.name}")
        arrays[kind] = value
        manifest["files"][kind] = {"sha256": actual, "size": path.stat().st_size}
    posts, ids, distances = (arrays[k] for k in ("postids", "ids", "dists"))
    if posts.ndim != 1 or ids.shape != (len(posts), 64) or distances.shape != ids.shape or not len(posts):
        raise ValueError("Index arrays must have matching rows and 64 neighbors")
    if np.any(posts <= 0) or len(np.unique(posts)) != len(posts):
        raise ValueError("Source IDs must be positive and unique")
    sorted_ids = bool(np.all(posts[:-1] < posts[1:]))
    for start in range(0, len(posts), 8192):
        block = distances[start : start + 8192].astype(np.float32)
        if (
            np.any(ids[start : start + 8192] <= 0)
            or not np.all(np.isfinite(block))
            or np.any(block < 0)
            or np.any(np.diff(block, axis=1) < -0.002)
        ):
            raise ValueError("Invalid neighbor IDs/distances/order")
    if not sorted_ids:
        np.save(directory / "sorted_rows.npy", np.argsort(posts), allow_pickle=False)
    manifest["sorted"] = sorted_ids
    (directory / "validated.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


class HydraIndex:
    revision = REVISION

    def __init__(self, directory):
        self.directory = directory
        self.error = "Install the Hydra index and configure STUDIO_HYDRA_INDEX_ROOT"
        self.arrays = {}
        self.rows = None

    def open(self):
        try:
            if self.directory is None:
                return
            manifest = json.loads((self.directory / "validated.json").read_text(encoding="utf-8"))
            if manifest["revision"] != REVISION:
                raise ValueError("Index revision mismatch")
            # Verify once per process, off the event loop. Never trust a stale installation marker.
            for kind, (dtype, shape, size, digest) in FILES.items():
                path = self.directory / f"hydra35_embed_{kind}.npy"
                with path.open("rb") as stream:
                    actual = file_sha256(stream)
                if path.stat().st_size != size or actual != digest:
                    raise ValueError("Index checksum mismatch")
                array = np.load(path, mmap_mode="r", allow_pickle=False)
                if array.dtype.str != dtype or array.shape != shape:
                    raise ValueError("Index format mismatch")
                self.arrays[kind] = array
            posts = self.arrays["postids"]
            if not np.all(posts[:-1] < posts[1:]):
                self.rows = np.load(self.directory / "sorted_rows.npy", mmap_mode="r", allow_pickle=False)
                if (
                    self.rows.shape != posts.shape
                    or self.rows.dtype.kind not in "iu"
                    or np.any(self.rows < 0)
                    or np.any(self.rows >= len(posts))
                    or not np.all(np.diff(posts[self.rows]) > 0)
                ):
                    raise ValueError("Invalid sorted row mapping")
            self.error = None
        except (OSError, ValueError, KeyError, TypeError):
            self.arrays.clear()
            self.rows = None
            self.error = "Hydra index validation failed; run python -m backend.install_hydra --validate-only DIRECTORY"

    def lookup_sync(self, seed_id):
        if self.error:
            raise HTTPException(503, self.error)
        posts = self.arrays["postids"]
        # searchsorted's sorter avoids materializing six million IDs per request.
        position = int(np.searchsorted(posts, seed_id, sorter=self.rows))
        row = int(self.rows[position]) if self.rows is not None and position < len(posts) else position
        if row >= len(posts) or int(posts[row]) != seed_id:
            return {"revision": self.revision, "coverage": "not_indexed", "neighbors": []}
        neighbors, seen = [], {seed_id}
        for rank, (post, distance) in enumerate(zip(self.arrays["ids"][row], self.arrays["dists"][row]), 1):
            post, distance = int(post), float(distance)
            if post <= 0 or not np.isfinite(distance) or distance < 0:
                raise HTTPException(503, "Invalid Hydra neighbor row")
            if post not in seen:
                neighbors.append({"post_id": post, "rank": rank, "distance": distance})
                seen.add(post)
        return {"revision": self.revision, "coverage": "available", "neighbors": neighbors}

    async def lookup(self, seed_id):
        return await asyncio.to_thread(self.lookup_sync, seed_id)
