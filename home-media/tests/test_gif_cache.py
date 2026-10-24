import json
from contextlib import contextmanager
from types import SimpleNamespace

from PIL import Image

from home_media import worker
from home_media.config import Settings
from home_media.state import State


def test_gif_policy_recomputes_legacy_cache_then_reuses_eight_sample_result(monkeypatch, tmp_path):
    settings = Settings("http://localhost", "test", tmp_path / "state.sqlite", ("/adult",))
    source = {"path": "/adult/animation.GIF"}
    signature = json.dumps([
        source["path"], 100, 200, "model-v1", ["midpoints-v2", "image", 32, 0],
    ])
    state = State(settings.state)
    state.put("old-image-key", {
        "scores": {"old-tag": 0.9}, "times": list(range(32)),
        "score_format": "probability-v1", "complete": True,
    })
    state.remember_source(signature, "old-image-key")

    @contextmanager
    def remote(_settings):
        yield SimpleNamespace(normalize=lambda path: path,
                              stat=lambda path: SimpleNamespace(st_size=100, st_mtime=200))

    downloads = []

    def fetch(_sftp, _path, destination, *_args):
        downloads.append(destination)
        first, second = Image.new("RGB", (8, 8), "red"), Image.new("RGB", (8, 8), "blue")
        first.save(destination, format="GIF", save_all=True, append_images=[second], duration=100)
        return "gif-hash"

    class Model:
        version = "model-v1"

        def __init__(self):
            self.paths = []

        def predict_batch(self, paths):
            self.paths.extend(paths)
            return [{"new-tag": 0.8} for _ in paths]

    monkeypatch.setattr(worker, "remote", remote)
    monkeypatch.setattr(worker, "fetch_file", fetch)
    try:
        prepared = worker.prepare_remote(settings, "model-v1", state, source, "image", tmp_path)
        assert prepared.sampling_kind == "gif"
        assert "gif-midpoints-v1" in prepared.key
        assert prepared.prediction is None
        model = Model()
        result = worker.predict_prepared(settings, model, state, prepared, "image")
        assert len(model.paths) == len(result["times"]) == 8
        assert result["scores"] == {"new-tag": 0.8}
        assert result["complete"] is True

        cached = worker.prepare_remote(settings, "model-v1", state, source, "image", tmp_path)
        assert cached.prediction == result
        assert len(downloads) == 1
    finally:
        state.close()
