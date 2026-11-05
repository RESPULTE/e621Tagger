import hashlib
import json

import numpy as np
import pytest

from backend.broad_index import FORMAT, BroadIndex

MODEL, SPACE = "a" * 64, "b" * 64


def snapshot(root, *, valid=True):
    raw = np.array([[2, 0, 0], [1, 1, 0], [0, 2, 1], [-2, 0, 1], [0, -2, 0]], dtype="<f2")
    ids = np.array([10, 20, 30, 40, 50], dtype="<i8")
    mean = raw.astype(np.float64).mean(0).astype(np.float32)
    for name, value in (("raw_vectors.npy", raw), ("post_ids.npy", ids), ("mean.npy", mean)):
        np.save(root / name, value)
    manifest = {"format": FORMAT, "model_revision": MODEL, "native_query_space": SPACE,
                "dataset_revision": "pinned-fixture", "count": len(ids), "dimension": 3,
                "validation": {"passed": valid}, "files": {
                    name: {"bytes": (root / name).stat().st_size,
                           "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
                    for name in ("raw_vectors.npy", "post_ids.npy", "mean.npy")}}
    (root / "manifest.json").write_text(json.dumps(manifest))
    return raw, ids, mean


def test_broad_exact_centered_image_search_matches_numpy_and_excludes_overrides(tmp_path):
    raw, ids, mean = snapshot(tmp_path)
    index = BroadIndex(tmp_path, model_revision=MODEL, query_space=SPACE, dimension=3, block_rows=2)
    query = index.transform(raw[1])
    values = raw.astype(np.float32) - mean
    values /= np.linalg.norm(values, axis=1, keepdims=True)
    distances = np.linalg.norm(values - query, axis=1)
    truth = sorted(zip(distances.tolist(), ids.tolist()))
    results = index.search(query, limit=5)
    assert [r["post_id"] for r in results] == [post for _, post in truth]
    assert np.allclose([r["distance"] for r in results], [d for d, _ in truth], atol=0.0005)
    assert all(r["md5"] is None for r in results)  # current hashes must be hydrated by the server
    assert index.contains([1, 10, 50, 51]).tolist() == [False, True, True, False]
    assert [r["post_id"] for r in index.search(query, limit=5, exclude_post=20, override_ids=[30])] == [
        post for _, post in truth if post not in (20, 30)]
    with pytest.raises(ValueError, match="raw query"):
        index.transform([float("nan"), 0, 1])
    index.close()


@pytest.mark.parametrize("failure", ["validation", "model", "space", "corruption"])
def test_broad_index_refuses_unvalidated_incompatible_or_changed_files(tmp_path, failure):
    snapshot(tmp_path, valid=failure != "validation")
    if failure == "corruption":
        path = tmp_path / "raw_vectors.npy"
        value = path.read_bytes()
        path.write_bytes(value[:-1] + bytes([value[-1] ^ 1]))
    with pytest.raises(ValueError):
        BroadIndex(tmp_path, model_revision=MODEL if failure != "model" else "c" * 64,
                   query_space=SPACE if failure != "space" else "d" * 64, dimension=3)


def test_accelerator_candidates_are_reranked_against_original_image_vectors(tmp_path):
    snapshot(tmp_path)
    index = BroadIndex(tmp_path, model_revision=MODEL, query_space=SPACE, dimension=3)

    class Approximate:
        def search(self, query, count):
            # Deliberately scrambled approximate scores/order, with an empty slot.
            return np.zeros((1, 5)), np.array([[4, 3, 2, 1, 0, -1]])

    query = index.transform(index.vectors[1])
    exact = index.search(query, limit=5)
    index.ann = Approximate()
    index.manifest["search"] = {"rerank": 8192}
    reranked = index.search(query, limit=5)
    assert [r["post_id"] for r in reranked] == [r["post_id"] for r in exact]
    assert np.allclose([r["distance"] for r in reranked], [r["distance"] for r in exact], atol=0.0005)
    index.close()


def test_full_exact_validation_excludes_query_rows_and_scans_each_block(tmp_path):
    from backend.validate_broad_index import exact_truth, heldout_rows

    raw, ids, mean = snapshot(tmp_path)
    index = BroadIndex(tmp_path, model_revision=MODEL, query_space=SPACE, dimension=3, block_rows=2)
    rows = np.array([1, 3])
    _, truth, _ = exact_truth(index, rows, k=3)
    values = raw.astype(np.float32) - mean
    values /= np.linalg.norm(values, axis=1, keepdims=True)
    for row, found in zip(rows, truth):
        distances = np.linalg.norm(values - values[row], axis=1)
        expected = [int(ids[i]) for i in np.lexsort((ids, distances)) if i != row][:3]
        assert found.tolist() == expected
    with pytest.raises(ValueError, match="held-out"):
        heldout_rows(10)
    index.mean = mean + 0.1
    with pytest.raises(ValueError, match="mean does not match"):
        exact_truth(index, rows, k=3)
    index.close()
