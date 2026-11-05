"""Build a resumable immutable raw image-vector snapshot from verified shards."""

import argparse
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

import numpy as np

from .broad_index import FORMAT, digest_file
from .hydra_encoder import CHECKPOINT_SHA256

DATASET = "RedRocket/Hydra-Embeddings"
REVISION = "02bd7bd04c732dd3de011d88d642a1d21024f70d"
POST_IDS_SHA256 = "33b7cc320ee4a33836948089453c00bab2df87389f5da074d0ffedad261d6100"


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def build(manifest_path, raw_root, post_ids, pipeline_report, output, *, wait_seconds=0):
    import msgpack

    manifest_path, raw_root, output = Path(manifest_path), Path(raw_root), Path(output)
    manifest_sha = digest_file(manifest_path)
    source = json.loads(manifest_path.read_text(encoding="utf-8"))
    if source.get("dataset") != DATASET or source.get("revision") != REVISION:
        raise ValueError("Unrecognized pinned image-embedding source")
    entries = source["entries"]
    if len(entries) != 1305 or [e["index"] for e in entries] != list(range(1305)):
        raise ValueError("Incomplete or unordered raw shard inventory")
    if digest_file(post_ids) != POST_IDS_SHA256:
        raise ValueError("Published post-ID catalog checksum mismatch")
    ids = np.load(post_ids, mmap_mode="r", allow_pickle=False)
    if ids.shape != (6_122_692,) or ids.dtype != np.int64 or (np.diff(ids) <= 0).any():
        raise ValueError("Published post-ID catalog shape/order mismatch")
    report = json.loads(Path(pipeline_report).read_text(encoding="utf-8"))
    pipeline = report["pipeline"]
    native_space = hashlib.sha256(json.dumps(pipeline, sort_keys=True).encode()).hexdigest()
    if native_space != report["embedding_space"] or pipeline["checkpoint_sha256"] != CHECKPOINT_SHA256:
        raise ValueError("Invalid native encoder contract")
    if output.exists() and (output / "manifest.json").exists():
        raise FileExistsError("A published generation must never be modified")
    output.mkdir(parents=True, exist_ok=True)
    progress_path = output / "build-progress.json"
    if progress_path.exists():
        progress = json.loads(progress_path.read_text())
        if progress["source_manifest_sha256"] != manifest_sha or progress["native_query_space"] != native_space:
            raise ValueError("Cannot resume a different image-index build")
        vectors = np.lib.format.open_memmap(output / "raw_vectors.npy", mode="r+")
        if vectors.shape != (len(ids), 2048) or vectors.dtype != np.float16:
            raise ValueError("Interrupted image matrix header changed")
        row, completed = progress["rows"], progress["completed_shards"]
        total = np.asarray(progress["sum_float64"], dtype=np.float64)
    else:
        if any(output.iterdir()):
            raise ValueError("Unjournaled output exists; preserve it and choose a new generation")
        required = len(ids) * (2048 * 2 + 8) + 1024**3
        if shutil.disk_usage(output).free < required:
            raise ValueError("Insufficient working space for the broad image index")
        if (output / "raw_vectors.npy").exists():
            raise ValueError("Unjournaled matrix exists; preserve it and choose a new generation")
        vectors = np.lib.format.open_memmap(output / "raw_vectors.npy", mode="w+", dtype="<f2", shape=(len(ids), 2048))
        np.save(output / "post_ids.npy", np.asarray(ids))
        row, completed, total = 0, 0, np.zeros(2048, dtype=np.float64)
        progress = {"source_manifest_sha256": manifest_sha, "native_query_space": native_space,
                    "rows": 0, "completed_shards": 0, "sum_float64": total.tolist()}
        atomic_json(progress_path, progress)
    deadline = time.monotonic() + wait_seconds
    started = time.monotonic()
    for entry in entries[completed:]:
        name = entry["path"]
        if not re.fullmatch(r"embeds/e621_static_[0-9]{5}\.hydra35_embed\.msgpack", name):
            raise ValueError("Unsafe shard name")
        path = raw_root / Path(name).name
        progress.update(phase="waiting-for-shard", next_shard=entry["index"])
        atomic_json(progress_path, progress)
        while not path.is_file():
            if time.monotonic() >= deadline:
                raise FileNotFoundError(f"Verified shard not yet available: {path.name}; build can be resumed")
            time.sleep(2)
        if path.is_symlink() or path.stat().st_size != entry["size"] or digest_file(path) != entry["sha256"]:
            raise ValueError(f"Raw shard integrity mismatch: {path.name}")
        records = msgpack.unpackb(path.read_bytes(), raw=False, strict_map_key=True)
        if not isinstance(records, list) or len(records) > 5000:
            raise ValueError("Invalid shard row array")
        batch = np.empty((len(records), 2048), dtype="<f2")
        for i, record in enumerate(records):
            if (not isinstance(record, dict) or set(record) != {"id", "vector"}
                    or type(record["id"]) is not int or row + i >= len(ids)
                    or record["id"] != int(ids[row + i]) or record["id"] // 5000 != entry["index"]
                    or not isinstance(record["vector"], bytes) or len(record["vector"]) != 4096):
                raise ValueError("Raw vector/catalog identity mismatch")
            batch[i] = np.frombuffer(record["vector"], dtype="<f2")
        if not np.isfinite(batch).all():
            raise ValueError("Nonfinite publisher image embedding")
        vectors[row:row + len(batch)] = batch
        vectors.flush()
        total += batch.sum(axis=0, dtype=np.float64)
        row += len(batch)
        # Raw bytes flush before this atomic journal. Uncommitted tail rows are
        # overwritten on resume; the centroid sum cannot double-count a shard.
        progress.update(phase="decoding", rows=row, completed_shards=entry["index"] + 1,
                        sum_float64=total.tolist(), elapsed_seconds=time.monotonic() - started)
        atomic_json(progress_path, progress)
        if entry["index"] % 10 == 0:
            print(f"Decoded {entry['index'] + 1}/1305 shards, {row:,}/{len(ids):,} images", flush=True)
        del records, batch
    if row != len(ids):
        raise ValueError("Raw corpus does not cover the exact pinned image catalog")
    mean = np.asarray(total / row, dtype=np.float32)
    np.save(output / "mean.npy", mean)
    vectors.flush()
    del vectors
    progress.update(phase="hashing")
    atomic_json(progress_path, progress)
    files = {name: {"bytes": (output / name).stat().st_size, "sha256": digest_file(output / name)}
             for name in ("post_ids.npy", "raw_vectors.npy", "mean.npy")}
    result = {"format": FORMAT, "dataset": DATASET, "dataset_revision": REVISION,
              "source_manifest_sha256": manifest_sha, "count": row, "dimension": 2048,
              "model_revision": CHECKPOINT_SHA256, "native_query_space": native_space,
              "native_pipeline": pipeline, "files": files,
              "validation": {"passed": False, "reason": "Awaiting compatibility, quality and runtime checks"}}
    atomic_json(output / "manifest.json", result)
    progress.update(phase="awaiting-validation")
    atomic_json(progress_path, progress)
    print(f"Broad image matrix ready for validation: {output}", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--post-ids", type=Path, required=True)
    parser.add_argument("--pipeline-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args()
    build(args.manifest, args.raw_root, args.post_ids, args.pipeline_report, args.output, wait_seconds=args.wait_seconds)


if __name__ == "__main__":
    main()
