"""Online SQLite backup, safe while Studio runs (no raw WAL file copies)."""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def backup(root: Path, destination: Path):
    source = root.resolve() / "studio.sqlite3"
    destination = destination.resolve()
    if destination == source or destination.exists():
        raise ValueError("Choose a new backup path outside the active database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Connection context managers commit/rollback but do not close handles.
        # Closing the writer checkpoints its WAL before the snapshot is copied.
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(destination)) as writer:
                reader.backup(writer, pages=256, sleep=0.05)
                if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup failed SQLite integrity verification")
                if writer.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("Backup failed foreign-key verification")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    args = parser.parse_args()
    backup(args.data_root, args.destination)
