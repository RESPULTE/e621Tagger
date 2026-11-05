import hashlib
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.datasets import snapshot
from backend.app.db import SCHEMA, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5, Database
from backend.app.feed import post_row
from backend.app.main import create_app
from backend.app.mock import post
from backend.app.reviews import fingerprint
from backend.tests.conftest import PASSWORD
from backend.tests.test_workflow import browse, project, run


def test_saved_historical_provenance_still_exports_and_undo_restores_it(client):
    pid = project(client)[0]["id"]
    browse(client, pid)
    path = f"/api/projects/{pid}/labels/999"
    saved = client.patch(path, json={
        "request_id": str(uuid4()), "state": "positive", "expected_version": 0,
    }).json()
    db = client.app.state.db

    async def historical_fixture():
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO similarity_provenance VALUES(?,?,?,?,?)",
                               (saved["event_id"], 1001, 2, 0.25, "legacy-saved-index"))
    run(client, historical_fixture())
    run(client, client.app.state.media.download_preview(999))
    frozen = client.post(f"/api/projects/{pid}/datasets", json={
        "request_id": str(uuid4()), "name": "Historical decisions remain auditable",
    })
    assert frozen.status_code == 200, frozen.text
    export = f"/api/datasets/{frozen.json()['id']}/export?format=json"
    original = client.get(export).content
    assert any(p["kind"] == "hydra35_knn" for p in json.loads(original)["samples"][0]["provenance"])
    changed = client.patch(path, json={
        "request_id": str(uuid4()), "state": "negative", "expected_version": 1,
    }).json()
    undone = client.post(path + "/undo", json={
        "request_id": str(uuid4()), "event_id": changed["event_id"], "expected_version": 2,
    })
    assert undone.status_code == 200 and undone.json()["state"] == "positive"
    _, rows = run(client, snapshot(db.connection, pid))
    assert any(p["kind"] == "hydra35_knn" for p in json.loads(rows[0]["provenance_json"]))
    assert client.get(export).content == original
    assert len(run(client, db.rows("SELECT * FROM similarity_provenance"))) == 2
    assert client.patch(path, json={
        "request_id": str(uuid4()), "state": "positive", "expected_version": 3,
        "similarity": {"seed_post_id": 1001, "index_revision": "legacy-saved-index"},
    }).status_code == 422


def test_only_binary_reviews_and_browsing_skip_have_no_writes(client):
    pid = project(client)[0]["id"]
    db = client.app.state.db
    tables = ("posts", "project_candidates", "labels", "label_events", "discoveries", "jobs", "saved_media")
    before = {t: run(client, db.rows(f"SELECT * FROM {t}")) for t in tables}
    browse(client, pid)
    for post_id in (999, 998):
        assert client.get(f"/api/projects/{pid}/candidates/{post_id}").status_code == 200
    assert client.patch(f"/api/projects/{pid}/labels/999", json={
        "request_id": str(uuid4()), "state": "unsure", "expected_version": 0,
    }).status_code == 422
    assert client.post(f"/api/projects/{pid}/labels/batch", json={
        "request_id": str(uuid4()), "items": [{"post_id": 999, "state": "unsure", "expected_version": 0}],
    }).status_code == 422
    assert client.get(f"/api/projects/{pid}/candidates?state=unsure").status_code == 422
    assert before == {t: run(client, db.rows(f"SELECT * FROM {t}")) for t in tables}


def test_undo_replay_normalizes_legacy_unsure_without_mutating_receipt(client):
    pid = project(client)[0]["id"]
    browse(client, pid)
    post_id = 1000
    first = client.patch(f"/api/projects/{pid}/labels/{post_id}", json={
        "request_id": str(uuid4()), "state": "positive", "expected_version": 0,
    })
    assert first.status_code == 200, first.text

    body = {"request_id": str(uuid4()), "event_id": first.json()["event_id"], "expected_version": 1}
    legacy_response = {"post_id": post_id, "state": "unsure", "version": 2, "event_id": first.json()["event_id"] + 1}
    legacy_json = json.dumps(legacy_response)
    signature = fingerprint(["undo", pid, post_id], body)
    db = client.app.state.db

    async def seed_receipt():
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO mutation_requests(request_id,fingerprint,response_json,created_at) VALUES(?,?,?,?)",
                (body["request_id"], signature, legacy_json, 0),
            )

    run(client, seed_receipt())
    before_receipt = run(client, db.one("SELECT * FROM mutation_requests WHERE request_id=?", (body["request_id"],)))
    before_labels = run(client, db.rows("SELECT * FROM labels WHERE project_id=? AND post_id=?", (pid, post_id)))
    before_events = run(client, db.rows("SELECT * FROM label_events WHERE project_id=? AND post_id=?", (pid, post_id)))

    replay = client.post(f"/api/projects/{pid}/labels/{post_id}/undo", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**legacy_response, "state": None}
    assert run(client, db.one("SELECT * FROM mutation_requests WHERE request_id=?", (body["request_id"],))) == before_receipt
    assert run(client, db.rows("SELECT * FROM labels WHERE project_id=? AND post_id=?", (pid, post_id))) == before_labels
    assert run(client, db.rows("SELECT * FROM label_events WHERE project_id=? AND post_id=?", (pid, post_id))) == before_events


def test_legacy_unsure_is_unreviewed_history_frozen_bytes_and_backup_preserved(settings):
    settings.data_root.mkdir(exist_ok=True)
    frozen = json.dumps({"schema_version": 1, "samples": [{"label": "unsure", "target": None}]})
    digest = hashlib.sha256(frozen.encode()).hexdigest()
    with sqlite3.connect(settings.data_root / "studio.sqlite3") as conn:
        conn.executescript(SCHEMA + SCHEMA_V2 + SCHEMA_V3 + SCHEMA_V4 + SCHEMA_V5 + "PRAGMA user_version=5;")
        conn.execute("INSERT INTO projects VALUES(1,'e621',1,'dragon','dragon',0)")
        conn.execute("INSERT INTO canonical_projects VALUES('e621',1,1)")
        row = post_row(post(999))
        conn.execute(f"INSERT INTO posts({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
        conn.execute("INSERT INTO project_candidates VALUES(1,999,1,0)")
        conn.execute("INSERT INTO labels VALUES(1,999,'unsure',0)")
        conn.execute("INSERT INTO label_events VALUES(1,1,999,NULL,'unsure',1,0,'review',NULL)")
        conn.execute("INSERT INTO datasets VALUES('00000000-0000-4000-8000-000000000001',1,'legacy',0,?,?)", (digest, frozen))
    with TestClient(create_app(settings, start_workers=False), headers={"Origin": "http://testserver"}) as client:
        login = client.post("/api/auth/login", json={"password": PASSWORD}).json()
        client.headers["X-CSRF-Token"] = login["csrf_token"]
        db = client.app.state.db
        assert client.get("/api/projects/1").json()["counts"] == {"positive": 0, "negative": 0, "total": 0}
        assert client.get("/api/projects/1/candidates").json()["total"] == 0
        candidate = client.get("/api/projects/1/candidates/999").json()
        assert candidate["state"] is None and candidate["version"] == 1
        assert client.get("/api/projects/1/quality").json()["total"] == 0
        assert run(client, db.one("SELECT state FROM labels"))["state"] == "unsure"
        changed = client.patch("/api/projects/1/labels/999", json={
            "request_id": str(uuid4()), "state": "positive", "expected_version": 1,
        }).json()
        undone = client.post("/api/projects/1/labels/999/undo", json={
            "request_id": str(uuid4()), "event_id": changed["event_id"], "expected_version": 2,
        })
        assert undone.status_code == 200 and undone.json()["state"] is None and undone.json()["version"] == 3
        assert run(client, db.one("SELECT new_state FROM label_events WHERE id=1"))["new_state"] == "unsure"
        assert client.get("/api/datasets/00000000-0000-4000-8000-000000000001/export?format=json").content == frozen.encode()
        assert run(client, db.one("SELECT manifest_sha256 FROM datasets"))["manifest_sha256"] == digest
        assert run(client, db.one("PRAGMA integrity_check"))["integrity_check"] == "ok"
        assert not run(client, db.rows("PRAGMA foreign_key_check"))

        async def forbidden():
            async with db.transaction() as transaction:
                await transaction.execute("INSERT INTO labels VALUES(1,999,'unsure',0)")
        with pytest.raises(sqlite3.IntegrityError, match="binary"):
            run(client, forbidden())
    backups = list((settings.data_root / "backups").glob("pre-schema-v6-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT state FROM labels").fetchone()[0] == "unsure"
        assert conn.execute("SELECT manifest_json FROM datasets").fetchone()[0] == frozen


@pytest.mark.parametrize("version", [3, 4])
async def test_intermediate_schema_upgrade_preserves_projects(tmp_path, version):
    with sqlite3.connect(tmp_path / "studio.sqlite3") as conn:
        conn.executescript(SCHEMA + SCHEMA_V2 + SCHEMA_V3)
        conn.execute("INSERT INTO projects VALUES(1,'custom',1,'legacy','dragon',0)")
        conn.commit()
        if version == 4:
            conn.executescript(SCHEMA_V4)
        conn.execute(f"PRAGMA user_version={version}")
    db = Database(tmp_path)
    try:
        await db.open()
        assert (await db.one("PRAGMA user_version"))["user_version"] == 6
        assert len(await db.rows("SELECT * FROM projects")) == 1
        assert await db.rows("PRAGMA foreign_key_check") == []
    finally:
        await db.close()


async def test_failed_v6_migration_rolls_back_schema_and_preserves_backup(tmp_path, monkeypatch):
    from backend.app import db as module

    with sqlite3.connect(tmp_path / "studio.sqlite3") as conn:
        conn.executescript(SCHEMA + SCHEMA_V2 + SCHEMA_V3 + SCHEMA_V4 + SCHEMA_V5 + "PRAGMA user_version=5;")
    monkeypatch.setattr(module, "SCHEMA_V6", "CREATE TABLE should_rollback(x); INSERT INTO missing_table VALUES(1);")
    db = Database(tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="missing_table"):
            await db.open()
    finally:
        await db.close()
    with sqlite3.connect(tmp_path / "studio.sqlite3") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert len(list((tmp_path / "backups").glob("pre-schema-v6-*.sqlite3"))) == 1
