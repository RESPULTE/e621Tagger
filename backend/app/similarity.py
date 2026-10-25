"""Session-only KNN discovery. Only the review transaction retains evidence."""

import asyncio
import json
import time
from collections import OrderedDict
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from . import auth, schemas
from .api import require_project

ACTIVE = {"pending", "running"}
router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_session)])


class Similarity:
    def __init__(self, index, feed):
        self.index, self.feed = index, feed
        self.searches = OrderedDict()
        self.receipts = OrderedDict()
        self.controls = OrderedDict()
        self.slots = asyncio.Semaphore(2)
        self.tasks = {}
        self.maximum = 16
        self.retained = 64
        self.ttl = 3600
        self.byte_limit = 32 * 1024**2

    def prune(self):
        terminal = [key for key, item in self.searches.items() if item["status"] not in ACTIVE]
        for key in terminal:
            if len(terminal) > self.retained or self.searches[key]["accessed"] < time.monotonic() - self.ttl:
                self.searches.pop(key)
                terminal = [k for k in terminal if k != key]
        while len(self.receipts) > 512:
            self.receipts.popitem(last=False)
        while len(self.controls) > 512:
            self.controls.popitem(last=False)

    def get(self, owner, project, key):
        self.prune()
        item = self.searches.get(key)
        if not item or item["owner"] != owner or item["project_id"] != project:
            raise HTTPException(410, "Similarity search expired; long press the source again")
        item["accessed"] = time.monotonic()
        return item

    def enqueue(self, owner, project, seed, limit, request_id):
        self.prune()
        signature = (project, seed, limit)
        receipt = self.receipts.get((owner, str(request_id)))
        if receipt:
            if receipt[0] != signature:
                raise HTTPException(409, "Request ID was already used for another search")
            return self.get(owner, project, receipt[1])
        if getattr(self.index, "error", None):
            raise HTTPException(503, self.index.error)
        for item in self.searches.values():
            if (item["owner"], item["project_id"], item["source_post_id"], item["requested_limit"]) == (
                owner,
                project,
                seed,
                limit,
            ):
                self.receipts[owner, str(request_id)] = (signature, item["id"])
                return self.get(owner, project, item["id"])
        if sum(item["status"] in ACTIVE for item in self.searches.values()) >= self.maximum:
            raise HTTPException(429, "Similarity queue is full; wait for a search to finish")
        item = dict(
            id=str(uuid4()),
            owner=owner,
            project_id=project,
            source_post_id=seed,
            requested_limit=limit,
            index_revision=getattr(self.index, "revision", "synthetic"),
            status="pending",
            stage="Queued",
            error=None,
            results=[],
            checked=0,
            total=0,
            accessed=time.monotonic(),
            bytes=0,
            evidence=None,
        )
        self.searches[item["id"]] = item
        self.receipts[owner, str(request_id)] = (signature, item["id"])
        self.start(item)
        return item

    def start(self, item):
        item.update(status="pending", error=None, stage="Queued", checked=0, total=0, results=[], bytes=0, evidence=None)
        task = asyncio.create_task(self.run(item))
        self.tasks[item["id"]] = task

        def finished(task):
            if self.tasks.get(item["id"]) is task:
                self.tasks.pop(item["id"], None)

        task.add_done_callback(finished)

    def retain(self, item, candidate):
        size = len(json.dumps(candidate).encode())
        while sum(i["bytes"] for i in self.searches.values()) + size > self.byte_limit:
            disposable = next((key for key, value in self.searches.items() if value["status"] not in ACTIVE), None)
            if disposable is None:
                raise HTTPException(503, "Similarity metadata memory limit reached; cancel searches and retry")
            self.searches.pop(disposable)
        item["bytes"] += size

    async def run(self, item):
        try:
            async with self.slots:
                item.update(status="running", stage="Waiting for processing machine lookup")
                evidence = await self.index.lookup(item["source_post_id"])
                item["evidence"] = evidence
                item["index_revision"] = evidence["revision"]
                if evidence["coverage"] != "available":
                    item.update(status="not_indexed", stage="Source is not indexed")
                    return
                neighbors = evidence["neighbors"]
                item["total"] = len(neighbors)
                item["results"] = [
                    dict(n, metadata_state="pending", candidate=None) for n in neighbors[: item["requested_limit"]]
                ]
                next_index = len(item["results"])
                item["stage"] = "Preparing post metadata"
                # Serial within a search, shared priority scheduler across searches and browsing.
                for result in item["results"]:
                    while True:
                        post = await self.feed.raw(result["post_id"], background=True)
                        item["checked"] += 1
                        unavailable = post.get("flags", {}).get("deleted") or not (post.get("file") or {}).get("url")
                        if unavailable:
                            if next_index < len(neighbors):
                                result.update(neighbors[next_index])
                                next_index += 1
                                continue
                            result["metadata_state"] = "unavailable"
                            break
                        self.feed.remember(
                            post, f"Hydra source #{item['source_post_id']}", "similarity", item["project_id"]
                        )
                        candidate = await self.feed.candidate(item["project_id"], post)
                        self.retain(item, candidate)
                        result.update(metadata_state="ready", candidate=candidate)
                        break
                item["results"] = [r for r in item["results"] if r["metadata_state"] != "unavailable"]
                item.update(status="done", stage="Ready")
        except asyncio.CancelledError:
            item.update(status="cancelled", stage="Cancelled")
        except Exception as exc:
            item.update(
                status="failed",
                stage="Needs attention",
                error=str(exc.detail) if isinstance(exc, HTTPException) else "Similarity preparation failed; retry",
            )
        finally:
            item["accessed"] = time.monotonic()
            self.prune()

    async def review_evidence(self, project, seed):
        self.prune()
        # Keep the authenticated row with its search, whose lifetime extends on access.
        # This avoids relying on the bridge's independently bounded lookup cache offline.
        for item in reversed(self.searches.values()):
            if item["project_id"] == project and item["source_post_id"] == seed and item["evidence"]:
                item["accessed"] = time.monotonic()
                return item["evidence"]
        return await self.index.lookup(seed)

    async def view(self, item, *, details=True):
        result = {k: v for k, v in item.items() if k not in {"owner", "accessed", "bytes", "evidence"}}
        result["results"] = []
        if details:
            for entry in sorted(item["results"], key=lambda entry: entry["rank"]):
                copy = dict(entry)
                if entry["candidate"]:
                    from .api import CANDIDATE_SELECT, decode_candidate

                    saved = await self.feed.db.one(
                        CANDIDATE_SELECT + " WHERE c.project_id=? AND c.post_id=?",
                        (item["project_id"], entry["post_id"]),
                    )
                    copy["candidate"] = decode_candidate(saved) if saved else dict(entry["candidate"])
                    copy["candidate"]["similarity"] = {
                        "seed_post_id": item["source_post_id"],
                        "index_revision": item["index_revision"],
                    }
                result["results"].append(copy)
        return result

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.searches.clear()
        self.receipts.clear()
        self.controls.clear()


async def context(request, project_id):
    project = await require_project(request.app.state.db, project_id)
    owner = auth.token_hash(request.cookies.get(request.app.state.settings.session_cookie_name, ""))
    return request.app.state.similarity, owner, project["id"]


@router.post("/projects/{project_id}/similar", response_model=schemas.SimilaritySearch)
async def enqueue(project_id: int, body: schemas.SimilarityRequest, request: Request):
    service, owner, project = await context(request, project_id)
    if getattr(service.index, "error", None):
        raise HTTPException(503, service.index.error)
    # Known IDs from either durable or transient browsing are valid sources.
    post = await service.feed.raw(body.post_id)
    if post.get("flags", {}).get("deleted"):
        raise HTTPException(404, "Source post is unavailable")
    return await service.view(service.enqueue(owner, project, body.post_id, body.limit, body.request_id))


@router.get("/projects/{project_id}/similar", response_model=schemas.SimilarityList)
async def listing(project_id: int, request: Request):
    service, owner, project = await context(request, project_id)
    service.prune()
    items = [item for item in service.searches.values() if item["owner"] == owner and item["project_id"] == project]
    return {
        "items": [await service.view(item, details=False) for item in items],
        "available": not bool(getattr(service.index, "error", None)),
        "message": getattr(service.index, "error", None),
    }


@router.get("/projects/{project_id}/similar/{search_id}", response_model=schemas.SimilaritySearch)
async def detail(project_id: int, search_id: str, request: Request):
    service, owner, project = await context(request, project_id)
    return await service.view(service.get(owner, project, search_id))


@router.post("/projects/{project_id}/similar/{search_id}/{action}", response_model=schemas.SimilaritySearch)
async def control(project_id: int, search_id: str, action: str, body: schemas.Mutation, request: Request):
    service, owner, project = await context(request, project_id)
    item = service.get(owner, project, search_id)
    key = (owner, str(body.request_id))
    signature = (project, search_id, action)
    if key in service.controls:
        if service.controls[key] != signature:
            raise HTTPException(409, "Request ID was already used for another operation")
        return await service.view(item)
    if action == "cancel":
        task = service.tasks.get(search_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            item.update(status="cancelled", stage="Cancelled")
    elif action == "retry":
        if item["status"] in {"failed", "cancelled"}:
            if getattr(service.index, "error", None):
                raise HTTPException(503, service.index.error)
            if sum(i["status"] in ACTIVE for i in service.searches.values()) >= service.maximum:
                raise HTTPException(429, "Similarity queue is full")
            service.start(item)
    else:
        raise HTTPException(404, "Unknown similarity action")
    service.controls[key] = signature
    return await service.view(item)
