import subprocess
import sys
from pathlib import Path

import pytest

from home_media import connection, supervisor, transient
from home_media.config import Settings
from home_media.state import worker_lock


def state_path(tmp_path):
    return tmp_path / "worker.sqlite"


def marked_root(state):
    root = transient.media_root(state)
    root.mkdir(parents=True, exist_ok=True)
    (root / ".owner").write_text(transient.OWNER, encoding="utf-8")
    return root


def test_cleanup_removes_only_owned_processing_directories_and_preserves_other_data(
    tmp_path,
):
    state = state_path(tmp_path)
    root = marked_root(state)
    owned = root / "home-media-run-a"
    owned.mkdir()
    (owned / "temporary.bin").write_bytes(b"synthetic scratch")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    outside_file = unrelated / "keep.bin"
    outside_file.write_bytes(b"preserve")

    assert transient.cleanup_media(state) == 1

    assert not owned.exists()
    assert (root / ".owner").read_text(encoding="utf-8") == transient.OWNER
    assert outside_file.read_bytes() == b"preserve"


def test_workspace_cleans_orphans_at_start_and_removes_files_after_success(tmp_path):
    state = state_path(tmp_path)
    root = marked_root(state)
    orphan = root / "home-media-orphan"
    orphan.mkdir()
    (orphan / "old-scratch.bin").write_bytes(b"orphan")

    with transient.media_workspace(state) as workspace:
        assert workspace == root
        assert not orphan.exists()
        active = workspace / "home-media-current"
        active.mkdir()
        scratch = active / "temporary.bin"
        scratch.write_bytes(b"synthetic workspace file")

    assert not active.exists()
    assert (root / ".owner").is_file()


def test_workspace_finally_cleans_real_temporary_file_after_exception(tmp_path):
    state = state_path(tmp_path)
    root = transient.media_root(state)
    scratch = None

    with pytest.raises(RuntimeError, match="synthetic processing failure"):
        with transient.media_workspace(state) as workspace:
            current = workspace / "home-media-failed-run"
            current.mkdir()
            scratch = current / "partial-download.bin"
            scratch.write_bytes(b"synthetic partial output")
            raise RuntimeError("synthetic processing failure")

    assert scratch is not None
    assert not scratch.exists()
    assert (root / ".owner").is_file()


def test_unmarked_processing_root_is_refused_without_deleting_contents(tmp_path):
    state = state_path(tmp_path)
    root = transient.media_root(state)
    unmarked = root / "home-media-orphan"
    unmarked.mkdir(parents=True)
    sentinel = unmarked / "preserve.bin"
    sentinel.write_bytes(b"must remain")

    with pytest.raises((OSError, ValueError)):
        transient.cleanup_media(state)

    assert sentinel.read_bytes() == b"must remain"


def test_unexpected_nonmatching_entry_is_refused_without_deleting_it(tmp_path):
    state = state_path(tmp_path)
    root = marked_root(state)
    unexpected = root / "do-not-remove.bin"
    unexpected.write_bytes(b"unrecognized")

    with pytest.raises(ValueError, match="Unexpected entry"):
        transient.cleanup_media(state)

    assert unexpected.read_bytes() == b"unrecognized"


def test_symlinked_processing_root_cannot_escape_state_directory(tmp_path):
    state = state_path(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.bin"
    sentinel.write_bytes(b"outside")
    link = tmp_path / "processing-media"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlinks are unavailable: {error}")

    with pytest.raises(ValueError, match="must stay inside"):
        transient.cleanup_media(state)

    assert sentinel.read_bytes() == b"outside"


def test_symlink_entry_is_refused_without_touching_its_target(tmp_path):
    state = state_path(tmp_path)
    root = marked_root(state)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    sentinel = outside / "keep.bin"
    sentinel.write_bytes(b"outside")
    link = root / "home-media-escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlinks are unavailable: {error}")

    with pytest.raises(ValueError, match="Unexpected entry"):
        transient.cleanup_media(state)

    assert sentinel.read_bytes() == b"outside"
    assert link.is_symlink()


def test_windows_junction_processing_root_cannot_escape_state_directory(tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows junction behavior only")
    state = state_path(tmp_path)
    outside = tmp_path / "outside-junction-target"
    outside.mkdir()
    sentinel = outside / "keep.bin"
    sentinel.write_bytes(b"outside")
    junction = tmp_path / "processing-media"
    command = (
        f"New-Item -ItemType Junction -Path '{junction}' -Target '{outside}' | Out-Null"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        pytest.skip(f"Could not create a temporary Windows junction: {result.stderr.strip()}")
    try:
        with pytest.raises(ValueError, match="must stay inside"):
            transient.cleanup_media(state)
        assert sentinel.read_bytes() == b"outside"
    finally:
        # rmdir removes the junction itself and never traverses into its target.
        junction.rmdir()


def test_cleanup_idle_media_leaves_workspace_while_worker_lock_is_held(tmp_path):
    state = state_path(tmp_path)
    root = marked_root(state)
    active = root / "home-media-active"
    active.mkdir()
    scratch = active / "working.bin"
    scratch.write_bytes(b"active worker")

    with worker_lock(state.with_suffix(".lock")):
        assert transient.cleanup_idle_media(state) is None
        assert scratch.read_bytes() == b"active worker"

    assert transient.cleanup_idle_media(state) == 1
    assert not active.exists()


def test_connection_cleans_orphan_before_duplicate_check_but_check_only_preserves_it(
    tmp_path, monkeypatch
):
    settings = Settings(
        stash_url="http://localhost",
        stash_key="test-key",
        state=state_path(tmp_path),
        adult_roots=("/media/adult",),
    )
    root = marked_root(settings.state)
    monkeypatch.setattr(connection, "worker_available", lambda _settings: True)
    monkeypatch.setattr(
        connection,
        "api_tunnel",
        lambda *_args: pytest.fail("duplicate connection must skip network access"),
    )

    orphan = root / "home-media-orphan"
    orphan.mkdir()
    (orphan / "partial.bin").write_bytes(b"stale")
    handled = connection.process_once(
        settings, Path("config.toml"), Path("upstream"), "same-connection",
        {"connection_id": "same-connection"},
    )
    assert handled["status"] == "already_handled"
    assert not orphan.exists()

    orphan.mkdir()
    (orphan / "partial.bin").write_bytes(b"stale")
    checked = connection.process_once(
        settings, Path("config.toml"), Path("upstream"), "same-connection",
        {"connection_id": "same-connection"}, check_only=True,
    )
    assert checked["status"] == "already_handled"
    assert (orphan / "partial.bin").is_file()


def test_supervisor_cleans_owned_workspace_after_forced_child_exit(tmp_path, monkeypatch):
    settings = Settings(
        stash_url="http://localhost",
        stash_key="test-key",
        state=state_path(tmp_path),
        adult_roots=("/media/adult",),
    )
    root = transient.media_root(settings.state)
    owned_file = root / "home-media-child" / "partial.bin"
    code = (
        "from pathlib import Path; import os; "
        f"root=Path({str(root)!r}); root.mkdir(parents=True, exist_ok=True); "
        f"(root/'.owner').write_text({transient.OWNER!r}, encoding='utf-8'); "
        "scratch=root/'home-media-child'; scratch.mkdir(); "
        "(scratch/'partial.bin').write_bytes(b'synthetic'); os._exit(17)"
    )
    real_popen = subprocess.Popen
    children = []

    def fixture_worker(_command, **kwargs):
        child = real_popen([sys.executable, "-c", code], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(supervisor.subprocess, "Popen", fixture_worker)
    monkeypatch.setattr(supervisor, "prepare_spool", lambda *_args: None)
    monkeypatch.setattr(supervisor, "start_collector", lambda _config: (None, None))

    assert supervisor.supervise(settings, [], None, interval=0.05) == 17

    assert children and children[0].returncode == 17
    assert not owned_file.exists()
    assert (root / ".owner").read_text(encoding="utf-8") == transient.OWNER
