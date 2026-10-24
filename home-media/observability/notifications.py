"""Concise Docker errors and durable batches of successfully completed media."""

import hashlib
import math
import time

LABELS = {
    "hydra": "Hydra tagging complete",
    "unmanic": "Unmanic processing complete",
    "immich": "Immich upload complete",
    "jellyfin": "Jellyfin library updated",
}


def readable_size(size):
    size = max(0, int(size))
    scale, unit = 1000, "KB"
    for candidate, name in ((1_000_000, "MB"), (1_000_000_000, "GB")):
        if size >= candidate or round(size / scale, 1) >= 1000:
            scale, unit = candidate, name
    value = f"{size / scale:.3f}" if size < 1000 else f"{size / scale:.1f}"
    return value.rstrip("0").rstrip(".") + " " + unit


def media_counts(images, videos, *, photos=False):
    parts = []
    for count, word in ((images, "photo" if photos else "image"), (videos, "video")):
        if count:
            parts.append(f"{count} {word}{'s' if count != 1 else ''}")
    return ", ".join(parts)


class Notifications:
    def __init__(self, incidents, *, now=None):
        self.incidents = incidents
        self.db = incidents.db
        now = time.time() if now is None else now
        self.since = incidents.state("notifications/since")
        if self.since is None:
            # Starting this policy never replays the existing media library or
            # sends old verbose incidents that were already in the delivery queue.
            self.since = now
            self.db.execute("DELETE FROM outbox")
            incidents.set_state("notifications/since", now)
        self.db.execute("""CREATE TABLE IF NOT EXISTS completion_events (
            id TEXT PRIMARY KEY, service TEXT NOT NULL, completed REAL NOT NULL,
            images INTEGER NOT NULL, videos INTEGER NOT NULL, bytes INTEGER NOT NULL,
            input_bytes INTEGER, queued INTEGER NOT NULL DEFAULT 0)""")
        self.db.commit()

    def completed(self, event):
        """Accept authoritative successes; source adapters never export paths/names."""
        service = event.get("service")
        at = event.get("completed_at")
        images, videos = event.get("images", 0), event.get("videos", 0)
        size = event.get("bytes")
        if (service not in LABELS or not event.get("id")
                or not isinstance(at, (int, float)) or not math.isfinite(at) or at < self.since
                or not isinstance(size, int) or isinstance(size, bool) or size < 0
                or type(images) is not int or type(videos) is not int
                or min(images, videos) < 0 or images + videos == 0):
            return False
        # IDs carry no filename, account identifier or media title.
        identity = hashlib.sha256(f"{service}/{event['id']}".encode()).hexdigest()
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO completion_events "
            "(id,service,completed,images,videos,bytes,input_bytes) VALUES (?,?,?,?,?,?,?)",
            (identity, service, at, images, videos, size, event.get("input_bytes")))
        self.db.commit()
        return cursor.rowcount == 1

    def hydra(self, event):
        if (event.get("event_type") != "worker_outcome" or event.get("outcome") != "completed"
                or event.get("failed", 0) != 0):
            return False
        return self.completed({"id": event.get("run_id"), "service": "hydra",
                               "completed_at": event.get("source_epoch"),
                               "images": event.get("completed_images", 0),
                               "videos": event.get("completed_videos", 0),
                               "bytes": event.get("completed_bytes")})

    def docker(self, name, status, evidence, *, failures=3):
        return self.incidents.observe(
            "notify/docker/" + name, status, evidence, failures=failures,
            notification=f"Docker error · {name}\n{evidence}")

    def flush(self, *, now=None, busy=(), enabled=None, quiet=120):
        now = time.time() if now is None else now
        sent = 0
        for service, label in LABELS.items():
            if service in busy or (enabled is not None and service not in enabled):
                continue
            rows = self.db.execute(
                "SELECT id,completed,images,videos,bytes,input_bytes FROM completion_events "
                "WHERE service=? AND queued=0 ORDER BY completed", (service,)).fetchall()
            if not rows or (service != "hydra" and now - max(r[1] for r in rows) < quiet):
                continue
            images, videos = sum(r[2] for r in rows), sum(r[3] for r in rows)
            size = readable_size(sum(r[4] for r in rows))
            if service == "unmanic" and all(isinstance(r[5], int) and r[5] >= 0 for r in rows):
                size = readable_size(sum(r[5] for r in rows)) + " → " + size
            body = label + "\n" + media_counts(images, videos, photos=service in {"immich", "jellyfin"})
            body += " · " + size
            batch = hashlib.sha256("/".join(r[0] for r in rows).encode()).hexdigest()
            try:
                self.incidents.enqueue("completion/" + batch, "completion", body, now)
                self.db.executemany("UPDATE completion_events SET queued=1 WHERE id=?",
                                    [(r[0],) for r in rows])
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            sent += 1
        self.db.execute("DELETE FROM completion_events WHERE queued=1 AND completed<?",
                        (now - 14 * 86400,))
        self.db.commit()
        return sent
