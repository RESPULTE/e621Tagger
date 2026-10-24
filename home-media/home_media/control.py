"""Cooperative, per-run stop requests. No idle process or network listener."""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from observability import events


class StopRequested(Exception):
    """The user stopped this batch; the current item remains unfinished."""


class RunControl:
    def __init__(self, directory: Path):
        self.directory = directory
        self.run_id = os.environ.get("HOME_MEDIA_RUN_ID") or uuid.uuid4().hex
        if len(self.run_id) != 32 or any(c not in "0123456789abcdef" for c in self.run_id):
            raise ValueError("Invalid worker run ID")
        self.started_at = datetime.now(timezone.utc).isoformat()

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self._write("running")
        events.emit("worker_started")
        return self

    def _write(self, status):
        path = self.directory / "worker-control.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"pid": os.getpid(), "run_id": self.run_id,
                                        "started_at": self.started_at, "status": status}),
                             encoding="utf-8")
        temporary.replace(path)

    def check(self):
        try:
            request = json.loads((self.directory / "worker-stop.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if request.get("run_id") == self.run_id:
            if request.get("reason") == "monitoring_failure":
                raise StopRequested("Monitoring supervisor interrupted; current item remains unfinished")
            raise StopRequested("Stopped manually; current item will be retried")

    def __exit__(self, error_type, _error, _traceback):
        self._write("exited" if error_type is None else "failed")
