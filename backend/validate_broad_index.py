"""Gate broad search against full raw-vector truth and freshly encoded originals."""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .broad_index import BroadIndex, digest_file
from .build_broad_ann import training_rows
from .build_broad_index import atomic_json


def heldout_rows(count, number=16):
    trained = set(training_rows(count).tolist())
    if count - len(trained) < number:
        raise ValueError("Insufficient held-out images outside quantizer training")
    rng = np.random.default_rng(3408)
    selected = []
    for _ in range(number * 1000):
        row = int(rng.integers(count))
        if row not in trained and row not in selected:
            selected.append(row)
            if len(selected) == number:
                break
    if len(selected) != number:
        raise ValueError("Could not select the bounded held-out validation sample")
    return np.asarray(selected, dtype=np.int64)


def exact_truth(corpus, rows, *, k=64):
    queries = np.stack([corpus.transform(corpus.vectors[row]) for row in rows])
    best = [[] for _ in rows]
    raw_sum = np.zeros(corpus.dimension, dtype=np.float64)
    started = time.monotonic()
    for start in range(0, corpus.size, corpus.block_rows):
        end = min(start + corpus.block_rows, corpus.size)
        raw_sum += corpus.vectors[start:end].sum(axis=0, dtype=np.float64)
        values = corpus._unit_rows(corpus.vectors[start:end])
        # All held-out queries share one sequential raw-data pass. This is
        # independent of quantizer candidate selection and visits every image.
        scores = values @ queries.T
        for column, own_row in enumerate(rows):
            distances = np.maximum(2 - 2 * scores[:, column], 0)
            if start <= own_row < end:
                distances[own_row - start] = np.inf
            amount = min(k, len(distances))
            selected = np.argpartition(distances, amount - 1)[:amount]
            # Include boundary ties, then use the same documented post-ID tie break.
            selected = np.flatnonzero(distances <= distances[selected].max())
            order = np.lexsort((corpus.ids[start:end][selected], distances[selected]))[:k]
            best[column].extend((float(distances[i]), int(corpus.ids[start + i])) for i in selected[order])
            best[column].sort()
            del best[column][k:]
        if start % (corpus.block_rows * 20) == 0:
            print(f"Exact held-out validation scanned {end:,}/{corpus.size:,} images", flush=True)
    # Independently audit the resumable decoder's centroid against the entire
    # committed matrix, rather than trusting its running journal sum.
    audited_mean = np.asarray(raw_sum / corpus.size, dtype=np.float32)
    if not np.allclose(audited_mean, corpus.mean, rtol=1e-6, atol=1e-7):
        raise ValueError("Broad corpus mean does not match the full raw image matrix")
    return queries, np.array([[post for _, post in result] for result in best], dtype=np.int64), time.monotonic() - started


def validate(root, checkpoint, paired_artifacts, *, wait_seconds=0):
    root, paired_artifacts = Path(root), Path(paired_artifacts)
    deadline = time.monotonic() + wait_seconds
    while True:
        if (root / "manifest.json").exists():
            manifest = json.loads((root / "manifest.json").read_text())
            if "search.faiss" in manifest.get("files", {}):
                break
        if time.monotonic() >= deadline:
            raise FileNotFoundError("Search index is not ready for validation")
        time.sleep(5)
    if manifest.get("validation", {}).get("passed") is True:
        raise FileExistsError("Generation already validated; do not alter its identity")
    corpus = BroadIndex(root, model_revision=manifest["model_revision"], query_space=manifest["native_query_space"],
                        require_validation=False)
    if corpus.ann is None:
        raise ValueError("Broad interactive search requires the measured accelerator")
    rows = heldout_rows(corpus.size)
    contract = {"method": "full-raw-scan-centroid-audit-v2", "raw": manifest["files"]["raw_vectors.npy"]["sha256"],
                "mean": manifest["files"]["mean.npy"]["sha256"], "heldout_rows": rows.tolist()}
    cache, cache_contract = root / "exact-validation.npz", root / "exact-validation-contract.json"
    if cache.exists() and cache_contract.exists():
        if json.loads(cache_contract.read_text()) != contract:
            raise ValueError("Exact validation cache belongs to a different corpus")
        with np.load(cache, allow_pickle=False) as data:
            queries, truth, exact_seconds = data["queries"], data["truth"], float(data["seconds"])
    else:
        queries, truth, exact_seconds = exact_truth(corpus, rows)
        np.savez(cache, queries=queries, truth=truth, seconds=exact_seconds)
        atomic_json(cache_contract, contract)
    attempts, accepted = [], None
    for rerank in (8192, 16384, 32768, 65536):
        corpus.manifest["search"]["rerank"] = rerank
        recalls, timings = [], []
        # Match the live worker's executor boundary, including thread-local
        # OpenMP settings, rather than measuring only the construction thread.
        with ThreadPoolExecutor(max_workers=1) as executor:
            for row, query, expected in zip(rows, queries, truth):
                started = time.monotonic()
                found = executor.submit(corpus.search, query, limit=32, exclude_post=int(corpus.ids[row])).result()
                timings.append(time.monotonic() - started)
                recalls.append(len({n["post_id"] for n in found} & set(expected[:32].tolist())) / 32)
        result = {"rerank": rerank, "recall_at_32": recalls, "mean_recall": float(np.mean(recalls)),
                  "minimum_recall": min(recalls), "query_seconds": timings,
                  "p95_query_seconds": float(np.percentile(timings, 95))}
        attempts.append(result)
        print(f"Broad recall mean {result['mean_recall']:.4f}, minimum {min(recalls):.4f}, p95 {result['p95_query_seconds']:.3f}s", flush=True)
        if result["mean_recall"] >= 0.95 and min(recalls) >= 0.90 and result["p95_query_seconds"] <= 10:
            accepted = result
            break
    if accepted is None:
        atomic_json(root / "validation-failed.json", {"passed": False, "attempts": attempts})
        raise RuntimeError("Full-corpus recall/runtime gates failed; live worker must not switch")
    from .hydra_encoder import HydraEncoder

    encoder = HydraEncoder(checkpoint)
    if encoder.space != manifest["native_query_space"]:
        raise ValueError("Native query extraction changed from the validated contract")
    evidence = json.loads((paired_artifacts / "parity-final.json").read_text())
    if len(evidence.get("images", [])) != 6 or len({image["post_id"] for image in evidence["images"]}) != 6:
        raise ValueError("Six independent paired original images are required")
    pairs = []
    for image in evidence["images"]:
        post_id = image["post_id"]
        path = paired_artifacts / f"post-{post_id}-original.jpg"
        if digest_file(path) != image["sha256"]:
            raise ValueError("Native compatibility original bytes changed")
        row = int(np.searchsorted(corpus.ids, post_id))
        if row >= corpus.size or corpus.ids[row] != post_id:
            raise ValueError("Paired source is absent from pinned corpus")
        native = encoder.encode_raw(path.read_bytes())
        published = np.asarray(corpus.vectors[row], dtype=np.float32)
        cosine = float(np.dot(native, published) / (np.linalg.norm(native) * np.linalg.norm(published)))
        relative = float(np.linalg.norm(native - published) / np.linalg.norm(published))
        query = corpus.transform(native)
        centered_delta = float(np.linalg.norm(query - corpus.transform(published)))
        found = corpus.search(query, limit=32)
        source_rank = next((n["rank"] for n in found if n["post_id"] == post_id), None)
        equivalent_rank = source_rank
        # Identical publisher vectors may come from duplicate uploads. They are
        # equally valid numerical matches for this compatibility check.
        for neighbor in found[:5]:
            candidate_row = int(np.searchsorted(corpus.ids, neighbor["post_id"]))
            difference = np.linalg.norm(corpus.transform(corpus.vectors[candidate_row]) - corpus.transform(published))
            if difference <= 1e-4:
                equivalent_rank = min(equivalent_rank or 33, neighbor["rank"])
        pair = {"post_id": post_id, "raw_cosine": cosine, "raw_relative_l2": relative,
                "centered_unit_difference": centered_delta, "source_rank": source_rank,
                "source_or_identical_vector_rank": equivalent_rank}
        pairs.append(pair)
        if (not np.isfinite([cosine, relative, centered_delta]).all() or cosine < 0.999 or relative > 0.03
                or centered_delta > 0.05 or equivalent_rank is None or equivalent_rank > 5):
            atomic_json(root / "validation-failed.json", {"passed": False, "attempts": attempts, "native_pairs": pairs})
            raise RuntimeError("Native-query/published-vector compatibility failed; do not activate")
    manifest["search"]["rerank"] = accepted["rerank"]
    manifest["validation"] = {"passed": True, "heldout_from_quantizer_training": len(rows),
                              "exact_full_corpus_seconds": exact_seconds, "exact_cache_sha256": digest_file(cache),
                              "search_attempts": attempts, "native_pairs": pairs,
                              "scope": "Numerical compatibility/nearest-neighbor recall, not supervised character accuracy"}
    corpus.close()
    atomic_json(root / "manifest.json", manifest)
    atomic_json(root / "validation-report.json", manifest["validation"])
    print("Broad image index passed integrity, exact-recall, runtime and native-query gates", flush=True)
    return manifest["validation"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--paired-artifacts", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args()
    validate(args.generation, args.checkpoint, args.paired_artifacts, wait_seconds=args.wait_seconds)


if __name__ == "__main__":
    main()
