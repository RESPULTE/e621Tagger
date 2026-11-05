import hashlib
import io
import json
from uuid import uuid4

import pytest
from PIL import Image

from backend.app.media_samples import decode_animation
from backend.tests.test_workflow import project, run


def png(color="red"):
    output = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(output, "PNG")
    return output.getvalue()


def setup(client):
    pid = project(client)[0]["id"]
    post = client.get(f"/api/projects/{pid}/candidates/1000").json()
    return pid, post, f"/api/projects/{pid}/posts/1000/samples"


def capture(client, path, post, at=100, color="red", kind="frame"):
    result = client.post(
        path,
        params={"source_md5": post["md5"], "timestamp_ms": at, "kind": kind, "request_id": str(uuid4())},
        content=png(color),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert result.status_code == 200, result.text
    return result.json()


def edit(client, path, sample, **changes):
    result = client.patch(
        f"{path}/{sample['id']}", json={"request_id": str(uuid4()), "expected_version": sample["version"], **changes}
    )
    assert result.status_code == 200, result.text
    return result.json()


def test_samples_independent_durable_reviews_and_favorites(client):
    pid, post, path = setup(client)
    first = capture(client, path, post)
    second = capture(client, path, post, 500, "blue")
    assert first["id"] != second["id"]
    first = edit(client, path, first, state="positive")
    first = edit(client, path, first, favorite=True)
    saved_files = list(client.app.state.library.root.glob("e621_1000_*.png"))
    assert len(saved_files) == 1
    assert Image.open(saved_files[0]).getpixel((0, 0)) == (255, 0, 0)
    second = edit(client, path, second, state="negative")
    samples = client.get(path).json()
    assert len(samples) == 2
    assert {s["state"] for s in samples} == {"positive", "negative"}
    assert sum(s["favorite"] for s in samples) == 1
    assert client.get("/api/library/posts/1000").json()["wanted"] is False
    detail = client.get(f"/api/projects/{pid}/candidates/1000").json()
    assert detail["state"] is None
    assert detail["sample_summary"] == {"positive": 1, "negative": 1, "total": 2}
    assert client.get(f"/api/projects/{pid}/candidates").json()["total"] == 1
    image = client.get(first["image_url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(image.content)).getpixel((0, 0)) == (255, 0, 0)
    assert capture(client, path, post)["id"] == first["id"]
    stale = client.patch(
        f"{path}/{first['id']}", json={"request_id": str(uuid4()), "expected_version": 0, "favorite": False}
    )
    assert stale.status_code == 409
    first = edit(client, path, first, favorite=False)
    assert not first["favorite"] and not saved_files[0].exists()
    assert client.get(first["image_url"]).status_code == 200  # unfavourite preserves the tagging sample
    # Another project cannot mutate or fetch this sample by its identifier.
    other = project(client, custom=True)[0]["id"]
    assert client.get(first["image_url"].replace(f"/projects/{pid}/", f"/projects/{other}/")).status_code == 404


def test_thumbnail_review_and_frame_export_complete(client):
    pid, post, path = setup(client)
    label = client.patch(
        f"/api/projects/{pid}/labels/1000",
        json={"request_id": str(uuid4()), "expected_version": 0, "state": "positive"},
    )
    assert label.status_code == 200, label.text
    thumbnail = client.get(path).json()[0]
    assert thumbnail["kind"] == "thumbnail" and thumbnail["state"] == "positive"
    frame = edit(client, path, capture(client, path, post), state="negative")
    frozen = client.post(f"/api/projects/{pid}/datasets", json={"request_id": str(uuid4()), "name": "Frames"})
    assert frozen.status_code == 200, frozen.text
    dataset = frozen.json()["id"]
    manifest = client.get(f"/api/datasets/{dataset}/export?format=json").json()
    included = [s for s in manifest["samples"] if s["target"] is not None]
    assert len(included) == 2 and {s["sample_kind"] for s in included} == {"thumbnail", "frame"}
    assert len({s["split"] for s in included}) == 1
    assert all(s["media_ext"] == "png" and s["source_md5"] == post["md5"] for s in included)
    queued = client.post(f"/api/datasets/{dataset}/media", json={"request_id": str(uuid4())})
    assert queued.status_code == 200, queued.text
    for job_id in queued.json()["job_ids"]:
        job = run(client, client.app.state.db.one("SELECT * FROM jobs WHERE id=?", (job_id,)))
        run(client, client.app.state.downloads.process(job, json.loads(job["payload_json"])))
    media = client.get(f"/api/datasets/{dataset}/media/manifest")
    assert media.status_code == 200, media.text
    assert len({s["relative_path"] for s in media.json()["samples"]}) == 2
    for sample in media.json()["samples"]:
        output = client.get(f"/api/datasets/{dataset}/media/samples/{sample['sample_id']}")
        assert output.status_code == 200
        assert hashlib.md5(output.content).hexdigest() == sample["md5"]
    from hydra_character_training.dataset import read_dataset

    folder = client.app.state.db.root / "exports" / dataset
    (folder / "manifest.json").write_bytes(media.content)
    # The importer accepts real sample identities, image bytes and review events;
    # a tiny one-artwork fixture correctly fails the independent-splits requirement.
    with pytest.raises(ValueError, match="needs both positive and negative"):
        read_dataset(folder, "dragon")
    thumbnail = edit(client, path, thumbnail, state="negative")
    assert client.get(f"/api/projects/{pid}/candidates/1000").json()["state"] == "negative"
    assert next(s for s in client.get(path).json() if s["id"] == frame["id"])["state"] == "negative"
    assert client.get(f"/api/datasets/{dataset}/export?format=json").json() == manifest


def test_sample_replay_validation_and_source_change(client):
    _, post, path = setup(client)
    params = {"source_md5": post["md5"], "timestamp_ms": 50, "request_id": str(uuid4())}
    first = client.post(path, params=params, content=png())
    assert first.status_code == 200
    assert client.post(path, params=params, content=png()).json() == first.json()
    assert client.post(path, params=params, content=png("blue")).status_code == 409
    assert (
        client.post(
            path, params={**params, "request_id": str(uuid4()), "source_md5": "a" * 32}, content=png()
        ).status_code
        == 409
    )
    assert client.post(path, params={**params, "timestamp_ms": -1}, content=png()).status_code == 422
    assert client.post(path, params={**params, "timestamp_ms": 2}, content=b"not an image").status_code == 415
    sample = first.json()
    before = list((client.app.state.db.root / "samples").glob("*.png"))
    duplicate = client.post(path, params={**params, "request_id": str(uuid4())}, content=png("blue"))
    assert duplicate.status_code == 200 and duplicate.json()["id"] == sample["id"]
    assert list((client.app.state.db.root / "samples").glob("*.png")) == before
    body = {"request_id": str(uuid4()), "expected_version": 0, "favorite": True}
    changed = client.patch(f"{path}/{sample['id']}", json=body)
    assert changed.status_code == 200
    assert client.patch(f"{path}/{sample['id']}", json=body).json() == changed.json()


def test_gif_frame_timing_pixels_and_bounds():
    output = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(
        output, "GIF", save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")], duration=[100, 250], loop=0
    )
    raw = output.getvalue()
    metadata = decode_animation(raw)
    assert metadata["animated"] and metadata["duration_ms"] == 350
    assert [f["timestamp_ms"] for f in metadata["frames"]] == [0, 100]
    assert Image.open(io.BytesIO(decode_animation(raw, 1))).getpixel((0, 0)) == (0, 0, 255)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        decode_animation(raw, 2)
    assert exc.value.status_code == 404


def test_gif_http_frames_cache_and_capture(client):
    pid = project(client)[0]["id"]
    post = client.get(f"/api/projects/{pid}/candidates/1005").json()
    params = {"source_md5": post["md5"]}
    metadata = client.get("/api/posts/1005/animation", params=params)
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["duration_ms"] == 1000 and len(metadata.json()["frames"]) == 4
    frame = client.get("/api/posts/1005/animation/frames/2", params=params)
    assert frame.status_code == 200 and frame.headers["content-type"] == "image/png"
    response = client.post(
        f"/api/projects/{pid}/posts/1005/samples",
        params={**params, "timestamp_ms": 500, "request_id": str(uuid4())},
        content=frame.content,
    )
    assert response.status_code == 200, response.text
    assert response.json()["timestamp_ms"] == 500
    assert len(client.app.state.media.animation_cache) == 1
    assert client.get("/api/posts/1005/animation/frames/4", params=params).status_code == 404


def test_legacy_review_materializes_thumbnail_and_favorite_merges_new_decision(client, monkeypatch):
    from backend.app import sample_library
    from backend.app.jobs import persist_post
    from backend.app.reviews import transition

    pid, post, path = setup(client)

    async def legacy():
        raw = await client.app.state.feed.raw(1000)
        async with client.app.state.db.transaction() as conn:
            await persist_post(conn, raw)
            await conn.execute(
                "INSERT OR IGNORE INTO project_candidates(project_id,post_id,first_seen_at) VALUES(?,?,0)", (pid, 1000)
            )
            await transition(conn, pid, 1000, 0, "positive")

    run(client, legacy())
    assert client.get(path).json() == []
    result = client.patch(
        f"/api/projects/{pid}/labels/1000",
        json={"request_id": str(uuid4()), "expected_version": 1, "state": "negative"},
    )
    assert result.status_code == 200, result.text
    thumbnail = client.get(path).json()[0]
    original = sample_library.change_favorite

    async def interleave(state, sample, wanted):
        await original(state, sample, wanted)
        async with state.db.transaction() as conn:
            parent = await (
                await conn.execute(
                    "SELECT review_version FROM canonical_candidates WHERE project_id=? AND post_id=1000", (pid,)
                )
            ).fetchone()
            await transition(conn, pid, 1000, parent["review_version"], "positive")

    monkeypatch.setattr(sample_library, "change_favorite", interleave)
    saved = edit(client, path, thumbnail, favorite=True)
    assert saved["state"] == "positive" and saved["favorite"]
    assert len(list(client.app.state.library.root.glob("e621_1000_*.png"))) == 1
