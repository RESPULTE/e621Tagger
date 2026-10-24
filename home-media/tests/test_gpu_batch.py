from contextlib import nullcontext
from types import SimpleNamespace

from home_media import worker
from home_media.worker import Hydra


class FakeOutOfMemoryError(RuntimeError):
    pass


class FakeLogits:
    def __init__(self, rows):
        self.rows = rows

    def float(self):
        return self


class FakeScores:
    def __init__(self, rows):
        self.rows = rows

    def cpu(self):
        return self

    def tolist(self):
        return self.rows


class FakeBatch:
    def __init__(self, images, max_seq):
        self.images = images
        self.max_seq = max_seq


def make_hydra(*, fail_batches=False):
    model = SimpleNamespace()
    model.loaded = []
    model.forwarded = []

    def load_image(source, *, max_seqlen):
        model.loaded.append((source, max_seqlen))
        return source

    def stack(images, _patch_size, max_seq, *, device):
        assert device == "cpu"
        return FakeBatch(images, max_seq), [(1, 1)] * len(images)

    def forward(patches, _sizes):
        model.forwarded.append((list(patches.images), patches.max_seq))
        if fail_batches and len(patches.images) > 1:
            raise FakeOutOfMemoryError
        return FakeLogits([[0.12345678, 0.98765432] for _ in patches.images])

    model.load_image = load_image
    model.from_srgb = lambda patches: patches
    model.__call__ = forward

    class CallableModel:
        def __call__(self, patches, sizes):
            return forward(patches, sizes)

    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(
            OutOfMemoryError=FakeOutOfMemoryError,
            empty_cache=lambda: None,
        ),
        inference_mode=nullcontext,
        sigmoid=lambda logits: FakeScores(logits.rows),
    )
    instance = object.__new__(Hydra)
    instance.torch = fake_torch
    instance.model = CallableModel()
    instance.model.load_image = load_image
    instance.model.from_srgb = model.from_srgb
    instance.image = SimpleNamespace(stack=stack)
    instance.labels = [("first", "general"), ("second", "general")]
    return instance, model


def test_predict_batch_scores_in_order_with_small_padded_cpu_batches(monkeypatch):
    monkeypatch.setattr(worker, "GPU_FRAME_BATCH_SIZE", 2)
    hydra, model = make_hydra()
    single_calls = []
    monkeypatch.setattr(
        hydra,
        "predict",
        lambda source: single_calls.append(source) or {"single": source},
    )

    predictions = hydra.predict_batch(["frame-a", "frame-b", "frame-c"])

    assert predictions == [
        {"first": 0.123457, "second": 0.987654},
        {"first": 0.123457, "second": 0.987654},
        {"single": "frame-c"},
    ]
    assert model.loaded == [("frame-a", 1024), ("frame-b", 1024)]
    assert model.forwarded == [(["frame-a", "frame-b"], 1024)]
    assert single_calls == ["frame-c"]


def test_predict_batch_splits_oom_batch_then_uses_existing_single_predict(monkeypatch):
    hydra, model = make_hydra(fail_batches=True)
    single_calls = []
    monkeypatch.setattr(
        hydra,
        "predict",
        lambda source: single_calls.append(source) or {"single": source},
    )

    predictions = hydra.predict_batch(["frame-a", "frame-b"])

    assert predictions == [{"single": "frame-a"}, {"single": "frame-b"}]
    assert model.forwarded == [(["frame-a", "frame-b"], 1024)]
    assert single_calls == ["frame-a", "frame-b"]
