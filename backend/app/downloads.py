"""Explicit dataset outputs and collection intents share the jobs scheduler."""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from backend.filesystem import disk, sync_directory

from . import auth, schemas
from .jobs import enqueue
from .library import digest, regular
from .reviews import fingerprint, receipt, save_receipt

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_session)])


class Downloads:
    def __init__(self, db, source, transfers):
        self.db, self.source, self.transfers = db, source, transfers
        self.root = db.root / "exports"

    def folder(self, dataset_id):
        name = str(UUID(dataset_id))
        if self.root.is_symlink():
            raise HTTPException(409, "Export root is unsafe")
        self.root.mkdir(exist_ok=True)
        path = self.root / name
        if path.is_symlink():
            raise HTTPException(409, "Export destination is unsafe")
        path.mkdir(exist_ok=True)
        return path

    async def recover_partials(self, library):
        # Only exact journalled inodes under an owned destination are disposable.
        library_ok = False
        if library.root:
            try:
                await library.prepare_root()
                await library.recover_staging()
                library_ok = True
            except (HTTPException, OSError):
                pass
        for row in await self.db.rows("SELECT * FROM download_partials"):
            if await self.db.one("SELECT 1 FROM library_staging WHERE job_id=?", (row["job_id"],)):
                continue  # The collection stage/publication journal owns recovery.
            path = Path(row["path"])
            roots = [self.root] + ([library.root / ".hydra-studio-tmp"] if library_ok else [])
            if not any(
                root.resolve() == root.absolute()
                and path.absolute().is_relative_to(root.absolute())
                and path.parent.resolve().is_relative_to(root.resolve())
                for root in roots
            ):
                continue
            if not path.name.endswith(".part") or path.parent.is_symlink():
                continue
            if regular(path) and path.stat().st_ino == row["inode"] and path.stat().st_dev == row["device"]:
                await disk(path.unlink)
                # Only an empty directory owned by this staged transfer.
                if library_ok and path.parent.parent == library.root / ".hydra-studio-tmp":
                    try:
                        await disk(path.parent.rmdir)
                    except OSError:
                        pass
            async with self.db.transaction() as conn:
                await conn.execute("DELETE FROM download_partials WHERE job_id=?", (row["job_id"],))

    async def current(self, job_id):
        row = await self.db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
        return row and row["status"] in {"pending", "running"}

    async def process(self, job, payload):
        dataset_id, post_id = payload["dataset_id"], payload["post_id"]
        row = await self.db.one("SELECT manifest_json FROM datasets WHERE id=?", (dataset_id,))
        if not row:
            raise HTTPException(404, "Frozen dataset unavailable")
        manifest = json.loads(row["manifest_json"])
        sample = next((s for s in manifest["samples"] if s["post_id"] == post_id and s["target"] is not None), None)
        if (
            not sample
            or not re.fullmatch("[a-f0-9]{32}", sample["md5"] or "")
            or sample["media_ext"] not in {"jpg", "jpeg", "png", "webp"}
        ):
            raise HTTPException(422, "Sample is not eligible for this frozen export")
        root = await disk(self.folder, dataset_id)
        name = f"{post_id}_{sample['md5']}.{sample['media_ext']}"
        destination, partial = root / name, root / f".job-{job['id']}.part"
        record = json.loads(job["result_json"]) if job.get("result_json") else None
        if record:
            if record.get("relative_path") != name:
                raise HTTPException(409, "Export publication journal is invalid")
            await disk(self.publish, partial, destination, record)
        else:
            post = (await self.source.request(f"/posts/{post_id}.json", background=True))["post"]
            file = post.get("file") or {}
            if post.get("flags", {}).get("deleted") or not file.get("url"):
                raise HTTPException(404, "Original is unavailable; frozen review was preserved")
            if post.get("id") != post_id or file.get("md5") != sample["md5"] or file.get("ext") != sample["media_ext"]:
                raise HTTPException(409, "Source identity changed since dataset freeze")
            if destination.exists() or destination.is_symlink() or partial.exists() or partial.is_symlink():
                raise HTTPException(409, "Export path already exists without a publication journal")
            try:
                verified = await self.transfers.save(
                    partial,
                    sample["media_url"],
                    sample["md5"],
                    file.get("size"),
                    root,
                    job_id=job["id"],
                    current=lambda: self.current(job["id"]),
                )
                record = {
                    **verified,
                    "relative_path": name,
                    "inode": partial.stat().st_ino,
                    "dataset_id": dataset_id,
                    "post_id": post_id,
                }
                async with self.db.transaction() as conn:
                    current = await (await conn.execute("SELECT status FROM jobs WHERE id=?", (job["id"],))).fetchone()
                    if current["status"] == "cancelled":
                        raise HTTPException(409, "Download cancelled")
                    await conn.execute("UPDATE jobs SET result_json=? WHERE id=?", (json.dumps(record), job["id"]))
                await disk(self.publish, partial, destination, record)
            except BaseException:
                if record is None:
                    await self.clean_partial(job["id"], partial)
                raise
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE jobs SET status='done',lease_until=NULL,error=NULL,updated_at=? WHERE id=? AND status!='cancelled'",
                (time.time(), job["id"]),
            )
            await conn.execute("DELETE FROM download_partials WHERE job_id=?", (job["id"],))

    async def clean_partial(self, job_id, path):
        row = await self.db.one("SELECT * FROM download_partials WHERE job_id=?", (job_id,))
        if (
            row
            and row["path"] == str(path.absolute())
            and regular(path)
            and path.stat().st_ino == row["inode"]
            and path.stat().st_dev == row["device"]
        ):
            await disk(path.unlink)
            async with self.db.transaction() as conn:
                await conn.execute("DELETE FROM download_partials WHERE job_id=?", (job_id,))

    @staticmethod
    def publish(partial, destination, record):
        source = destination if destination.exists() or destination.is_symlink() else partial
        if (
            not regular(source)
            or source.stat().st_ino != record["inode"]
            or source.stat().st_size != record["bytes"]
            or digest(source) != record["sha256"]
        ):
            raise HTTPException(409, "Export files changed outside Studio; files were preserved")
        if source == partial:
            os.link(partial, destination)
            sync_directory(destination.parent)
        if partial.exists():
            if not regular(partial) or partial.stat().st_ino != record["inode"]:
                raise HTTPException(409, "Export partial changed outside Studio")
            partial.unlink()
            sync_directory(destination.parent)


@router.post("/datasets/{dataset_id}/media")
async def queue_export(dataset_id: UUID, body: schemas.Mutation, request: Request):
    db = request.app.state.db
    signature = fingerprint(["export-media", str(dataset_id)], {})
    async with db.transaction() as conn:
        previous = await receipt(conn, body.request_id, signature)
        if previous is not None:
            return previous
        row = await (await conn.execute("SELECT * FROM datasets WHERE id=?", (str(dataset_id),))).fetchone()
        if not row:
            raise HTTPException(404, "Dataset not found")
        samples = [s for s in json.loads(row["manifest_json"])["samples"] if s["target"] is not None]
        queued = (
            await (
                await conn.execute(
                    "SELECT count(*) FROM jobs WHERE type IN ('library','dataset_media') AND status IN ('pending','running')"
                )
            ).fetchone()
        )[0]
        keys = [(s, f"dataset:{dataset_id}:{s['post_id']}") for s in samples]
        missing = []
        ids = []
        for sample, key in keys:
            old = await (await conn.execute("SELECT id FROM jobs WHERE job_key=?", (key,))).fetchone()
            if old:
                ids.append(old["id"])
            else:
                missing.append((sample, key))
        if queued + len(missing) > 5000:
            raise HTTPException(429, "Download queue is full; retry after existing downloads finish")
        for sample, key in missing:
            ids.append(
                await enqueue(
                    conn,
                    "dataset_media",
                    key,
                    {"dataset_id": str(dataset_id), "post_id": sample["post_id"], "project_id": row["project_id"]},
                )
            )
        result = {"job_ids": ids, "dataset_id": str(dataset_id)}
        await save_receipt(conn, body.request_id, signature, result)
    return result


@router.get("/downloads")
async def list_downloads(request: Request):
    rows = await request.app.state.db.rows(
        "SELECT id,type,status,attempts,error,progress_bytes,total_bytes,result_json,payload_json,updated_at FROM jobs WHERE type IN ('library','dataset_media') ORDER BY id DESC LIMIT 200"
    )
    for row in rows:
        payload = json.loads(row.pop("payload_json"))
        row.update(
            purpose="Collection" if row["type"] == "library" else "Dataset export",
            post_id=payload["post_id"],
            dataset_id=payload.get("dataset_id"),
            destination="Smut" if row["type"] == "library" else "Dataset exports",
        )
        row.pop("result_json")
    return {
        "items": rows,
        "metrics": request.app.state.transfers.metrics,
        "active": (
            await request.app.state.db.one(
                "SELECT count(*) AS n FROM jobs WHERE type IN ('library','dataset_media') AND status IN ('pending','running')"
            )
        )["n"],
    }


@router.post("/downloads/{job_id}/{action}")
async def control(job_id: int, action: str, body: schemas.Mutation, request: Request):
    db = request.app.state.db
    if action not in {"cancel", "retry"}:
        raise HTTPException(404, "Unknown download action")
    signature = fingerprint(["download-control", job_id, action], {})
    async with db.transaction() as conn:
        previous = await receipt(conn, body.request_id, signature)
        if previous is not None:
            return previous
        job = await (
            await conn.execute("SELECT * FROM jobs WHERE id=? AND type IN ('library','dataset_media')", (job_id,))
        ).fetchone()
        if not job:
            raise HTTPException(404, "Download not found")
        payload = json.loads(job["payload_json"])
        if action == "cancel":
            if job["status"] in {"done", "cancelled", "failed"}:
                result = {"id": job_id, "status": job["status"]}
                await save_receipt(conn, body.request_id, signature, result)
                return result
            if job["result_json"]:
                raise HTTPException(409, "Verified files are publishing; wait for completion")
            if job["type"] == "library":
                saved = await (
                    await conn.execute("SELECT * FROM saved_media WHERE post_id=?", (payload["post_id"],))
                ).fetchone()
                if saved["status"] in {"publishing", "removing"} or (saved["record_json"] and not saved["present"]):
                    raise HTTPException(409, "Collection files are publishing; wait for completion")
                await conn.execute(
                    "UPDATE saved_media SET version=version+1,status='cancelled',updated_at=? WHERE post_id=? AND version=?",
                    (time.time(), payload["post_id"], payload["version"]),
                )
            await conn.execute(
                "UPDATE jobs SET status='cancelled',lease_until=NULL,updated_at=? WHERE id=?", (time.time(), job_id)
            )
        elif job["status"] in {"failed", "cancelled"}:
            # A running cancelled transfer must relinquish its destination before retry.
            if job_id in request.app.state.workers.active_downloads:
                raise HTTPException(409, "Cancellation is finishing; retry shortly")
            if job["type"] == "library":
                saved = await (
                    await conn.execute("SELECT * FROM saved_media WHERE post_id=?", (payload["post_id"],))
                ).fetchone()
                payload["version"] = saved["version"] + 1
                await conn.execute(
                    "UPDATE saved_media SET version=?,status='queued',error=NULL,updated_at=? WHERE post_id=?",
                    (payload["version"], time.time(), payload["post_id"]),
                )
            await conn.execute(
                "UPDATE jobs SET status='pending',attempts=0,available_at=?,error=NULL,payload_json=?,updated_at=? WHERE id=?",
                (time.time(), json.dumps(payload), time.time(), job_id),
            )
        result = {
            "id": job_id,
            "status": (await (await conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,))).fetchone())[
                "status"
            ],
        }
        await save_receipt(conn, body.request_id, signature, result)
    return result


@router.get("/datasets/{dataset_id}/media/manifest")
async def media_manifest(dataset_id: UUID, request: Request):
    db = request.app.state.db
    row = await db.one("SELECT * FROM datasets WHERE id=?", (str(dataset_id),))
    if not row:
        raise HTTPException(404, "Dataset not found")
    manifest = json.loads(row["manifest_json"])
    samples = [s for s in manifest["samples"] if s["target"] is not None]
    verified = []
    for sample in samples:
        job = await db.one(
            "SELECT * FROM jobs WHERE job_key=? AND status='done'", (f"dataset:{dataset_id}:{sample['post_id']}",)
        )
        if not job:
            raise HTTPException(409, "Media export is incomplete; check Downloads")
        record = json.loads(job["result_json"])
        root = await disk(request.app.state.downloads.folder, str(dataset_id))
        path = root / record["relative_path"]
        if not regular(path) or path.stat().st_size != record["bytes"] or await disk(digest, path) != record["sha256"]:
            raise HTTPException(409, "Export media changed; completion cannot be verified")
        verified.append({**sample, **{key: record[key] for key in ("relative_path", "bytes", "sha256")}})
    output = {
        **manifest,
        "source_manifest_sha256": row["manifest_sha256"],
        "samples": verified,
        "media_export_version": 1,
    }
    content = json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return Response(
        content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="dataset-{dataset_id}-media.json"',
            "X-Manifest-SHA256": hashlib.sha256(content.encode()).hexdigest(),
        },
    )


@router.get("/datasets/{dataset_id}/media/files/{post_id}")
async def exported_file(dataset_id: UUID, post_id: int, request: Request):
    job = await request.app.state.db.one(
        "SELECT result_json FROM jobs WHERE job_key=? AND status='done'", (f"dataset:{dataset_id}:{post_id}",)
    )
    if not job:
        raise HTTPException(404, "Verified export file not found")
    record = json.loads(job["result_json"])
    root = await disk(request.app.state.downloads.folder, str(dataset_id))
    path = root / record["relative_path"]
    if not regular(path) or path.stat().st_size != record["bytes"] or await disk(digest, path) != record["sha256"]:
        raise HTTPException(409, "Export media changed")
    return FileResponse(path, filename=path.name, headers={"Cache-Control": "private, no-store"})


@router.get("/datasets/{dataset_id}/media/bundle")
async def media_bundle(dataset_id: UUID, request: Request):
    # Packaging is an explicit export, after the shared queue has checked every
    # original. A temporary archive is closed on disconnect and never retained.
    exported = await media_manifest(dataset_id, request)
    content = exported.body
    manifest = json.loads(content)
    root = await disk(request.app.state.downloads.folder, str(dataset_id))
    required = sum(s["bytes"] for s in manifest["samples"]) + len(content) + 1024 * 1024
    if required > 4 * 1024**3:
        raise HTTPException(413, "Bundle exceeds 4 GiB; download the checked manifest and individual files")
    if (await disk(shutil.disk_usage, root)).free < required + request.app.state.settings.library_reserve_bytes:
        raise HTTPException(507, "Insufficient space for a temporary export bundle")

    def package():
        archive = tempfile.TemporaryFile(dir=root)
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
                for sample in manifest["samples"]:
                    name = sample["relative_path"]
                    if not re.fullmatch(r"[1-9][0-9]*_[a-f0-9]{32}\.(jpg|jpeg|png|webp)", name):
                        raise HTTPException(409, "Invalid export path")
                    path = root / name
                    if not regular(path):
                        raise HTTPException(409, "Export file changed")
                    checksum = hashlib.sha256()
                    count = 0
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    with path.open("rb") as source, bundle.open(info, "w", force_zip64=True) as output:
                        for chunk in iter(lambda: source.read(65536), b""):
                            count += len(chunk)
                            checksum.update(chunk)
                            output.write(chunk)
                    if count != sample["bytes"] or checksum.hexdigest() != sample["sha256"]:
                        raise HTTPException(409, "Export file changed while packaging")
                bundle.writestr(zipfile.ZipInfo("manifest.json", date_time=(1980, 1, 1, 0, 0, 0)), content)
            archive.seek(0)
            return archive
        except BaseException:
            archive.close()
            raise

    archive = await disk(package)

    async def chunks():
        try:
            while chunk := await disk(archive.read, 65536):
                yield chunk
        finally:
            await disk(archive.close)

    return StreamingResponse(
        chunks(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="dataset-{dataset_id}.zip"',
            "Cache-Control": "private, no-store",
        },
    )
