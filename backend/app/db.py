"""Short, serialized transactions; never hold a transaction across network I/O."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tag_catalog(
 tag_id INTEGER PRIMARY KEY, canonical_name TEXT UNIQUE NOT NULL, category INTEGER NOT NULL,
 post_count INTEGER NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS aliases(name TEXT PRIMARY KEY, canonical_name TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS custom_tags(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS projects(
 id INTEGER PRIMARY KEY, target_kind TEXT NOT NULL CHECK(target_kind IN ('e621','custom')),
 target_id INTEGER NOT NULL, display_name TEXT NOT NULL, query TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS posts(
 post_id INTEGER PRIMARY KEY, rating TEXT, md5 TEXT, tags_json TEXT NOT NULL, score INTEGER NOT NULL,
 posted_at TEXT, media_ext TEXT, width INTEGER, height INTEGER, preview_url TEXT, sample_url TEXT,
 file_url TEXT, video_url TEXT, unavailable INTEGER NOT NULL DEFAULT 0, fetched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS search_runs(
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), query TEXT NOT NULL,
 source_kind TEXT NOT NULL, cursor INTEGER, exhausted INTEGER NOT NULL DEFAULT 0,
 UNIQUE(project_id,query,source_kind));
CREATE TABLE IF NOT EXISTS project_candidates(
 project_id INTEGER NOT NULL REFERENCES projects(id), post_id INTEGER NOT NULL REFERENCES posts(post_id),
 review_version INTEGER NOT NULL DEFAULT 0, first_seen_at REAL NOT NULL, PRIMARY KEY(project_id,post_id));
CREATE TABLE IF NOT EXISTS discoveries(
 project_id INTEGER NOT NULL, post_id INTEGER NOT NULL, search_run_id INTEGER NOT NULL REFERENCES search_runs(id),
 discovered_at REAL NOT NULL, PRIMARY KEY(project_id,post_id,search_run_id),
 FOREIGN KEY(project_id,post_id) REFERENCES project_candidates(project_id,post_id));
CREATE TABLE IF NOT EXISTS labels(
 project_id INTEGER NOT NULL, post_id INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('positive','negative','unsure')),
 reviewed_at REAL NOT NULL, PRIMARY KEY(project_id,post_id),
 FOREIGN KEY(project_id,post_id) REFERENCES project_candidates(project_id,post_id));
CREATE TABLE IF NOT EXISTS label_events(
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
 old_state TEXT, new_state TEXT, version INTEGER NOT NULL, reviewed_at REAL NOT NULL,
 source TEXT NOT NULL, undo_of INTEGER REFERENCES label_events(id),
 UNIQUE(project_id,post_id,version),
 FOREIGN KEY(project_id,post_id) REFERENCES project_candidates(project_id,post_id));
CREATE TABLE IF NOT EXISTS mutation_requests(
 request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS image_cache(
 post_id INTEGER NOT NULL REFERENCES posts(post_id), variant TEXT NOT NULL, relative_path TEXT NOT NULL,
 bytes INTEGER NOT NULL, etag TEXT NOT NULL, last_access_at REAL NOT NULL, PRIMARY KEY(post_id,variant));
CREATE TABLE IF NOT EXISTS jobs(
 id INTEGER PRIMARY KEY, type TEXT NOT NULL, job_key TEXT UNIQUE NOT NULL, payload_json TEXT NOT NULL,
 status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
 lease_until REAL, result_json TEXT, error TEXT, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, csrf_token TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, reset_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runtime_state(key TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE INDEX IF NOT EXISTS labels_state ON labels(project_id,state);
CREATE INDEX IF NOT EXISTS candidates_order ON project_candidates(project_id,post_id DESC);
CREATE INDEX IF NOT EXISTS search_project ON search_runs(project_id);
CREATE INDEX IF NOT EXISTS job_schedule ON jobs(type,status,available_at);
CREATE INDEX IF NOT EXISTS cache_lru ON image_cache(last_access_at);
CREATE INDEX IF NOT EXISTS post_md5 ON posts(md5);
CREATE INDEX IF NOT EXISTS events_post ON label_events(project_id,post_id,id DESC);
"""


class Database:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.lock = asyncio.Lock()
        self.connection: aiosqlite.Connection | None = None
        self._process_lock = None

    async def open(self):
        self.root.mkdir(parents=True, exist_ok=True)
        # Prevent a second Uvicorn worker from silently creating another limiter.
        self._process_lock = (self.root / "studio.lock").open("a+b")
        try:
            if __import__("os").name == "nt":
                import msvcrt

                self._process_lock.seek(0)
                if not self._process_lock.read(1):
                    self._process_lock.write(b"0")
                    self._process_lock.flush()
                self._process_lock.seek(0)
                msvcrt.locking(self._process_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._process_lock.close()
            self._process_lock = None
            raise RuntimeError("Studio requires exactly one process per data directory") from None
        self.connection = await aiosqlite.connect(self.root / "studio.sqlite3", isolation_level=None)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA foreign_keys=ON")
        await self.connection.execute("PRAGMA busy_timeout=5000")
        current = await self.connection.execute("PRAGMA user_version")
        version = (await current.fetchone())[0]
        if version > 1:
            raise RuntimeError("Database schema is newer than this application")
        if version == 0:
            await self.connection.executescript("BEGIN IMMEDIATE;" + SCHEMA + "PRAGMA user_version=1; COMMIT;")

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    async def rows(self, sql: str, args=()):
        async with self.lock:
            cursor = await self.connection.execute(sql, args)
            return [dict(row) for row in await cursor.fetchall()]

    async def one(self, sql: str, args=()):
        rows = await self.rows(sql, args)
        return rows[0] if rows else None

    async def close(self):
        if self.connection:
            await self.connection.close()
            self.connection = None
        if self._process_lock:
            self._process_lock.close()
            self._process_lock = None
