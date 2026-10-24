import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from home_media.config import Settings
from home_media.state import State
from scripts import family_backup


def test_monthly_backup_due_boundaries():
    now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    assert family_backup.due(None, now)
    assert family_backup.due((now - timedelta(days=29, hours=23)).isoformat(), now) is False
    assert family_backup.due((now - timedelta(days=30)).isoformat(), now)
    assert family_backup.due((now - timedelta(days=92)).isoformat(), now)


def test_backup_layout_rejects_nested_repository(tmp_path):
    source = tmp_path / "media"
    source.mkdir()
    nested_repo = source / "backup-repository"

    with pytest.raises(ValueError, match="separate, non-nested"):
        family_backup.validate_layout(source, nested_repo)


def test_uuid_mismatch_refuses_backup(monkeypatch, tmp_path):
    source = tmp_path / "media"
    source.mkdir()
    monkeypatch.setattr(family_backup, "command", lambda *args, **kwargs: "wrong-uuid\n")

    with pytest.raises(RuntimeError, match="not on expected filesystem"):
        family_backup.require_uuid(source, "expected-uuid")


def test_failed_backup_does_not_advance_last_success(monkeypatch, tmp_path):
    source = tmp_path / "media"
    source.mkdir()
    repository = tmp_path / "repo"
    repository.mkdir()
    password_file = tmp_path / "password"
    password_file.write_text("secret\n")
    old_success = "2026-09-01T12:00:00+00:00"
    state_path = tmp_path / "state.json"
    state = {"usb": {"last_success": old_success}}
    config = {
        "source": str(source),
        "source_uuid": "source-uuid",
        "password_file": str(password_file),
        "binary": "kopia",
        "state": str(state_path),
    }
    target = {
        "name": "usb",
        "repository": str(repository),
        "uuid": "repo-uuid",
        "config": str(tmp_path / "kopia.config"),
        "keep_latest": 2,
        "keep_monthly": 12,
        "reserve_gib": 0,
    }
    monkeypatch.setattr(family_backup, "require_uuid", lambda *_: None)
    monkeypatch.setattr(
        family_backup.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 10**12})()
    )

    def fail_verification(*args, **kwargs):
        if "verify" in args:
            raise RuntimeError("verification failed")
        if "create" in args and "snapshot" in args:
            return json.dumps({"id": "snapshot-id", "rootEntry": {"summary": {"numFailed": 0}}})
        return ""

    monkeypatch.setattr(family_backup, "command", fail_verification)
    with pytest.raises(RuntimeError, match="verification failed"):
        family_backup.backup_target(config, target, state, force=True)

    assert state["usb"]["last_success"] == old_success
    assert not state_path.exists()


def test_space_guard_terminates_backup_when_reserve_is_breached(monkeypatch, tmp_path):
    class FakeProcess:
        def __init__(self):
            self.communicate_calls = 0
            self.terminated = False
            self.killed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise family_backup.subprocess.TimeoutExpired("kopia", timeout)
            return "", ""

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    process = FakeProcess()
    monkeypatch.setattr(family_backup.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        family_backup.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=99),
    )

    with pytest.raises(RuntimeError, match="free-space reserve"):
        family_backup.command("kopia", "snapshot", "create", space_guard=(tmp_path, 100))

    assert process.terminated is True
    assert process.killed is False
    assert process.communicate_calls == 2


def test_verified_backup_persists_completion_timestamp_and_snapshot_id(monkeypatch, tmp_path):
    source = tmp_path / "media"
    source.mkdir()
    repository = tmp_path / "repo"
    repository.mkdir()
    password_file = tmp_path / "password"
    password_file.write_text("secret\n")
    state_path = tmp_path / "state.json"
    config_path = tmp_path / "kopia.config"
    config_path.touch()
    config = {
        "source": str(source),
        "source_uuid": "source-uuid",
        "password_file": str(password_file),
        "binary": "kopia",
        "state": str(state_path),
    }
    target = {
        "name": "usb",
        "repository": str(repository),
        "uuid": "repo-uuid",
        "config": str(config_path),
        "keep_latest": 2,
        "keep_monthly": 12,
        "reserve_gib": 0,
    }
    monkeypatch.setattr(family_backup, "require_uuid", lambda *_: None)
    monkeypatch.setattr(family_backup.shutil, "disk_usage", lambda _: SimpleNamespace(free=10**12))
    commands = []

    def successful_command(*args, **kwargs):
        commands.append((args, kwargs))
        if "snapshot" in args and "create" in args:
            return json.dumps(
                {"id": "verified-snapshot-123", "rootEntry": {"summary": {"numFailed": 0}}}
            )
        return ""

    monkeypatch.setattr(family_backup, "command", successful_command)
    state = {}
    before = datetime.now(timezone.utc)

    family_backup.backup_target(config, target, state, force=True)

    after = datetime.now(timezone.utc)
    persisted = json.loads(state_path.read_text())["usb"]
    timestamp = datetime.fromisoformat(persisted["last_success"])
    assert before <= timestamp <= after
    assert persisted["snapshot"] == "verified-snapshot-123"
    assert persisted["error"] is None
    assert state["usb"] == persisted
    assert any("verify" in args for args, _kwargs in commands)


def test_adult_roots_reject_path_traversal(tmp_path):
    config_path = tmp_path / "settings.toml"
    config_path.write_text(
        'stash_url = "http://localhost"\n'
        'stash_key = "test-key"\n'
        'adult_roots = ["/media/adult", "/media/../family"]\n'
    )

    with pytest.raises(ValueError, match="adult_roots"):
        Settings.load(str(config_path))


def test_prediction_cache_gzip_round_trip(tmp_path):
    state = State(tmp_path / "state.sqlite")
    prediction = {"entities": [{"id": "42", "score": 0.875}], "model": "test"}
    try:
        state.put("scene:42", prediction)
        assert state.get("scene:42") == prediction
        assert state.get("missing") is None
    finally:
        state.db.close()


@pytest.mark.parametrize(
    "focus_setting",
    [
        'focus_categories = "fetish"',
        'focus_categories = ["", "action"]',
        'focus_categories = [42]',
    ],
)
def test_focus_categories_require_a_list_of_nonempty_names(tmp_path, focus_setting):
    config_path = tmp_path / "settings.toml"
    config_path.write_text(
        'stash_url = "http://localhost"\n'
        'stash_key = "test-key"\n'
        'adult_roots = ["/media/adult"]\n'
        + focus_setting
    )

    with pytest.raises(ValueError, match="focus_categories must be a list of nonempty names"):
        Settings.load(str(config_path))


def test_focus_categories_deduplicate_configured_names(tmp_path):
    config_path = tmp_path / "settings.toml"
    config_path.write_text(
        'stash_url = "http://localhost"\n'
        'stash_key = "test-key"\n'
        'adult_roots = ["/media/adult"]\n'
        'focus_categories = ["fetish", "action", "character", "fetish"]\n'
    )

    settings = Settings.load(str(config_path))

    assert settings.focus_categories == ("fetish", "action", "character")
