"""Immutable image-vector snapshot with original-vector centered reranking.

The publisher supplies post IDs and raw image embeddings, not image hashes.
Current candidate hashes must therefore be resolved from e621 before review.
"""

import hashlib
import json
from pathlib import Path

import numpy as np

from .vector_index import unit_vector

FORMAT = "hydra-broad-image-v1"
MAX_IMAGES = 10_000_000
FILES = {"post_ids.npy", "raw_vectors.npy", "mean.npy"}


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BroadIndex:
    def __init__(self, root, *, model_revision, query_space, dimension=2048, block_rows=32768, require_validation=True):
        self.root = Path(root).resolve(strict=True)
        self.block_rows = block_rows
        if not 1 <= block_rows <= 65536:
            raise ValueError("Invalid broad-index block bound")
        raw = (self.root / "manifest.json").read_bytes()
        self.manifest = json.loads(raw)
        m = self.manifest
        if require_validation and m.get("validation", {}).get("passed") is not True:
            raise ValueError("Broad image index has not passed compatibility/runtime validation")
        if (m.get("format") != FORMAT or m.get("model_revision") != model_revision
                or m.get("native_query_space") != query_space or m.get("dimension") != dimension
                or type(m.get("count")) is not int or not 1 <= m["count"] <= MAX_IMAGES
                or set(m.get("files", {})) not in (FILES, FILES | {"search.faiss"})):
            raise ValueError("Broad image-index identity mismatch")
        for name, identity in m["files"].items():
            path = self.root / name
            if (not path.is_file() or path.is_symlink() or path.stat().st_size != identity["bytes"]
                    or digest_file(path) != identity["sha256"]):
                raise ValueError(f"Broad image-index file integrity failed: {name}")
        self.revision = hashlib.sha256(raw).hexdigest()
        self.space = hashlib.sha256(json.dumps({
            "format": FORMAT, "native_query_space": query_space,
            "dataset_revision": m["dataset_revision"], "mean_sha256": m["files"]["mean.npy"]["sha256"],
            "transform": "float32-subtract-corpus-mean-then-unit-l2",
        }, sort_keys=True).encode()).hexdigest()
        self.ids = np.load(self.root / "post_ids.npy", mmap_mode="r", allow_pickle=False)
        self.vectors = np.load(self.root / "raw_vectors.npy", mmap_mode="r", allow_pickle=False)
        self.mean = np.load(self.root / "mean.npy", allow_pickle=False)
        self.size, self.dimension = m["count"], dimension
        if (self.ids.shape != (self.size,) or self.ids.dtype != np.dtype("<i8")
                or self.vectors.shape != (self.size, dimension) or self.vectors.dtype != np.dtype("<f2")
                or self.mean.shape != (dimension,) or self.mean.dtype != np.float32
                or not np.isfinite(self.mean).all() or (self.ids <= 0).any()
                or (np.diff(self.ids) <= 0).any()):
            raise ValueError("Broad image-index dimensions/order are invalid")
        self.ann = None
        self._faiss = None
        if "search.faiss" in m["files"]:
            import faiss

            self._faiss = faiss
            faiss.omp_set_num_threads(8)
            self.ann = faiss.read_index(str(self.root / "search.faiss"))
            config = m["search"]
            if (self.ann.d != dimension or self.ann.ntotal != self.size or not self.ann.is_trained
                    or not isinstance(self.ann, faiss.IndexPQ) or self.ann.pq.M != 256 or self.ann.pq.nbits != 8
                    or type(config["rerank"]) is not int or not 256 <= config["rerank"] <= 65536):
                raise ValueError("Broad search-index dimensions/configuration mismatch")

    def transform(self, raw_vector):
        raw_vector = np.asarray(raw_vector, dtype=np.float32)
        if raw_vector.shape != (self.dimension,) or not np.isfinite(raw_vector).all():
            raise ValueError("Invalid raw query image embedding")
        return unit_vector(raw_vector - self.mean, self.dimension)

    def contains(self, ids):
        values = np.asarray(ids, dtype=np.int64)
        positions = np.searchsorted(self.ids, values)
        return (positions < self.size) & (self.ids[np.minimum(positions, self.size - 1)] == values)

    def search(self, vector, *, limit=128, exclude_post=None, override_ids=()):
        if not 1 <= limit <= 256:
            raise ValueError("Invalid broad result limit")
        query = unit_vector(vector, self.dimension)
        overrides = np.array(sorted(set(override_ids)), dtype=np.int64)
        if self.ann is not None:
            # OpenMP's setting is thread-local. The live worker searches in its
            # executor, rather than the thread that constructed this index.
            if self._faiss is not None:
                self._faiss.omp_set_num_threads(8)
            count = min(self.size, max(limit, self.manifest["search"]["rerank"]))
            _, rows = self.ann.search(query[None], count)
            rows = np.unique(rows[0][rows[0] >= 0])
            ids = self.ids[rows]
            keep = ids != exclude_post if exclude_post is not None else np.ones(len(ids), dtype=bool)
            if overrides.size:
                keep &= ~np.isin(ids, overrides)
            rows, ids = rows[keep], ids[keep]
            values = self._unit_rows(self.vectors[rows])
            differences = values - query
            distances = np.sqrt(np.einsum("ij,ij->i", differences, differences))
            order = np.lexsort((ids, distances))[:limit]
            return [{"post_id": int(ids[i]), "md5": None, "rank": rank, "distance": float(distances[i])}
                    for rank, i in enumerate(order, 1)]
        best = []
        for start in range(0, self.size, self.block_rows):
            end = min(start + self.block_rows, self.size)
            # A block is at most 512 MiB in FP32, regardless of corpus size.
            values = self._unit_rows(self.vectors[start:end])
            # einsum avoids launching a large BLAS thread pool for every block.
            scores = np.einsum("ij,j->i", values, query)
            distances = np.sqrt(np.maximum(2.0 - 2.0 * scores, 0))
            ids = self.ids[start:end]
            keep = ids != exclude_post if exclude_post is not None else np.ones(len(ids), dtype=bool)
            if overrides.size:
                positions = np.searchsorted(overrides, ids)
                keep &= (positions == len(overrides)) | (overrides[np.minimum(positions, len(overrides) - 1)] != ids)
            available = np.flatnonzero(keep)
            order = np.lexsort((ids[available], distances[available]))[:limit]
            selected = available[order]
            best.extend((float(distances[i]), int(ids[i])) for i in selected)
            best.sort()
            del best[limit:]
        return [{"post_id": post_id, "md5": None, "rank": rank, "distance": distance}
                for rank, (distance, post_id) in enumerate(best, 1)]

    def _unit_rows(self, raw):
        values = np.array(raw, dtype=np.float32)
        values -= self.mean
        norm = np.sqrt(np.einsum("ij,ij->i", values, values))
        if not np.isfinite(norm).all() or (norm <= 1e-12).any():
            raise ValueError("Invalid centered corpus embedding")
        values /= norm[:, None]
        return values

    def close(self):
        self.ann = None
        for value in (self.ids, self.vectors):
            value._mmap.close()
