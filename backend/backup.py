"""Online SQLite backup, safe while Studio runs (no raw WAL file copies)."""

import argparse
import sqlite3
from pathlib import Path


def backup(root: Path, destination: Path):
    source = root.resolve() / "studio.sqlite3"
    destination = destination.resolve()
    if destination == source or destination.exists():
        raise ValueError("Choose a new backup path outside the active database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as reader:
            with sqlite3.connect(destination) as writer:
                reader.backup(writer, pages=256, sleep=0.05)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    args = parser.parse_args()
    backup(args.data_root, args.destination)
