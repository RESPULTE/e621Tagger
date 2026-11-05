"""Sample favourites use the same owned-file publication/removal as originals."""

import json
import os
import shutil
from uuid import UUID, uuid4

from fastapi import HTTPException

from backend.filesystem import disk, sync_directory

from .library import conflict, digest, regular


async def change_favorite(state, sample, wanted):
    library, db = state.library, state.db
    saved = await db.one("SELECT record_json FROM sample_library WHERE sample_id=?", (sample["id"],))
    if not wanted and not saved:
        return
    root_id = await library.prepare_root()
    record = json.loads(saved["record_json"]) if saved else None
    if record and record["root_id"] != root_id:
        raise conflict("Saved sample belongs to another collection; files were preserved")
    if not wanted:
        await disk(library.remove_files, record)
        async with db.transaction() as conn:
            await conn.execute("DELETE FROM sample_library WHERE sample_id=?", (sample["id"],))
        return
    if record:
        if not await disk(library.recover_for_save, record):
            async with db.transaction() as conn:
                await conn.execute("DELETE FROM sample_library WHERE sample_id=?", (sample["id"],))
            record = None
        else:
            await library.publish(record)
            return
    from .media_sampling import sample_path

    source = sample_path(db, sample)
    name = f"e621_{sample['post_id']}_{UUID(sample['id']).hex}.png"
    record = {
        "token": uuid4().hex,
        "root_id": root_id,
        "directory": library.settings.library_subdirectory,
        "original": name,
        "files": [{"name": name}],
    }
    destination = library.destination(record)
    if (destination / name).exists() or (destination / name).is_symlink():
        raise conflict("A sample filename already exists; Studio will not overwrite or adopt it")
    stage = library.paths(record)

    def prepare():
        if not regular(source) or source.stat().st_size > state.settings.reference_max_bytes * 8:
            raise HTTPException(409, "Sample image is unavailable or exceeds the save limit")
        if digest(source) != source.stem:
            raise conflict("Saved sample image changed outside Studio")
        if shutil.disk_usage(destination).free < source.stat().st_size + state.settings.library_reserve_bytes:
            raise HTTPException(507, "Collection storage has insufficient free space")
        stage.mkdir(mode=0o700)
        try:
            with source.open("rb") as input_file, (stage / name).open("xb") as output:
                shutil.copyfileobj(input_file, output, length=65536)
                output.flush()
                os.fsync(output.fileno())
            if digest(stage / name) != digest(source):
                raise conflict("Sample image changed during save")
            record["files"] = library.describe(stage, [name])
            sync_directory(stage)
            return record
        except BaseException:
            # This invocation alone owns this newly created random stage.
            library.clean_stage(stage, [name])
            raise

    reserved = False
    try:
        record = await disk(prepare)
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO sample_library VALUES(?,?)", (sample["id"], json.dumps(record)))
        reserved = True
        await library.publish(record)
    except OSError:
        raise HTTPException(507, "Sample could not be saved; check collection storage and retry") from None
    finally:
        if not reserved and stage.exists():
            await disk(library.clean_stage, stage, [name])
