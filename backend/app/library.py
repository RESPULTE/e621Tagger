"""Explicit, global collection saves. Human labels and datasets are never written.

Originals publish using no-clobber hard links. A durable inode/hash manifest
is reserved before publication, so restart can finish partial saves or removals
without adopting an existing collection file. No recursive deletion is used.
"""

import asyncio
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Path as APIPath

from backend.filesystem import cancel_and_join, disk, sync_directory

from . import auth, schemas
from .jobs import enqueue
from .reviews import fingerprint, receipt, save_receipt

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_session)])
PositiveId = Annotated[int, APIPath(gt=0, le=2**63 - 1)]
EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "webm", "mp4"}


def state(post_id, row=None):
    return {
        "post_id": post_id,
        "version": row["version"] if row else 0,
        "wanted": bool(row["wanted"]) if row else False,
        "present": bool(row["present"]) if row else False,
        "status": row["status"] if row else "idle",
        "error": row["error"] if row else None,
        "updated_at": row["updated_at"] if row else 0,
    }


async def queue_intent(conn, post_id, version, wanted, *, source=None, context=None):
    """Commit one versioned save/removal intent; callers own conflict checks."""
    await conn.execute(
        "INSERT INTO saved_media(post_id,version,wanted,status,updated_at) VALUES(?,?,?,'queued',?) "
        "ON CONFLICT(post_id) DO UPDATE SET version=excluded.version,wanted=excluded.wanted,status='queued',error=NULL,updated_at=excluded.updated_at",
        (post_id, version, int(wanted), time.time()),
    )
    # Superseded queued work must not delay the intent that cancels it.
    await conn.execute(
        "UPDATE jobs SET status='done',result_json=? WHERE type='library' AND status='pending' "
        "AND json_extract(payload_json,'$.post_id')=?",
        (json.dumps({"outcome": "superseded"}), post_id),
    )
    return await enqueue(conn, "library", f"library:{post_id}:{version}", {
        **(context or {}), "post_id": post_id, "version": version, "source": source,
    })


def conflict(message="Saved files changed outside Studio; existing files were left untouched"):
    return HTTPException(409, message)


def regular(path):
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            value.update(chunk)
    return value.hexdigest()


class Library:
    def __init__(self, db, source, media, settings):
        self.db, self.source, self.media, self.settings = db, source, media, settings
        self.root = settings.library_root.absolute() if settings.library_root is not None else None
        self.operation_locks = [asyncio.Lock() for _ in range(64)]
        self.active_jobs = {}
        self.operations = {}
        self.transfers = None
        self.feed = None

    def capability(self):
        available = bool(
            self.root
            and self.root.is_dir()
            and not self.root.is_symlink()
            and regular(self.root / ".hydra-studio-library-id")
        )
        if available:
            try:
                self.destination()
            except HTTPException:
                available = False
        return {
            "configured": self.root is not None,
            "available": available,
            "message": (f"Saves only original media to Smut/{self.settings.library_subdirectory}."
                        if self.settings.library_subdirectory else "Saves only original media to your Smut folder.")
            if available
            else "Smut folder is offline or not configured. Review remains available.",
        }

    def destination(self, record=None):
        # The collection marker/identity stays at its mounted root. Each file
        # manifest records its destination so enabling Inbox preserves older
        # top-level saves and their deletion/recovery paths.
        directory = record.get("directory", "") if record is not None else self.settings.library_subdirectory
        if not isinstance(directory, str) or (directory and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", directory)):
            raise conflict("Saved destination is invalid; files were preserved")
        if self.root is None:
            raise HTTPException(409, "Smut folder saving is not configured")
        destination = self.root / directory
        if not destination.is_dir() or self.root.is_symlink() or destination.is_symlink():
            raise HTTPException(503, "Save destination is missing or unsafe; existing files were preserved")
        if destination.stat().st_dev != self.root.stat().st_dev:
            raise HTTPException(409, "Save destination must share the collection filesystem")
        return destination

    async def prepare_root(self):
        if self.root is None:
            raise HTTPException(409, "Smut folder saving is not configured")
        identity = await self.db.one("SELECT token FROM library_identity WHERE id=1")
        expected = identity["token"] if identity else None
        token = await disk(self._root_identity, expected)
        async with self.db.transaction() as conn:
            await conn.execute("INSERT OR IGNORE INTO library_identity VALUES(1,?)", (token,))
            row = await (await conn.execute("SELECT token FROM library_identity WHERE id=1")).fetchone()
            if row["token"] != token:
                raise conflict("Configured Smut folder identity changed; no media was touched")
        return token

    def _root_identity(self, expected):
        if not self.root.is_dir() or self.root.is_symlink():
            raise HTTPException(503, "Smut folder is offline; no replacement directory was created")
        marker = self.root / ".hydra-studio-library-id"
        if marker.exists() or marker.is_symlink():
            if not regular(marker) or marker.stat().st_size > 100:
                raise conflict("Smut folder identity marker is invalid")
            try:
                token = str(UUID(marker.read_text(encoding="ascii").strip()))
            except (ValueError, UnicodeError):
                raise conflict("Smut folder identity marker is invalid") from None
            if expected and token != expected:
                raise conflict("Configured Smut folder identity changed; no media was touched")
        else:
            raise HTTPException(503, "Smut folder identity is missing; reconnect or initialize the intended storage")
        temporary = self.root / ".hydra-studio-tmp"
        if temporary.is_symlink() or (temporary.exists() and not temporary.is_dir()):
            raise conflict("Smut staging directory is unsafe")
        temporary.mkdir(mode=0o700, exist_ok=True)
        sync_directory(self.root)
        return token

    async def current(self, post_id, version):
        row = await self.db.one("SELECT * FROM saved_media WHERE post_id=?", (post_id,))
        return row if row and row["version"] == version and row["status"] != "cancelled" else None

    async def process(self, job, payload):
        # Stripe by post: one destination writer, independent posts may overlap.
        # HTTP intent changes are checked again before publication.
        async with self.operation_locks[payload["post_id"] % len(self.operation_locks)]:
            try:
                operation = asyncio.create_task(self._process(job, payload))
                stop = asyncio.Event()
                stopped = asyncio.create_task(stop.wait())
                self.operations[(payload["post_id"], payload["version"])] = (operation, stop)
                try:
                    done, _ = await asyncio.wait({operation, stopped}, timeout=self.settings.library_timeout,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if not done:
                        raise asyncio.TimeoutError
                    if stop.is_set():
                        # Off intents stop this operation without cancelling the
                        # scheduler that must run the subsequent removal.
                        await cancel_and_join(operation)
                        await self.finish(job, payload["post_id"], payload["version"], "superseded")
                    else:
                        await operation
                except BaseException:
                    # The wrapper can be cancelled again during shutdown.
                    # Retain the writer lock until inner disk cleanup is joined.
                    await cancel_and_join(operation)
                    raise
                finally:
                    stopped.cancel()
                    await asyncio.gather(stopped, return_exceptions=True)
            except asyncio.TimeoutError:
                raise HTTPException(
                    504, "Collection save timed out; retry when storage and CDN are available"
                ) from None
            except OSError:
                raise HTTPException(
                    507, "Smut folder could not be written safely; check storage and permissions"
                ) from None
            finally:
                self.active_jobs.pop((payload["post_id"], payload["version"]), None)
                self.operations.pop((payload["post_id"], payload["version"]), None)

    async def cancel_superseded(self, post_id, version):
        """Abort blocked network reads and join owned staging cleanup after commit."""
        operations = [entry for (post, old_version), entry in list(self.operations.items())
                      if post == post_id and old_version < version]
        for _, stop in operations:
            stop.set()
        # process() alone owns cancellation: cancelling twice can interrupt its
        # staging-finally block while it is awaiting the cleanup journal.
        await asyncio.gather(*(asyncio.shield(operation) for operation, _ in operations), return_exceptions=True)

    async def _process(self, job, payload):
        post_id, version = payload["post_id"], payload["version"]
        self.active_jobs[(post_id, version)] = job["id"]
        row = await self.current(post_id, version)
        if not row:
            await self.finish(job, post_id, version, "superseded")
            return
        root_id = await self.prepare_root()
        record = json.loads(row["record_json"]) if row["record_json"] else None
        if record and record["root_id"] != root_id:
            raise conflict("Saved media belongs to another folder; nothing was removed")
        if not row["wanted"]:
            await self.set_status(post_id, version, "removing")
            if record:
                await disk(self.remove_files, record)
            async with self.db.transaction() as conn:
                await conn.execute("UPDATE saved_media SET present=0,record_json=NULL WHERE post_id=?", (post_id,))
                if record:
                    await self.clear_staging_journal(conn, record["token"])
            await self.finish(job, post_id, version, "idle")
            return
        if record:
            if await disk(self.recover_for_save, record):
                await self.publish(record)
                await self.finish(job, post_id, version, "saved")
                return
            # A prior off operation durably removed some/all recovery sources
            # before it could clear the journal. Finish that removal and fetch
            # anew; edited or replaced extant files still cause a conflict.
            async with self.db.transaction() as conn:
                await conn.execute("UPDATE saved_media SET present=0,record_json=NULL WHERE post_id=?", (post_id,))
                await self.clear_staging_journal(conn, record["token"])
        await self.clean_staging(job["id"])
        await self.set_status(post_id, version, "downloading")
        snapshot = payload.get("source") or await self.db.one("SELECT * FROM posts WHERE post_id=?", (post_id,))
        post = (await self.source.request(f"/posts/{post_id}.json", background=True))["post"]
        file = post.get("file") or {}
        md5, ext = file.get("md5", ""), file.get("ext", "")
        if post.get("id") != post_id or not re.fullmatch("[a-f0-9]{32}", md5) or ext not in EXTENSIONS:
            raise HTTPException(415, "This post has no supported checksummed original")
        if post.get("flags", {}).get("deleted") or not file.get("url"):
            raise HTTPException(404, "Original is unavailable")
        if md5 != snapshot["md5"] or file["url"] != snapshot["file_url"]:
            raise conflict("Post original changed since this view; refresh before saving")
        self.media.validate_url(file["url"])
        directory = self.settings.library_subdirectory
        destination = self.destination({"directory": directory})
        base = f"e621_{post_id}_{md5}"
        names = [base + "." + ext]
        if any((destination / name).exists() or (destination / name).is_symlink() for name in names):
            raise conflict("A matching filename already exists; Studio will not overwrite or adopt it")
        token = uuid4().hex
        stage = self.root / ".hydra-studio-tmp" / token
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO library_staging(job_id,root_id,token,names_json) VALUES(?,?,?,?)",
                (job["id"], root_id, token, json.dumps(names)),
            )
        reserved = False
        try:
            await disk(stage.mkdir, 0o700)
            identity = stage.stat()
            async with self.db.transaction() as conn:
                await conn.execute(
                    "UPDATE library_staging SET directory_inode=?,device=? WHERE job_id=?",
                    (identity.st_ino, identity.st_dev, job["id"]),
                )
            original = stage / (names[0] + ".part")
            expected_size = file.get("size")
            await self.download(original, file["url"], md5, expected_size, post_id, version)
            await disk(original.rename, stage / names[0])
            if not await self.current(post_id, version):
                await self.finish(job, post_id, version, "superseded")
                return
            await disk(sync_directory, stage)
            await disk(sync_directory, stage.parent)
            entries = await disk(self.describe, stage, names)
            record = {"schema_version": 1, "root_id": root_id, "token": token, "files": entries, "original": names[0]}
            if directory:
                record["directory"] = directory
            async with self.db.transaction() as conn:
                current = await (
                    await conn.execute("SELECT version,wanted,status FROM saved_media WHERE post_id=?", (post_id,))
                ).fetchone()
                if current["version"] != version or not current["wanted"] or current["status"] == "cancelled":
                    await self.done_job(conn, job, "superseded")
                    return
                await conn.execute(
                    "UPDATE saved_media SET record_json=?,status='publishing',updated_at=? WHERE post_id=?",
                    (json.dumps(record), time.time(), post_id),
                )
                reserved = True
            await self.publish(record)
            await self.finish(job, post_id, version, "saved")
        finally:
            # Before journal reservation only this invocation owns the scratch
            # files. After reservation retain them for exact crash recovery.
            if not reserved:
                await self.clean_staging(job["id"])

    async def clean_staging(self, job_id):
        row = await self.db.one("SELECT * FROM library_staging WHERE job_id=?", (job_id,))
        if not row:
            return
        identity = await self.db.one("SELECT token FROM library_identity WHERE id=1")
        if not identity or identity["token"] != row["root_id"]:
            raise conflict("Interrupted download belongs to another collection; files were preserved")
        names = json.loads(row["names_json"])
        stage = self.paths({"token": row["token"], "files": [{"name": name} for name in names]})
        entries = json.loads(row["files_json"])
        partial = await self.db.one("SELECT * FROM download_partials WHERE job_id=?", (job_id,))
        if partial and Path(partial["path"]).parent == stage:
            for name in (names[0] + ".part", names[0]):
                entries.append({"name": name, "inode": partial["inode"], "device": partial["device"]})

        def cleanup():
            if not stage.exists():
                return
            if (
                stage.is_symlink()
                or not row["directory_inode"]
                or stage.stat().st_ino != row["directory_inode"]
                or stage.stat().st_dev != row["device"]
            ):
                raise conflict("Interrupted staging directory changed; files were preserved")
            for entry in entries:
                if entry["name"] not in names and entry["name"] != names[0] + ".part":
                    raise conflict()
                path = stage / entry["name"]
                if path.exists() or path.is_symlink():
                    if (
                        not regular(path)
                        or path.stat().st_ino != entry["inode"]
                        or path.stat().st_dev != entry["device"]
                    ):
                        raise conflict("Interrupted staging file changed; files were preserved")
            for entry in entries:
                (stage / entry["name"]).unlink(missing_ok=True)
            sync_directory(stage)
            if any(stage.iterdir()):
                raise conflict("Unjournalled staging files were preserved for explicit recovery")
            stage.rmdir()
            sync_directory(stage.parent)

        await disk(cleanup)
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM library_staging WHERE job_id=?", (job_id,))
            await conn.execute("DELETE FROM download_partials WHERE job_id=?", (job_id,))

    async def recover_staging(self):
        for row in await self.db.rows("SELECT s.*,j.payload_json FROM library_staging s JOIN jobs j ON j.id=s.job_id"):
            payload = json.loads(row["payload_json"])
            saved = await self.db.one("SELECT record_json FROM saved_media WHERE post_id=?", (payload["post_id"],))
            record = json.loads(saved["record_json"]) if saved and saved["record_json"] else None
            if record and record["token"] == row["token"]:
                continue  # The full publication journal now owns these files.
            try:
                await self.clean_staging(row["job_id"])
            except (HTTPException, OSError) as exc:
                message = (
                    str(exc.detail)
                    if isinstance(exc, HTTPException)
                    else "Interrupted collection storage is unavailable; files were preserved"
                )
                async with self.db.transaction() as conn:
                    await conn.execute("UPDATE jobs SET status='failed',error=? WHERE id=?", (message, row["job_id"]))
                    await self.failed(conn, payload, message, True)

    @staticmethod
    async def clear_staging_journal(conn, token):
        await conn.execute(
            "DELETE FROM download_partials WHERE job_id IN (SELECT job_id FROM library_staging WHERE token=?)", (token,)
        )
        await conn.execute("DELETE FROM library_staging WHERE token=?", (token,))

    async def download(self, path, url, md5, expected_size, post_id, version):
        async def current():
            return bool(await self.current(post_id, version))

        return await self.transfers.save(
            path, url, md5, expected_size, self.root, job_id=self.active_jobs.get((post_id, version)), current=current
        )

    @staticmethod
    def describe(stage, names):
        return [
            {
                "name": name,
                "sha256": digest(stage / name),
                "inode": (stage / name).stat().st_ino,
                "bytes": (stage / name).stat().st_size,
            }
            for name in names
        ]

    def paths(self, record):
        if not re.fullmatch("[a-f0-9]{32}", record["token"]):
            raise conflict()
        for entry in record["files"]:
            if not re.fullmatch(
                r"e621_[1-9][0-9]*_[a-f0-9]{32}\.(jpg|jpeg|png|gif|webp|avif|bmp|webm|mp4|json|txt|xmp)", entry["name"]
            ):
                raise conflict()
        stage = self.root / ".hydra-studio-tmp" / record["token"]
        if stage.is_symlink() or (stage.exists() and not stage.is_dir()):
            raise conflict()
        return stage

    @staticmethod
    def verify(path, entry):
        if (
            not regular(path)
            or path.stat().st_ino != entry["inode"]
            or path.stat().st_size != entry["bytes"]
            or digest(path) != entry["sha256"]
        ):
            raise conflict()

    async def publish(self, record):
        await disk(self.publish_files, record)

    def publish_files(self, record):
        stage = self.paths(record)
        root = self.destination(record)
        # Hard-linking never replaces another file, even if one appears
        # after the preflight. Older multi-file journals remain recoverable.
        entries = sorted(record["files"], key=lambda entry: entry["name"] == record["original"])
        for entry in entries:
            destination = root / entry["name"]
            if destination.exists() or destination.is_symlink():
                self.verify(destination, entry)
                continue
            source = stage / entry["name"]
            if not source.exists():
                source = stage / (".remove-" + entry["name"])
            self.verify(source, entry)
            try:
                os.link(source, destination)
            except FileExistsError:
                self.verify(destination, entry)
            except OSError:
                raise HTTPException(507, "Smut folder could not safely publish the saved files") from None

        sync_directory(root)

    def remove_files(self, record):
        stage = self.paths(record)
        root = self.destination(record)
        stage.mkdir(mode=0o700, exist_ok=True)
        sync_directory(stage.parent)
        # Check everything before starting. Quarantine each name atomically,
        # then recheck its identity: unlinking a verified pathname directly can
        # delete an external replacement made between verification and unlink.
        for entry in record["files"]:
            destination = root / entry["name"]
            quarantine = stage / (".remove-" + entry["name"])
            if quarantine.exists() or quarantine.is_symlink():
                self.verify(quarantine, entry)
            if destination.exists() or destination.is_symlink():
                self.verify(destination, entry)
        for entry in record["files"]:
            destination = root / entry["name"]
            quarantine = stage / (".remove-" + entry["name"])
            if quarantine.exists():
                # A crash after quarantine but before acknowledgement resumes
                # here. A newly appeared destination is never unlinked.
                if destination.exists() or destination.is_symlink():
                    raise conflict()
                continue
            try:
                os.rename(destination, quarantine)
            except FileNotFoundError:
                continue
            try:
                self.verify(quarantine, entry)
            except HTTPException:
                # Restore a raced replacement without overwriting anything that
                # appeared meanwhile. If restoration cannot be done, retain it
                # in scratch and report its exact recovery location.
                try:
                    os.link(quarantine, destination, follow_symlinks=False)
                    quarantine.unlink()
                    sync_directory(root)
                    sync_directory(stage)
                except OSError:
                    raise conflict(
                        f"File changed during removal; preserved at .hydra-studio-tmp/{record['token']}/{quarantine.name}"
                    ) from None
                raise
        # Make removal of visible names durable before deleting recovery links
        # or clearing the database manifest. Missing files are already removed.
        sync_directory(root)
        sync_directory(stage)
        self.clean_record_stage(record)

    def recover_for_save(self, record):
        stage = self.paths(record)
        root = self.destination(record)
        complete = True
        for entry in record["files"]:
            present = False
            for path in (
                root / entry["name"],
                stage / entry["name"],
                stage / (".remove-" + entry["name"]),
            ):
                if path.exists() or path.is_symlink():
                    self.verify(path, entry)
                    present = True
            complete = complete and present
        if not complete:
            self.remove_files(record)
        return complete

    def clean_record_stage(self, record):
        stage = self.paths(record)
        names = []
        for entry in record["files"]:
            for name in (entry["name"], ".remove-" + entry["name"]):
                path = stage / name
                if path.exists() or path.is_symlink():
                    self.verify(path, entry)
                names.append(name)
        self.clean_stage(stage, names)

    @staticmethod
    def clean_stage(stage, names):
        if stage.is_symlink():
            raise conflict()
        if stage.exists():
            for name in names:
                (stage / name).unlink(missing_ok=True)
            sync_directory(stage)
            # Never recursively delete a directory or an unexpected extra file.
            stage.rmdir()
            sync_directory(stage.parent)

    async def set_status(self, post_id, version, status):
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE saved_media SET status=?,error=NULL,updated_at=? WHERE post_id=? AND version=?",
                (status, time.time(), post_id, version),
            )

    @staticmethod
    async def done_job(conn, job, outcome):
        await conn.execute(
            "UPDATE jobs SET status='done',lease_until=NULL,error=NULL,result_json=?,updated_at=? WHERE id=? AND status!='cancelled'",
            (json.dumps({"outcome": outcome}), time.time(), job["id"]),
        )

    async def finish(self, job, post_id, version, status):
        snapshot = await self.db.one("SELECT * FROM saved_media WHERE post_id=?", (post_id,))
        if snapshot and status == "saved" and snapshot["record_json"]:
            record = json.loads(snapshot["record_json"])
            await disk(self.clean_record_stage, record)
        async with self.db.transaction() as conn:
            row = await (await conn.execute("SELECT * FROM saved_media WHERE post_id=?", (post_id,))).fetchone()
            if row and status == "saved":
                await conn.execute("UPDATE saved_media SET present=1 WHERE post_id=?", (post_id,))
            if row and row["version"] == version and status != "superseded":
                await conn.execute(
                    "UPDATE saved_media SET status=?,error=NULL,updated_at=? WHERE post_id=?",
                    (status, time.time(), post_id),
                )
            await self.done_job(conn, job, status)
            if status == "saved":
                if snapshot and snapshot["record_json"]:
                    await self.clear_staging_journal(conn, json.loads(snapshot["record_json"])["token"])
                await conn.execute("DELETE FROM download_partials WHERE job_id=?", (job["id"],))

    @staticmethod
    async def failed(conn, payload, message, failed):
        await conn.execute(
            "UPDATE saved_media SET status=?,error=?,updated_at=? WHERE post_id=? AND version=?",
            ("failed" if failed else "retrying", message[:500], time.time(), payload["post_id"], payload["version"]),
        )


@router.get("/library/status", response_model=schemas.LibraryCapability)
async def capability(request: Request):
    return request.app.state.library.capability()


@router.get("/library/posts/{post_id}", response_model=schemas.SaveState)
async def saved_state(post_id: PositiveId, request: Request):
    return state(post_id, await request.app.state.db.one("SELECT * FROM saved_media WHERE post_id=?", (post_id,)))


@router.put("/projects/{project_id}/library/{post_id}", response_model=schemas.SaveState)
async def set_saved(project_id: PositiveId, post_id: PositiveId, body: schemas.SaveMutation, request: Request):
    db = request.app.state.db
    from .jobs import persist_post
    from .projects import require_active_project, require_project

    project = await require_project(db, project_id)
    project_id = project["id"]
    signature = fingerprint(["save-media", project_id, post_id], body.model_dump(mode="json", exclude_none=True))
    async with db.transaction() as conn:
        await require_active_project(conn, project_id)
        previous = await receipt(conn, body.request_id, signature)
        if previous is not None:
            return previous
    known = await db.one("SELECT 1 FROM posts WHERE post_id=?", (post_id,))
    if not known and not request.app.state.feed.peek(post_id):
        raise HTTPException(404, "Open or browse this post before explicitly saving it")
    raw = await request.app.state.feed.raw(post_id) if body.wanted or not known else None
    from .feed import post_row

    source = post_row(raw) if body.wanted else None
    if body.source_md5 and source and body.source_md5 != source["md5"]:
        raise conflict("Post original changed since this view; refresh before saving")
    async with db.transaction() as conn:
        await require_active_project(conn, project_id)
        previous = await receipt(conn, body.request_id, signature)
        if previous is not None:
            return previous
        if raw and not known:
            await persist_post(conn, raw)
        candidate = await (await conn.execute("SELECT * FROM posts WHERE post_id=?", (post_id,))).fetchone()
        if not candidate:
            raise HTTPException(404, "Post is not a candidate in this project")
        if request.app.state.library.root is None:
            raise HTTPException(409, "Smut folder saving is not configured")
        old = await (await conn.execute("SELECT * FROM saved_media WHERE post_id=?", (post_id,))).fetchone()
        if (
            body.wanted
            and old
            and old["wanted"]
            and old["status"] in {"queued", "downloading", "publishing", "retrying", "saved"}
        ):
            if body.source_md5 and old["record_json"]:
                original = json.loads(old["record_json"])["original"]
                if not re.search("_" + body.source_md5 + r"\.", original):
                    raise conflict("A different original is already saved; existing files were preserved")
            result = state(post_id, old)
            await save_receipt(conn, body.request_id, signature, result)
            return result
        if (old["version"] if old else 0) != body.expected_version:
            raise conflict("Saved state changed in another tab; refresh before toggling")
        if (
            body.wanted
            and not (old and old["record_json"])
            and (source["unavailable"] or not source["file_url"] or source["media_ext"] not in EXTENSIONS)
        ):
            raise HTTPException(415, "This post has no available supported original")
        queued = (
            await (
                await conn.execute("SELECT count(*) FROM jobs WHERE type='library' AND status IN ('pending','running')")
            ).fetchone()
        )[0]
        own = await (
            await conn.execute(
                "SELECT 1 FROM jobs WHERE type='library' AND status IN ('pending','running') AND json_extract(payload_json,'$.post_id')=?",
                (post_id,),
            )
        ).fetchone()
        if body.wanted and queued >= 16 and not own:
            raise HTTPException(429, "Save queue is full; retry shortly")
        version = body.expected_version + 1
        if body.wanted:
            upvotes = (raw.get("score") or {}).get("up")
            context = {"project_id": project_id, "display": {
                "search_tag": project["display_name"],
                "upvotes": upvotes if type(upvotes) is int and upvotes >= 0 else None,
            }}
        else:
            previous_job = await (await conn.execute(
                "SELECT payload_json FROM jobs WHERE job_key=?", (f"library:{post_id}:{body.expected_version}",),
            )).fetchone()
            previous_payload = json.loads(previous_job["payload_json"]) if previous_job else {}
            context = {key: previous_payload[key] for key in ("project_id", "display") if key in previous_payload}
        await queue_intent(conn, post_id, version, body.wanted,
                           source={"md5": source["md5"], "file_url": source["file_url"]} if source else None,
                           context=context)
        result = state(
            post_id, await (await conn.execute("SELECT * FROM saved_media WHERE post_id=?", (post_id,))).fetchone()
        )
        await save_receipt(conn, body.request_id, signature, result)
    if not body.wanted:
        await request.app.state.library.cancel_superseded(post_id, version)
    return result
