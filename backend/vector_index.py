"""Small exact worker-side corpus with atomic incremental updates and bounded reads.

SQLite owns vector bytes and identity together. No separate mutable ANN artifact
can get ahead of its catalog after interruption. This is a prototype, capped at
50,000 images; it deliberately cannot ingest the multi-million historical space.
"""

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import numpy as np

FORMAT = "hydra-local-exact-v1"
MAX_VECTORS = 50_000


def unit_vector(value, dimension):
    vector = np.asarray(value, dtype="<f4")
    if vector.shape != (dimension,) or not np.isfinite(vector).all():
        raise ValueError("Invalid vector dimension or values")
    norm = np.linalg.norm(vector)
    if norm <= 1e-12:
        raise ValueError("Zero vector cannot be normalized")
    return np.asarray(vector / norm, dtype="<f4")


class VectorIndex:
    def __init__(self, path, *, space, model_revision, dimension=2048, maximum=MAX_VECTORS):
        if not 1 <= dimension <= 8192 or not 1 <= maximum <= MAX_VECTORS:
            raise ValueError("Invalid exact index bounds")
        for value in (space, model_revision):
            if not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError("Embedding/model revision must be a SHA-256 identity")
        self.path, self.space, self.model_revision, self.dimension = Path(path), space, model_revision, dimension
        self.maximum = maximum
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(id INTEGER PRIMARY KEY CHECK(id=1),contract TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS vectors(post_id INTEGER PRIMARY KEY CHECK(post_id>0),md5 TEXT NOT NULL,
                    sha256 TEXT NOT NULL,vector BLOB NOT NULL);
                CREATE INDEX IF NOT EXISTS vectors_md5 ON vectors(md5);
                CREATE INDEX IF NOT EXISTS vectors_sha256 ON vectors(sha256);
            """)
            contract = json.dumps({"format": FORMAT, "space": space, "model_revision": model_revision,
                                   "dimension": dimension, "metric": "unit_l2"}, sort_keys=True)
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES(1,?,0)", (contract,))
            if self.connection.execute("SELECT contract FROM metadata").fetchone()[0] != contract:
                raise ValueError("Embedding space or checkpoint revision mismatch; use a separate index")
            if self.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Vector index integrity check failed")
            if self.connection.execute("SELECT count(*) FROM vectors").fetchone()[0] > maximum:
                raise ValueError("Exact corpus exceeds configured capacity")
        except BaseException:
            self.connection.close()
            raise

    @property
    def size(self):
        return self.connection.execute("SELECT count(*) FROM vectors").fetchone()[0]

    @property
    def revision(self):
        # Include the catalog identities as well as generation, so independently
        # created DBs with equal insertion counts cannot claim the same corpus.
        metadata = self.connection.execute("SELECT * FROM metadata").fetchone()
        cached = getattr(self, "_revision_cache", None)
        if cached and cached[0] == metadata:
            return cached[1]
        digest = hashlib.sha256()
        digest.update(str(metadata).encode())
        for row in self.connection.execute("SELECT post_id,md5,sha256 FROM vectors ORDER BY post_id"):
            digest.update(json.dumps(row).encode())
        revision = digest.hexdigest()
        self._revision_cache = (metadata, revision)
        return revision

    def get(self, post_id, md5):
        row = self.connection.execute("SELECT sha256,vector FROM vectors WHERE post_id=? AND md5=?", (post_id, md5)).fetchone()
        if row:
            return row[0], np.frombuffer(row[1], dtype="<f4").copy()
        return None

    def post_ids(self):
        return np.array([row[0] for row in self.connection.execute("SELECT post_id FROM vectors ORDER BY post_id")], dtype=np.int64)

    def upsert(self, post_id, md5, sha256, vector):
        if type(post_id) is not int or not 0 < post_id <= 2**63 - 1:
            raise ValueError("Invalid post ID")
        if not re.fullmatch(r"[a-f0-9]{32}", md5) or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise ValueError("Invalid image identity")
        vector = unit_vector(vector, self.dimension)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            duplicate = self.connection.execute(
                "SELECT md5,sha256,vector FROM vectors WHERE (md5=? OR sha256=?) AND post_id!=? LIMIT 1",
                (md5, sha256, post_id),
            ).fetchone()
            if duplicate and duplicate != (md5, sha256, vector.tobytes()):
                raise ValueError("Duplicate source identity has inconsistent vector bytes")
            old = self.connection.execute("SELECT md5,sha256,vector FROM vectors WHERE post_id=?", (post_id,)).fetchone()
            if old and old[:2] == (md5, sha256):
                if old[2] != vector.tobytes():
                    raise ValueError("Same source identity produced a different vector; pipeline is not reproducible")
                self.connection.commit()
                return False
            if not old and self.connection.execute("SELECT count(*) FROM vectors").fetchone()[0] >= self.maximum:
                raise ValueError("Exact corpus is full; evaluate a larger index before expanding it")
            self.connection.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)", (post_id, md5, sha256, vector.tobytes()))
            self.connection.execute("UPDATE metadata SET generation=generation+1 WHERE id=1")
            self.connection.commit()
            return True
        except BaseException:
            self.connection.rollback()
            raise

    def search(self, vector, *, limit=64, exclude_post=None, exclude_md5=None, exclude_sha256=None):
        if not 1 <= limit <= 64:
            raise ValueError("Search limit must be 1–64")
        query = unit_vector(vector, self.dimension)
        self.connection.execute("BEGIN")
        try:
            revision, best = self.revision, []
            cursor = self.connection.execute("SELECT post_id,md5,sha256,vector FROM vectors ORDER BY post_id")
            while rows := cursor.fetchmany(512):
                candidates = [r for r in rows if r[0] != exclude_post and r[1] != exclude_md5 and r[2] != exclude_sha256]
                if not candidates:
                    continue
                matrix = np.stack([np.frombuffer(r[3], dtype="<f4") for r in candidates])
                if matrix.shape[1] != self.dimension or not np.isfinite(matrix).all():
                    raise ValueError("Corrupt vector bytes")
                if not np.allclose(np.linalg.norm(matrix, axis=1), 1, atol=1e-5):
                    raise ValueError("Index vectors are not normalized")
                distances = np.linalg.norm(matrix - query, axis=1)
                best.extend((float(d), r[0], r[1], r[2]) for d, r in zip(distances, candidates))
                # Keep at most k unique media representatives after each block.
                best.sort(key=lambda item: (item[0], item[1]))
                selected, md5s, hashes = [], set(), set()
                for item in best:
                    if item[2] not in md5s and item[3] not in hashes:
                        selected.append(item)
                        md5s.add(item[2])
                        hashes.add(item[3])
                    if len(selected) == limit:
                        break
                best = selected
            corpus_size = self.size
            self.connection.commit()
            return {"revision": revision, "corpus_size": corpus_size, "neighbors": [
                {"post_id": post, "md5": md5, "rank": rank, "distance": distance}
                for rank, (distance, post, md5, _) in enumerate(best, 1)
            ]}
        except BaseException:
            self.connection.rollback()
            raise

    def close(self):
        self.connection.close()
