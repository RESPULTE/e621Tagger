import json
import math
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

import pytest

from home_media import worker
from home_media.config import Settings


def make_settings(
    tmp_path, *, roots=("/media/adult",), max_frames=32, video_frames=8,
    threshold=0.70, context_threshold=0.90, focus_only=False
):
    return Settings(
        stash_url="http://localhost",
        stash_key="test-key",
        state=tmp_path / "state.sqlite",
        adult_roots=roots,
        max_frames=max_frames,
        video_frames=video_frames,
        threshold=threshold,
        context_threshold=context_threshold,
        focus_only=focus_only,
    )


def test_hydra_loader_is_asked_for_logits_without_loading_real_model_or_cuda(
    monkeypatch, tmp_path
):
    model_path = tmp_path / "tiny-model.bin"
    model_path.write_bytes(b"test model fingerprint")
    upstream_path = tmp_path / "upstream"
    upstream_path.mkdir()
    loader_calls = []

    class FakeModel:
        labels = [SimpleNamespace(label="tag", category="general", subcategory=None)]

        def to(self, *, device, dtype):
            assert device == "cuda"
            return self

        def eval(self):
            return self

        def requires_grad_(self, enabled):
            assert enabled is False
            return self

    fake_model = FakeModel()

    def load_model(*args, **kwargs):
        loader_calls.append((args, kwargs))
        return fake_model

    fake_torch = ModuleType("torch")
    fake_torch.cuda = SimpleNamespace(
        is_available=lambda: True,
        get_device_name=lambda: "mock cuda",
    )
    fake_torch.bfloat16 = object()
    fake_hydra = ModuleType("hydra")
    fake_hydra.image = object()
    fake_hydra_model = ModuleType("hydra.model")
    fake_hydra_model.load_model = load_model
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "hydra", fake_hydra)
    monkeypatch.setitem(sys.modules, "hydra.model", fake_hydra_model)
    # Hydra.__init__ prepends upstream to sys.path; isolate and restore that mutation.
    monkeypatch.setattr(sys, "path", list(sys.path))

    instance = worker.Hydra(str(model_path), str(upstream_path))

    assert instance.model is fake_model
    assert loader_calls == [((str(model_path.resolve()),), {"logit": True})]


@pytest.mark.parametrize("duration", [12, 95, 10_000])
def test_sample_times_uses_eight_evenly_spaced_midpoints(duration):
    expected = [duration * (index + 0.5) / 8 for index in range(8)]

    assert worker.sample_times(duration) == pytest.approx(expected)


def test_sample_times_accepts_a_configurable_positive_count():
    assert worker.sample_times(95, count=4) == pytest.approx(
        [11.875, 35.625, 59.375, 83.125]
    )


@pytest.mark.parametrize("count", [0, -1, 1.5, True, None])
def test_sample_times_rejects_invalid_counts(count):
    with pytest.raises(ValueError, match="positive integer"):
        worker.sample_times(95, count=count)


@pytest.mark.parametrize("duration", [0, -1, math.nan, math.inf, -math.inf])
def test_sample_times_rejects_nonpositive_or_nonfinite_duration(duration):
    with pytest.raises(ValueError, match="Media duration must be positive"):
        worker.sample_times(duration)


def test_video_frames_decodes_eight_frames_at_even_midpoints(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **_kwargs):
        timestamp = float(command[command.index("-ss") + 1])
        calls.append(timestamp)
        (tmp_path / command[-1].split("\\")[-1]).write_bytes(b"frame")
        return SimpleNamespace(returncode=0)

    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        SimpleNamespace(get_ffmpeg_exe=lambda: "ffmpeg"),
    )
    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    samples = worker.frames(
        tmp_path / "movie.mp4", tmp_path, "scene", 95, make_settings(tmp_path)
    )

    expected = [95 * (index + 0.5) / 8 for index in range(8)]
    assert len(calls) == 8
    assert calls == pytest.approx(expected)
    assert [timestamp for timestamp, _ in samples] == pytest.approx(expected)
    assert all(path.is_file() for _, path in samples)


def test_reconciled_tags_preserves_manual_tags_and_drops_auto_tags_no_longer_predicted():
    final, automatic, rejected = worker.reconciled_tags(
        current={"manual", "auto-low", "auto-high"},
        previous={"auto_ids": ["auto-low", "auto-high"]},
        predicted={"auto-high", "auto-new"},
    )

    assert final == {"manual", "auto-high", "auto-new"}
    assert automatic == {"auto-high", "auto-new"}
    assert rejected == set()


def test_reconciled_tags_remembers_removed_automatic_tag_as_rejection():
    final, automatic, rejected = worker.reconciled_tags(
        current={"manual"},
        previous={"auto_ids": ["removed-auto"]},
        predicted={"manual", "removed-auto", "new-auto"},
    )

    assert final == {"manual", "new-auto"}
    assert automatic == {"new-auto"}
    assert rejected == {"removed-auto"}


def test_reconciled_tags_clears_a_rejection_when_user_readds_tag():
    final, automatic, rejected = worker.reconciled_tags(
        current={"rejected-tag"},
        previous={"auto_ids": [], "rejected_ids": ["rejected-tag"]},
        predicted={"rejected-tag", "new-auto"},
    )

    assert final == {"rejected-tag", "new-auto"}
    assert automatic == {"new-auto"}
    assert rejected == set()


def test_fetch_file_rejects_normalized_remote_symlink_escape_before_stat_or_read(tmp_path):
    settings = make_settings(tmp_path)

    class FakeSftp:
        stat_calls = 0
        get_calls = 0

        def normalize(self, _path):
            return "/media/family/private.mp4"

        def stat(self, _path):
            self.stat_calls += 1
            raise AssertionError("stat must not run for a path outside the allowlist")

        def get(self, *_args):
            self.get_calls += 1
            raise AssertionError("get must not run for a path outside the allowlist")

    sftp = FakeSftp()
    destination = tmp_path / "download.mp4"
    with pytest.raises(ValueError, match="outside the explicit adult-media allowlist"):
        worker.fetch_file(sftp, "/media/adult/link.mp4", destination, settings)

    assert sftp.stat_calls == 0
    assert sftp.get_calls == 0
    assert not destination.exists()


@pytest.mark.parametrize("video_frames", [8, 6])
def test_prepare_remote_uses_unchanged_stat_cache_without_download_or_inference(
    monkeypatch, tmp_path, video_frames
):
    settings = make_settings(tmp_path, video_frames=video_frames)
    cached_prediction = {
        "hash": "cached-hash",
        "model": "model-v1",
        "scores": {"x": 0.9},
        "times": list(range(video_frames)),
        "score_format": "probability-v1",
        "complete": True,
    }

    class FakeSftp:
        def __init__(self):
            self.stat_paths = []
            self.get_calls = 0

        def normalize(self, source_path):
            return source_path

        def stat(self, path):
            self.stat_paths.append(path)
            return SimpleNamespace(st_size=1234, st_mtime=5678)

        def get(self, *_args):
            self.get_calls += 1
            raise AssertionError("unchanged source cache should avoid SFTP download")

    sftp = FakeSftp()

    @contextmanager
    def fake_remote(_settings):
        yield sftp

    class FakeState:
        def __init__(self):
            self.signatures = []

        def source(self, signature):
            self.signatures.append(json.loads(signature))
            return cached_prediction

        def get(self, _key):
            pytest.fail("source cache hit must not query prediction cache")

        def put(self, *_args):
            pytest.fail("source cache hit must not rewrite predictions")

        def remember_source(self, *_args):
            pytest.fail("source cache hit must not rewrite its mapping")

    class FakeModel:
        version = "model-v1"

        def predict_batch(self, _paths):
            raise AssertionError("cached source should avoid model inference")

    state = FakeState()
    monkeypatch.setattr(worker, "remote", fake_remote)
    monkeypatch.setattr(
        worker,
        "fetch_file",
        lambda *_args: (_ for _ in ()).throw(AssertionError("download must be skipped")),
    )
    monkeypatch.setattr(
        worker,
        "frames",
        lambda *_args: (_ for _ in ()).throw(AssertionError("frame decoding must be skipped")),
    )

    model = FakeModel()
    prepared = worker.prepare_remote(
        settings,
        model.version,
        state,
        {"path": "/media/adult/movie.mp4", "duration": 95},
        "scene",
        tmp_path,
    )
    result = worker.predict_prepared(settings, model, state, prepared, "scene")

    assert result == cached_prediction
    assert prepared.key is None
    assert prepared.samples is None
    assert prepared.input_bytes == 1234
    assert sftp.stat_paths == ["/media/adult/movie.mp4"]
    assert sftp.get_calls == 0
    assert state.signatures == [
        [
            "/media/adult/movie.mp4",
            1234,
            5678,
            "model-v1",
            ["midpoints-v2", "scene", video_frames, 95],
        ]
    ]


def test_focused_labels_uses_hydra_categories_without_name_matches():
    labels = [
        SimpleNamespace(label="arbitrary_name_one", category="general", subcategory="fetish"),
        SimpleNamespace(label="unrelated_custom_label", category="action", subcategory=None),
        SimpleNamespace(label="character_classified_label", category="character", subcategory="general"),
        SimpleNamespace(label="fetish_adjacent_name", category="general", subcategory="general"),
        SimpleNamespace(label="vore_like", category="general", subcategory="general"),
    ]

    assert worker.focused_labels(labels, categories=("fetish", "action", "character")) == {
        "arbitrary_name_one", "unrelated_custom_label", "character_classified_label"
    }


def test_focused_labels_rejects_unknown_category():
    labels = [SimpleNamespace(label="known", category="general", subcategory="general")]

    with pytest.raises(ValueError, match="Unknown focus categories"):
        worker.focused_labels(labels, categories=("missing_group",))



def test_apply_predictions_noops_for_unchanged_metadata_and_excludes_artist_copyright(tmp_path):
    prediction = {
        "scores": {"performer-label": 0.99, "copyright-label": 0.98},
        "model": "model-v1",
        "hash": "hash-v1",
        "times": [6.0],
    }
    provenance = {
        "auto_ids": [],
        "rejected_ids": [],
        "model": prediction["model"],
        "hash": prediction["hash"],
        "threshold": 0.7,
        "context_threshold": 0.9,
        "focus_only": False,
        "tag_policy": "category-priority-v1",
        "focus_categories": ["fetish", "action", "character"],
        "sample_times": prediction["times"],
        "score_format": "probability-v1",
        "completion_state": "complete",
    }

    class FakeStash:
        def __init__(self):
            self.ensure_names = None
            self.item_calls = 0
            self.update_calls = []

        def ensure_tags(self, names):
            self.ensure_names = names
            return {}

        def item(self, _kind, _identity):
            self.item_calls += 1
            return {
                "custom_fields": {worker.PROVENANCE: json.dumps(provenance)},
                "tags": [{"id": "manual-id"}],
                "updated_at": "unchanged",
            }

        def update(self, *args):
            self.update_calls.append(args)

    stash = FakeStash()
    worker.apply_predictions(
        stash,
        "image",
        "image-id",
        prediction,
        {"performer-label": "artist", "copyright-label": "copyright"},
        make_settings(tmp_path),
        set(),
    )

    assert stash.ensure_names == []
    assert stash.item_calls == 1
    assert stash.update_calls == []


def test_apply_predictions_applies_character_focus_threshold_and_keeps_scores(tmp_path):
    class FakeStash:
        def __init__(self):
            self.names = []

        def ensure_tags(self, names):
            self.names = names
            return {name: f"id-{name}" for name in names}

        def item(self, _kind, _identity):
            return {"custom_fields": {}, "tags": [], "updated_at": "same"}

        def update(self, *_args):
            pass

    scores = {
        "context-low": 0.89,
        "context-boundary": 0.90,
        "fetish-low": 0.69,
        "fetish-boundary": 0.70,
        "character-low": 0.69,
        "character-boundary": 0.70,
        "artist-high": 0.99,
    }
    prediction = {
        "scores": scores.copy(),
        "model": "model-v1",
        "hash": "hash-v1",
        "times": [0.0],
    }
    labels = [
        SimpleNamespace(label=name, category=category, subcategory=subcategory)
        for name, category, subcategory in (
            ("fetish-low", "general", "fetish"),
            ("fetish-boundary", "general", "fetish"),
            ("action-label", "action", "general"),
            ("character-low", "character", "general"),
            ("character-boundary", "character", "general"),
            ("artist-high", "artist", "general"),
        )
    ]
    focus_labels = worker.focused_labels(labels, categories=("fetish", "action", "character"))
    categories = {label.label: label.category for label in labels}
    categories.update({"context-low": "general", "context-boundary": "general"})
    stash = FakeStash()

    worker.apply_predictions(
        stash,
        "image",
        "image-id",
        prediction,
        categories,
        make_settings(tmp_path),
        focus_labels,
    )

    assert stash.names == ["context-boundary", "fetish-boundary", "character-boundary"]
    assert prediction["scores"] == scores



def test_apply_predictions_focus_only_omits_context_and_preserves_manual_tags(tmp_path):
    class FakeStash:
        def __init__(self):
            self.names = None
            self.values = None

        def ensure_tags(self, names):
            self.names = names
            return {name: f"id-{name}" for name in names}

        def item(self, _kind, _identity):
            return {
                "custom_fields": {},
                "tags": [{"id": "manual-id"}],
                "updated_at": "same",
            }

        def update(self, _kind, _identity, values):
            self.values = values

    stash = FakeStash()
    worker.apply_predictions(
        stash,
        "image",
        "image-id",
        {
            "scores": {"context": 0.99, "fetish": 0.70},
            "model": "model-v1",
            "hash": "hash-v1",
            "times": [0.0],
        },
        {"context": "general", "fetish": "general"},
        make_settings(tmp_path, focus_only=True),
        {"fetish"},
    )

    assert stash.names == ["fetish"]
    assert stash.values["tag_ids"] == ["id-fetish", "manual-id"]


def test_apply_predictions_excludes_four_digit_years_from_tag_creation(tmp_path):
    class FakeStash:
        def __init__(self):
            self.ensure_names = None
            self.updates = []

        def ensure_tags(self, names):
            self.ensure_names = names
            return {name: f"id-{name}" for name in names}

        def item(self, _kind, _identity):
            return {"custom_fields": {}, "tags": [], "updated_at": "same"}

        def update(self, _kind, _identity, values):
            self.updates.append(values)

    stash = FakeStash()
    worker.apply_predictions(
        stash,
        "image",
        "image-id",
        {
            "scores": {"2024": 0.99},
            "model": "model-v1",
            "hash": "hash-v1",
            "times": [0.0],
        },
        {"2024": "year"},
        make_settings(tmp_path),
        set(),
    )

    assert stash.ensure_names == []
    assert stash.updates[0]["tag_ids"] == []

