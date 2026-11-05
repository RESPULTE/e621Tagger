"""Build a CPU search accelerator; exact raw-vector reranking remains authoritative."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np

from .broad_index import BroadIndex, digest_file
from .build_broad_index import atomic_json


def training_rows(count, sample=131072, seed=3407):
    return np.sort(np.random.default_rng(seed).choice(count, min(sample, count), replace=False))


def build(root, *, wait_seconds=0):
    import faiss

    root = Path(root)
    deadline = time.monotonic() + wait_seconds
    while not (root / "manifest.json").exists():
        if time.monotonic() >= deadline:
            raise FileNotFoundError("Raw image matrix is not ready")
        time.sleep(5)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if "search.faiss" in manifest["files"]:
        raise FileExistsError("Search index already published; validate/resume that generation")
    corpus = BroadIndex(root, model_revision=manifest["model_revision"],
                        query_space=manifest["native_query_space"], require_validation=False)
    faiss.omp_set_num_threads(8)
    if corpus.size < 256 * 39:
        raise ValueError("Broad quantizer training requires at least 9,984 images")
    started = time.monotonic()
    progress = {"phase": "training-search-index", "images": corpus.size, "added": 0}
    atomic_json(root / "ann-progress.json", progress)
    selected = training_rows(corpus.size)
    samples = corpus._unit_rows(corpus.vectors[selected])
    # Exhaustive compressed-code search preserves every original feature axis.
    # The bounded prefix experiment lost neighbors with PCA/coarse partitions;
    # PQ256 + exact raw reranking recovered all held-out top-32 in that test.
    index = faiss.IndexPQ(corpus.dimension, 256, 8)
    index.pq.cp.seed, index.pq.cp.niter = 3407, 15
    index.train(samples)
    del samples
    progress.update(phase="adding-images", training_seconds=time.monotonic() - started)
    for start in range(0, corpus.size, 8192):
        end = min(start + 8192, corpus.size)
        index.add(corpus._unit_rows(corpus.vectors[start:end]))
        progress.update(added=end, elapsed_seconds=time.monotonic() - started)
        atomic_json(root / "ann-progress.json", progress)
        if start % (8192 * 50) == 0:
            print(f"Indexed {end:,}/{corpus.size:,} image vectors", flush=True)
    temporary = root / "search.faiss.tmp"
    faiss.write_index(index, str(temporary))
    if (root / "search.faiss").exists():
        raise FileExistsError("Search index was published concurrently")
    os.rename(temporary, root / "search.faiss")
    manifest["files"]["search.faiss"] = {"bytes": (root / "search.faiss").stat().st_size,
                                         "sha256": digest_file(root / "search.faiss")}
    manifest["search"] = {"kind": "PQ256x8-exhaustive-codes-with-exact-reranking", "rerank": 8192,
                          "training_seed": 3407, "training_samples": len(selected),
                          "training_rows_sha256": hashlib.sha256(selected.tobytes()).hexdigest(),
                          "faiss": faiss.__version__}
    manifest["validation"] = {"passed": False, "reason": "Awaiting held-out recall and native-query compatibility"}
    atomic_json(manifest_path, manifest)
    progress.update(phase="awaiting-validation", elapsed_seconds=time.monotonic() - started)
    atomic_json(root / "ann-progress.json", progress)
    corpus.close()
    print("Broad image search index ready for validation", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args()
    build(args.generation, wait_seconds=args.wait_seconds)


if __name__ == "__main__":
    main()
