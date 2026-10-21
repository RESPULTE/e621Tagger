"""Publish verified online backups outside the Docker volume; run on the Ubuntu host."""

import argparse
import fcntl
import os
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERIFY_VOLUME_BACKUP = """from pathlib import Path
import re, sqlite3, sys
current = Path(sys.argv[1])
with sqlite3.connect(current.as_uri() + '?mode=ro', uri=True) as database:
    assert database.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'Invalid volume backup'
copies = sorted(p for p in current.parent.iterdir() if not p.is_symlink() and p.is_file()
                and re.fullmatch(r'studio-\\d{8}T\\d{12}Z\\.sqlite3', p.name))
assert current in copies
# Retain three verified recovery generations even when repeated host copies fail.
for previous in copies[:-3]:
    previous.unlink()
"""


def backup_deployment(root: Path, retention_days: int):
    root = root.resolve(strict=True)
    compose = root / "studio-compose"
    if not compose.is_file() or not os.access(compose, os.X_OK):
        raise ValueError("Deployment needs an executable studio-compose wrapper")
    backups = root / "backups"
    if backups.is_symlink():
        raise ValueError("Backup directory must be inside this deployment")
    backups.mkdir(mode=0o700, exist_ok=True)
    backups.chmod(0o700)
    with (backups / ".backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        filename = f"studio-{stamp}.sqlite3"
        container_path = f"/data/backups/{filename}"
        subprocess.run(
            [str(compose), "exec", "-T", "studio", "python", "-m", "backend.backup", container_path,
             "--data-root", "/data"],
            check=True,
        )
        subprocess.run(
            [str(compose), "exec", "-T", "studio", "python", "-c", VERIFY_VOLUME_BACKUP, container_path],
            check=True,
        )
        with tempfile.TemporaryDirectory(prefix=".backup-", dir=backups) as temporary:
            staged = Path(temporary) / filename
            subprocess.run([str(compose), "cp", f"studio:{container_path}", str(staged)], check=True)
            staged.chmod(0o600)
            with sqlite3.connect(staged.as_uri() + "?mode=ro", uri=True) as database:
                if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise RuntimeError("Backup failed SQLite integrity_check")
            with staged.open("rb") as stream:
                os.fsync(stream.fileno())
            # Same-filesystem hard link publishes atomically and refuses to replace any file.
            destination = backups / filename
            os.link(staged, destination)
            descriptor = os.open(backups, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        # Keep the volume copy if host publication failed, so it can be recovered.
        subprocess.run(
            [str(compose), "exec", "-T", "studio", "python", "-c",
             "from pathlib import Path; import sys; Path(sys.argv[1]).unlink()", container_path],
            check=True,
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        for previous in backups.iterdir():
            if previous.is_symlink() or not previous.is_file():
                continue
            match = re.fullmatch(r"studio-(\d{8}T\d{12}Z)\.sqlite3", previous.name)
            if not match:
                continue
            created = datetime.strptime(match[1], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
            if created < cutoff and previous != destination:
                previous.unlink()
        print(f"Verified online backup: {destination.name}; retention {retention_days} days")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-root", type=Path, default=Path("/opt/hydra-studio"))
    parser.add_argument("--retention-days", type=int, default=30)
    args = parser.parse_args()
    if args.retention_days < 1:
        parser.error("--retention-days must be at least 1")
    os.umask(0o077)
    backup_deployment(args.deployment_root, args.retention_days)
