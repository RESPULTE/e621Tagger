"""Short, serialized transactions; never hold a transaction across network I/O."""

import asyncio
import time
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

SCHEMA_V2 = """
ALTER TABLE image_cache ADD COLUMN source_url TEXT;
ALTER TABLE image_cache ADD COLUMN source_md5 TEXT;
CREATE TABLE image_fingerprints(
 post_id INTEGER PRIMARY KEY REFERENCES posts(post_id), md5 TEXT, preview_url TEXT,
 dhash TEXT NOT NULL CHECK(length(dhash)=16));
CREATE TABLE datasets(
 id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL, created_at REAL NOT NULL, manifest_sha256 TEXT NOT NULL,
 manifest_json TEXT NOT NULL);
CREATE INDEX datasets_project ON datasets(project_id,created_at DESC);
CREATE TRIGGER datasets_no_update BEFORE UPDATE ON datasets
 BEGIN SELECT RAISE(ABORT,'Frozen datasets are immutable'); END;
CREATE TRIGGER datasets_no_delete BEFORE DELETE ON datasets
 BEGIN SELECT RAISE(ABORT,'Frozen datasets are immutable'); END;
"""


SCHEMA_V3 = """
CREATE TABLE library_identity(id INTEGER PRIMARY KEY CHECK(id=1),token TEXT NOT NULL);
CREATE TABLE saved_media(
 post_id INTEGER PRIMARY KEY REFERENCES posts(post_id), version INTEGER NOT NULL,
 wanted INTEGER NOT NULL, present INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
 error TEXT, record_json TEXT, updated_at REAL NOT NULL);
"""

SCHEMA_V4 = """
CREATE TABLE canonical_projects(
 target_kind TEXT NOT NULL,target_id INTEGER NOT NULL,project_id INTEGER NOT NULL REFERENCES projects(id),
 PRIMARY KEY(target_kind,target_id));
INSERT INTO canonical_projects SELECT target_kind,target_id,min(id) FROM projects GROUP BY target_kind,target_id;
CREATE VIEW project_family AS SELECT p.id AS member_id,k.project_id AS project_id
 FROM projects p JOIN canonical_projects k USING(target_kind,target_id);
CREATE VIEW project_conflicts AS SELECT f.project_id,l.post_id,count(DISTINCT l.state) AS states
 FROM labels l JOIN project_family f ON f.member_id=l.project_id GROUP BY f.project_id,l.post_id HAVING states>1;
CREATE VIEW canonical_candidates AS SELECT project_id,owner_project_id,post_id,review_version,first_seen_at FROM (
 SELECT f.project_id,c.project_id AS owner_project_id,c.post_id,c.review_version,c.first_seen_at,
 row_number() OVER(PARTITION BY f.project_id,c.post_id ORDER BY l.state IS NULL,c.project_id!=f.project_id,c.project_id) AS rank
 FROM project_candidates c JOIN project_family f ON f.member_id=c.project_id
 LEFT JOIN labels l ON l.project_id=c.project_id AND l.post_id=c.post_id) WHERE rank=1;
ALTER TABLE jobs ADD COLUMN progress_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN total_bytes INTEGER;
CREATE TABLE download_partials(job_id INTEGER PRIMARY KEY REFERENCES jobs(id),path TEXT NOT NULL,inode INTEGER NOT NULL,device INTEGER NOT NULL);
CREATE TABLE library_staging(job_id INTEGER PRIMARY KEY REFERENCES jobs(id),root_id TEXT NOT NULL,token TEXT NOT NULL,
 names_json TEXT NOT NULL,files_json TEXT NOT NULL DEFAULT '[]',directory_inode INTEGER,device INTEGER);
CREATE INDEX labels_post_id ON labels(post_id);
CREATE INDEX label_history_post_id ON label_events(post_id);
UPDATE jobs SET status='cancelled',error='Legacy discovery stopped; Query now fetches fresh transient results'
 WHERE type IN ('discovery','preview') AND status IN ('pending','running');
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
        if version > 4:
            raise RuntimeError("Database schema is newer than this application")
        if version == 0:
            await self.connection.executescript(
                "BEGIN IMMEDIATE;" + SCHEMA + SCHEMA_V2 + SCHEMA_V3 + SCHEMA_V4 + "PRAGMA user_version=4; COMMIT;"
            )
        elif version in {1, 2, 3}:
            from backend.backup import backup

            # The process lock is already held. Use SQLite online backup, never a
            # raw DB-file copy that could omit WAL commits. Failure aborts startup.
            destination = self.root / "backups" / f"pre-schema-v4-{time.time_ns()}.sqlite3"
            await asyncio.to_thread(backup, self.root, destination)
            try:
                await self.connection.executescript(
                    "BEGIN IMMEDIATE;"
                    + (SCHEMA_V2 if version == 1 else "")
                    + (SCHEMA_V3 if version < 3 else "")
                    + SCHEMA_V4
                    + "PRAGMA user_version=4; COMMIT;"
                )
            except BaseException:
                await self.connection.rollback()
                raise

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
