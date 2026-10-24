"""Write successful adult-library video completions to a private local feed."""

from __future__ import annotations

import json
import logging
import math
import os
import stat
import uuid
from pathlib import Path, PurePosixPath

from unmanic.libs.unplugins.settings import PluginSettings

LOGGER = logging.getLogger(__name__)
MAX_FEED_BYTES = 1024 * 1024
VIDEO_EXTENSIONS = frozenset({
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".mts", ".ogv", ".ts", ".vob", ".webm", ".wmv",
})


class Settings(PluginSettings):
    """Settings are scoped per library by Unmanic."""

    settings = {
        "Enable completion feed": False,
        "Adult library ID": "1",
        "Adult media roots": "",
    }


def _inside_root(path: object, root: Path) -> Path | None:
    if not isinstance(path, str) or not path:
        return None
    try:
        resolved = Path(os.path.realpath(path))
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def _timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _task_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _configured_library_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        library_id = int(value)
    except (TypeError, ValueError):
        return None
    return library_id if library_id > 0 else None


def _configured_media_roots(value: object) -> tuple[Path, ...]:
    """Parse a JSON array of adult roots; reject empty and shared mount roots."""
    if not isinstance(value, str) or not value.strip():
        return ()
    try:
        raw_roots = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(raw_roots, list) or not raw_roots:
        return ()

    roots = []
    for item in raw_roots:
        if not isinstance(item, str) or not os.path.isabs(item):
            return ()
        # This installation's /library bind includes non-adult content; require a
        # narrower per-category root even if the event library ID also matches.
        if PurePosixPath(item) in (PurePosixPath("/"), PurePosixPath("/library")):
            return ()
        root = Path(os.path.realpath(item))
        if os.name == "posix" and root in (Path("/"), Path("/library")):
            return ()
        roots.append(root)
    return tuple(roots)


def build_event(data: dict, library_id: int, media_roots: object) -> dict | None:
    """Return a path-free completion record, or None when scope/metadata is invalid."""
    if (data.get("library_id") != library_id
            or data.get("task_success") is not True
            or data.get("file_move_processes_success") is not True):
        return None
    roots = _configured_media_roots(media_roots)
    if not roots:
        return None

    try:
        source_path = data.get("source_data", {}).get("abspath")
        destination_path = data.get("destination_data", {}).get("abspath")
        source = next((path for root in roots
                       if (path := _inside_root(source_path, root)) is not None), None)
        destination = next((path for root in roots
                            if (path := _inside_root(destination_path, root)) is not None), None)
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    task_id = _task_id(data.get("task_id"))
    finished_at = _timestamp(data.get("finish_time"))
    started_at = _timestamp(data.get("start_time"))
    if (source is None or destination is None or source.suffix.lower() not in VIDEO_EXTENSIONS
            or task_id is None or finished_at is None or started_at is None):
        return None

    try:
        info = destination.stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size < 0:
        return None

    return {
        "event_type": "adult_video_processing_finished",
        "job_id": str(task_id),
        "completed_at": finished_at,
        "media_kind": "video",
        "input_bytes": None,
        "output_bytes": info.st_size,
    }


def _installation_id(profile: Path) -> uuid.UUID:
    """Keep job IDs stable for this Unmanic profile without exposing task IDs."""
    path = profile / "installation-id"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return uuid.UUID(path.read_text(encoding="ascii").strip())
    value = uuid.uuid4()
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(str(value))
        handle.flush()
        os.fsync(handle.fileno())
    return value


def _stable_job_id(profile: Path, task_id: int, started_at: float) -> str:
    namespace = _installation_id(profile)
    return str(uuid.uuid5(namespace, f"{task_id}:{float(started_at).hex()}"))


def _append_bounded(profile: Path, event: dict) -> None:
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    feed = profile / "completion-events.ndjson"
    archive = profile / "completion-events.ndjson.1"
    line = (json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    try:
        size = feed.stat().st_size
    except FileNotFoundError:
        size = 0
    if size > MAX_FEED_BYTES or len(line) > MAX_FEED_BYTES:
        feed.unlink(missing_ok=True)
        archive.unlink(missing_ok=True)
        size = 0
    if size + len(line) > MAX_FEED_BYTES:
        try:
            archive.unlink()
        except FileNotFoundError:
            pass
        try:
            os.replace(feed, archive)
        except FileNotFoundError:
            pass
    descriptor = os.open(feed, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def emit_postprocessor_complete(data: dict) -> dict:
    """Persist one successful in-scope event; never change Unmanic's event payload."""
    try:
        settings = Settings(library_id=data.get("library_id"))
        if not settings.get_setting("Enable completion feed"):
            return data
        configured_library = _configured_library_id(settings.get_setting("Adult library ID"))
        media_roots = settings.get_setting("Adult media roots")
        if configured_library is None:
            return data
        event = build_event(data, configured_library, media_roots)
        if event is None:
            return data
        profile = Path(settings.get_profile_directory())
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        started_at = _timestamp(data.get("start_time"))
        if started_at is None:
            return data
        event["job_id"] = _stable_job_id(profile, data["task_id"], started_at)
        _append_bounded(profile, event)
    except Exception:
        LOGGER.error("Could not write Unmanic completion event")
    return data
