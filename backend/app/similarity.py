"""Session-only GPU image discovery. Only human reviews retain evidence."""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from . import auth, schemas
from .api import require_project
from .reference_images import read_upload

ACTIVE = {"pending", "running"}
router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_session)])


class Similarity:
    def __init__(self, index, feed):
        self.index, self.feed = index, feed
        self.searches = OrderedDict()
        self.receipts = OrderedDict()
        self.controls = OrderedDict()
        self.slots = asyncio.Semaphore(2)
        self.upload_slots = asyncio.Semaphore(2)
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

    def enqueue(self, owner, project, seed, limit, request_id, *, query_type="post", raw=None, post=None, digest=None):
        self.prune()
        signature = (project, seed, limit, query_type, digest, (post or {}).get("file", {}).get("md5"))
        receipt = self.receipts.get((owner, str(request_id)))
        if receipt:
            if receipt[0] != signature:
                raise HTTPException(409, "Request ID was already used for another search")
            return self.get(owner, project, receipt[1])
        if self.error:
            raise HTTPException(503, self.error)
        for item in self.searches.values():
            if item["owner"] == owner and item["_signature"] == signature:
                self.receipts[owner, str(request_id)] = (signature, item["id"])
                return self.get(owner, project, item["id"])
        if sum(item["status"] in ACTIVE for item in self.searches.values()) >= self.maximum:
            raise HTTPException(429, "Similarity queue is full; wait for a search to finish")
        item = dict(
            id=str(uuid4()),
            owner=owner,
            project_id=project,
            source_post_id=seed,
            query_type=query_type,
            query_sha256=digest,
            _signature=signature,
            _raw=raw,
            _post=post,
            requested_limit=limit,
            index_revision=getattr(self.index, "revision", "synthetic"),
            status="pending",
            stage="Queued",
            error=None,
            results=[],
            checked=0,
            total=0,
            duplicates_hidden=0,
            accessed=time.monotonic(),
            bytes=0,
            evidence=None,
            corpus_size=None,
        )
        if raw:
            self.retain(item, None, size=len(raw))
        self.searches[item["id"]] = item
        self.receipts[owner, str(request_id)] = (signature, item["id"])
        self.start(item)
        return item

    def start(self, item, *, more=False):
        item.update(status="pending", error=None, stage="Queued")
        if not more:
            item.update(checked=0, total=0, results=[], bytes=len(item.get("_raw") or b""),
                        evidence=None, corpus_size=None, duplicates_hidden=0, _next_neighbor=0, _filters=OrderedDict())
        task = asyncio.create_task(self.run(item, more=more))
        self.tasks[item["id"]] = task

        def finished(task):
            if self.tasks.get(item["id"]) is task:
                self.tasks.pop(item["id"], None)

        task.add_done_callback(finished)

    @property
    def error(self):
        return getattr(self.index, "error", "Image search unavailable: GPU worker is not ready")

    def retain(self, item, candidate, *, size=None):
        size = len(json.dumps(candidate).encode()) if size is None else size
        while sum(i["bytes"] for i in self.searches.values()) + size > self.byte_limit:
            disposable = next((key for key, value in self.searches.items() if value["status"] not in ACTIVE), None)
            if disposable is None:
                raise HTTPException(503, "Similarity metadata memory limit reached; cancel searches and retry")
            self.searches.pop(disposable)
        item["bytes"] += size

    async def run(self, item, *, more=False):
        try:
            async with self.slots:
                item.update(status="running", stage="Preparing more matches" if more else "Waiting for GPU image retrieval")
                if not more:
                    evidence = await self.index.search_image({
                        "query_type": item["query_type"], "post_id": item["source_post_id"],
                        "source_md5": (item["_post"] or {}).get("file", {}).get("md5"),
                        "query_sha256": item["query_sha256"],
                    }, raw=item["_raw"], post=item["_post"])
                    for key in ("query_sha256", "model_revision", "embedding_space", "metric"):
                        item[key] = evidence[key]
                    item["evidence"] = evidence
                    item["index_revision"] = evidence["revision"]
                    item["corpus_size"] = evidence.get("corpus_size")
                else:
                    evidence = item["evidence"]
                neighbors = evidence["neighbors"]
                # Each image query owns its complete ranking. Earlier searches and
                # project reviews must never suppress this image's closest matches.
                item["duplicates_hidden"] = 0
                item["total"] = len(neighbors)
                cursor = item.get("_next_neighbor", 0)
                page = [dict(n, metadata_state="pending", candidate=None)
                        for n in neighbors[cursor:cursor + item["requested_limit"]]]
                item["results"].extend(page)
                next_index = cursor + len(page)
                item["_next_neighbor"] = next_index
                item["stage"] = "Preparing post metadata"
                query_md5 = ((item.get("_post") or {}).get("file", {}).get("md5")
                             or (hashlib.md5(item["_raw"]).hexdigest() if item.get("_raw") else None))
                seen_md5 = {entry["candidate"]["md5"] for entry in item["results"]
                            if entry.get("candidate") and entry["candidate"].get("md5")}
                # Serial within a search, shared priority scheduler across searches and browsing.
                for result in page:
                    while True:
                        post = await self.feed.raw(result["post_id"], background=True)
                        item["checked"] += 1
                        current_md5 = (post.get("file") or {}).get("md5")
                        unavailable = (post.get("flags", {}).get("deleted") or not (post.get("file") or {}).get("url")
                                       or not current_md5 or current_md5 in seen_md5 or current_md5 == query_md5
                                       or (result.get("md5") and result["md5"] != current_md5))
                        if unavailable:
                            if next_index < len(neighbors):
                                result.update(neighbors[next_index])
                                next_index += 1
                                item["_next_neighbor"] = next_index
                                continue
                            result["metadata_state"] = "unavailable"
                            break
                        self.feed.remember(
                            post, f"Hydra search {item['id']}", "similarity", item["project_id"]
                        )
                        candidate = await self.feed.candidate(item["project_id"], post)
                        # Persist provenance only after binding this publisher ID
                        # to the actual current original's hash. Review rechecks it.
                        result["md5"] = current_md5
                        for neighbor in evidence["neighbors"]:
                            if neighbor["post_id"] == result["post_id"]:
                                neighbor["md5"] = current_md5
                                break
                        seen_md5.add(current_md5)
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

    async def review_evidence(self, project, *, search_id, owner):
        item = self.get(owner, project, str(search_id))
        if not item["evidence"]:
            raise HTTPException(409, "Similarity evidence is not ready")
        return item["evidence"]

    async def view(self, item, *, details=True, query=""):
        from .api import clean_query

        query = clean_query(query)
        # Snapshot before I/O: hydration/load-more may change results while e621 responds.
        entries = sorted((dict(entry) for entry in item["results"]), key=lambda entry: entry["rank"])
        unfiltered_count = len(entries)
        # Keep completion/cursor state from the same snapshot. Otherwise hydration
        # can finish during filtering and stop UI polling with a partial last page.
        result = {k: v for k, v in item.items() if not k.startswith("_") and k not in {"owner", "accessed", "bytes", "evidence"}}
        result.update(filter_query=query, unfiltered_count=unfiltered_count,
                      source_artists=sorted(set((item.get("_post") or {}).get("tags", {}).get("artist", []))))
        result["has_more"] = bool(item["status"] == "done" and item["evidence"]
                                  and item.get("_next_neighbor", 0) < len(item["evidence"]["neighbors"]))
        if details and query:
            ids = tuple(entry["post_id"] for entry in entries if entry["candidate"])
            key = (query, ids)
            cache = item.setdefault("_filters", OrderedDict())
            cached = cache.get(key)
            if cached and cached[0] > time.monotonic() - 60:
                matches = cached[1]
            else:
                posts = await self.feed.search_posts(query, post_ids=ids)
                matches = {post["id"] for post in posts}
                cache[key] = (time.monotonic(), matches)
                cache.move_to_end(key)
                while len(cache) > 8:
                    cache.popitem(last=False)
            entries = [entry for entry in entries if entry["post_id"] in matches]
        result["results"] = []
        if details:
            for entry in entries:
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
                        "search_id": item["id"],
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
    if service.error:
        raise HTTPException(503, service.error)
    # Known IDs from either durable or transient browsing are valid sources.
    post = await service.feed.raw(body.post_id)
    if post.get("flags", {}).get("deleted"):
        raise HTTPException(404, "Source post is unavailable")
    if ((post.get("file") or {}).get("ext") not in {"jpg", "jpeg", "png", "webp"}
            or not (post.get("file") or {}).get("md5")):
        raise HTTPException(415, "Use a still JPEG, PNG or WebP post with a verified image identity")
    return await service.view(service.enqueue(owner, project, body.post_id, body.limit, body.request_id,
                              query_type="post", post=post))


@router.post("/projects/{project_id}/similar/reference", response_model=schemas.SimilaritySearch,
             openapi_extra={"requestBody": {"required": True, "content": {"application/octet-stream": {
                 "schema": {"type": "string", "format": "binary"}}}}})
async def upload(project_id: int, request: Request, request_id: UUID, limit: int = Query(default=32, ge=1, le=32)):
    service, owner, project = await context(request, project_id)
    if service.error:
        raise HTTPException(503, service.error)
    async with service.upload_slots:
        raw, digest = await read_upload(request)
        item = service.enqueue(owner, project, None, limit, request_id, query_type="upload", raw=raw, digest=digest)
    return await service.view(item)


@router.get("/projects/{project_id}/similar", response_model=schemas.SimilarityList)
async def listing(project_id: int, request: Request):
    service, owner, project = await context(request, project_id)
    service.prune()
    items = [item for item in service.searches.values() if item["owner"] == owner and item["project_id"] == project]
    return {
        "items": [await service.view(item, details=False) for item in items],
        "available": not bool(service.error),
        "message": service.error,
        "corpus_size": getattr(service.index, "corpus_size", None),
    }


@router.get("/projects/{project_id}/similar/{search_id}", response_model=schemas.SimilaritySearch)
async def detail(project_id: int, search_id: str, request: Request, query: str = Query(default="", max_length=500)):
    service, owner, project = await context(request, project_id)
    return await service.view(service.get(owner, project, search_id), query=query)


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
    elif action == "more":
        if item["status"] != "done" or not item["evidence"]:
            raise HTTPException(409, "Wait for this image search to finish before loading more")
        if item.get("_next_neighbor", 0) < len(item["evidence"]["neighbors"]):
            if sum(i["status"] in ACTIVE for i in service.searches.values()) >= service.maximum:
                raise HTTPException(429, "Similarity queue is full")
            service.start(item, more=True)
    elif action == "retry":
        if item["status"] in {"failed", "cancelled", "done"}:
            if service.error:
                raise HTTPException(503, service.error)
            if sum(i["status"] in ACTIVE for i in service.searches.values()) >= service.maximum:
                raise HTTPException(429, "Similarity queue is full")
            service.start(item)
    else:
        raise HTTPException(404, "Unknown similarity action")
    service.controls[key] = signature
    return await service.view(item)
