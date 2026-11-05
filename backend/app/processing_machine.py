"""Authenticated outbound GPU image-search bridge; no historical lookup provider."""

import hmac
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from starlette.responses import Response

from .image_jobs import ImageCapability, ImageJob, ImageJobs, ImageResult


class WorkerPoll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal[2]
    worker_id: UUID
    image_capability: ImageCapability | None = None
    busy: bool = False


class WorkerAssignment(BaseModel):
    job: ImageJob | None = None


class ProcessingMachine:
    def __init__(self, token):
        self.token = token
        self.worker_id = None
        self.seen = 0
        self.heartbeat_ttl = 15
        self.images = ImageJobs(self)

    @property
    def error(self):
        return self.images.error

    @property
    def revision(self):
        return self.images.capability.index_revision if self.images.capability else ""

    @property
    def corpus_size(self):
        return self.images.capability.corpus_size if self.images.capability else None

    async def search_image(self, query, *, raw=None, post=None):
        return await self.images.search(query, raw=raw, post=post)

    def poll(self, body):
        now = time.monotonic()
        if self.worker_id != body.worker_id and now - self.seen < self.heartbeat_ttl:
            raise HTTPException(409, "Another processing worker is online")
        self.worker_id, self.seen = body.worker_id, now
        self.images.capability = body.image_capability
        return {"job": None if body.busy else self.images.poll(body)}

    async def close(self):
        self.images.close()


async def require_worker(request: Request):
    token = request.app.state.settings.similarity_worker_token
    supplied = request.headers.get("authorization", "")
    if not token or not hmac.compare_digest(supplied, "Bearer " + token):
        raise HTTPException(401, "Processing worker authentication required")
    return request.app.state.processing_machine


router = APIRouter(prefix="/api/processing-machine", dependencies=[Depends(require_worker)])


@router.post("/poll", response_model=WorkerAssignment)
async def poll(body: WorkerPoll, request: Request):
    return request.app.state.processing_machine.poll(body)


@router.post("/results", status_code=204)
async def complete(body: ImageResult, request: Request):
    request.app.state.processing_machine.images.complete(body)


@router.get("/jobs/{job_id}/image")
async def reference(job_id: UUID, lease: UUID, worker_id: UUID, request: Request):
    raw, digest = await request.app.state.processing_machine.images.image(job_id, lease, worker_id)
    return Response(raw, media_type="application/octet-stream", headers={"X-Image-SHA256": digest})
