"""Strictly scoped, disposable media workspace. Call under the worker lock."""

import shutil
from contextlib import contextmanager
from pathlib import Path

from .state import worker_lock

OWNER = "home-media disposable processing files\n"


def media_root(state: Path) -> Path:
    root = state.parent.resolve() / "processing-media"
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("Processing directory must stay inside the worker data directory")
    return root


def cleanup_media(state: Path) -> int:
    root = media_root(state)
    if not root.exists():
        return 0
    if (root / ".owner").read_text(encoding="utf-8") != OWNER:
        raise ValueError("Refusing to clean an unrecognized processing directory")
    paths = [path for path in root.iterdir() if path.name != ".owner"]
    for path in paths:
        if (not path.name.startswith("home-media-") or not path.is_dir()
                or path.is_symlink() or path.resolve() != path):
            raise ValueError("Unexpected entry in the processing directory; refusing deletion")
    for path in paths:
        shutil.rmtree(path)
    return len(paths)


def cleanup_idle_media(state: Path):
    """Leave active inference alone; clean crash leftovers during one-shot checks."""
    try:
        with worker_lock(state.with_suffix(".lock")):
            return cleanup_media(state)
    except RuntimeError:
        return None  # Another worker owns these files.


@contextmanager
def media_workspace(state: Path):
    root = media_root(state)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".owner"
    if not marker.exists() and not any(root.iterdir()):
        marker.write_text(OWNER, encoding="utf-8")
    cleanup_media(state)
    try:
        yield root
    finally:
        cleanup_media(state)
