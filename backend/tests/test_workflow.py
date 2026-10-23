import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.config import Settings
from backend.app.main import create_app


def run(client, coroutine):
    return client.portal.call(lambda: coroutine)


def project(client, custom=False):
    body = {
        "request_id": str(uuid4()),
        "target_kind": "custom" if custom else "e621",
        "name": "my_dragon" if custom else "drake",
    }
    if custom:
        body.update(query="dragon", seed_ids=[42])
    response = client.post("/api/projects", json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


def import_job(client, job_id):
    app = client.app
    job = run(client, app.state.db.one("SELECT * FROM jobs WHERE id=?", (job_id,)))
    run(client, app.state.workers.discover(job, json.loads(job["payload_json"])))


def test_alias_import_pagination_and_idempotent_project(client):
    created, body = project(client)
    assert created["display_name"] == "dragon"
    assert client.post("/api/projects", json=body).json() == created
    import_job(client, created["jobs"][0]["job_id"])
    pid = created["id"]
    page = client.get(f"/api/projects/{pid}/candidates").json()
    assert len(page["items"]) == page["total"] == 50
    assert all(item["state"] is None for item in page["items"])
    assert page["items"][0]["sources"] == ["target: dragon"]
    request = {"request_id": str(uuid4())}
    discovery = client.post(f"/api/projects/{pid}/discovery", json=request).json()
    assert client.post(f"/api/projects/{pid}/discovery", json=request).json() == discovery
    import_job(client, discovery["job_id"])
    # Re-executing the same completed page must not import another page.
    assert client.get(f"/api/projects/{pid}").json()["counts"]["total"] == 100
    page = client.get(f"/api/projects/{pid}/candidates?limit=30").json()
    assert len(page["items"]) == 30 and page["next_cursor"]
    second = client.get(f"/api/projects/{pid}/candidates?limit=30&cursor={page['next_cursor']}").json()
    assert not {p["post_id"] for p in page["items"]} & {p["post_id"] for p in second["items"]}


def test_labels_conflicts_atomic_batch_and_durable_undo(client):
    created, _ = project(client)
    import_job(client, created["jobs"][0]["job_id"])
    pid = created["id"]
    body = {"request_id": str(uuid4()), "expected_version": 0, "state": "positive"}
    path = f"/api/projects/{pid}/labels/1000"
    first = client.patch(path, json=body)
    assert first.status_code == 200
    assert client.patch(path, json=body).json() == first.json()
    assert client.patch(path, json={**body, "state": "negative"}).status_code == 409
    assert client.patch(path, json={**body, "request_id": str(uuid4())}).status_code == 409
    response = client.post(
        f"/api/projects/{pid}/labels/batch",
        json={
            "request_id": str(uuid4()),
            "items": [
                {"post_id": 999, "state": "negative", "expected_version": 0},
                {"post_id": 1000, "state": "unsure", "expected_version": 0},
            ],
        },
    )
    assert response.status_code == 409
    assert client.get(f"/api/projects/{pid}/candidates/999").json()["state"] is None
    undo = {"request_id": str(uuid4()), "expected_version": 1, "event_id": first.json()["event_id"]}
    undone = client.post(path + "/undo", json=undo)
    assert undone.status_code == 200 and undone.json()["state"] is None and undone.json()["version"] == 2
    assert client.post(path + "/undo", json=undo).json() == undone.json()
    assert client.patch(path, json={**body, "request_id": str(uuid4()), "expected_version": 0}).status_code == 409
    assert len(run(client, client.app.state.db.rows("SELECT * FROM label_events"))) == 2


def test_custom_seeds_sources_and_project_specific_labels(client):
    created, _ = project(client, True)
    for job in created["jobs"]:
        import_job(client, job["job_id"])
    pid = created["id"]
    seed = client.get(f"/api/projects/{pid}/candidates/42").json()
    assert seed["sources"] == ["seed: seeds:42"]
    assert (
        client.patch(
            f"/api/projects/{pid}/labels/42",
            json={"request_id": str(uuid4()), "state": "negative", "expected_version": 0},
        ).status_code
        == 200
    )
    assert client.get(f"/api/projects/{pid}?state=negative").json()["counts"]["negative"] == 1
    for route in (f"/api/projects/{pid}/candidates?rating=x", f"/api/projects/{pid}/candidates?after=garbage"):
        assert client.get(route).status_code == 422
    assert (
        client.post(
            f"/api/projects/{pid}/discovery",
            json={"request_id": str(uuid4()), "query": "dragon order:score", "source_kind": "query"},
        ).status_code
        == 422
    )


def test_auth_csrf_host_and_retired_service_worker(client):
    assert client.get("/api/projects", headers={"Host": "evil.example"}).status_code == 400
    assert client.post("/api/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    response = client.get("/service-worker.js")
    assert "unregister" in response.text and response.headers["cache-control"] == "no-store"
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/projects").status_code == 401


def test_restart_and_worker_recovery(settings):
    app = create_app(settings, start_workers=True)
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        from backend.tests.conftest import PASSWORD

        login = client.post("/api/auth/login", json={"password": PASSWORD})
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        created, _ = project(client)
        for _ in range(100):
            if client.get(f"/api/projects/{created['id']}").json()["counts"]["total"] == 50:
                break
            time.sleep(0.1)
        assert (
            client.patch(
                f"/api/projects/{created['id']}/labels/1000",
                json={"request_id": str(uuid4()), "state": "unsure", "expected_version": 0},
            ).status_code
            == 200
        )
    with TestClient(create_app(settings), headers={"Origin": "http://testserver"}) as client:
        login = client.post("/api/auth/login", json={"password": PASSWORD})
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        row = client.get(f"/api/projects/{created['id']}/candidates/1000").json()
        assert row["state"] == "unsure" and row["version"] == 1


def test_online_backup_and_sqlite_busy_save(client, settings, tmp_path):
    import sqlite3

    from backend.backup import backup

    created, _ = project(client)
    import_job(client, created["jobs"][0]["job_id"])
    pid = created["id"]
    path = f"/api/projects/{pid}/labels/1000"
    body = {"request_id": str(uuid4()), "state": "positive", "expected_version": 0}
    with sqlite3.connect(settings.data_root / "studio.sqlite3", isolation_level=None) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        failed = client.patch(path, json=body)
        assert failed.status_code == 503
        blocker.execute("ROLLBACK")
    assert client.patch(path, json=body).status_code == 200
    destination = tmp_path / "backup" / "studio.sqlite3"
    backup(settings.data_root, destination)
    with sqlite3.connect(destination) as restored:
        assert restored.execute("SELECT state FROM labels").fetchone()[0] == "positive"
        assert restored.execute("SELECT count(*) FROM label_events").fetchone()[0] == 1


def test_auth_limits_and_project_isolation(client):
    from backend.tests.conftest import PASSWORD

    first, _ = project(client, True)
    second, _ = project(client, True)
    for created in (first, second):
        for job in created["jobs"]:
            import_job(client, job["job_id"])
    response = client.patch(
        f"/api/projects/{first['id']}/labels/42",
        json={"request_id": str(uuid4()), "state": "positive", "expected_version": 0},
    )
    assert response.status_code == 200
    assert client.get(f"/api/projects/{second['id']}/candidates/42").json()["state"] is None
    assert client.get("/api/projects/999999").status_code == 404
    for _ in range(10):
        assert client.post("/api/auth/login", json={"password": "incorrect"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429


def test_passwordless_access_requires_explicit_opt_in(client, tmp_path):
    assert client.get("/api/auth/options").json() == {"password_required": True}
    assert client.post("/api/auth/connect").status_code == 403
    with pytest.raises(ValidationError, match="STUDIO_PASSWORD_HASH"):
        Settings(_env_file=None, data_root=tmp_path, mock_e621=True, secure_cookie=False)


def test_private_passwordless_sessions_keep_origin_csrf_and_cookie_protection(tmp_path):
    settings = Settings(
        _env_file=None,
        data_root=tmp_path,
        passwordless=True,
        password_hash="",
        mock_e621=True,
        public_origin="https://testserver",
        secure_cookie=True,
    )
    with TestClient(create_app(settings, start_workers=False), base_url=settings.public_origin) as client:
        assert client.get("/api/auth/options").json() == {"password_required": False}
        assert client.get("/api/projects").status_code == 401
        assert client.post("/api/auth/connect").status_code == 403
        assert client.post("/api/auth/connect", headers={"Origin": "https://other.example"}).status_code == 403
        client.headers["Origin"] = settings.public_origin
        response = client.post("/api/auth/connect")
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
        assert client.get("/api/auth/session").json()["csrf_token"] == response.json()["csrf_token"]
        body = {"request_id": str(uuid4()), "target_kind": "custom", "name": "private_test", "query": "dragon"}
        assert client.post("/api/projects", json=body).status_code == 403
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        assert client.post("/api/projects", json=body).status_code == 200
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/projects").status_code == 401
        replacement = client.post("/api/auth/connect")
        assert replacement.status_code == 200
        assert replacement.json()["csrf_token"] != response.json()["csrf_token"]


def test_private_http_session_does_not_collide_with_previous_https_cookie(tmp_path):
    settings = Settings(
        _env_file=None, data_root=tmp_path, passwordless=True, mock_e621=True,
        public_origin="http://testserver", secure_cookie=False,
    )
    with TestClient(create_app(settings, start_workers=False), base_url=settings.public_origin) as client:
        client.cookies.set("studio_session", "previous-https-cookie")
        client.headers["Origin"] = settings.public_origin
        response = client.post("/api/auth/connect")
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("studio_session_http=") and "HttpOnly" in cookie and "Secure" not in cookie
        assert client.get("/api/auth/session").status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/auth/session").status_code == 401


def test_overlapping_search_reports_existing_and_filters_by_run(client):
    created, _ = project(client)
    import_job(client, created["jobs"][0]["job_id"])
    pid = created["id"]
    path = f"/api/projects/{pid}"
    reviewed = client.patch(path + "/labels/1000", json={
        "request_id": str(uuid4()), "state": "positive", "expected_version": 0,
    }).json()
    # Deliberate overlapping upstream page: new provenance, zero new candidates.
    body = {"request_id": str(uuid4()), "query": "dragon solo", "source_kind": "query"}
    result = client.post(path + "/discovery", json=body).json()
    assert client.post(path + "/discovery", json=body).json() == result
    import_job(client, result["job_id"])
    job = client.get(f"/api/jobs/{result['job_id']}").json()
    counts = json.loads(job["result_json"])
    assert counts["added"] == 0 and counts["existing"] == 50
    filtered = client.get(path + f"/candidates?run_id={result['run_id']}").json()
    assert filtered["total"] == 50
    assert all("query: dragon solo" in p["sources"] for p in filtered["items"])
    assert client.get(path).json()["counts"]["total"] == 50
    assert client.get(path + "/candidates/1000").json()["version"] == reviewed["version"]
    other, _ = project(client)
    assert client.get(f"/api/projects/{other['id']}/candidates?run_id={result['run_id']}").status_code == 404
    assert client.post(path + "/discovery", json={
        "request_id": str(uuid4()), "query": "", "source_kind": "query",
    }).status_code == 422
