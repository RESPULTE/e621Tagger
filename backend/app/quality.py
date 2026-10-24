"""Bounded, deterministic duplicate grouping; no network or model dependency."""

import hashlib
import io
import json
from collections import Counter, defaultdict

from fastapi import HTTPException
from PIL import Image

MAX_CANDIDATES = 5000
STILL_IMAGES = {"jpg", "jpeg", "png", "webp"}


def difference_hash(raw):
    with Image.open(io.BytesIO(raw)) as image:
        pixels = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS).tobytes()
    value = 0
    for y in range(8):
        for x in range(8):
            value = (value << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    return f"{value:016x}"


def availability(row):
    if row["unavailable"]:
        return "deleted_or_unavailable"
    if not row["file_url"]:
        return "missing_url"
    if row["media_ext"] not in STILL_IMAGES:
        return "unsupported_media"
    if not row["md5"]:
        return "missing_md5"
    if not row.get("preview_url"):
        return "missing_preview"
    return "available"


def eligible(row):
    return (
        not row.get("project_conflict")
        and row["state"] in {"positive", "negative"}
        and availability(row) == "available"
    )


def groups(rows, *, threshold=4, artists=False, series=False):
    """Union exact MD5, dHash neighbors, and optional artist/copyright tags.

    A BK-tree indexes unique 64-bit hashes; identical hashes share one node.
    A hard comparison budget bounds adversarial worst-case tree traversal.
    """
    parent = {row["post_id"]: row["post_id"] for row in rows}

    def root(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(a, b):
        a, b = root(a), root(b)
        parent[max(a, b)] = min(a, b)

    keys, hashes = {}, {}
    for row in rows:
        post_id = row["post_id"]
        identifiers = [("md5", row["md5"])] if row["md5"] else []
        tags = json.loads(row["tags_json"])
        for category, enabled in (("artist", artists), ("copyright", series)):
            if enabled:
                identifiers.extend(
                    (category, tag)
                    for tag in tags.get(category, [])
                    if tag not in {"unknown_artist", "anonymous_artist", "third-party_edit", "conditional_dnp"}
                )
        for key in identifiers:
            if key in keys:
                union(post_id, keys[key])
            else:
                keys[key] = post_id
        if row.get("dhash"):
            value = int(row["dhash"], 16)
            if value in hashes:
                union(post_id, hashes[value])
            else:
                hashes[value] = post_id
    tree = None
    comparisons = 0
    for value, post_id in sorted(hashes.items()):
        if tree is None:
            tree = (value, post_id, {})
            continue
        stack = [tree]
        while stack:
            other, other_id, children = stack.pop()
            distance = (value ^ other).bit_count()
            comparisons += 1
            if comparisons > 1_000_000:
                raise HTTPException(422, "Duplicate analysis exceeded its comparison budget; use a smaller project")
            if distance <= threshold:
                union(post_id, other_id)
            stack.extend(
                child for edge, child in children.items() if distance - threshold <= edge <= distance + threshold
            )
        node = tree
        while True:
            distance = (value ^ node[0]).bit_count()
            if distance not in node[2]:
                node[2][distance] = (value, post_id, {})
                break
            node = node[2][distance]
    return {post_id: root(post_id) for post_id in parent}


def summarize(rows):
    labels = Counter(row["state"] or "unreviewed" for row in rows)
    states = Counter(availability(row) for row in rows)
    md5s, sources = defaultdict(list), Counter()
    for row in rows:
        if row["md5"]:
            md5s[row["md5"]].append(row)
        sources.update({source["kind"] for source in json.loads(row["provenance_json"])})
    clusters = Counter(groups(rows).values())
    conflicts = [
        sorted(item["post_id"] for item in values)
        for values in md5s.values()
        if {item["state"] for item in values} >= {"positive", "negative"}
    ]
    missing = [row["post_id"] for row in rows if eligible(row) and not row.get("dhash")]
    return {
        "total": len(rows),
        "labels": dict(labels),
        "availability": dict(states),
        "sources": dict(sources),
        "exact_duplicate_groups": sum(len(values) > 1 for values in md5s.values()),
        "exact_duplicate_posts": sum(max(0, len(values) - 1) for values in md5s.values()),
        "duplicate_groups": sum(count > 1 for count in clusters.values()),
        "fingerprinted": sum(bool(row.get("dhash")) for row in rows),
        "missing_fingerprints": len(missing),
        "prepare_ids": missing[:64],
        "conflicting_groups": len(conflicts),
        "conflict_samples": conflicts[:20],
        "unsure_samples": [row["post_id"] for row in rows if row["state"] == "unsure"][:20],
    }


def manifest_samples(rows, options):
    clusters = groups(rows, threshold=options.near_hamming, artists=options.group_artists, series=options.group_series)
    exact = defaultdict(list)
    for row in rows:
        if row["md5"] and row["state"] in {"positive", "negative"}:
            exact[row["md5"]].append(row)
    conflicting = {md5 for md5, values in exact.items() if {row["state"] for row in values} >= {"positive", "negative"}}
    representatives = {
        md5: min(row["post_id"] for row in values if eligible(row))
        for md5, values in exact.items()
        if any(eligible(row) for row in values)
    }
    samples = []
    for row in rows:
        state = row["state"] or "unreviewed"
        available = availability(row)
        reason = None
        if row.get("project_conflict"):
            reason = "conflicting_duplicate_project_reviews"
        elif state not in {"positive", "negative"}:
            reason = state
        elif available != "available":
            reason = available
        elif row["md5"] in conflicting:
            reason = "conflicting_exact_labels"
        elif representatives[row["md5"]] != row["post_id"]:
            reason = "exact_duplicate"
        group_id = f"post:{clusters[row['post_id']]}"
        bucket = int(hashlib.sha256(f"{options.split_seed}:{group_id}".encode()).hexdigest()[:8], 16) % 100
        split = "train" if bucket < 80 else "validation" if bucket < 90 else "test"
        samples.append(
            {
                "post_id": row["post_id"],
                "label": state,
                "target": int(state == "positive") if reason is None else None,
                "review_version": row["review_version"],
                "reviewed_at": row["reviewed_at"],
                "event_id": row["event_id"],
                "md5": row["md5"],
                "media_url": row["file_url"],
                "media_ext": row["media_ext"],
                "relative_path": None,
                "availability": available,
                "group": group_id,
                "split": split if reason is None else None,
                "excluded_reason": reason,
                "dhash": row.get("dhash"),
                "provenance": json.loads(row["provenance_json"]),
            }
        )
    return samples
