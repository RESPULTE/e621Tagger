import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from backend.app.db import SCHEMA, Database
from backend.app.quality import groups, manifest_samples
from backend.app.schemas import DatasetCreate
from backend.tests.test_workflow import browse, project, run


def review(client, pid, post_id, state, version=0):
    result = client.patch(
        f"/api/projects/{pid}/labels/{post_id}",
        json={
            "request_id": str(uuid4()),
            "expected_version": version,
            "state": state,
        },
    )
    assert result.status_code == 200


def test_frozen_manifest_exports_idempotency_and_later_reviews(client):
    created, _ = project(client)
    browse(client, created["id"])
    pid = created["id"]
    for post_id, state in ((999, "positive"), (998, "negative"), (997, "unsure"), (1000, "positive")):
        review(client, pid, post_id, state)
    body = {"request_id": str(uuid4()), "name": "First frozen version"}
    path = f"/api/projects/{pid}/datasets"
    assert client.post(path, json=body).status_code == 409
    assert client.post(f"/api/projects/{pid}/quality/prepare").json()["scheduled"] == 2
    for post_id in (999, 998):
        run(client, client.app.state.media.download_preview(post_id))
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: client.post(path, json=body), range(2)))
    assert all(result.status_code == 200 for result in results)
    frozen = results[0].json()
    assert results[1].json() == frozen
    assert frozen["counts"]["included"] == 2
    version_id = frozen["id"]
    export = f"/api/datasets/{version_id}/export"
    original = client.get(export + "?format=json")
    assert hashlib.sha256(original.content).hexdigest() == frozen["manifest_sha256"]
    manifest = original.json()
    included = [row for row in manifest["samples"] if row["target"] is not None]
    assert {row["target"] for row in included} == {0, 1}
    assert len({row["split"] for row in included}) == 1  # mock artwork shares one near-duplicate group
    assert all(row["relative_path"] is None and row["provenance"] for row in included)
    reasons = {row["post_id"]: row["excluded_reason"] for row in manifest["samples"]}
    assert reasons[997] == "unsure" and reasons[1000] == "unsupported_media"
    lines = client.get(export).text.splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["type"] == "manifest"
    assert json.loads(lines[0])["manifest_sha256"] == frozen["manifest_sha256"]
    csv = client.get(export + "?format=csv")
    assert csv.status_code == 200 and "provenance" in csv.text and "attachment" in csv.headers["content-disposition"]
    assert client.get(export + "?format=zip").status_code == 422
    review(client, pid, 999, "negative", 1)
    assert client.get(export + "?format=json").content == original.content
    assert client.post(path, json=body).json() == frozen
    assert client.post(path, json={**body, "name": "different"}).status_code == 409
    assert len(client.get(path).json()) == 1

    async def mutate():
        async with client.app.state.db.transaction() as conn:
            await conn.execute("UPDATE datasets SET name='changed'")

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        run(client, mutate())
    quality = client.get(f"/api/projects/{pid}/quality").json()
    assert quality["missing_fingerprints"] == 0
    assert quality["labels"]["unsure"] == 1 and quality["availability"]["unsupported_media"] == 1


def sample(post_id, *, md5=None, dhash=None, state="positive", artist=None):
    return {
        "post_id": post_id,
        "md5": md5 or str(post_id),
        "dhash": dhash,
        "state": state,
        "tags_json": json.dumps({"artist": [artist] if artist else []}),
        "unavailable": 0,
        "file_url": f"https://static1.e621.net/{post_id}.png",
        "media_ext": "png",
        "preview_url": f"https://static1.e621.net/preview/{post_id}.png",
        "review_version": 1,
        "reviewed_at": 1,
        "event_id": post_id,
        "provenance_json": "[]",
    }


def test_transitive_near_groups_artist_groups_exact_conflicts_and_deterministic_splits():
    rows = [
        sample(1, dhash="0000000000000000"),
        sample(2, dhash="000000000000000f"),
        sample(3, dhash="00000000000000ff"),
        sample(4, dhash="ffffffffffffffff", artist="same"),
        sample(5, dhash="aaaaaaaaaaaaaaaa", artist="same"),
        sample(6, md5="duplicate"),
        sample(7, md5="duplicate", state="negative"),
        sample(8, md5="repeat"),
        sample(9, md5="repeat"),
    ]
    clustered = groups(rows, artists=True)
    assert clustered[1] == clustered[2] == clustered[3]
    assert clustered[4] == clustered[5] and clustered[4] != clustered[1]
    options = DatasetCreate(request_id=uuid4(), name="test")
    samples = manifest_samples(rows, options)
    reasons = {row["post_id"]: row["excluded_reason"] for row in samples}
    assert reasons[6] == reasons[7] == "conflicting_exact_labels"
    assert reasons[9] == "exact_duplicate" and reasons[8] is None
    assert len({row["split"] for row in samples[:3]}) == 1
    assert sorted(manifest_samples(list(reversed(rows)), options), key=lambda row: row["post_id"]) == samples


def test_candidate_purpose_never_assigns_negative(client):
    created, _ = project(client)
    pid = created["id"]
    for kind in ("unrelated", "hard_negative"):
        response = client.post(
            f"/api/projects/{pid}/discovery", json={"request_id": str(uuid4()), "query": "dragon", "source_kind": kind}
        )
        assert response.status_code == 200
        assert len(response.json()["items"]) == 50
        assert all(p["state"] is None for p in response.json()["items"])
    assert not run(client, client.app.state.db.rows("SELECT * FROM labels"))
    assert not run(client, client.app.state.db.rows("SELECT * FROM project_candidates"))
    review(client, pid, 999, "unsure")
    assert "hard_negative: dragon" in client.get(f"/api/projects/{pid}/candidates/999").json()["sources"]


async def test_v1_migration_preserves_data_and_makes_online_backup(tmp_path):
    source = tmp_path / "studio.sqlite3"
    with sqlite3.connect(source) as conn:
        conn.executescript(SCHEMA + "PRAGMA user_version=1;")
        conn.execute("INSERT INTO custom_tags(name) VALUES('test')")
        conn.execute("INSERT INTO projects VALUES(1,'custom',1,'test','dragon',0)")
    db = Database(tmp_path)
    try:
        await db.open()
        assert (await db.one("PRAGMA user_version"))["user_version"] == 4
        assert (await db.one("SELECT count(*) AS n FROM projects"))["n"] == 1
        assert (await db.one("PRAGMA integrity_check"))["integrity_check"] == "ok"
        assert await db.rows("PRAGMA foreign_key_check") == []
        backups = list((tmp_path / "backups").glob("pre-schema-v4-*.sqlite3"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
    finally:
        await db.close()


def test_legacy_cache_is_preserved_but_never_reused_for_browsing(client):
    created, _ = project(client)
    pid = created["id"]
    browse(client, pid)
    review(client, pid, 999, "positive")
    media, db = client.app.state.media, client.app.state.db
    media.cache_root.mkdir()
    legacy = media.cache_root / "999.webp"
    legacy.write_bytes(b"legacy cache")

    async def seed():
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO image_cache VALUES(999,'preview','cache/999.webp',12,'legacy',0,NULL,NULL)")

    run(client, seed())
    response = client.get("/api/media/999/preview")
    assert response.status_code == 200 and response.content != b"legacy cache"
    assert response.headers["cache-control"] == "private, no-store"
    run(client, media.download_preview(999))
    assert legacy.read_bytes() == b"legacy cache"
    assert run(client, db.one("SELECT * FROM image_fingerprints WHERE post_id=999"))


def test_fingerprint_never_rebinds_transient_bytes_to_concurrent_metadata_refresh(client, monkeypatch):
    created, _ = project(client)
    pid = created["id"]
    browse(client, pid)
    review(client, pid, 999, "positive")
    media, db = client.app.state.media, client.app.state.db
    original = media.thumbnail_bytes

    async def changed(post):
        raw = await original(post)
        async with db.transaction() as conn:
            await conn.execute("UPDATE posts SET md5='concurrent-replacement' WHERE post_id=999")
        return raw

    monkeypatch.setattr(media, "thumbnail_bytes", changed)
    run(client, media.download_preview(999))
    assert run(client, db.one("SELECT * FROM image_fingerprints WHERE post_id=999")) is None
