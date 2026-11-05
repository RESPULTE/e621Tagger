"""Prepare a bounded local corpus from explicitly supplied e621 original files.

Input JSONL: {"post_id": 123, "md5": "...", "image": "relative/original.jpg"}.
This tool downloads nothing and never accesses Studio review/production data.
Interrupted runs can be repeated: matching identities skip inference.
"""

import argparse
import hashlib
import json
import time
from pathlib import Path

from .app.reference_images import validate_image
from .hydra_encoder import HydraEncoder
from .vector_index import VectorIndex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-image-bytes", type=int, default=8 * 1024**2, metavar="BYTES",
                        help="maximum original image size in bytes (1024 through 16777216; default 8388608)")
    args = parser.parse_args()
    if not 1024 <= args.max_image_bytes <= 16 * 1024**2:
        parser.error("--max-image-bytes must be between 1024 and 16777216")
    started = time.perf_counter()
    encoder = HydraEncoder(args.checkpoint)
    index = VectorIndex(args.index, space=encoder.space, model_revision=encoder.model_revision)
    results, failed = [], False
    try:
        with args.manifest.open(encoding="utf-8") as source:
            for number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    path = args.manifest.parent / record["image"]
                    if not 0 < path.stat().st_size <= args.max_image_bytes:
                        raise ValueError("Original exceeds byte limit")
                    raw = path.read_bytes()
                    digest = validate_image(raw, 20_000_000)
                    if hashlib.md5(raw).hexdigest() != record["md5"]:
                        raise ValueError("Original MD5 mismatch")
                    cached = index.get(record["post_id"], record["md5"])
                    if cached and cached[0] == digest:
                        results.append({"line": number, "post_id": record["post_id"], "status": "unchanged"})
                        continue
                    before = time.perf_counter()
                    vector = encoder.encode(raw)
                    index.upsert(record["post_id"], record["md5"], digest, vector)
                    results.append({"line": number, "post_id": record["post_id"], "status": "indexed",
                                    "embedding_ms": (time.perf_counter() - before) * 1000, "sha256": digest})
                except Exception as exc:
                    failed = True
                    results.append({"line": number, "status": "failed", "error": type(exc).__name__})
        report = {"pipeline": encoder.contract, "embedding_space": encoder.space, "index_revision": index.revision,
                  "results": results, "elapsed_seconds": time.perf_counter() - started,
                  "peak_gpu_bytes": encoder.torch.cuda.max_memory_allocated(),
                  "gpu": encoder.torch.cuda.get_device_name()}
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"{len(results)} records processed; report: {args.report}")
    finally:
        index.close()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
