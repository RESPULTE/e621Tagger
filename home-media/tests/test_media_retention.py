"""Prediction cache metadata must survive without retaining generated media covers."""

import gzip
import json

from home_media.state import State


def test_put_omits_cover_from_disk_without_mutating_publish_payload(tmp_path):
    state = State(tmp_path / "state.sqlite")
    payload = {
        "hash": "synthetic-hash",
        "model": "model-v7",
        "scores": {"tag-a": 0.812345, "tag-b": 0.25},
        "times": [0.0, 2.5, 9.75],
        "score_format": "probability-v1",
        "complete": True,
        "cover": "data:image/jpeg;base64,TEST",
    }

    state.put("new-key", payload)

    assert payload["cover"] == "data:image/jpeg;base64,TEST"
    assert state.get("new-key") == {
        "hash": "synthetic-hash",
        "model": "model-v7",
        "scores": {"tag-a": 0.812345, "tag-b": 0.25},
        "times": [0.0, 2.5, 9.75],
        "score_format": "probability-v1",
        "complete": True,
    }
    state.close()


def test_remove_media_scrubs_legacy_covers_and_preserves_prediction_metadata(tmp_path):
    state = State(tmp_path / "state.sqlite")
    preserved = {
        "hash": "synthetic-current",
        "model": "model-v7",
        "scores": {"general": 0.91, "character": 0.77},
        "times": [1.0, 4.5],
        "score_format": "probability-v1",
        "complete": True,
    }
    legacy = {
        "hash": "synthetic-legacy",
        "model": "model-v6",
        "scores": {"tag-a": 0.875, "tag-b": 0.125},
        "times": [0.25, 8.5],
        "score_format": "probability-v1",
        "complete": True,
        "cover": "data:image/jpeg;base64,TEST",
    }
    state.put("current-key", preserved)
    state.status("scene:synthetic-1", "done")
    state.remember_source("current-signature", "current-key")
    state.remember_source("legacy-signature", "legacy-key")
    # Seed a legacy row exactly as an earlier cache version would have written it.
    blob = gzip.compress(json.dumps(legacy, separators=(",", ":")).encode())
    state.db.execute(
        "INSERT INTO predictions VALUES(?,?,?)", ("legacy-key", blob, 123.0)
    )
    state.db.commit()

    assert state.remove_media() == 1

    legacy_without_cover = {key: value for key, value in legacy.items() if key != "cover"}
    assert state.get("legacy-key") == legacy_without_cover
    assert state.source("legacy-signature") == legacy_without_cover
    assert state.get("current-key") == preserved
    assert state.source("current-signature") == preserved
    assert state.job_status("scene:synthetic-1") == "done"
    assert state.remove_media() == 0

    stored_blob = state.db.execute(
        "SELECT payload FROM predictions WHERE key=?", ("legacy-key",)
    ).fetchone()[0]
    assert b"TEST" not in stored_blob
    state.close()
