"""Strict native V2 extension contract, shared by training and inference."""

import hashlib
import re
from pathlib import Path

from .dataset import canonical, file_digest

ARCHITECTURE = "naflexvit_so400m_patch16_siglip+rr_hydra2"
IMPLEMENTATION = "redrocket.extension.label.v2"
BASE_SHA256 = "5e9337c21019c51f2bb5b33d053ea3e2c0412e5ca972b5ea3c4c21e6987452ca"
CLASS_SHAPES = {"attn_pool.q": (32, 1, 64), "head.weight": (1, 2048)}


def checkpoint_contract(path, expected_sha256=BASE_SHA256):
    from safetensors import safe_open

    digest = file_digest(path)
    if expected_sha256 and digest != expected_sha256:
        raise ValueError("Base checkpoint SHA-256 mismatch")
    with safe_open(str(path), framework="pt", device="cpu") as source:
        metadata = source.metadata()
        if metadata["modelspec.architecture"] != ARCHITECTURE:
            raise ValueError("Only native Hydra 3.5 rr_hydra2 is supported")
        labels = [line.split()[0] for line in metadata["classifier.labels"].splitlines()]
        count = len(labels)
        expected = {"attn_pool.q": [32, count, 64], "head.weight": [count, 2048], "validation": [count, 99, 4]}
        if len(set(labels)) != count or any(source.get_slice(k).get_shape() != shape for k, shape in expected.items()):
            raise ValueError("Unexpected checkpoint class/validation dimensions")
    return {"sha256": digest, "metadata": metadata, "labels": labels}


def validation_table(probabilities, targets):
    import torch

    from hydra.classification import Calibration

    p, y = probabilities.detach().float().cpu().flatten(), targets.detach().cpu().bool().flatten()
    if p.shape != y.shape or not p.numel() or not torch.isfinite(p).all() or not ((p >= 0) & (p <= 1)).all():
        raise ValueError("Invalid validation probabilities")
    # Hydra calibrates each nominal grid point through a BF16 logit round trip.
    thresholds = torch.tensor([Calibration.bf16_threshold(i / 100) for i in range(1, 100)])
    predicted = p[None, :] >= thresholds[:, None]
    return torch.stack((
        (predicted & y).sum(1), (predicted & ~y).sum(1),
        (~predicted & ~y).sum(1), (~predicted & y).sum(1),
    ), dim=1).float()


def operating_threshold(table, minimum_precision=0.1):
    from hydra.classification import Calibration, f_score, min_precision

    threshold, score = Calibration.calibrate(table, min_precision(f_score(), minimum_precision))
    if score is None:
        raise ValueError("No validation threshold satisfies minimum precision")
    return threshold


def metrics(probabilities, targets, threshold):
    p, y = probabilities.detach().float().cpu().flatten(), targets.detach().cpu().bool().flatten()
    yes = p >= threshold
    tp, fp, tn, fn = (int(x.sum()) for x in (yes & y, yes & ~y, ~yes & ~y, ~yes & y))
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "n": len(y),
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0,
            "false_positive_rate": fp / (fp + tn) if fp + tn else None}


def write_extension(path, model, contract, label, table, provenance):
    import torch
    from safetensors.torch import save_file

    if Path(path).exists():
        raise FileExistsError(path)
    tensors = {name: model.get_parameter(name).detach().to(device="cpu", dtype=torch.bfloat16).contiguous()
               for name in CLASS_SHAPES}
    tensors["validation"] = table.detach().float().cpu().contiguous()
    metadata = {
        "modelspec.implementation": IMPLEMENTATION,
        "modelspec.architecture": ARCHITECTURE,
        "classifier.model": contract["metadata"]["modelspec.title"],
        "classifier.label": label,
        "classifier.label.category": "character",
        "classifier.base.sha256": contract["sha256"],
        "classifier.max_seqlen": str(provenance.get("settings", {}).get("max_seqlen", 1024)),
        "classifier.label.minimum_precision": str(provenance.get("settings", {}).get("minimum_precision", 0.1)),
        "training.provenance": canonical(provenance),
    }
    save_file(tensors, str(path), metadata=metadata)


def load_verified_extensions(model, paths, base_sha256):
    """Validate every file before mutating the model; reject replacements/collisions."""
    paths = list(paths)
    if not paths:
        return []
    import torch
    from safetensors import safe_open

    from hydra.model import Extension

    pending, descriptors = [], []
    labels = {label.label for label in model.labels}
    for path in paths:
        path = str(Path(path).resolve())
        digest = file_digest(path)
        with safe_open(path, framework="pt", device="cpu") as source:
            meta = source.metadata() or {}
            if (meta.get("modelspec.implementation") != IMPLEMENTATION
                    or meta.get("modelspec.architecture") != ARCHITECTURE
                    or model.architecture != ARCHITECTURE
                    or meta.get("classifier.model") != model.name
                    or meta.get("classifier.base.sha256") != base_sha256
                    or meta.get("classifier.label.category") != "character"):
                raise ValueError(f"Extension model identity/category mismatch: {path}")
            label = meta.get("classifier.label", "")
            if not re.fullmatch(r"[^\s\x00-\x1f]+", label) or label in labels:
                raise ValueError(f"Extension label is invalid or already exists: {label}")
            if set(source.keys()) != {*CLASS_SHAPES, "validation"}:
                raise ValueError("Unexpected/missing extension tensor keys")
            for name, shape in CLASS_SHAPES.items():
                tensor, target = source.get_tensor(name), model.get_parameter(name)
                axis = getattr(target, "rr_clsdim", None)
                expected = list(target.shape)
                if axis is None:
                    raise ValueError("Target is not class-specific")
                expected[axis] = 1
                if tuple(tensor.shape) != shape or list(tensor.shape) != expected or tensor.dtype != target.dtype:
                    raise ValueError(f"Extension tensor shape/dtype mismatch: {name}")
                if not torch.isfinite(tensor).all():
                    raise ValueError("Nonfinite extension weights")
            table = source.get_tensor("validation")
            if (table.shape != (99, 4) or table.dtype != torch.float32
                    or not torch.isfinite(table).all() or (table < 0).any() or (table != table.round()).any()
                    or not (table.sum(1) == table[0].sum()).all() or table[0].sum() == 0
                    or not ((table[:, 0] + table[:, 3]) == (table[0, 0] + table[0, 3])).all()
                    or not (table[1:, :2] <= table[:-1, :2]).all()
                    or not (table[1:, 2:] >= table[:-1, 2:]).all()):
                raise ValueError("Invalid extension validation counts")
        max_seqlen = int(meta.get("classifier.max_seqlen", "1024"))
        minimum_precision = float(meta.get("classifier.label.minimum_precision", "0.1"))
        if max_seqlen not in (256, 512, 1024) or not 0 <= minimum_precision <= 1:
            raise ValueError("Invalid extension inference configuration")
        threshold = operating_threshold(table, minimum_precision)
        extension = Extension.load(path)
        if file_digest(path) != digest:
            raise ValueError("Extension changed during loading")
        labels.add(label)
        pending.append(extension)
        descriptors.append({"sha256": digest, "label": label, "threshold": threshold, "max_seqlen": max_seqlen})
    if pending:
        count = model.num_classes
        model.load_extensions(pending, strict=True)
        if model.num_classes != count + len(pending) or len(model.labels) != model.num_classes:
            raise RuntimeError("Extension did not append exactly one output per label")
    return descriptors


def prediction_identity(base_sha256, extensions, configuration):
    return hashlib.sha256(canonical({"base_sha256": base_sha256, "extensions": extensions,
                                     "inference": configuration}).encode()).hexdigest()
