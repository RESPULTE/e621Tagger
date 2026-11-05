"""Offline, journalled conversion of Studio-owned favourites to image-only Inbox."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from .app.config import Settings
from .app.db import Database
from .app.library import Library
from .filesystem import sync_directory


def journal(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


async def migrate(library, report, *, apply=False):
    """Caller holds Database's exclusive application process lock throughout."""
    report = Path(report)
    root_id = await library.prepare_root()
    inbox = library.destination({"directory": "Inbox"})
    active = await library.db.one("SELECT count(*) AS n FROM jobs WHERE type='library' AND status IN ('pending','running')")
    if active["n"] or await library.db.one("SELECT 1 FROM library_staging LIMIT 1"):
        raise RuntimeError("Finish active favourite jobs/staging before migrating")
    if report.exists():
        plan = json.loads(report.read_text())
        if (plan.get("format") != "studio-image-only-inbox-v1" or plan.get("root_id") != root_id
                or plan.get("root") != str(library.root)):
            raise ValueError("Migration journal belongs to another collection")
    else:
        plan = {"format": "studio-image-only-inbox-v1", "root": str(library.root), "root_id": root_id, "items": []}
        for row in await library.db.rows("SELECT * FROM saved_media WHERE record_json IS NOT NULL ORDER BY post_id"):
            if row["status"] != "saved" or not row["present"] or not row["wanted"]:
                raise RuntimeError("Only settled saved favourites can migrate")
            record = json.loads(row["record_json"])
            library.paths(record)  # Validate every owned filename and token.
            if record["root_id"] != root_id:
                raise ValueError("Favourite belongs to another collection")
            original = [entry for entry in record["files"] if entry["name"] == record["original"]]
            extras = [entry for entry in record["files"] if entry["name"] != record["original"]]
            if len(original) != 1 or any(Path(entry["name"]).suffix not in {".json", ".txt", ".xmp"} for entry in extras):
                raise ValueError("Unexpected favourite manifest; nothing was removed")
            source = library.destination(record)
            for entry in record["files"]:
                path = source / entry["name"]
                if entry in original or path.exists() or path.is_symlink():
                    library.verify(path, entry)
            destination = inbox / original[0]["name"]
            if source != inbox and (destination.exists() or destination.is_symlink()):
                raise FileExistsError("Inbox already contains a destination; preserve it and inspect")
            if source == inbox and not extras:
                continue
            updated = {**record, "directory": "Inbox", "files": original}
            plan["items"].append({"post_id": row["post_id"], "version": row["version"],
                                  "old_json": row["record_json"], "new_json": json.dumps(updated, sort_keys=True),
                                  "phase": "planned"})
        if apply:
            report.parent.mkdir(parents=True, exist_ok=True)
            journal(report, plan)
    if not apply:
        return plan
    if not os.access(inbox, os.W_OK | os.X_OK):
        raise PermissionError("Studio cannot write to the existing Inbox")
    for item in plan["items"]:
        old, new = json.loads(item["old_json"]), json.loads(item["new_json"])
        library.paths(old)
        library.paths(new)
        expected = [entry for entry in old["files"] if entry["name"] == old["original"]]
        if (len(expected) != 1 or new != {**old, "files": expected, "directory": "Inbox"}
                or old["root_id"] != root_id or new["root_id"] != root_id):
            raise ValueError("Invalid image-only migration journal")
        row = await library.db.one("SELECT * FROM saved_media WHERE post_id=?", (item["post_id"],))
        if (not row or row["version"] != item["version"] or row["record_json"] not in {item["old_json"], item["new_json"]}
                or not row["present"] or not row["wanted"] or row["status"] != "saved"):
            raise RuntimeError("Favourite changed since migration planning; preserve files and recheck")
        entry = new["files"][0]
        source_root = library.destination(old)
        source, destination = source_root / entry["name"], inbox / entry["name"]
        if not destination.exists() and not destination.is_symlink():
            library.verify(source, entry)
            os.link(source, destination)  # No-clobber; same inode and original bytes.
            sync_directory(inbox)
        library.verify(destination, entry)
        async with library.db.transaction() as conn:
            await conn.execute("UPDATE saved_media SET record_json=? WHERE post_id=? AND version=?",
                               (item["new_json"], item["post_id"], item["version"]))
        extras = [entry for entry in old["files"] if entry["name"] != old["original"]]
        obsolete = old["files"] if source_root != inbox else extras
        if obsolete:
            # Atomically quarantine and recheck every old name before unlinking.
            # The image remains linked in Inbox; a raced external replacement at
            # its old path is restored and preserved by the existing helper.
            library.remove_files({**old, "files": obsolete})
        item["phase"] = "complete"
        journal(report, plan)
    plan["complete"] = True
    journal(report, plan)
    return plan


async def run(args):
    db = Database(args.data_root)
    await db.open()  # Refuses while the live application owns this database.
    try:
        settings = Settings(data_root=args.data_root, library_root=args.library_root, library_subdirectory="Inbox")
        library = Library(db, None, None, settings)
        result = await migrate(library, args.report, apply=args.apply)
        print(json.dumps({"applied": args.apply, "images": len(result["items"]), "complete": result.get("complete", False)}))
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--library-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
