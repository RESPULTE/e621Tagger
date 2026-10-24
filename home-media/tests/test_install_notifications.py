import json
import sqlite3
from pathlib import PurePosixPath

import pytest

from observability import install_notifications as installer


def test_adult_roots_map_to_narrow_container_directories(tmp_path):
    media = tmp_path / "media"
    config = tmp_path / "unmanic-config"
    first = media / "adult-one"
    second = media / "adult-two"
    for directory in (first, second, config):
        directory.mkdir(parents=True, exist_ok=True)
    mounts = [
        {"Type": "bind", "Source": str(config), "Destination": "/config"},
        {"Type": "bind", "Source": str(media), "Destination": "/library"},
    ]

    resolved = installer._adult_roots([str(second), str(first)])
    config_root, mapped = installer._map_adult_roots(resolved, mounts)

    assert config_root == config.resolve()
    assert mapped == [PurePosixPath("/library/adult-one"),
                      PurePosixPath("/library/adult-two")]


def test_adult_roots_reject_broad_unmapped_and_overlapping_paths(tmp_path):
    media = tmp_path / "media"
    config = tmp_path / "unmanic-config"
    media.mkdir()
    config.mkdir()
    child = media / "child"
    child.mkdir()
    mounts = [
        {"Type": "bind", "Source": str(config), "Destination": "/config"},
        {"Type": "bind", "Source": str(media), "Destination": "/library"},
    ]

    with pytest.raises(RuntimeError, match="below /library"):
        installer._map_adult_roots([media.resolve()], mounts)
    with pytest.raises(RuntimeError, match="not covered"):
        installer._map_adult_roots([tmp_path.resolve()], mounts)
    with pytest.raises(RuntimeError, match="must not overlap"):
        installer._adult_roots([str(media), str(child)])


def test_library_assignment_preserves_existing_plugin_settings_and_appends_once():
    baseline = {
        "library_config": {"id": 1, "name": "Default", "path": "/library",
                           "enable_scanner": False, "tags": ["keep"]},
        "plugins": {"enabled_plugins": [
            {"plugin_id": "existing", "name": "Existing", "has_config": True},
        ]},
    }
    exported = [{"plugin_id": "existing", "has_config": True,
                 "settings": {"token": "preserve"}}]

    payload = installer._library_payload(baseline, exported)

    assert payload["library_config"] == baseline["library_config"]
    assert payload["plugins"]["enabled_plugins"][0] == {
        "plugin_id": "existing", "name": "Existing", "has_config": True,
        "settings": {"token": "preserve"},
    }
    added = payload["plugins"]["enabled_plugins"][1]
    assert added["plugin_id"] == installer.PLUGIN_ID
    assert added["library_id"] == installer.LIBRARY_ID
    assert added["has_config"] is False
    assert baseline["plugins"]["enabled_plugins"][0] == {
        "plugin_id": "existing", "name": "Existing", "has_config": True,
    }
    already_exported = exported + [{"plugin_id": installer.PLUGIN_ID, "has_config": False}]
    assert len(installer._library_payload(payload, already_exported)["plugins"]["enabled_plugins"]) == 2


def test_settings_update_changes_only_the_three_completion_values():
    rows = [
        {"key": "Enable completion feed", "key_id": "opaque-1", "value": False,
         "label": "Enable", "input_type": "checkbox"},
        {"key": "Adult library ID", "key_id": "opaque-2", "value": "2",
         "label": "Library", "input_type": "text"},
        {"key": "Adult media roots", "key_id": "opaque-3", "value": "",
         "label": "Roots", "input_type": "text"},
        {"key": "unrelated", "key_id": "opaque-4", "value": "preserve",
         "label": "Other", "input_type": "text"},
    ]

    updated = installer._set_settings(rows, [PurePosixPath("/library/adult")])

    assert updated[0]["value"] is True
    assert updated[1]["value"] == "1"
    assert json.loads(updated[2]["value"]) == ["/library/adult"]
    assert updated[3] == rows[3]
    assert [row["key_id"] for row in updated] == [row["key_id"] for row in rows]


def test_notification_config_scopes_immich_and_disables_unselected_jellyfin(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "NOTIFICATIONS", tmp_path / "notifications.json")
    monkeypatch.setattr(installer, "CONFIG", tmp_path)
    config = installer._notifications_config(
        "upload-library-id", tmp_path / "feed.ndjson", tmp_path / "unmanic.db"
    )

    assert config["unmanic"] == {
        "enabled": True, "feed": str(tmp_path / "feed.ndjson"),
        "database": str(tmp_path / "unmanic.db"),
    }
    assert config["immich"]["enabled"] is True
    assert config["immich"]["library"] == "upload-library-id"
    assert config["immich"]["account"] == "immich_viewer"
    assert config["jellyfin"]["enabled"] is False
    assert config["jellyfin"]["libraries"] == []
    assert config["jellyfin"]["credentials"] == str(tmp_path / "secrets.json")


def test_plugin_registration_detection_reads_existing_database_without_writing(tmp_path):
    database = tmp_path / "unmanic.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE plugins (plugin_id TEXT)")
        db.execute("INSERT INTO plugins VALUES (?)", (installer.PLUGIN_ID,))

    assert installer._plugin_registered(database)

    with sqlite3.connect(database) as db:
        assert db.execute("SELECT plugin_id FROM plugins").fetchall() == [(installer.PLUGIN_ID,)]
