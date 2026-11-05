import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.migrate_favourites import migrate
from backend.tests.test_library import process, setup, toggle
from backend.tests.test_workflow import run


def legacy(client):
    pid = setup(client)
    toggle(client, pid)
    process(client)
    library = client.app.state.library
    (library.root / "Inbox").mkdir()
    row = run(client, library.db.one("SELECT * FROM saved_media WHERE post_id=999"))
    record = json.loads(row["record_json"])
    stem = Path(record["original"]).stem
    extra_names = [stem + suffix for suffix in (".json", ".txt", ".xmp")]
    for name in extra_names:
        (library.root / name).write_bytes(b"old Studio metadata")
    record["files"].extend(library.describe(library.root, extra_names))

    async def save_record():
        async with library.db.transaction() as conn:
            await conn.execute("UPDATE saved_media SET record_json=? WHERE post_id=999", (json.dumps(record),))

    run(client, save_record())
    return library, record


def test_migration_moves_exact_original_removes_only_owned_sidecars_and_replays(client, tmp_path):
    library, old = legacy(client)
    original = library.root / old["original"]
    content, inode = original.read_bytes(), original.stat().st_ino
    manual = library.root / "manual.json"
    manual.write_bytes(b"must remain")
    report = tmp_path / "migration.json"
    before = client.get("/api/library/posts/999").json()
    plan = run(client, migrate(library, report))
    assert len(plan["items"]) == 1 and original.exists() and not report.exists()
    result = run(client, migrate(library, report, apply=True))
    assert result["complete"]
    target = library.root / "Inbox" / old["original"]
    assert target.read_bytes() == content and target.stat().st_ino == inode
    assert not original.exists()
    assert not list(library.root.glob("e621_999_*"))
    assert manual.read_bytes() == b"must remain"
    assert client.get("/api/library/posts/999").json() == before
    saved = run(client, library.db.one("SELECT record_json FROM saved_media WHERE post_id=999"))["record_json"]
    assert json.loads(saved)["files"] == [old["files"][0]]
    assert run(client, migrate(library, report, apply=True))["complete"]
    assert run(client, library.db.one("SELECT record_json FROM saved_media WHERE post_id=999"))["record_json"] == saved
    # Ordinary favourite deletion still follows its migrated ownership record.
    toggle(client, 1, wanted=False, version=1)
    process(client)
    assert not target.exists() and manual.exists()


def test_migration_resumes_after_record_commit_before_original_unlink(client, tmp_path, monkeypatch):
    library, old = legacy(client)
    source = library.root / old["original"]
    remove = library.remove_files
    failed = False

    def interrupted(record):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("interrupted move after durable Inbox link")
        return remove(record)

    monkeypatch.setattr(library, "remove_files", interrupted)
    report = tmp_path / "migration.json"
    with pytest.raises(OSError):
        run(client, migrate(library, report, apply=True))
    target = library.root / "Inbox" / old["original"]
    assert target.exists() and source.exists() and target.stat().st_ino == source.stat().st_ino
    assert run(client, migrate(library, report, apply=True))["complete"]
    assert target.exists() and not source.exists()


def test_migration_preserves_replacement_raced_during_old_name_removal(client, tmp_path, monkeypatch):
    import os

    library, old = legacy(client)
    source = library.root / old["original"]
    original_bytes = source.read_bytes()
    rename = os.rename
    replaced = False

    def raced_move(before, after):
        nonlocal replaced
        if before == source and not replaced:
            replaced = True
            replacement = library.root / "external-replacement"
            replacement.write_bytes(b"external file must survive")
            os.replace(replacement, source)
        return rename(before, after)

    monkeypatch.setattr(os, "rename", raced_move)
    with pytest.raises(HTTPException, match="409"):
        run(client, migrate(library, tmp_path / "migration.json", apply=True))
    assert source.read_bytes() == b"external file must survive"
    assert (library.root / "Inbox" / old["original"]).read_bytes() == original_bytes


@pytest.mark.parametrize("changed", ["sidecar", "destination"])
def test_migration_refuses_replacements_and_destination_collisions_before_changes(client, tmp_path, changed):
    library, old = legacy(client)
    source = library.root / old["original"]
    if changed == "sidecar":
        path = library.root / old["files"][1]["name"]
        path.write_bytes(b"manual replacement")
        error = HTTPException
    else:
        path = library.root / "Inbox" / old["original"]
        path.write_bytes(b"manual destination")
        error = FileExistsError
    with pytest.raises(error):
        run(client, migrate(library, tmp_path / "migration.json", apply=True))
    assert source.exists() and path.read_bytes().startswith(b"manual")
    saved = json.loads(run(client, library.db.one("SELECT record_json FROM saved_media WHERE post_id=999"))["record_json"])
    assert "directory" not in saved and len(saved["files"]) == 4
