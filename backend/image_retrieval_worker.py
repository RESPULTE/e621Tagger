"""GPU image work; the outer worker keeps polling/heartbeats while this runs."""

import hashlib
import json
from pathlib import Path

from .app.image_jobs import ImageJob
from .app.reference_images import validate_image
from .broad_index import BroadIndex
from .hydra_encoder import HydraEncoder
from .vector_index import VectorIndex


class ImageEngine:
    def __init__(self, checkpoint, index_path, *, broad_index=None):
        self.encoder = HydraEncoder(checkpoint)
        self.broad = (BroadIndex(broad_index, model_revision=self.encoder.model_revision, query_space=self.encoder.space)
                      if broad_index else None)
        self.space = self.broad.space if self.broad else self.encoder.space
        if self.broad:
            # Centered query caches have their own identity and never mix with the
            # earlier uncentered pilot. Preserve that corpus for rollback.
            path = Path(index_path)
            index_path = path.with_name(path.stem + "." + self.space[:12] + path.suffix)
        self.index = VectorIndex(index_path, space=self.space, model_revision=self.encoder.model_revision)
        self.capability = self._capability()

    def _revision(self, local_revision):
        if not self.broad:
            return local_revision
        return hashlib.sha256(json.dumps({"published": self.broad.revision, "local": local_revision}, sort_keys=True).encode()).hexdigest()

    def _capability(self):
        count = self.index.size
        if self.broad:
            count = self.broad.size + int((~self.broad.contains(self.index.post_ids())).sum())
        return {"model_revision": self.encoder.model_revision, "embedding_space": self.space,
                "index_revision": self._revision(self.index.revision), "corpus_size": count, "metric": "unit_l2"}

    def process(self, client, assignment, worker_id):
        job = ImageJob.model_validate(assignment)
        if (job.model_revision, job.embedding_space) != (self.encoder.model_revision, self.space):
            raise ValueError("Server assigned an incompatible embedding space")
        result = {**job.model_dump(mode="json"), "worker_id": worker_id, "metric": "unit_l2",
                  "coverage": "failed", "error": "inference_failed", "neighbors": [],
                  "index_revision": self.capability["index_revision"]}
        try:
            stored = self.index.get(job.post_id, job.source_md5) if job.query_type == "post" else None
            if stored:
                digest, vector = stored
            else:
                result["error"] = "invalid_image"
                with client.stream("GET", f"jobs/{job.id}/image", params={
                    "lease": str(job.lease), "worker_id": worker_id,
                }) as response:
                    if response.status_code in {401, 403}:
                        raise PermissionError("Worker reference transfer authorization rejected")
                    response.raise_for_status()
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise ValueError("Reference encoding changed")
                    expected = response.headers.get("x-image-sha256")
                    length = response.headers.get("content-length")
                    maximum = 16 * 1024**2
                    if length and (not length.isdigit() or not 0 < int(length) <= maximum):
                        raise ValueError("Reference transfer exceeds byte limit")
                    raw = bytearray()
                    for chunk in response.iter_raw(65536):
                        if len(raw) + len(chunk) > maximum:
                            raise ValueError("Reference transfer exceeds byte limit")
                        raw.extend(chunk)
                    if length and len(raw) != int(length):
                        raise ValueError("Incomplete reference transfer")
                raw = bytes(raw)
                digest = validate_image(raw, 40_000_000)
                if digest != expected or (job.query_sha256 and digest != job.query_sha256):
                    raise ValueError("Reference checksum mismatch")
                if job.query_type == "post" and hashlib.md5(raw).hexdigest() != job.source_md5:
                    raise ValueError("Source post checksum mismatch")
                result["query_sha256"] = digest
                result["error"] = "inference_failed"
                vector = (self.broad.transform(self.encoder.encode_raw(raw)) if self.broad else self.encoder.encode(raw))
                if job.query_type == "post":
                    result["error"] = "index_failed"
                    self.index.upsert(job.post_id, job.source_md5, digest, vector)
                    # Use the exact canonical bytes just committed, so the first
                    # query and cache hits in other projects share normalization.
                    digest, vector = self.index.get(job.post_id, job.source_md5)
            result["error"] = "index_failed"
            found = self.index.search(vector, exclude_post=job.post_id, exclude_md5=job.source_md5, exclude_sha256=digest)
            if self.broad:
                neighbors = self.broad.search(vector, exclude_post=job.post_id, override_ids=self.index.post_ids())
                neighbors.extend(found["neighbors"])
                neighbors.sort(key=lambda n: (n["distance"], n["post_id"]))
                # Current originals in the local supplement supersede older
                # published rows. Source/media duplicates are also checked during
                # server metadata hydration when publisher rows lack image hashes.
                unique, seen = [], set()
                for neighbor in neighbors:
                    if neighbor["post_id"] not in seen:
                        seen.add(neighbor["post_id"])
                        unique.append({**neighbor, "rank": len(unique) + 1})
                    if len(unique) == 64:
                        break
                found.update(neighbors=unique, revision=self._revision(found["revision"]))
            self.capability = self._capability()
            result.update(coverage="available", error=None, neighbors=found["neighbors"],
                          query_sha256=digest, index_revision=found["revision"], corpus_size=self.capability["corpus_size"])
        except PermissionError:
            raise
        except Exception as exc:
            # No source URLs, parser errors, local paths or tracebacks cross the
            # bridge. The code indicates which bounded operation failed.
            print(f"Image job {job.id} failed during {result['error']} ({type(exc).__name__})", flush=True)
        return result

    def close(self):
        self.index.close()
        if self.broad:
            self.broad.close()
