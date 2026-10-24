import asyncio
import hashlib
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from backend.tests.test_workflow import run


def create(client, name="dragon"):
    response = client.post("/api/projects", json={"request_id": str(uuid4()), "target_kind": "e621", "name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def label(client, pid, post, state, version=0):
    response = client.patch(
        f"/api/projects/{pid}/labels/{post}",
        json={"request_id": str(uuid4()), "expected_version": version, "state": state},
    )
    assert response.status_code == 200, response.text
    return response.json()


def save(client, pid, post=999, version=0):
    response = client.put(
        f"/api/projects/{pid}/library/{post}",
        json={"request_id": str(uuid4()), "expected_version": version, "wanted": True},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_browsing_is_transient_reviews_survive_and_clear_is_independent(client):
    pid = create(client)
    db = client.app.state.db
    before = {
        t: run(client, db.one(f"SELECT count(*) AS n FROM {t}"))["n"]
        for t in ("posts", "project_candidates", "discoveries", "labels", "image_cache")
    }
    cursor = None
    for _ in range(3):
        page = client.get(f"/api/projects/{pid}/query", params={"cursor": cursor} if cursor else {}).json()
        assert len(page["items"]) == 50
        cursor = page["next_cursor"]
    assert before == {t: run(client, db.one(f"SELECT count(*) AS n FROM {t}"))["n"] for t in before}
    assert client.get("/api/media/999/preview").status_code == 200
    assert client.get("/api/media/999/original", headers={"Range": "bytes=0-15"}).status_code == 206
    assert client.get("/api/media/1000/original", headers={"Range": "bytes=0-15"}).status_code == 206
    assert not list(db.root.glob("cache/*"))
    for post, state in ((999, "positive"), (998, "negative"), (997, "unsure")):
        label(client, pid, post, state)
    tagged = client.get(f"/api/projects/{pid}/candidates").json()
    assert {p["state"] for p in tagged["items"]} == {"positive", "negative", "unsure"}
    assert tagged["total"] == 3
    assert run(client, db.one("SELECT count(*) AS n FROM posts"))["n"] == 3
    assert not list(db.root.glob("cache/*"))
    # A fresh query starts at upstream's current first page and overlays labels.
    page = client.get(f"/api/projects/{pid}/query").json()
    assert page["items"][0]["post_id"] == 1000
    assert next(p for p in page["items"] if p["post_id"] == 999)["state"] == "positive"
    save(client, pid)
    job = run(client, db.one("SELECT * FROM jobs WHERE type='library'"))
    run(client, client.app.state.library.process(job, json.loads(job["payload_json"])))
    files = {p.name: p.read_bytes() for p in client.app.state.library.root.glob("e621_*")}
    assert len(files) == 4
    label(client, pid, 999, None, 1)
    assert client.get(f"/api/projects/{pid}/candidates").json()["total"] == 2
    assert {p.name: p.read_bytes() for p in client.app.state.library.root.glob("e621_*")} == files
    assert client.get("/api/library/posts/999").json()["status"] == "saved"


def test_concurrent_alias_reuses_one_project(client):
    pid = create(client)
    with ThreadPoolExecutor(4) as pool:
        ids = list(pool.map(lambda n: create(client, n), ["dragon", "drake", "dragon", "drake"]))
    assert set(ids) == {pid}
    assert len(run(client, client.app.state.db.rows("SELECT * FROM projects"))) == 1


def test_explicit_export_and_collection_use_same_transfers_and_checked_bytes(client):
    pid = create(client)
    client.get(f"/api/projects/{pid}/query")
    label(client, pid, 999, "positive")
    label(client, pid, 998, "negative")
    label(client, pid, 997, "unsure")
    for post in (999, 998):
        run(client, client.app.state.media.download_preview(post))
    response = client.post(
        f"/api/projects/{pid}/datasets",
        json={"request_id": str(uuid4()), "name": "synthetic", "near_hamming": 0, "group_artists": False},
    )
    assert response.status_code == 200, response.text
    frozen = response.json()
    did = frozen["id"]
    original = client.get(f"/api/datasets/{did}/export?format=json").content
    assert len(json.loads(original)["samples"]) == 3
    assert not list((client.app.state.db.root / "exports").glob("**/*"))
    body = {"request_id": str(uuid4())}
    first = client.post(f"/api/datasets/{did}/media", json=body)
    assert first.status_code == 200, first.text
    assert client.post(f"/api/datasets/{did}/media", json={"request_id": str(uuid4())}).json() == first.json()
    save(client, pid)
    assert save(client, pid)["version"] == 1
    jobs = run(client, client.app.state.db.rows("SELECT * FROM jobs WHERE type IN ('library','dataset_media')"))

    async def execute():
        await asyncio.gather(
            *[
                (client.app.state.library if j["type"] == "library" else client.app.state.downloads).process(
                    j, json.loads(j["payload_json"])
                )
                for j in jobs
            ]
        )

    run(client, execute())
    manifest = client.get(f"/api/datasets/{did}/media/manifest")
    assert manifest.status_code == 200, manifest.text
    samples = manifest.json()["samples"]
    assert {s["target"] for s in samples} == {0, 1}
    for sample in samples:
        downloaded = client.get(f"/api/datasets/{did}/media/files/{sample['post_id']}")
        assert downloaded.status_code == 200
        assert hashlib.sha256(downloaded.content).hexdigest() == sample["sha256"]
        assert hashlib.md5(downloaded.content).hexdigest() == sample["md5"]
        assert sample["relative_path"] and len(downloaded.content) == sample["bytes"]
    first_bundle = client.get(f"/api/datasets/{did}/media/bundle")
    second_bundle = client.get(f"/api/datasets/{did}/media/bundle")
    assert first_bundle.status_code == 200 and first_bundle.content == second_bundle.content
    with zipfile.ZipFile(io.BytesIO(first_bundle.content)) as bundle:
        assert bundle.read("manifest.json") == manifest.content
        for sample in samples:
            assert hashlib.sha256(bundle.read(sample["relative_path"])).hexdigest() == sample["sha256"]
    assert client.get(f"/api/datasets/{did}/export?format=json").content == original
    assert client.app.state.transfers.metrics["completed"] == 3
    assert client.app.state.transfers.metrics["peak_active"] <= 2
    assert client.get(f"/api/projects/{pid}/candidates").json()["total"] == 3
