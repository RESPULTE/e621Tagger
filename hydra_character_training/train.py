"""Train only a native character query and head; never resplit a frozen export."""

import argparse
import gc
import hashlib
import json
import logging
import math
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from .dataset import file_digest, read_dataset
from .export import (
    BASE_SHA256,
    CLASS_SHAPES,
    checkpoint_contract,
    load_verified_extensions,
    metrics,
    operating_threshold,
    validation_table,
    write_extension,
)
from .features import FeatureCache, batches, cache_contract, source_digest

LOG = logging.getLogger(__name__)


def frozen_digest(model):
    """Hash actual frozen parameter bytes, including the CPU-resident backbone."""
    import torch

    digest = hashlib.sha256()
    for name, value in model.named_parameters():
        if name in CLASS_SHAPES:
            continue
        digest.update(name.encode())
        digest.update(value.detach().cpu().contiguous().view(dtype=torch.uint8).numpy().tobytes())
    return digest.hexdigest()


def prepare_trainable(model, device="cuda"):
    model.requires_grad_(False)
    model.attn_pool.float().to(device)
    model.head.float().to(device)
    found = {name for name, value in model.named_parameters() if hasattr(value, "rr_clsdim")}
    if found != set(CLASS_SHAPES):
        raise ValueError(f"Unexpected class-specific parameters: {found}")
    for name, shape in CLASS_SHAPES.items():
        parameter = model.get_parameter(name)
        if tuple(parameter.shape) != shape:
            raise ValueError(f"Unexpected one-label parameter shape: {name}")
        parameter.requires_grad_(True)
    if sum(p.numel() for p in model.parameters() if p.requires_grad) != 4096:
        raise ValueError("Expected exactly 4096 trainable scalars")
    model.eval()  # No dropout or mutable training state in the frozen computation.


def score(model, cache, samples, batch_size, dtype):
    import torch

    logits, targets = [], []
    with torch.no_grad():
        for x, valid, y in batches(cache, samples, batch_size, device=model.head.weight.device, dtype=dtype):
            output = model.forward_head(x, valid).float().cpu()
            if not torch.isfinite(output).all():
                raise ValueError("Nonfinite inference logits")
            logits.append(output)
            targets.append(y.cpu())
    return torch.cat(logits), torch.cat(targets)


def fit(model, cache, train, validation, *, epochs=100, patience=10, batch_size=4,
        learning_rate=1e-4, weight_decay=0.01, seed=3407):
    import torch
    from torch.nn.functional import binary_cross_entropy_with_logits

    parameters = [p for p in model.parameters() if p.requires_grad]
    if {n for n, p in model.named_parameters() if p.requires_grad} != set(CLASS_SHAPES):
        raise ValueError("Only native query and output weight may train")
    frozen_before = frozen_digest(model)
    initial = {name: model.get_parameter(name).detach().cpu().clone() for name in CLASS_SHAPES}
    optimizer = torch.optim.AdamW(parameters, lr=learning_rate, weight_decay=weight_decay)
    generator = torch.Generator().manual_seed(seed)
    history, best, best_epoch, best_weights = [], math.inf, 0, None
    started = time.monotonic()
    for epoch in range(1, epochs + 1):
        total, count = 0.0, 0
        order = torch.randperm(len(train), generator=generator).tolist()
        for x, valid, y in batches(cache, train, batch_size, device=parameters[0].device,
                                   dtype=torch.float32, order=order):
            optimizer.zero_grad(set_to_none=True)
            logits = model.forward_head(x, valid).flatten()
            loss = binary_cross_entropy_with_logits(logits, y)
            if not torch.isfinite(loss):
                raise ValueError("Nonfinite training loss")
            loss.backward()
            if any(p.grad is None or not torch.isfinite(p.grad).all() for p in parameters):
                raise ValueError("Missing or nonfinite native parameter gradients")
            optimizer.step()
            total += loss.item() * len(y)
            count += len(y)
        logits, y = score(model, cache, validation, batch_size, torch.float32)
        val_loss = binary_cross_entropy_with_logits(logits.flatten(), y).item()
        if not math.isfinite(val_loss):
            raise ValueError("Nonfinite validation loss")
        history.append({"epoch": epoch, "train_bce": total / count, "validation_bce": val_loss})
        LOG.info("Epoch %d train BCE %.6f validation BCE %.6f", epoch, total / count, val_loss)
        if val_loss < best:
            best, best_epoch = val_loss, epoch
            best_weights = {name: model.get_parameter(name).detach().cpu().clone() for name in CLASS_SHAPES}
        elif epoch - best_epoch >= patience:
            break
    with torch.no_grad():
        for name, value in best_weights.items():
            model.get_parameter(name).copy_(value)
    if frozen_digest(model) != frozen_before or any(p.grad is not None for n, p in model.named_parameters() if n not in CLASS_SHAPES):
        raise RuntimeError("Frozen parameter integrity failed")
    updated = {name: not torch.equal(initial[name], best_weights[name]) for name in CLASS_SHAPES}
    if not all(updated.values()):
        raise RuntimeError("Both new query and head must update")
    return {"history": history, "best_epoch": best_epoch, "best_validation_bce": best,
            "seconds": time.monotonic() - started, "frozen_parameters_sha256": frozen_before,
            "updated": updated, "trainable_scalars": 4096}


def round_trip(model_path, extension_path, contract, cache, dataset, one_label,
               probability_tolerance, logit_tolerance):
    """Full BF16 Hydra inference, with base-score and one-label equivalence gates."""
    import torch

    from hydra import image
    from hydra.model import load_model

    full = load_model(str(model_path), logit=True).cuda().eval().requires_grad_(False)
    regression = dataset.split("train")[:4]

    def direct(sample):
        tensor = full.load_image(str(sample.path), max_seqlen=cache.contract["max_seqlen"])
        sizes = torch.tensor([[tensor.shape[0] // 16, tensor.shape[1] // 16]], device="cuda", dtype=torch.int32)
        with torch.no_grad():
            output = full(full.from_srgb(image.patchify(tensor, 16)), sizes).float().cpu()
            if not torch.isfinite(output).all():
                raise ValueError("Nonfinite direct inference logits")
            return output

    before = torch.cat([direct(s) for s in regression])
    original = {name: full.get_parameter(name).detach().cpu().clone() for name in CLASS_SHAPES}
    descriptors = load_verified_extensions(full, [extension_path], contract["sha256"])
    for name, value in original.items():
        parameter = full.get_parameter(name)
        axis = getattr(parameter, "rr_clsdim")
        if not torch.equal(value, parameter.detach().cpu().narrow(axis, 0, value.shape[axis])):
            raise RuntimeError("Original class-specific weights changed")
    after = torch.cat([direct(s) for s in regression])
    old_prob_delta = (before.sigmoid() - after[:, :-1].sigmoid()).abs().max().item()
    old_logit_delta = (before - after[:, :-1]).abs().max().item()
    if old_prob_delta > probability_tolerance:
        raise RuntimeError(f"Existing tag probability regression: {old_prob_delta}")
    cached_regression, _ = score(full, cache, regression, 1, torch.bfloat16)
    cache_delta = (cached_regression.sigmoid() - after.sigmoid()).abs().max().item()
    if cache_delta > probability_tolerance:
        raise RuntimeError(f"Cached-token/direct-image mismatch: {cache_delta}")
    output = {}
    new_logit_delta, new_prob_delta = 0.0, 0.0
    # No test examples affect epoch or threshold selection. Only final evaluation reads them.
    for split in ("validation", "test"):
        samples = dataset.split(split)
        full_logits, targets = score(full, cache, samples, 1, torch.bfloat16)
        single_logits, _ = score(one_label, cache, samples, 1, torch.bfloat16)
        logits = full_logits[:, -1]
        new_logit_delta = max(new_logit_delta, (logits - single_logits.flatten()).abs().max().item())
        new_prob_delta = max(new_prob_delta, (logits.sigmoid() - single_logits.flatten().sigmoid()).abs().max().item())
        output[split] = (logits.sigmoid(), targets)
    if new_logit_delta > logit_tolerance or new_prob_delta > probability_tolerance:
        raise RuntimeError(f"One-label/full-model mismatch: logits={new_logit_delta}, probabilities={new_prob_delta}")
    evidence = {"old_probability_max_abs_difference": old_prob_delta,
                "old_logit_max_abs_difference": old_logit_delta,
                "new_logit_max_abs_difference": new_logit_delta,
                "new_probability_max_abs_difference": new_prob_delta,
                "cached_vs_direct_probability_max_abs_difference": cache_delta,
                "regression_post_ids": [s.post_id for s in regression],
                "base_output_count": len(contract["labels"]), "extended_output_count": full.num_classes,
                "original_class_weights_bitwise_preserved": True, "extensions": descriptors}
    del original
    full = None
    gc.collect()
    torch.cuda.empty_cache()
    return output, evidence


def run(args):
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    import torch

    from hydra.label import Label
    from hydra.model import load_model

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for the native BF16 extraction/inference contract")
    output = Path(args.output).resolve()
    report_path = output.with_suffix(".report.json")
    if output.exists() or report_path.exists():
        raise FileExistsError("Output or report already exists; choose a new run path")
    dataset = read_dataset(args.dataset, args.label)
    contract = checkpoint_contract(args.model, args.expected_base_sha256)
    if args.label in contract["labels"]:
        raise ValueError("New character label already exists in the base vocabulary")
    output.parent.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.use_deterministic_algorithms(True)
    model = load_model(str(args.model), reset_labels=[Label(args.label, "character")], logit=True)
    model.cuda().eval().requires_grad_(False)
    cache = FeatureCache(args.cache or output.parent / "token-cache", cache_contract(model, contract["sha256"], args.max_seqlen))
    LOG.info("Extracting %d reviewed images; cache %s", len(dataset.samples), cache.identity)
    extraction = cache.extract(model, dataset.samples)
    model.cpu()
    torch.cuda.empty_cache()
    prepare_trainable(model)
    torch.cuda.reset_peak_memory_stats()
    training = fit(model, cache, dataset.split("train"), dataset.split("validation"),
                   epochs=args.epochs, patience=args.patience, batch_size=args.batch_size,
                   learning_rate=args.learning_rate, weight_decay=args.weight_decay, seed=args.seed)
    training["peak_allocated_bytes"] = torch.cuda.max_memory_allocated()
    model.attn_pool.bfloat16()
    model.head.bfloat16()
    # A temporary candidate exercises the real loader before publishing an artifact.
    val_logits, val_targets = score(model, cache, dataset.split("validation"), 1, torch.bfloat16)
    table = validation_table(val_logits.sigmoid(), val_targets)
    provenance = {"dataset_id": dataset.manifest["dataset_id"], "media_manifest_sha256": dataset.manifest_sha256,
                  "source_manifest_sha256": dataset.manifest["source_manifest_sha256"],
                  "trainer_source_sha256": source_digest(Path(__file__).parent),
                  "settings": vars(args), "cache_identity": cache.identity}
    if file_digest(args.model) != contract["sha256"]:
        raise ValueError("Base checkpoint changed during training")
    with TemporaryDirectory(prefix=".hydra-training-", dir=output.parent) as temporary:
        candidate = Path(temporary) / "candidate.safetensors"
        write_extension(candidate, model, contract, args.label, table, provenance)
        torch.cuda.reset_peak_memory_stats()
        started = time.monotonic()
        scores, integrity = round_trip(args.model, candidate, contract, cache, dataset, model,
                                       args.probability_tolerance, args.logit_tolerance)
        table = validation_table(*scores["validation"])
        threshold = operating_threshold(table, args.minimum_precision)
        # Final calibration counts come from the exported BF16 head in full Hydra.
        final = Path(temporary) / "extension.safetensors"
        write_extension(final, model, contract, args.label, table, provenance)
        # Load the exact final artifact strictly into a fresh unmodified base on CPU.
        final_model = load_model(str(args.model), logit=True)
        integrity["extensions"] = load_verified_extensions(final_model, [final], contract["sha256"])
        for name, shape in CLASS_SHAPES.items():
            axis = getattr(final_model.get_parameter(name), "rr_clsdim")
            appended = final_model.get_parameter(name).narrow(axis, len(contract["labels"]), 1)
            if tuple(appended.shape) != shape or not torch.equal(appended, model.get_parameter(name).detach().cpu()):
                raise RuntimeError("Final artifact differs from verified native weights")
        integrity["final_artifact_strict_reload"] = True
        del final_model
        evaluation = {split: metrics(p, y, threshold) for split, (p, y) in scores.items()}
        test_samples = dataset.split("test")
        for kind in ("unrelated", "hard_negative"):
            indices = [i for i, s in enumerate(test_samples) if not s.target and any(
                item.get("kind") == kind for item in s.provenance)]
            if indices:
                evaluation["test_" + kind] = metrics(scores["test"][0][indices], scores["test"][1][indices], threshold)
        report = {"format": "hydra-character-training-v1", "label": args.label, "base_sha256": contract["sha256"],
                  "extension_sha256": file_digest(final), "provenance": provenance, "split_policy": dataset.manifest["policy"],
                  "cache_contract": cache.contract, "extraction": extraction, "training": training,
                  "threshold": threshold, "minimum_precision": args.minimum_precision,
                  "evaluation": evaluation, "integrity": integrity,
                  "inference_verification_seconds": time.monotonic() - started,
                  "inference_peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                  "recognition_gate_passed": (evaluation["test"]["precision"] or 0) >= 0.9 and (evaluation["test"]["recall"] or 0) >= 0.9,
                  "limitations": ["Small strata have wide confidence intervals; inspect counts.",
                                  "Artist identities are absent from export samples; group-disjoint policy is preserved, artist metrics unavailable.",
                                  "Negative strata use frozen provenance and may overlap; no test mining or threshold reselection."]}
        staged_report = Path(temporary) / "report.json"
        staged_report.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        # Never overwrite an earlier result, even if another run finished during training.
        if output.exists() or report_path.exists():
            raise FileExistsError("Another run published this output path")
        # Hard links create without replacement on Windows and Linux (same volume).
        os.link(staged_report, report_path)
        try:
            os.link(final, output)
        except OSError:
            report_path.unlink()
            raise
    LOG.info("Exported %s; threshold %.6f; test %s", output, threshold, evaluation["test"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cache")
    parser.add_argument("--expected-base-sha256", default=BASE_SHA256)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--patience", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--seed", type=int, default=3407)
    parser.add_argument("--max-seqlen", type=int, choices=(256, 512, 1024), default=1024)
    parser.add_argument("--minimum-precision", type=float, default=0.1)
    parser.add_argument("--probability-tolerance", type=float, default=0.01)
    parser.add_argument("--logit-tolerance", type=float, default=0.125)
    args = parser.parse_args()
    if min(args.epochs, args.patience, args.batch_size) < 1 or args.batch_size > 8:
        parser.error("epochs/patience must be positive and batch-size must be 1–8")
    if (not math.isfinite(args.learning_rate) or args.learning_rate <= 0
            or not math.isfinite(args.weight_decay) or args.weight_decay < 0
            or not 0 <= args.minimum_precision <= 1
            or not 0 < args.probability_tolerance <= 0.01
            or not 0 < args.logit_tolerance <= 0.125):
        parser.error("Invalid training/calibration settings or weakened integrity tolerances")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(args)


if __name__ == "__main__":
    main()
