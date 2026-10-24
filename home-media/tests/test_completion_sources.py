import json
import sqlite3
from types import SimpleNamespace

from observability.completion_sources import CompletionSources
from observability.incidents import Incidents
from observability.notifications import Notifications


def setup(tmp_path, config):
    store = Incidents(tmp_path / "state.sqlite")
    notices = Notifications(store, now=1000)
    writer = SimpleNamespace(emit=lambda *_a, **_k: None)
    return store, notices, CompletionSources(notices, writer, config)


def test_unmanic_reads_rotated_feed_without_counting_duplicates_or_partial_records(tmp_path):
    feed = tmp_path / "completion-events.ndjson"
    db = tmp_path / "unmanic.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE tasks (id INTEGER)")
    connection.commit()
    connection.close()
    config = {"unmanic": {"enabled": True, "feed": str(feed), "database": str(db)}}
    store, notices, source = setup(tmp_path, config)
    row = {"job_id": "one", "completed_at": 1100, "output_bytes": 1_000_000}
    feed.write_text(json.dumps(row) + "\n")
    assert not source.unmanic()
    assert not source.unmanic()
    feed.rename(feed.with_name(feed.name + ".1"))
    second = json.dumps({**row, "job_id": "two"})
    feed.write_text(second)
    assert not source.unmanic()
    with feed.open("a") as handle:
        handle.write("\n")
    assert not source.unmanic()
    assert notices.flush(now=1400) == 1
    assert store.db.execute("SELECT body FROM outbox").fetchone()[0] == (
        "Unmanic processing complete\n2 videos · 2 MB")
    store.close()


def test_unmanic_waits_for_existing_application_queue_to_empty(tmp_path):
    db = tmp_path / "unmanic.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE tasks (id INTEGER)")
    connection.execute("INSERT INTO tasks VALUES (1)")
    connection.commit()
    connection.close()
    store, _notices, source = setup(tmp_path, {"unmanic": {
        "feed": str(tmp_path / "not-yet-created"), "database": str(db)}})
    assert source.unmanic()
    store.close()


def test_immich_uses_ingest_time_not_old_camera_date_and_excludes_archive(tmp_path):
    store, notices, source = setup(tmp_path, {"immich": {"library": "uploads"}})
    asset = {"id": "new", "libraryId": "uploads", "type": "IMAGE",
             "createdAt": "1970-01-01T00:18:20Z", "fileCreatedAt": "1960-01-01T00:00:00Z",
             "exifInfo": {"fileSizeInByte": 2_500_000}}
    assert source.immich_event(asset)
    assert source.immich_event({**asset, "id": "archive", "libraryId": "archive"})
    assert not source.immich_event({**asset, "id": "pending", "exifInfo": {}})
    assert notices.flush(now=1400) == 1
    assert store.db.execute("SELECT body FROM outbox").fetchone()[0] == (
        "Immich upload complete\n1 photo · 2.5 MB")
    store.close()


def test_jellyfin_baselines_history_and_only_notifies_new_member_even_with_old_file_date(
    tmp_path, monkeypatch
):
    store, notices, source = setup(tmp_path, {"jellyfin": {"libraries": ["family"]}})
    rows = [{"Id": "old", "Type": "Video", "MediaSources": [{"Size": 3_000_000}]}]
    monkeypatch.setattr(source, "jellyfin_request", lambda *_args: {
        "Items": list(rows), "TotalRecordCount": len(rows)})
    monkeypatch.setattr("observability.completion_sources.time.time", lambda: 1100)
    assert not source.jellyfin()
    assert notices.flush(now=1400) == 0
    rows.append({"Id": "new", "Type": "Photo", "DateCreated": "1960-01-01T00:00:00Z"})
    monkeypatch.setattr(source, "jellyfin_size", lambda _row: 4_000_000)
    assert not source.jellyfin()
    assert not source.jellyfin()
    assert notices.flush(now=1400) == 1
    assert store.db.execute("SELECT body FROM outbox").fetchone()[0] == (
        "Jellyfin library updated\n1 photo · 4 MB")
    store.close()


def test_jellyfin_photo_stat_never_follows_path_outside_mapped_mount(tmp_path):
    root = tmp_path / "family"
    root.mkdir()
    photo = root / "photo.jpg"
    photo.write_bytes(b"photo")
    store, _notices, source = setup(tmp_path, {"jellyfin": {
        "mounts": [{"container": "/photos", "host": str(root)}]}})
    assert source.jellyfin_size({"Path": "/photos/photo.jpg"}) == 5
    assert source.jellyfin_size({"Path": "/photos/../private.jpg"}) is None
    assert source.jellyfin_size({"Path": "/unrelated/photo.jpg"}) is None
    store.close()


def test_immich_keeps_missing_metadata_and_rotates_retries_without_losing_counts(tmp_path, monkeypatch):
    store, notices, source = setup(tmp_path, {"immich": {"library": "uploads"}})
    now = 200_000
    monkeypatch.setattr("observability.completion_sources.time.time", lambda: now)
    pending = {f"id-{index}": 1100 for index in range(21)}
    store.set_state("completion/immich_pending", pending)
    requests = []
    ready = set()

    def api(path, _payload=None):
        if path == "/search/metadata":
            return {"assets": {"items": [], "nextPage": None}}
        identity = path.rsplit("/", 1)[-1]
        requests.append(identity)
        return {"id": identity, "libraryId": "uploads", "type": "IMAGE",
                "createdAt": "1970-01-01T00:18:20Z",
                "exifInfo": {"fileSizeInByte": 2000} if identity in ready else {}}

    monkeypatch.setattr(source, "immich_request", api)
    assert source.immich()
    assert len(store.state("completion/immich_pending")) == 21
    ready.add("id-20")
    assert source.immich()
    assert "id-20" in requests and "id-20" not in store.state("completion/immich_pending")
    assert store.db.execute("SELECT sum(bytes) FROM completion_events").fetchone()[0] == 2000
    assert notices.flush(now=now, busy={"immich"}) == 0
    store.close()
