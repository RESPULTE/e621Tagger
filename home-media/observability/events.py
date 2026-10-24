"""Bounded structured evidence shared by the worker and server checks."""

import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_context = threading.local()
_writer = None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            output.write(json.dumps(value, separators=(",", ":")))
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def redact(text):
    text = str(text)
    text = re.sub(r"(?i)authorization[\s\"':=]+(?:basic|bearer)\s+[^\s,;\"'}]+",
                  "Authorization=[redacted]", text)
    text = re.sub(r"(?i)(authorization|apikey|api_key|token|password|cookie)([\s\"':=]+)[^\s,;\"}]+",
                  r"\1\2[redacted]", text)
    text = re.sub(r"(?i)https?://[^\s\"']+", "[url]", text)
    text = re.sub(r"(?i)[A-Z]:[\\/][^\r\n\"']+", "[local-path]", text)
    text = re.sub(r"/(?:media|home|mnt|DATA|var/backups)/[^\s\"']+", "[private-path]", text)
    return text[:16000]


class EventWriter:
    def __init__(self, directory, run_id, host="tagging-laptop", producer="worker"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.host = host
        self.producer = producer
        self.lock = threading.RLock()
        self.sequence = 0
        self.last_checkpoint = {}
        self.handler = RotatingFileHandler(
            self.directory / f"{producer}-{run_id}-{os.getpid()}.ndjson",
            maxBytes=8 * 1024**2, backupCount=2, encoding="utf-8",
        )
        self.handler.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, event_type, level="info", **fields):
        # Monitoring failure must not fail a media operation or change its success state.
        try:
            with self.lock:
                self.sequence += 1
                event = {
                    "schema_version": 1, "event_id": uuid.uuid4().hex,
                    "source_time": utc_now(), "source_epoch": time.time(),
                    "host": self.host, "service": "hydra", "source_type": "worker",
                    "run_id": self.run_id, "producer": self.producer,
                    "sequence": self.sequence, "event_type": event_type, "level": level,
                    **getattr(_context, "fields", {}), **fields,
                }
                self.handler.emit(logging.makeLogRecord({
                    "msg": json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                }))
                if event_type in {"approved", "approved_request", "worker_outcome",
                                  "run_finished", "monitoring_stopped"} and self.handler.stream:
                    os.fsync(self.handler.stream.fileno())
                return event
        except (OSError, ValueError):
            return None

    def checkpoint(self, stage, value=None, *, force=False):
        fields = getattr(_context, "fields", {})
        identity = fields.get("entity", "model")
        role = fields.get("role", "current")
        key = (identity, role, stage)
        now = time.monotonic()
        previous = self.last_checkpoint.get(key)
        if not force and previous and (previous[1] == value or now - previous[0] < 2):
            return
        self.last_checkpoint[key] = (now, value)
        try:
            data = {"run_id": self.run_id, **fields, "stage": stage,
                    "progress_at": time.time(), "progress_value": value}
            name = "current" if role == "current" else re.sub(r"[^a-zA-Z0-9_-]", "_", identity)
            atomic_json(self.directory / f"progress-{self.run_id}-{name}.json", data)
            self.emit("progress", **data)
        except OSError:
            pass

    def close(self):
        global _writer
        self.handler.close()
        if _writer is self:
            _writer = None


def configure(directory, run_id, producer="worker"):
    global _writer
    _writer = EventWriter(directory, run_id, producer=producer)
    return _writer


def emit(event_type, level="info", **fields):
    if _writer is not None:
        return _writer.emit(event_type, level, **fields)
    return None


def checkpoint(stage, value=None, *, force=False):
    if _writer is not None:
        _writer.checkpoint(stage, value, force=force)


def finish(outcome, completed=0, failed=0, **fields):
    if _writer is None:
        return
    try:
        reserved = {"run_id", "outcome", "completed", "failed"}
        data = {key: value for key, value in fields.items() if key not in reserved}
        data.update({"run_id": _writer.run_id, "outcome": outcome,
                     "completed": completed, "failed": failed})
        atomic_json(_writer.directory / f"outcome-{_writer.run_id}.json", data)
        emit("worker_outcome", "error" if outcome == "failed" else "info", **data)
    except OSError:
        pass


@contextmanager
def item(entity, request_id=None, role="current"):
    previous = getattr(_context, "fields", {})
    _context.fields = {"entity": entity, "request_id": request_id, "role": role}
    try:
        yield
    finally:
        _context.fields = previous


class StructuredLogHandler(logging.Handler):
    def emit(self, record):
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
        emit("application_log", record.levelname.lower(), message=redact(message))


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def cleanup(directory, limit=256 * 1024**2, max_age=7 * 86400, exclude=()):
    """Run only on approved startup; no idle cleaner or worker-state deletion."""
    directory = Path(directory)
    excluded = {Path(p).resolve() for p in exclude}
    files = sorted((p for p in directory.glob("*") if p.is_file() and not p.name.endswith(".tmp")
                    and p.resolve() not in excluded),
                   key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for path in files:
        size = path.stat().st_size
        if total > limit or time.time() - path.stat().st_mtime > max_age:
            path.unlink()
            total -= size
