"""Bounded, outbound-polled lookup bridge. The server never opens the index."""

import asyncio
import hmac
import time
from collections import OrderedDict
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .neighbor_index import REVISION

PostId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
OFFLINE = "Processing machine offline"


class WorkerPoll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: UUID
    ready: bool
    index_revision: Literal[REVISION]


class LookupJob(BaseModel):
    id: UUID
    lease: UUID
    post_id: PostId
    index_revision: Literal[REVISION]


class WorkerAssignment(BaseModel):
    job: LookupJob | None = None


class Neighbor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    post_id: PostId
    rank: int = Field(strict=True, ge=1, le=64)
    distance: float = Field(ge=0, le=2.01, allow_inf_nan=False)


class WorkerResult(LookupJob):
    model_config = ConfigDict(extra="forbid")
    worker_id: UUID
    coverage: Literal["available", "not_indexed"]
    neighbors: list[Neighbor] = Field(max_length=64)


class ProcessingMachine:
    revision = REVISION

    def __init__(self, token):
        self.token = token
        self.worker_id = None
        self.seen = 0
        self.ready = False
        self.heartbeat_ttl = 15
        self.lookup_timeout = 20
        self.requests = {}
        self.cache = OrderedDict()

    @property
    def error(self):
        if not self.token or time.monotonic() - self.seen >= self.heartbeat_ttl:
            return OFFLINE
        if not self.ready:
            return "Processing machine index unavailable; install or validate the index on that machine"
        return None

    def poll(self, body):
        now = time.monotonic()
        if self.worker_id != body.worker_id and now - self.seen < self.heartbeat_ttl:
            raise HTTPException(409, "Another processing worker is online")
        self.worker_id, self.seen, self.ready = body.worker_id, now, body.ready
        if self.ready:
            for item in self.requests.values():
                if item["future"].done() or item["expires"] <= now:
                    continue
                if item["lease"] is None:
                    item["lease"] = uuid4()
                    item["worker"] = body.worker_id
                    return {"job": {k: item[k] for k in ("id", "lease", "post_id", "index_revision")}}
        return {"job": None}

    async def lookup(self, seed_id):
        now = time.monotonic()
        # Authenticated worker evidence may still support reviews of retained results offline.
        cached = self.cache.get(seed_id)
        if cached and cached[0] > now:
            self.cache.move_to_end(seed_id)
            return cached[1]
        self.cache.pop(seed_id, None)
        if self.error:
            raise HTTPException(503, self.error)
        item = self.requests.get(seed_id)
        if item is None:
            if len(self.requests) >= 32:
                raise HTTPException(429, "Processing machine lookup queue is full")
            item = dict(
                id=uuid4(),
                lease=None,
                worker=None,
                post_id=seed_id,
                index_revision=REVISION,
                expires=now + self.lookup_timeout,
                future=asyncio.get_running_loop().create_future(),
                waiters=0,
            )
            self.requests[seed_id] = item
        item["waiters"] += 1
        try:
            return await asyncio.wait_for(asyncio.shield(item["future"]), max(0, item["expires"] - now))
        except asyncio.TimeoutError:
            raise HTTPException(503, "Processing machine did not finish the lookup; retry when it is online") from None
        finally:
            item["waiters"] -= 1
            if not item["waiters"]:
                self.requests.pop(seed_id, None)
                if not item["future"].done():
                    item["future"].cancel()

    def complete(self, body):
        item = self.requests.get(body.post_id)
        if (
            item is None
            or item["future"].done()
            or item["expires"] <= time.monotonic()
            or item["id"] != body.id
            or item["lease"] != body.lease
            or item["worker"] != body.worker_id
        ):
            raise HTTPException(409, "Lookup lease expired or was cancelled")
        neighbors = [n.model_dump() for n in body.neighbors]
        if (
            (body.coverage == "not_indexed" and neighbors)
            or any(n["post_id"] == body.post_id for n in neighbors)
            or len({n["post_id"] for n in neighbors}) != len(neighbors)
            or any(
                a["rank"] >= b["rank"] or a["distance"] > b["distance"] + 0.002
                for a, b in zip(neighbors, neighbors[1:])
            )
        ):
            raise HTTPException(422, "Invalid processing machine neighbor evidence")
        result = {"revision": REVISION, "coverage": body.coverage, "neighbors": neighbors}
        self.cache[body.post_id] = (time.monotonic() + 3600, result)
        self.cache.move_to_end(body.post_id)
        while len(self.cache) > 128:
            self.cache.popitem(last=False)
        item["future"].set_result(result)

    async def close(self):
        for item in self.requests.values():
            item["future"].cancel()
        self.cache.clear()


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
async def complete(body: WorkerResult, request: Request):
    request.app.state.processing_machine.complete(body)
