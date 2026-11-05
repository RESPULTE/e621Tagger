"""Contracts and opt-in GPU plumbing; synthetic accuracy is never a quality claim."""

import hashlib
import json
import os
from argparse import Namespace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from hydra_character_training.dataset import canonical, read_dataset
from hydra_character_training.export import prediction_identity


def make_export(root, *, count_per_split=2):
    root.mkdir(parents=True, exist_ok=True)
    seed, rows, candidate = "training-test-v1", [], 1
    for split in ("train", "validation", "test"):
        for i in range(count_per_split):
            while True:
                group = f"post:{candidate}"
                candidate += 1
                bucket = int(hashlib.sha256(f"{seed}:{group}".encode()).hexdigest()[:8], 16) % 100
                expected = "train" if bucket < 80 else "validation" if bucket < 90 else "test"
                if expected == split:
                    break
            target = i % 2
            post_id = candidate - 1
            path = root / f"{post_id}.png"
            image = Image.new("RGB", (64 + i * 16, 64), (20 + post_id % 100, 220 if target else 20, 20 if target else 220))
            image.putpixel((0, 0), (post_id % 256, i, 50))
            image.save(path)
            raw = path.read_bytes()
            rows.append({"post_id": post_id, "label": "positive" if target else "negative", "target": target,
                         "review_version": 1, "reviewed_at": 1.0, "event_id": post_id,
                         "md5": hashlib.md5(raw).hexdigest(), "sha256": hashlib.sha256(raw).hexdigest(),
                         "bytes": len(raw), "relative_path": path.name, "media_ext": "png", "media_url": "fixture",
                         "availability": "available", "group": group, "split": split, "excluded_reason": None,
                         "dhash": "0123456789abcdef", "provenance": [{"kind": "unrelated" if not target else "target"}]})
    manifest = {"schema_version": 1, "media_export_version": 1, "dataset_id": str(uuid4()),
                "source_manifest_sha256": "a" * 64, "project": {"display_name": "__hydra_training_smoke__"},
                "policy": {"version": "studio-split-v1", "split_seed": seed, "group_artists": True},
                "counts": {"included": len(rows), "excluded": 0,
                           "splits": {name: count_per_split for name in ("train", "validation", "test")}},
                "samples": rows}
    (root / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
    return manifest


def test_frozen_media_manifest_preserves_order_targets_and_splits(tmp_path):
    manifest = make_export(tmp_path)
    dataset = read_dataset(tmp_path, "__hydra_training_smoke__")
    assert [s.post_id for s in dataset.samples] == [s["post_id"] for s in manifest["samples"]]
    assert [s.split for s in dataset.samples] == [s["split"] for s in manifest["samples"]]
    assert dataset.manifest_sha256 == hashlib.sha256((tmp_path / "manifest.json").read_bytes()).hexdigest()


@pytest.mark.parametrize("mutation", ["missing_split", "changed_split", "duplicate_id", "duplicate_hash", "escape",
                                    "group_leak", "missing_target", "metadata_only", "bad_counts", "one_class", "wrong_label"])
def test_manifest_rejects_ambiguous_or_leaking_supervision(tmp_path, mutation):
    manifest = make_export(tmp_path)
    row = manifest["samples"][0]
    if mutation == "missing_split":
        del row["split"]
    elif mutation == "changed_split":
        row["split"] = "test"
    elif mutation == "duplicate_id":
        manifest["samples"][1]["post_id"] = row["post_id"]
    elif mutation == "duplicate_hash":
        manifest["samples"][1]["sha256"] = row["sha256"]
    elif mutation == "escape":
        row["relative_path"] = "../outside.png"
    elif mutation == "group_leak":
        manifest["samples"][-1]["group"] = row["group"]
    elif mutation == "missing_target":
        row["target"] = None
    elif mutation == "metadata_only":
        del manifest["media_export_version"]
    elif mutation == "bad_counts":
        manifest["counts"]["included"] += 1
    elif mutation == "one_class":
        row.update(target=1, label="positive")
    elif mutation == "wrong_label":
        manifest["project"]["display_name"] = "another_character"
    (tmp_path / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
    with pytest.raises((ValueError, KeyError)):
        read_dataset(tmp_path, "__hydra_training_smoke__")


def test_manifest_rechecks_media_and_rejects_duplicate_json_keys(tmp_path):
    manifest = make_export(tmp_path)
    path = tmp_path / manifest["samples"][0]["relative_path"]
    raw = path.read_bytes()
    path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    with pytest.raises(ValueError, match="hash mismatch"):
        read_dataset(tmp_path, "__hydra_training_smoke__")
    (tmp_path / "manifest.json").write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError, match="Duplicate"):
        read_dataset(tmp_path, "__hydra_training_smoke__")


def test_prediction_identity_changes_with_extension_order_and_configuration():
    base, config = "a" * 64, {"max_seqlen": 1024, "threshold": 0.7}
    first, second = {"sha256": "b" * 64}, {"sha256": "c" * 64}
    version = prediction_identity(base, [first, second], config)
    assert version == prediction_identity(base, [first, second], dict(reversed(list(config.items()))))
    assert len({version, prediction_identity(base, [second, first], config),
                prediction_identity(base, [first], config),
                prediction_identity(base, [first, second], {**config, "threshold": 0.8}),
                prediction_identity("d" * 64, [first, second], config)}) == 5


def torch_modules():
    torch = pytest.importorskip("torch")
    pytest.importorskip("safetensors")
    return torch


def test_calibration_counts_match_hydra_threshold_including_bf16_boundary():
    torch = torch_modules()
    from hydra.classification import Calibration
    from hydra_character_training.export import metrics, operating_threshold, validation_table

    boundary = Calibration.bf16_threshold(0.73)
    probabilities = torch.tensor([0.1, 0.2, boundary, 0.9])
    targets = torch.tensor([0, 0, 1, 1])
    table = validation_table(probabilities, targets)
    assert table.shape == (99, 4)
    assert table[72].tolist() == [2, 0, 2, 0]
    threshold = operating_threshold(table, 0.9)
    assert metrics(probabilities, targets, threshold) == {
        "tp": 2, "fp": 0, "tn": 2, "fn": 0, "n": 4,
        "precision": 1.0, "recall": 1.0, "f1": 1.0, "false_positive_rate": 0.0}
    with pytest.raises(ValueError, match="minimum precision"):
        operating_threshold(validation_table(torch.tensor([0.9, 0.1]), torch.tensor([0, 1])), 1.0)


def native_stub(torch):
    from hydra.head import LinearHead
    from hydra.label import Label
    from hydra.model import Hydra
    from hydra.pool import HydraPool
    from hydra_character_training.export import ARCHITECTURE

    # Use the real extension loader and class dimensions without allocating the backbone.
    model = Hydra.__new__(Hydra)
    torch.nn.Module.__init__(model)
    model.name, model.architecture = "Hydra 3.5", ARCHITECTURE
    model.labels = [Label("existing", "general")]
    model.attn_pool = HydraPool(1, 2048, 64, input_dim=1152, ff_dim=16, mid_blocks=1, dtype=torch.bfloat16)
    model.head = LinearHead(1, 2048, dtype=torch.bfloat16)
    return model


@pytest.mark.parametrize("invalid", [None, "base", "architecture", "collision", "keys", "shape", "dtype", "counts", "nonfinite", "second_file"])
def test_strict_extension_loader_is_atomic_and_preserves_original_columns(tmp_path, invalid):
    torch = torch_modules()
    from safetensors import safe_open
    from safetensors.torch import save_file

    from hydra_character_training.export import load_verified_extensions, validation_table, write_extension

    model = native_stub(torch)
    contract = {"sha256": "a" * 64, "metadata": {"modelspec.title": model.name}}
    table = validation_table(torch.tensor([0.1, 0.9]), torch.tensor([0, 1]))
    path = tmp_path / "extension.safetensors"
    write_extension(path, model, contract, "new_character", table, {})
    old_q, old_w = model.attn_pool.q.detach().clone(), model.head.weight.detach().clone()
    with safe_open(str(path), framework="pt") as source:
        tensors = {key: source.get_tensor(key).clone() for key in source.keys()}
        meta = source.metadata()
    if invalid == "base":
        meta["classifier.base.sha256"] = "b" * 64
    elif invalid == "architecture":
        meta["modelspec.architecture"] = "old"
    elif invalid == "collision":
        meta["classifier.label"] = "existing"
    elif invalid == "keys":
        tensors["unexpected"] = torch.zeros(1)
    elif invalid == "shape":
        tensors["attn_pool.q"] = tensors["attn_pool.q"][:, :, :32].contiguous()
    elif invalid == "dtype":
        tensors["head.weight"] = tensors["head.weight"].float()
    elif invalid == "counts":
        tensors["validation"][0, 0] = -1
    elif invalid == "nonfinite":
        tensors["head.weight"][0, 0] = float("nan")
    amended = tmp_path / "amended.safetensors"
    save_file(tensors, str(amended), metadata=meta)
    path = amended
    paths = [path, path] if invalid == "second_file" else [path]
    if invalid:
        with pytest.raises(ValueError):
            load_verified_extensions(model, paths, "a" * 64)
        assert model.num_classes == 1 and len(model.labels) == 1
    else:
        descriptors = load_verified_extensions(model, paths, "a" * 64)
        assert model.num_classes == 2 and [x.label for x in model.labels] == ["existing", "new_character"]
        assert descriptors[0]["threshold"] > 0
    assert torch.equal(model.attn_pool.q[:, :1], old_q)
    assert torch.equal(model.head.weight[:1], old_w)


def test_cached_token_padding_uses_validity_masks_and_rejects_wrong_identity(tmp_path):
    torch = torch_modules()
    from safetensors.torch import save_file

    from hydra_character_training.features import FeatureCache, batches

    cache = FeatureCache(tmp_path, {"max_seqlen": 1024})
    samples = [SimpleNamespace(sha256=str(i), target=i) for i in range(2)]
    for i, sample in enumerate(samples):
        save_file({"features": torch.ones(i + 1, 1152, dtype=torch.bfloat16),
                   "valid": torch.ones(i + 1, dtype=torch.bool)}, str(cache.path(sample)),
                  metadata={"cache_identity": cache.identity, "image_sha256": sample.sha256})
    x, valid, y = next(batches(cache, samples, 2, device="cpu", dtype=torch.float32))
    assert x.shape == (2, 2, 1152) and x.dtype == torch.float32
    assert valid.tolist() == [[True, False], [True, True]] and y.tolist() == [0, 1]
    assert not x[0, 1].any()
    cache.identity = "changed"
    with pytest.raises(ValueError, match="identity"):
        cache.read(samples[0])


@pytest.mark.skipif(not os.environ.get("HYDRA_TRAINING_CHECKPOINT"), reason="Opt-in real checkpoint/CUDA plumbing")
def test_native_gpu_training_export_and_full_model_roundtrip(tmp_path):
    torch = torch_modules()
    from hydra_character_training.export import BASE_SHA256
    from hydra_character_training.train import run

    assert torch.cuda.is_available()
    dataset_path = tmp_path / "dataset"
    make_export(dataset_path, count_per_split=4)
    output = tmp_path / "__hydra_training_smoke__.safetensors"
    args = Namespace(model=os.environ["HYDRA_TRAINING_CHECKPOINT"], dataset=str(dataset_path),
                     label="__hydra_training_smoke__", output=str(output), cache=str(tmp_path / "cache"),
                     expected_base_sha256=BASE_SHA256, epochs=4, patience=3, batch_size=2,
                     learning_rate=1e-4, weight_decay=0.01, seed=3407, max_seqlen=1024,
                     minimum_precision=0.1, probability_tolerance=0.01, logit_tolerance=0.125)
    report = run(args)
    assert output.exists() and output.with_suffix(".report.json").exists()
    assert report["training"]["history"][-1]["train_bce"] < report["training"]["history"][0]["train_bce"]
    assert all(report["training"]["updated"].values())
    assert report["integrity"]["extended_output_count"] == report["integrity"]["base_output_count"] + 1
    assert report["integrity"]["old_probability_max_abs_difference"] <= 0.01
    assert report["evaluation"]["test"]["n"] == 4
    # Optional durable local evidence; never publish or install this synthetic label.
    if evidence := os.environ.get("HYDRA_TRAINING_EVIDENCE"):
        from pathlib import Path

        Path(evidence).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
