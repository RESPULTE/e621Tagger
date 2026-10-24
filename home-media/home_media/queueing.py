"""Durable request metadata stored in Stash; media never moves into the queue."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

QUEUE_FIELD = "home_media_queue"
ACTIVE_STATUSES = {"queued", "running"}


class RequestInactive(RuntimeError):
    """An approved request was cancelled or replaced before publication."""


@dataclass(frozen=True)
class QueueRequest:
    kind: str
    identity: str
    request_id: str

    def __post_init__(self):
        if self.kind not in {"image", "scene"}:
            raise ValueError("Unsupported queued media type")
        if any(not isinstance(value, str) or not value or len(value) > 200
               for value in (self.identity, self.request_id)):
            raise ValueError("Queued IDs must be nonempty strings")


def queue_record(item: dict) -> dict:
    raw = item.get("custom_fields", {}).get(QUEUE_FIELD, {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def is_current_request(item: dict, request: QueueRequest) -> bool:
    record = queue_record(item)
    return (record.get("request_id") == request.request_id
            and record.get("status") in ACTIVE_STATUSES)


def pending_requests(stash, settings) -> list[QueueRequest]:
    requests = []
    for kind in ("image", "scene"):
        for item in stash.items(kind):
            record = queue_record(item)
            if record.get("status") not in ACTIVE_STATUSES:
                continue
            files = item["visual_files"] if kind == "image" else item["files"]
            if not files or not settings.allowed(files[0]["path"]):
                continue
            try:
                request = QueueRequest(kind, item["id"], record.get("request_id"))
            except ValueError:
                continue
            requests.append(request)
    return requests


def update_request(stash, request: QueueRequest, status: str, error: str | None = None) -> bool:
    """Do not acknowledge a cancelled request or overwrite a newer request ID."""
    item = stash.item(request.kind, request.identity)
    record = queue_record(item)
    if not is_current_request(item, request):
        return False
    record = {**record, "status": status,
              "updated_at": datetime.now(timezone.utc).isoformat()}
    record.pop("error", None)
    if error:
        record["error"] = error[:500]
    stash.update(request.kind, request.identity, {
        "custom_fields": {"partial": {QUEUE_FIELD: json.dumps(record, separators=(",", ":"))}}
    })
    return True


def save_requests(path: Path, requests: list[QueueRequest]):
    path.write_text(json.dumps([asdict(request) for request in requests]), encoding="utf-8")


def load_requests(path: Path) -> list[QueueRequest]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Approved requests must be a list")
    requests = [QueueRequest(**row) for row in raw]
    if len({(row.kind, row.identity) for row in requests}) != len(requests):
        raise ValueError("Duplicate approved media IDs")
    return requests
