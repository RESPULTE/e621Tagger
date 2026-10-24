"""Crash-safe prediction cache. Never stores family files or credentials."""

import gzip
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS predictions (
            key TEXT PRIMARY KEY, payload BLOB NOT NULL, updated REAL NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            entity TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT, updated REAL NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS sources (
            signature TEXT PRIMARY KEY, prediction_key TEXT NOT NULL)""")
        self.db.commit()

    def get(self, key: str):
        row = self.db.execute("SELECT payload FROM predictions WHERE key=?", (key,)).fetchone()
        return json.loads(gzip.decompress(row[0])) if row else None

    def put(self, key: str, payload: dict):
        # Covers are sent to Stash from memory, never retained in the laptop cache.
        metadata = {name: value for name, value in payload.items() if name != "cover"}
        encoded = gzip.compress(json.dumps(metadata, separators=(",", ":")).encode())
        self.db.execute(
            "INSERT OR REPLACE INTO predictions VALUES(?,?,?)", (key, encoded, time.time())
        )
        self.db.commit()

    def remove_media(self) -> int:
        """Remove legacy embedded covers, including old SQLite/WAL page copies.

        Call only while holding the worker lock, with no inference in progress.
        """
        self.db.execute("PRAGMA secure_delete=ON")
        removed = 0
        for key, encoded in self.db.execute("SELECT key,payload FROM predictions").fetchall():
            payload = json.loads(gzip.decompress(encoded))
            if "cover" in payload:
                self.put(key, payload)
                removed += 1
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.execute("VACUUM")
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return removed

    def status(self, entity: str, status: str, error: str | None = None):
        self.db.execute(
            "INSERT OR REPLACE INTO jobs VALUES(?,?,?,?)", (entity, status, error, time.time())
        )
        self.db.commit()

    def job_status(self, entity: str) -> str | None:
        row = self.db.execute("SELECT status FROM jobs WHERE entity=?", (entity,)).fetchone()
        return row[0] if row else None

    def source(self, signature: str):
        row = self.db.execute(
            "SELECT prediction_key FROM sources WHERE signature=?", (signature,)
        ).fetchone()
        return self.get(row[0]) if row else None

    def remember_source(self, signature: str, prediction_key: str):
        self.db.execute("INSERT OR REPLACE INTO sources VALUES(?,?)", (signature, prediction_key))
        self.db.commit()

    def close(self):
        self.db.close()


@contextmanager
def worker_lock(path: Path):
    """OS-backed lock is released even after a killed worker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another worker is already running") from exc
        yield


def worker_available(settings) -> bool:
    try:
        with worker_lock(settings.state.with_suffix(".lock")):
            return True
    except RuntimeError:
        return False
