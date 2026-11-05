"""Exact retrieval benchmark using explicitly human-verified character judgments.

JSONL records: {"target":"name", "query":"image.jpg", "verified_by":"reviewer",
"verified_at":"ISO date", "positive_ids":[1,2], "negative_ids":[3],
"already_confirmed_ids":[1]}. Unknown results remain unjudged.
"""

import argparse
import json
import time
from pathlib import Path

from .app.reference_images import validate_image
from .hydra_encoder import HydraEncoder
from .vector_index import VectorIndex


def discovery_metrics(post_ids, judgments, *, k=32):
    positives, negatives = set(judgments["positive_ids"]), set(judgments["negative_ids"])
    if positives & negatives or not positives or not judgments.get("verified_by") or not judgments.get("verified_at"):
        raise ValueError("Disjoint, human-verified binary judgments and review identity/date are required")
    found = set(post_ids[:k])
    positive_hits, negative_hits = len(found & positives), len(found & negatives)
    judged = positive_hits + negative_hits
    return {"requested_k": k, "returned": len(post_ids[:k]), "positive_hits": positive_hits,
            "negative_hits": negative_hits, "unjudged": len(found) - judged,
            "precision_at_k_lower_bound": positive_hits / k,
            "precision_among_judged": positive_hits / judged if judged else None,
            "recall_of_known_positives_at_k": positive_hits / len(positives),
            "new_confirmed_positives": len((found & positives) - set(judgments.get("already_confirmed_ids", []))),
            "hard_negative_yield": negative_hits,
            "limitation": "Recall uses known positives only; unjudged results do not establish true precision"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-image-bytes", type=int, default=8 * 1024**2, metavar="BYTES",
                        help="maximum query image size in bytes (1024 through 16777216; default 8388608)")
    args = parser.parse_args()
    if not 1024 <= args.max_image_bytes <= 16 * 1024**2:
        parser.error("--max-image-bytes must be between 1024 and 16777216")
    encoder = HydraEncoder(args.checkpoint)
    index = VectorIndex(args.index, space=encoder.space, model_revision=encoder.model_revision)
    results = []
    try:
        with args.judgments.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                path = args.judgments.parent / record["query"]
                if not 0 < path.stat().st_size <= args.max_image_bytes:
                    raise ValueError("Query exceeds byte limit")
                raw = path.read_bytes()
                digest = validate_image(raw, 20_000_000)
                started = time.perf_counter()
                vector = encoder.encode(raw)
                embedded = time.perf_counter()
                found = index.search(vector, limit=32, exclude_sha256=digest)
                searched = time.perf_counter()
                metrics = discovery_metrics([n["post_id"] for n in found["neighbors"]], record)
                results.append({"target": record["target"], "verified_by": record["verified_by"],
                                "verified_at": record["verified_at"], "query_sha256": digest,
                                "index_revision": found["revision"], "neighbors": found["neighbors"],
                                "embedding_ms": (embedded - started) * 1000,
                                "search_ms": (searched - embedded) * 1000, **metrics})
        args.report.write_text(json.dumps({"pipeline": encoder.contract, "results": results}, indent=2) + "\n", encoding="utf-8")
    finally:
        index.close()


if __name__ == "__main__":
    main()
