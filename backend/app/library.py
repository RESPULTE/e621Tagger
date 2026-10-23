"""Explicit, global collection saves. Human labels and datasets are never written.

Originals publish last using no-clobber hard links. A durable inode/hash manifest
is reserved before publication, so restart can finish partial saves or removals
without adopting an existing collection file. No recursive deletion is used.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import time
from typing import Annotated
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

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


def conflict(message="Saved files changed outside Studio; existing files were left untouched"):
    return HTTPException(409, message)


def regular(path):
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            value.update(chunk)
    return value.hexdigest()


def captions(post, token):
    tags = post.get("tags")
    if not isinstance(tags, dict) or any(
        not isinstance(v, list) or any(not isinstance(t, str) for t in v) for v in tags.values()
    ):
        raise HTTPException(502, "e621 returned invalid categorized tags")
    all_tags = sorted({tag for values in tags.values() for tag in values})
    if len(all_tags) > 20000 or any(len(tag) > 1000 or "\x00" in tag for tag in all_tags):
        raise HTTPException(413, "Post tags exceed the metadata limit")
    metadata = {
        **post,
        "_hydra_studio": {
            "schema_version": 1,
            "ownership_token": token,
            "post_url": f"https://e621.net/posts/{post['id']}",
            "saved_at": time.time(),
            "tag_source": "e621 source tags; independent from Studio human labels",
        },
    }
    raw = json.dumps(metadata, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2).encode("utf-8")
    if len(raw) > 1024 * 1024:
        raise HTTPException(413, "Post metadata exceeds 1 MiB")
    # Sidecars preserve canonical tag spellings. Never rewrite original bytes to
    # embed metadata: the e621 MD5 remains an independently checked identity.
    rdf = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    dc = "http://purl.org/dc/elements/1.1/"
    ET.register_namespace("rdf", rdf)
    ET.register_namespace("dc", dc)
    xmp = ET.Element("{adobe:ns:meta/}xmpmeta")
    description = ET.SubElement(ET.SubElement(xmp, f"{{{rdf}}}RDF"), f"{{{rdf}}}Description")
    bag = ET.SubElement(ET.SubElement(description, f"{{{dc}}}subject"), f"{{{rdf}}}Bag")
    for tag in all_tags:
        ET.SubElement(bag, f"{{{rdf}}}li").text = tag
    ET.SubElement(description, f"{{{dc}}}source").text = f"https://e621.net/posts/{post['id']}"
    return raw, (", ".join(all_tags) + "\n").encode("utf-8"), ET.tostring(xmp, encoding="utf-8", xml_declaration=True)


class Library:
    def __init__(self, db, source, media, settings):
        self.db, self.source, self.media, self.settings = db, source, media, settings
        self.root = settings.library_root.absolute() if settings.library_root is not None else None
        self.operation_lock = asyncio.Lock()

    def capability(self):
        available = bool(
            self.root
            and self.root.is_dir()
            and not self.root.is_symlink()
            and regular(self.root / ".hydra-studio-library-id")
        )
        return {
            "configured": self.root is not None,
            "available": available,
            "message": "Saves originals and e621 tag sidecars directly to your Smut folder."
            if available
            else "Smut folder is offline or not configured. Review remains available.",
        }

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
        return row if row and row["version"] == version else None

    async def process(self, job, payload):
        # One worker owns filesystem changes. HTTP can update intent during I/O;
        # each publication/final state checks the durable version again.
        async with self.operation_lock:
            try:
                operation = asyncio.create_task(self._process(job, payload))
                try:
                    await asyncio.wait_for(asyncio.shield(operation), timeout=self.settings.library_timeout)
                except BaseException:
                    # wait_for itself can be cancelled again during shutdown.
                    # Retain the writer lock until inner disk cleanup is joined.
                    await cancel_and_join(operation)
                    raise
            except asyncio.TimeoutError:
                raise HTTPException(
                    504, "Collection save timed out; retry when storage and CDN are available"
                ) from None
            except OSError:
                raise HTTPException(
                    507, "Smut folder could not be written safely; check storage and permissions"
                ) from None

    async def _process(self, job, payload):
        post_id, version = payload["post_id"], payload["version"]
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
        await self.set_status(post_id, version, "downloading")
        snapshot = await self.db.one("SELECT * FROM posts WHERE post_id=?", (post_id,))
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
        base = f"e621_{post_id}_{md5}"
        names = [base + "." + ext, base + ".json", base + ".txt", base + ".xmp"]
        if any((self.root / name).exists() or (self.root / name).is_symlink() for name in names):
            raise conflict("A matching filename already exists; Studio will not overwrite or adopt it")
        token = uuid4().hex
        stage = self.root / ".hydra-studio-tmp" / token
        stage.mkdir(mode=0o700)
        created = []
        reserved = False
        try:
            original = stage / (names[0] + ".part")
            created.append(original.name)
            expected_size = file.get("size")
            await self.download(original, file["url"], md5, expected_size, post_id, version)
            original.rename(stage / names[0])
            created.append(names[0])
            for name, data in zip(names[1:], captions(post, token)):
                created.append(name)
                await disk(self.write_file, stage / name, data)
            if not await self.current(post_id, version):
                await self.finish(job, post_id, version, "superseded")
                return
            await disk(sync_directory, stage)
            await disk(sync_directory, stage.parent)
            entries = await disk(self.describe, stage, names)
            record = {"schema_version": 1, "root_id": root_id, "token": token, "files": entries, "original": names[0]}
            async with self.db.transaction() as conn:
                current = await (
                    await conn.execute("SELECT version,wanted FROM saved_media WHERE post_id=?", (post_id,))
                ).fetchone()
                if current["version"] != version or not current["wanted"]:
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
                await disk(self.clean_stage, stage, created)

    async def download(self, path, url, md5, expected_size, post_id, version):
        maximum = self.settings.library_max_bytes
        if isinstance(expected_size, int) and (expected_size < 1 or expected_size > maximum):
            raise HTTPException(413, "Original exceeds the configured save size limit")
        required = expected_size if isinstance(expected_size, int) else maximum
        if (await disk(shutil.disk_usage, self.root)).free < required + self.settings.library_reserve_bytes:
            raise HTTPException(507, "Smut storage has insufficient free space")
        response = None
        await self.media.acquire_slot("original")
        count = 0
        last_checked = 0
        checked_at = time.monotonic()
        hasher = hashlib.md5()
        try:
            response = await self.media._open(url)
            if response.status_code != 200:
                raise HTTPException(
                    response.status_code if response.status_code in {403, 404, 429, 503} else 502,
                    "CDN could not supply the original",
                )
            kind = response.headers.get("content-type", "").split(";")[0]
            if not (kind.startswith("image/") or kind.startswith("video/")) or kind == "image/svg+xml":
                raise HTTPException(415, "Original response is not supported media")
            length = response.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > maximum):
                raise HTTPException(413, "Original exceeds the configured save size limit")
            if response.headers.get("content-encoding", "identity") != "identity":
                raise HTTPException(502, "Original response unexpectedly changed its encoding")
            with path.open("xb") as output:
                async for chunk in response.aiter_raw(65536):
                    count += len(chunk)
                    if count > maximum:
                        raise HTTPException(413, "Original exceeds the configured save size limit")
                    if count - last_checked >= 1024 * 1024 or time.monotonic() - checked_at >= 0.25:
                        if not await self.current(post_id, version):
                            raise HTTPException(409, "Save superseded by a newer heart toggle")
                        last_checked = count
                        checked_at = time.monotonic()
                    await disk(output.write, chunk)
                    hasher.update(chunk)
                    self.media.metrics["bytes"] += len(chunk)
                await disk(output.flush)
                await disk(os.fsync, output.fileno())
            if (
                not count
                or (length and count != int(length))
                or (isinstance(expected_size, int) and count != expected_size)
            ):
                raise HTTPException(502, "Original download was incomplete")
            if hasher.hexdigest() != md5:
                raise HTTPException(502, "Original checksum did not match e621; nothing was saved")
        finally:
            try:
                if response:
                    await response.aclose()
            finally:
                self.media.release_slot("original")

    @staticmethod
    def write_file(path, data):
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

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
        # Metadata is present before the original becomes visible. Hard-linking
        # never replaces another file, even if one appears after the preflight.
        entries = sorted(record["files"], key=lambda entry: entry["name"] == record["original"])
        for entry in entries:
            destination = self.root / entry["name"]
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

        sync_directory(self.root)

    def remove_files(self, record):
        stage = self.paths(record)
        stage.mkdir(mode=0o700, exist_ok=True)
        sync_directory(stage.parent)
        # Check everything before starting. Quarantine each name atomically,
        # then recheck its identity: unlinking a verified pathname directly can
        # delete an external replacement made between verification and unlink.
        for entry in record["files"]:
            destination = self.root / entry["name"]
            quarantine = stage / (".remove-" + entry["name"])
            if quarantine.exists() or quarantine.is_symlink():
                self.verify(quarantine, entry)
            if destination.exists() or destination.is_symlink():
                self.verify(destination, entry)
        for entry in record["files"]:
            destination = self.root / entry["name"]
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
                    sync_directory(self.root)
                    sync_directory(stage)
                except OSError:
                    raise conflict(
                        f"File changed during removal; preserved at .hydra-studio-tmp/{record['token']}/{quarantine.name}"
                    ) from None
                raise
        # Make removal of visible names durable before deleting recovery links
        # or clearing the database manifest. Missing files are already removed.
        sync_directory(self.root)
        sync_directory(stage)
        self.clean_record_stage(record)

    def recover_for_save(self, record):
        stage = self.paths(record)
        complete = True
        for entry in record["files"]:
            present = False
            for path in (
                self.root / entry["name"],
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
            "UPDATE jobs SET status='done',lease_until=NULL,error=NULL,result_json=?,updated_at=? WHERE id=?",
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
    if not await request.app.state.db.one("SELECT 1 FROM posts WHERE post_id=?", (post_id,)):
        raise HTTPException(404, "Post not found")
    return state(post_id, await request.app.state.db.one("SELECT * FROM saved_media WHERE post_id=?", (post_id,)))


@router.put("/projects/{project_id}/library/{post_id}", response_model=schemas.SaveState)
async def set_saved(project_id: PositiveId, post_id: PositiveId, body: schemas.SaveMutation, request: Request):
    db = request.app.state.db
    signature = fingerprint(["save-media", project_id, post_id], body.model_dump(mode="json"))
    async with db.transaction() as conn:
        previous = await receipt(conn, body.request_id, signature)
        if previous is not None:
            return previous
        candidate = await (
            await conn.execute(
                "SELECT p.* FROM posts p JOIN project_candidates c USING(post_id) WHERE c.project_id=? AND c.post_id=?",
                (project_id, post_id),
            )
        ).fetchone()
        if not candidate:
            raise HTTPException(404, "Post is not a candidate in this project")
        if request.app.state.library.root is None:
            raise HTTPException(409, "Smut folder saving is not configured")
        old = await (await conn.execute("SELECT * FROM saved_media WHERE post_id=?", (post_id,))).fetchone()
        if (old["version"] if old else 0) != body.expected_version:
            raise conflict("Saved state changed in another tab; refresh before toggling")
        if (
            body.wanted
            and not (old and old["record_json"])
            and (candidate["unavailable"] or not candidate["file_url"] or candidate["media_ext"] not in EXTENSIONS)
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
        if queued >= 16 and not own:
            raise HTTPException(429, "Save queue is full; retry shortly")
        version = body.expected_version + 1
        await conn.execute(
            "INSERT INTO saved_media(post_id,version,wanted,status,updated_at) VALUES(?,?,?,'queued',?) "
            "ON CONFLICT(post_id) DO UPDATE SET version=excluded.version,wanted=excluded.wanted,status='queued',error=NULL,updated_at=excluded.updated_at",
            (post_id, version, int(body.wanted), time.time()),
        )
        # Obsolete queued work cannot prevent an off toggle from reaching the
        # single filesystem worker. A running transfer checks version per chunk.
        await conn.execute(
            "UPDATE jobs SET status='done',result_json='{}' WHERE type='library' AND status='pending' AND json_extract(payload_json,'$.post_id')=?",
            (post_id,),
        )
        await enqueue(conn, "library", f"library:{post_id}:{version}", {"post_id": post_id, "version": version})
        result = state(
            post_id, await (await conn.execute("SELECT * FROM saved_media WHERE post_id=?", (post_id,))).fetchone()
        )
        await save_receipt(conn, body.request_id, signature, result)
        return result
