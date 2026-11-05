"""Version-2 image jobs on the existing outbound-only, authenticated bridge."""

import asyncio
import time
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .reference_images import read_post

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
MD5 = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
PostId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
OFFLINE = "Image search unavailable: RTX 4070 laptop offline"
ERRORS = {
    "invalid_image": "Reference image could not be encoded",
    "inference_failed": "Hydra inference failed on the processing machine; check its GPU/runtime",
    "index_failed": "Vector index failed validation or reached capacity; check the processing machine",
}


class ImageCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_revision: Digest
    embedding_space: Digest
    index_revision: Digest
    metric: Literal["unit_l2"] = "unit_l2"
    corpus_size: int | None = Field(default=None, strict=True, ge=0, le=10_050_000)


class ImageJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["image_search"] = "image_search"
    protocol_version: Literal[2] = 2
    id: UUID
    lease: UUID
    query_type: Literal["upload", "post"]
    post_id: PostId | None = None
    source_md5: MD5 | None = None
    query_sha256: Digest | None = None
    model_revision: Digest
    embedding_space: Digest

    @model_validator(mode="after")
    def query_identity(self):
        if self.query_type == "post" and (self.post_id is None or self.source_md5 is None):
            raise ValueError("Post queries require source identity")
        if self.query_type == "upload" and (self.post_id is not None or self.source_md5 is not None or self.query_sha256 is None):
            raise ValueError("Uploaded queries have a checksum, never a fabricated post ID")
        return self


class ImageNeighbor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    post_id: PostId
    # Publisher snapshots contain post IDs and vectors. Missing hashes are
    # resolved from current metadata before a candidate is eligible for review.
    md5: MD5 | None = None
    rank: int = Field(strict=True, ge=1, le=64)
    distance: float = Field(ge=0, le=2.001, allow_inf_nan=False)


class ImageResult(ImageJob):
    worker_id: UUID
    query_sha256: Digest | None = None
    index_revision: Digest
    metric: Literal["unit_l2"] = "unit_l2"
    corpus_size: int | None = Field(default=None, strict=True, ge=0, le=10_050_000)
    coverage: Literal["available", "failed"]
    error: Literal["invalid_image", "inference_failed", "index_failed"] | None = None
    neighbors: list[ImageNeighbor] = Field(max_length=64)


class ImageJobs:
    def __init__(self, machine):
        self.machine = machine
        self.capability = None
        self.requests = {}
        self.timeout = 90
        self.media = None

    @property
    def error(self):
        if not self.machine.token or time.monotonic() - self.machine.seen >= self.machine.heartbeat_ttl:
            return OFFLINE
        if not self.capability:
            return "Image search unavailable: GPU worker is not ready"
        return None

    def poll(self, body):
        self.capability = body.image_capability
        if not self.capability:
            return None
        for item in self.requests.values():
            if item["future"].done() or item["expires"] <= time.monotonic() or item["job"].lease is not None:
                continue
            # A replacement worker must use the space that accepted this search.
            if (item["job"].model_revision, item["job"].embedding_space) != (
                self.capability.model_revision, self.capability.embedding_space,
            ):
                item["future"].set_exception(HTTPException(409, "Processing machine embedding space changed; retry the search"))
                continue
            item["job"].lease = uuid4()
            item["worker"] = body.worker_id
            return item["job"].model_dump(mode="json")
        return None

    async def search(self, query, *, raw=None, post=None):
        if self.error:
            raise HTTPException(503, self.error)
        if len(self.requests) >= 32:
            raise HTTPException(429, "Processing machine queue is full")
        job = ImageJob(id=uuid4(), lease=uuid4(), **query,
                       model_revision=self.capability.model_revision, embedding_space=self.capability.embedding_space)
        # None is an internal not-yet-leased marker, never sent on the wire.
        job.lease = None
        future = asyncio.get_running_loop().create_future()
        item = {"job": job, "future": future, "worker": None, "expires": time.monotonic() + self.timeout,
                "raw": raw, "post": post, "digest": query.get("query_sha256"), "lock": asyncio.Lock()}
        self.requests[job.id] = item
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.timeout)
        except asyncio.TimeoutError:
            raise HTTPException(503, "Image retrieval timed out; retry when the processing machine is ready") from None
        finally:
            self.requests.pop(job.id, None)
            if not future.done():
                future.cancel()

    def leased(self, job_id, lease, worker):
        item = self.requests.get(job_id)
        if (item is None or item["future"].done() or item["expires"] <= time.monotonic()
                or item["job"].lease != lease or item["worker"] != worker or self.machine.worker_id != worker):
            raise HTTPException(409, "Image job lease expired or was cancelled")
        return item

    async def image(self, job_id, lease, worker):
        item = self.leased(job_id, lease, worker)
        async with item["lock"]:
            if item["raw"] is None:
                # The worker requests bytes only on a local-corpus miss. Metadata and
                # original transfers use the existing shared media gate and slots.
                try:
                    raw, digest = await asyncio.wait_for(read_post(self.media, item["post"]), timeout=30)
                except asyncio.TimeoutError:
                    raise HTTPException(503, "Source image transfer timed out") from None
                self.leased(job_id, lease, worker)
                item.update(raw=raw, digest=digest)
        return item["raw"], item["digest"]

    def complete(self, body):
        item = self.leased(body.id, body.lease, body.worker_id)
        job = item["job"]
        for field in ("query_type", "post_id", "source_md5", "model_revision", "embedding_space"):
            if getattr(body, field) != getattr(job, field):
                raise HTTPException(409, "Image job identity or embedding space changed")
        if body.coverage == "available" and (not body.query_sha256 or (item["digest"] and body.query_sha256 != item["digest"])):
            raise HTTPException(409, "Image reference checksum changed")
        neighbors = [n.model_dump() for n in body.neighbors]
        hashes = [n["md5"] for n in neighbors if n["md5"] is not None]
        if (len({n["post_id"] for n in neighbors}) != len(neighbors)
                or (body.corpus_size is not None and len(neighbors) > body.corpus_size)
                or len(set(hashes)) != len(hashes)
                or any(n["post_id"] == body.post_id or (n["md5"] is not None and n["md5"] == body.source_md5) for n in neighbors)
                or [n["rank"] for n in neighbors] != list(range(1, len(neighbors) + 1))
                or any((a["distance"], a["post_id"]) > (b["distance"], b["post_id"]) for a, b in zip(neighbors, neighbors[1:]))
                or (body.coverage == "failed" and (neighbors or not body.error))
                or (body.coverage == "available" and body.error)):
            raise HTTPException(422, "Invalid image retrieval evidence")
        if body.coverage == "failed":
            item["future"].set_exception(HTTPException(422, ERRORS[body.error]))
        else:
            item["future"].set_result({"revision": body.index_revision, "coverage": "available", "neighbors": neighbors,
                "query_type": body.query_type, "query_sha256": body.query_sha256, "source_post_id": body.post_id,
                "source_md5": body.source_md5, "model_revision": body.model_revision,
                "embedding_space": body.embedding_space, "metric": body.metric, "corpus_size": body.corpus_size})

    def close(self):
        for item in self.requests.values():
            item["future"].cancel()
        self.capability = None
