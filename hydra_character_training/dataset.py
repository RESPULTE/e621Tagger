"""Read immutable Studio media exports without assigning or changing splits."""

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def file_digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate manifest key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class Sample:
    post_id: int
    path: Path
    target: int
    split: str
    group: str
    sha256: str
    md5: str
    size: int
    provenance: list


@dataclass(frozen=True)
class Dataset:
    manifest: dict
    manifest_sha256: str
    samples: tuple[Sample, ...]

    def split(self, name):
        return tuple(sample for sample in self.samples if sample.split == name)


def verify_media(sample):
    # Recheck before extraction too: a manifest check must not authorize changed bytes.
    if not sample.path.is_file() or sample.path.stat().st_size != sample.size:
        raise ValueError(f"Missing or changed media length: {sample.post_id}")
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with sample.path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            sha.update(block)
            md5.update(block)
    if sha.hexdigest() != sample.sha256 or md5.hexdigest() != sample.md5:
        raise ValueError(f"Media hash mismatch: {sample.post_id}")


def read_dataset(path, label):
    path = Path(path)
    if path.is_dir():
        path /= "manifest.json"
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Manifest exceeds 16 MiB")
    raw = path.read_bytes()
    manifest = json.loads(raw, object_pairs_hook=_unique_object)
    # Reject NaN/Infinity as well as ambiguous JSON keys.
    canonical(manifest)
    if manifest.get("schema_version") != 1 or manifest.get("media_export_version") != 1:
        raise ValueError("Expected schema_version=1, media_export_version=1; download a media bundle")
    UUID(manifest["dataset_id"])
    if not re.fullmatch(r"[0-9a-f]{64}", manifest.get("source_manifest_sha256", "")):
        raise ValueError("Missing frozen source manifest digest")
    if not re.fullmatch(r"[^\s\x00-\x1f]+", label):
        raise ValueError("Label must be one exact tag")
    if manifest["project"]["display_name"] != label:
        raise ValueError("Label differs from frozen project display_name")
    policy = manifest["policy"]
    if policy.get("version") != "studio-split-v1" or not isinstance(policy.get("split_seed"), str):
        raise ValueError("Unsupported or missing frozen split policy")
    rows = manifest["samples"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError("Expected 1–5000 supervised media samples")
    samples, ids, paths, hashes, md5s, groups = [], set(), set(), set(), set(), {}
    root = path.parent.resolve()
    for row in rows:
        post_id, target, split = row["post_id"], row["target"], row["split"]
        if type(post_id) is not int or post_id < 1 or post_id in ids:
            raise ValueError("Invalid or repeated post ID")
        ids.add(post_id)
        if type(target) is not int or target not in (0, 1) or row["label"] != ("positive" if target else "negative"):
            raise ValueError(f"Expected a human binary label: {post_id}")
        if row.get("excluded_reason") is not None or row["availability"] != "available":
            raise ValueError(f"Excluded/unavailable sample: {post_id}")
        if type(row["review_version"]) is not int or row["review_version"] < 1 or not row.get("event_id"):
            raise ValueError(f"Missing review evidence: {post_id}")
        group = row["group"]
        if not isinstance(group, str) or not re.fullmatch(r"post:[1-9][0-9]*", group):
            raise ValueError("Missing frozen duplicate/artist group")
        bucket = int(hashlib.sha256(f"{policy['split_seed']}:{group}".encode()).hexdigest()[:8], 16) % 100
        expected = "train" if bucket < 80 else "validation" if bucket < 90 else "test"
        if split != expected or groups.setdefault(group, split) != split:
            raise ValueError(f"Inconsistent frozen split: {post_id}")
        relative = row["relative_path"]
        if not isinstance(relative, str) or "\\" in relative or ":" in relative:
            raise ValueError("Missing or unsafe relative media path")
        parts = PurePosixPath(relative)
        if parts.is_absolute() or ".." in parts.parts or not parts.parts:
            raise ValueError("Unsafe media path")
        media = (root / relative).resolve()
        if not media.is_relative_to(root) or media in paths:
            raise ValueError("Escaping or duplicate media path")
        paths.add(media)
        if row["media_ext"] not in {"jpg", "jpeg", "png", "webp"} or media.suffix.lower() != "." + row["media_ext"]:
            raise ValueError("Unsupported/mismatched media extension")
        sha, md5, size = row["sha256"], row["md5"], row["bytes"]
        if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
                or not isinstance(md5, str) or not re.fullmatch(r"[0-9a-f]{32}", md5)
                or type(size) is not int or size < 1):
            raise ValueError("Invalid media integrity fields")
        if sha in hashes or md5 in md5s:
            raise ValueError("Repeated image hash; export must contain duplicate representatives only")
        hashes.add(sha)
        md5s.add(md5)
        sample = Sample(post_id, media, target, split, group, sha, md5, size, row["provenance"])
        verify_media(sample)
        samples.append(sample)
    if manifest["counts"]["included"] != len(samples) or manifest["counts"]["splits"] != dict(Counter(s.split for s in samples)):
        raise ValueError("Manifest counts disagree with samples")
    for split in ("train", "validation", "test"):
        if {s.target for s in samples if s.split == split} != {0, 1}:
            raise ValueError(f"{split} needs both positive and negative reviewed samples; freeze more independent groups")
    return Dataset(manifest, hashlib.sha256(raw).hexdigest(), tuple(samples))
