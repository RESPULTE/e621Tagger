"""Explicitly initialize an existing collection root; never create a mountpoint."""

import argparse
import os
from pathlib import Path
from uuid import UUID, uuid4

from backend.filesystem import sync_directory


def initialize(root: Path):
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Choose an existing, mounted collection folder")
    marker = root / ".hydra-studio-library-id"
    if marker.is_symlink():
        raise ValueError("Refusing a symlinked collection identity")
    if marker.exists():
        if not marker.is_file() or marker.stat().st_size > 100:
            raise ValueError("Collection identity is invalid")
        UUID(marker.read_text(encoding="ascii").strip())
        return
    with marker.open("x", encoding="ascii") as stream:
        stream.write(str(uuid4()) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    initialize(parser.parse_args().root)
    print("Collection identity ready; existing media was not changed")
