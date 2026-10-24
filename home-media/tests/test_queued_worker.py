"""Queued processing must respect cancellation and the locally approved snapshot."""

import json
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

import pytest

from home_media import worker
from home_media.config import Settings
from home_media.control import StopRequested
from home_media.queueing import QUEUE_FIELD, QueueRequest


def test_worker_skips_cancelled_snapshot_request_without_inference(monkeypatch, tmp_path):
    request = QueueRequest("image", "image-1", "approved-request")
    settings = Settings(
        stash_url="http://localhost",
        stash_key="test-key",
        state=tmp_path / "state.sqlite",
        adult_roots=("/media/adult",),
        focus_categories=("general",),
    )

    @contextmanager
    def lock(_path):
        yield

    class FakeModel:
        version = "model-v1"
        labels = [("tag", "general")]
        model = SimpleNamespace(
            labels=[SimpleNamespace(label="tag", category="general", subcategory="general")]
        )

    class FakeState:
        def __init__(self, _path):
            pass

        def status(self, *_args):
            pytest.fail("cancelled snapshot request should stop before status/inference")

        def close(self):
            pass

    class FakeStash:
        def __init__(self, *_args):
            pass

        def items(self, _kind):
            pytest.fail("approved snapshot must not scan the full library")

        def item(self, kind, identity):
            assert (kind, identity) == ("image", "image-1")
            return {
                "id": "image-1",
                "visual_files": [{"path": "/media/adult/image.jpg"}],
                "custom_fields": {
                    QUEUE_FIELD: {"request_id": "new-request", "status": "cancelled"}
                },
                "tags": [],
            }

    monkeypatch.setattr(worker, "worker_lock", lock)
    monkeypatch.setattr(worker, "Hydra", lambda *_args: FakeModel())
    monkeypatch.setattr(worker, "State", FakeState)
    monkeypatch.setattr(worker, "Stash", FakeStash)
    monkeypatch.setattr(
        worker,
        "prepare_remote",
        lambda *_args: pytest.fail("cancelled request must not run inference"),
    )
    monkeypatch.setattr(
        worker,
        "predict_prepared",
        lambda *_args: pytest.fail("cancelled request must not run inference"),
    )

    assert worker.run(settings, "unused-upstream", requests=[request]) == (0, 0)


def prediction_settings(tmp_path, *, video_frames=2):
    return Settings(
        stash_url="http://localhost",
        stash_key="test-key",
        state=tmp_path / "state.sqlite",
        adult_roots=("/media/adult",),
        video_frames=video_frames,
    )


@pytest.mark.parametrize("raw", ["not-json", ["not", "an", "object"], None])
def test_read_provenance_treats_malformed_metadata_as_empty(raw):
    item = {"custom_fields": {worker.PROVENANCE: raw}}

    assert worker.read_provenance(item) == {}


def install_fake_media_source(monkeypatch):
    class FakeSftp:
        def normalize(self, path):
            return path

        def stat(self, _path):
            return SimpleNamespace(st_size=100, st_mtime=200)

    @contextmanager
    def remote(_settings):
        yield FakeSftp()

    monkeypatch.setattr(worker, "remote", remote)
    monkeypatch.setattr(worker, "fetch_file", lambda *_args, **_kwargs: "media-hash")


def install_fake_cover(monkeypatch):
    class FakeImage:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def thumbnail(self, _size):
            pass

        def convert(self, _mode):
            return self

        def save(self, destination, *_args, **_kwargs):
            destination.write(b"mock-jpeg")

    monkeypatch.setattr(worker.Image, "open", lambda _path: FakeImage())


def test_stop_before_second_prediction_batch_does_not_publish_or_cache_partial_scores(
    monkeypatch, tmp_path
):
    settings = prediction_settings(tmp_path, video_frames=5)
    install_fake_media_source(monkeypatch)
    monkeypatch.setattr(
        worker,
        "frames",
        lambda *_args: [(float(index), f"frame-{index}") for index in range(5)],
    )

    class FakeState:
        def __init__(self):
            self.puts = []
            self.remembered = []

        def source(self, _signature):
            return None

        def get(self, _key):
            return None

        def put(self, *args):
            self.puts.append(args)

        def remember_source(self, *args):
            self.remembered.append(args)

    class FakeModel:
        version = "model-v1"

        def __init__(self):
            self.predictions = []

        def predict_batch(self, paths):
            self.predictions.extend(paths)
            return [{"tag": 0.8} for _ in paths]

    checks = 0

    def stop_on_second_frame():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise StopRequested("stop")

    state = FakeState()
    model = FakeModel()

    prepared = worker.prepare_remote(
        settings,
        model.version,
        state,
        {"path": "/media/adult/movie.mp4", "duration": 20},
        "scene",
        tmp_path,
    )
    with pytest.raises(StopRequested, match="stop"):
        worker.predict_prepared(
            settings, model, state, prepared, "scene", stop_check=stop_on_second_frame
        )

    assert model.predictions == ["frame-0", "frame-1", "frame-2", "frame-3"]
    assert state.puts == []
    assert state.remembered == []


def test_incomplete_cached_video_vector_is_recomputed(monkeypatch, tmp_path):
    settings = prediction_settings(tmp_path)
    install_fake_media_source(monkeypatch)
    install_fake_cover(monkeypatch)
    partial = {
        "times": [0.0],
        "scores": {"tag": 0.8},
        "score_format": "probability-v1",
    }

    class FakeState:
        def __init__(self):
            self.puts = []

        def source(self, _signature):
            return partial

        def get(self, _key):
            return partial

        def put(self, key, prediction):
            self.puts.append((key, prediction))

        def remember_source(self, *_args):
            pass

    class FakeModel:
        version = "model-v1"

        def predict_batch(self, paths):
            return [{"tag": 0.9} for _ in paths]

    monkeypatch.setattr(
        worker,
        "frames",
        lambda *_args: [(0.0, "frame-0"), (1.0, "frame-1")],
    )
    state = FakeState()

    prepared = worker.prepare_remote(
        settings,
        "model-v1",
        state,
        {"path": "/media/adult/movie.mp4", "duration": 20},
        "scene",
        tmp_path,
    )
    result = worker.predict_prepared(settings, FakeModel(), state, prepared, "scene")

    assert result is not partial
    assert result["times"] == [0.0, 1.0]
    assert result["complete"] is True
    assert len(state.puts) == 1


def test_use_cache_false_bypasses_source_and_prediction_cache(monkeypatch, tmp_path):
    settings = prediction_settings(tmp_path)
    install_fake_media_source(monkeypatch)
    install_fake_cover(monkeypatch)

    class FakeState:
        def __init__(self):
            self.puts = []
            self.remembered = []

        def source(self, _signature):
            pytest.fail("use_cache=False must skip source cache")

        def get(self, _key):
            pytest.fail("use_cache=False must skip prediction cache")

        def put(self, key, prediction):
            self.puts.append((key, prediction))

        def remember_source(self, signature, key):
            self.remembered.append((signature, key))

    class FakeModel:
        version = "model-v1"

        def predict_batch(self, paths):
            return [{"tag": 0.9} for _ in paths]

    monkeypatch.setattr(
        worker,
        "frames",
        lambda *_args: [(0.0, "frame-0"), (1.0, "frame-1")],
    )
    state = FakeState()

    prepared = worker.prepare_remote(
        settings,
        "model-v1",
        state,
        {"path": "/media/adult/movie.mp4", "duration": 20},
        "scene",
        tmp_path,
        use_cache=False,
    )
    result = worker.predict_prepared(settings, FakeModel(), state, prepared, "scene")

    assert result["complete"] is True
    assert result["times"] == [0.0, 1.0]
    assert len(state.puts) == 1
    assert len(state.remembered) == 1


def install_run_harness(monkeypatch, tmp_path, *, prior_status=None, fail_tag_update=False):
    settings = Settings(
        stash_url="http://localhost",
        stash_key="test-key",
        state=tmp_path / "state.sqlite",
        adult_roots=("/media/adult",),
        focus_categories=("general",),
    )
    events = []

    @contextmanager
    def lock(_path):
        yield

    class FakeControl:
        def __init__(self, _directory):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def check(self):
            pass

    class FakeModel:
        version = "model-v1"
        labels = [("tag", "general")]
        model = SimpleNamespace(
            labels=[SimpleNamespace(label="tag", category="general", subcategory="general")]
        )

    status_rows = []

    class FakeState:
        def __init__(self, _path):
            self.statuses = status_rows

        def job_status(self, _identity):
            return prior_status

        def status(self, identity, status, *_args):
            self.statuses.append((identity, status))
            events.append(f"state:{status}")

        def close(self):
            pass

    class FakeStash:
        def __init__(self, *_args):
            self.media = {
                "id": "image-1",
                "visual_files": [{"path": "/media/adult/image.jpg"}],
                "custom_fields": {},
                "tags": [],
                "updated_at": "same",
            }
            self.completions = []

        def items(self, kind):
            return [self.media] if kind == "image" else []

        def ensure_tags(self, names):
            return {name: f"{name}-id" for name in names}

        def item(self, kind, identity):
            assert (kind, identity) == ("image", "image-1")
            return self.media

        def update(self, kind, identity, values):
            if "tag_ids" in values:
                events.append("stash_tag_update")
                if fail_tag_update:
                    raise RuntimeError("stash update failed")
                self.media["tags"] = [{"id": tag_id} for tag_id in values["tag_ids"]]
            custom = values.get("custom_fields", {}).get("partial", {})
            for key, serialized in custom.items():
                self.media["custom_fields"][key] = serialized
                if key == worker.PROVENANCE:
                    completion = json.loads(serialized)["completion_state"]
                    self.completions.append(completion)
                    events.append(f"completion:{completion}")

    state = FakeState(settings.state)
    stash = FakeStash()

    monkeypatch.setattr(worker, "worker_lock", lock)
    monkeypatch.setattr(worker, "RunControl", FakeControl)
    monkeypatch.setattr(worker, "Hydra", lambda *_args: FakeModel())
    monkeypatch.setattr(worker, "State", FakeState)
    monkeypatch.setattr(worker, "Stash", lambda *_args: stash)
    return settings, state, stash, events


def test_interrupted_resume_disables_cache_and_never_marks_job_done(monkeypatch, tmp_path):
    settings, state, stash, events = install_run_harness(
        monkeypatch, tmp_path, prior_status="interrupted"
    )
    cache_flags = []

    def stop_prediction(*_args, **_kwargs):
        raise StopRequested("manual stop")

    monkeypatch.setattr(worker, "predict_prepared", stop_prediction)
    monkeypatch.setattr(
        worker,
        "prepare_remote",
        lambda *args, **kwargs: cache_flags.append(args[-1])
        or worker.PreparedMedia("image", "signature"),
    )
    monkeypatch.setattr(
        worker,
        "apply_predictions",
        lambda *_args: pytest.fail("stopped item must not publish predictions"),
    )

    assert worker.run(settings, "unused-upstream", kind="image") == (0, 0)

    assert cache_flags == [False]
    assert state.statuses == [("image:image-1", "running"), ("image:image-1", "interrupted")]
    assert stash.completions == ["processing", "interrupted"]
    assert "completion:complete" not in events


@pytest.mark.parametrize("prior_status", [None, "done"])
@pytest.mark.parametrize("marker", ["server_provenance", "queued_request"])
def test_unfinished_server_or_request_marker_disables_cache_and_skips_done(
    monkeypatch, tmp_path, prior_status, marker
):
    settings, state, stash, events = install_run_harness(
        monkeypatch, tmp_path, prior_status=prior_status
    )
    request = None
    if marker == "server_provenance":
        stash.media["custom_fields"][worker.PROVENANCE] = json.dumps(
            {"completion_state": "interrupted"}
        )
    else:
        request = QueueRequest("image", "image-1", "approved-request")
        stash.media["custom_fields"][QUEUE_FIELD] = {
            "request_id": request.request_id,
            "status": "running",
        }

    cache_flags = []

    def prepare(*args, **_kwargs):
        cache_flags.append(args[-1])
        return worker.PreparedMedia("image", "signature")

    monkeypatch.setattr(worker, "prepare_remote", prepare)
    monkeypatch.setattr(
        worker,
        "predict_prepared",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(StopRequested("stop while resuming")),
    )
    monkeypatch.setattr(
        worker,
        "apply_predictions",
        lambda *_args: pytest.fail("interrupted resume must not publish"),
    )

    assert worker.run(
        settings, "unused-upstream", kind="image",
        requests=[request] if request else None,
    ) == (0, 0)

    assert cache_flags == [False]
    assert state.statuses[-1] == ("image:image-1", "interrupted")
    assert not any(status == "done" for _, status in state.statuses)
    assert "complete" not in stash.completions
    if request:
        assert json.loads(stash.media["custom_fields"][QUEUE_FIELD])["status"] == "queued"


@pytest.mark.parametrize("fail_tag_update", [False, True])
def test_job_completion_is_recorded_only_after_stash_tag_update_succeeds(
    monkeypatch, tmp_path, fail_tag_update
):
    settings, state, stash, events = install_run_harness(
        monkeypatch, tmp_path, fail_tag_update=fail_tag_update
    )
    monkeypatch.setattr(
        worker,
        "prepare_remote",
        lambda *_args, **_kwargs: worker.PreparedMedia("image", "signature"),
    )
    monkeypatch.setattr(
        worker,
        "predict_prepared",
        lambda *_args, **_kwargs: {
            "scores": {"tag": 0.8},
            "times": [0.0],
            "score_format": "probability-v1",
            "hash": "media-hash",
            "model": "model-v1",
        },
    )

    result = worker.run(settings, "unused-upstream", kind="image")

    if fail_tag_update:
        assert result == (0, 1)
        assert stash.completions == ["processing"]
        assert state.statuses[-1] == ("image:image-1", "failed")
        assert "completion:complete" not in events
    else:
        assert result == (1, 0)
        assert events.index("stash_tag_update") < events.index("completion:complete")
        assert stash.completions == ["processing", "complete"]
        assert events.index("stash_tag_update") < events.index("state:done")
        assert state.statuses[-1] == ("image:image-1", "done")


@pytest.mark.parametrize("media_type,duration", [("image", None), ("video", 20)])
@pytest.mark.parametrize("fail_tag_update", [False, True])
def test_worker_completion_telemetry_counts_only_published_media(
    monkeypatch, tmp_path, media_type, duration, fail_tag_update
):
    settings, _state, stash, _events = install_run_harness(
        monkeypatch, tmp_path, fail_tag_update=fail_tag_update
    )
    source = stash.media["visual_files"][0]
    source["size"] = 345
    if duration is not None:
        source["duration"] = duration
    emitted = []
    outcomes = []
    monkeypatch.setattr(worker.events, "emit", lambda *args, **kwargs: emitted.append((args, kwargs)))
    monkeypatch.setattr(
        worker.events, "finish",
        lambda *args, **kwargs: outcomes.append((args, kwargs)),
    )
    monkeypatch.setattr(
        worker,
        "prepare_remote",
        lambda *_args, **_kwargs: worker.PreparedMedia(
            "image", "signature", input_bytes=source["size"]
        ),
    )
    monkeypatch.setattr(
        worker,
        "predict_prepared",
        lambda *_args, **_kwargs: {
            "scores": {"tag": 0.8},
            "times": [0.0],
            "score_format": "probability-v1",
            "hash": "media-hash",
            "model": "model-v1",
        },
    )

    assert worker.run(settings, "unused-upstream", kind="image") == (
        (0, 1) if fail_tag_update else (1, 0)
    )

    completed = [kwargs for args, kwargs in emitted if args[0] == "item_completed"]
    assert completed == ([] if fail_tag_update else [{
        "media_type": media_type, "input_files": 1, "input_bytes": 345,
    }])
    assert outcomes == [(("failed" if fail_tag_update else "completed", 0 if fail_tag_update else 1,
                          1 if fail_tag_update else 0), {
        "completed_files": 0 if fail_tag_update else 1,
        "completed_bytes": 0 if fail_tag_update else 345,
        "completed_images": 0 if fail_tag_update or media_type != "image" else 1,
        "completed_videos": 0 if fail_tag_update or media_type != "video" else 1,
    })]


def test_cancelled_after_cpu_preparation_is_not_claimed_or_inferred(monkeypatch, tmp_path):
    settings, state, stash, events = install_run_harness(monkeypatch, tmp_path)
    request = QueueRequest("image", "image-1", "request-1")
    stash.media["custom_fields"][QUEUE_FIELD] = {
        "request_id": request.request_id,
        "status": "queued",
    }
    prepared_event = Event()
    temp_paths = []

    def prepare(_settings, version, _cache, source, kind, temporary, _check, use_cache):
        assert use_cache is True
        temp_paths.append(temporary)
        (temporary / "prepared.marker").write_text("prepared", encoding="utf-8")
        prepared_event.set()
        return worker.PreparedMedia(kind, f"{source['path']}:{version}")

    actual_prefetched = worker.prefetched

    def wait_for_preparation(items, prepare_item, stop_check, temporary_root=None):
        pipeline = actual_prefetched(items, prepare_item, stop_check, temporary_root)
        try:
            job, future = next(pipeline)
            assert prepared_event.wait(timeout=3), "CPU preparation did not start"
            future.result(timeout=3)
            # Simulate a native client cancelling after the worker selected the
            # queued item, but before the main thread attempts to claim it.
            stash.media["custom_fields"][QUEUE_FIELD] = {
                "request_id": request.request_id,
                "status": "cancelled",
            }
            yield job, future
        finally:
            pipeline.close()

    monkeypatch.setattr(worker, "prepare_remote", prepare)
    monkeypatch.setattr(worker, "prefetched", wait_for_preparation)
    monkeypatch.setattr(
        worker,
        "predict_prepared",
        lambda *_args: pytest.fail("cancelled request must not reach GPU inference"),
    )
    monkeypatch.setattr(
        worker,
        "apply_predictions",
        lambda *_args: pytest.fail("cancelled request must not mutate tags"),
    )

    assert worker.run(settings, "unused-upstream", requests=[request]) == (0, 0)

    assert stash.media["custom_fields"][QUEUE_FIELD]["status"] == "cancelled"
    assert "stash_tag_update" not in events
    assert not any(status in {"running", "done"} for _, status in state.statuses)
    assert len(temp_paths) == 1
    assert not temp_paths[0].exists()


@pytest.mark.parametrize("phase", ["inference", "tag_creation"])
@pytest.mark.parametrize("replacement", [
    {"request_id": "request-1", "status": "cancelled"},
    {"request_id": "request-2", "status": "queued"},
])
def test_request_changed_before_publication_never_updates_tags_or_marks_done(
    monkeypatch, tmp_path, phase, replacement
):
    settings, state, stash, events = install_run_harness(monkeypatch, tmp_path)
    request = QueueRequest("image", "image-1", "request-1")
    stash.media["tags"] = [{"id": "manual-tag"}]
    stash.media["custom_fields"][QUEUE_FIELD] = {
        "request_id": request.request_id, "status": "queued",
    }
    temporary_paths = []

    def prepare(_settings, _version, _cache, _source, kind, temporary, *_args):
        temporary_paths.append(temporary)
        (temporary / "synthetic-frame.bin").write_bytes(b"temporary")
        return worker.PreparedMedia(kind, "signature")

    def replace_request():
        # Keep updated_at unchanged to test the request guard independently of tag edits.
        stash.media["custom_fields"][QUEUE_FIELD] = dict(replacement)

    def predict(*_args):
        if phase == "inference":
            replace_request()
        return {"scores": {"tag": 0.8}, "times": [0.0],
                "score_format": "probability-v1", "hash": "hash", "model": "model"}

    def ensure_tags(names):
        assert phase == "tag_creation", "inactive requests must not create tags"
        replace_request()
        return {name: "predicted-tag" for name in names}

    monkeypatch.setattr(worker, "prepare_remote", prepare)
    monkeypatch.setattr(worker, "predict_prepared", predict)
    stash.ensure_tags = ensure_tags

    assert worker.run(settings, "unused-upstream", requests=[request]) == (0, 0)

    assert stash.media["tags"] == [{"id": "manual-tag"}]
    assert stash.media["custom_fields"][QUEUE_FIELD] == replacement
    assert stash.completions == ["processing"]
    assert state.statuses == [("image:image-1", "running"), ("image:image-1", "interrupted")]
    assert "stash_tag_update" not in events
    assert len(temporary_paths) == 1
    assert not temporary_paths[0].exists()


def test_stop_on_current_job_after_next_job_is_prepared(monkeypatch, tmp_path):
    settings, state, stash, events = install_run_harness(monkeypatch, tmp_path)
    second = {
        "id": "image-2",
        "visual_files": [{"path": "/media/adult/image-2.jpg"}],
        "custom_fields": {},
        "tags": [],
        "updated_at": "same",
    }
    items = {stash.media["id"]: stash.media, second["id"]: second}
    stash.items = lambda kind: list(items.values()) if kind == "image" else []
    stash.item = lambda kind, identity: items[identity]

    first_prepared = Event()
    next_prepared = Event()
    temp_paths = []
    prediction_attempts = []

    def prepare(_settings, version, _cache, source, kind, temporary, _check, use_cache):
        assert use_cache is True
        temp_paths.append(temporary)
        marker = temporary / "prepared.marker"
        marker.write_text("prepared", encoding="utf-8")
        (next_prepared if source["path"].endswith("image-2.jpg") else first_prepared).set()
        return worker.PreparedMedia(kind, f"{source['path']}:{version}")

    def stop_current(_settings, _model, _state, prepared, _kind, _stop_check):
        assert first_prepared.wait(timeout=3), "current job was not prepared"
        assert next_prepared.wait(timeout=3), "lookahead job was not prepared"
        prediction_attempts.append(prepared.signature)
        assert prepared.signature.endswith("image.jpg:model-v1")
        raise StopRequested("stop current job")

    monkeypatch.setattr(worker, "prepare_remote", prepare)
    monkeypatch.setattr(worker, "predict_prepared", stop_current)

    assert worker.run(settings, "unused-upstream", kind="image") == (0, 0)

    assert len(temp_paths) == 2
    assert all(not path.exists() for path in temp_paths)
    assert prediction_attempts == ["/media/adult/image.jpg:model-v1"]
    assert state.statuses == [
        ("image:image-1", "running"),
        ("image:image-1", "interrupted"),
    ]
    assert not any(status == "done" for _, status in state.statuses)
    assert "stash_tag_update" not in events
    assert "completion:complete" not in events
