import json
from types import SimpleNamespace

import pytest

from home_media.queueing import (
    QUEUE_FIELD,
    QueueRequest,
    load_requests,
    pending_requests,
    queue_record,
    save_requests,
    update_request,
)


def test_approved_requests_round_trip_as_json(tmp_path):
    requests = [
        QueueRequest("image", "image-1", "request-1"),
        QueueRequest("scene", "scene-2", "request-2"),
    ]
    path = tmp_path / "approved.json"

    save_requests(path, requests)

    assert load_requests(path) == requests


@pytest.mark.parametrize("content", ['{"kind": "image"}', '[{"kind": "image"}]'])
def test_load_requests_rejects_malformed_or_non_list_snapshots(tmp_path, content):
    path = tmp_path / "approved.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises((ValueError, TypeError)):
        load_requests(path)


def test_load_requests_rejects_duplicate_media_identities(tmp_path):
    path = tmp_path / "approved.json"
    row = {"kind": "image", "identity": "same", "request_id": "r1"}
    path.write_text(json.dumps([row, {**row, "request_id": "r2"}]), encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate approved media IDs"):
        load_requests(path)


def test_queue_record_handles_json_objects_and_malformed_values():
    assert queue_record({"custom_fields": {QUEUE_FIELD: '{"status":"queued"}'}}) == {
        "status": "queued"
    }
    assert queue_record({"custom_fields": {QUEUE_FIELD: "not-json"}}) == {}
    assert queue_record({"custom_fields": {QUEUE_FIELD: ["not", "an", "object"]}}) == {}


def test_pending_requests_filters_status_and_allowlist():
    def item(identity, status, path, *, kind="image", request_id=None):
        return {
            "id": identity,
            "custom_fields": {
                QUEUE_FIELD: {
                    "status": status,
                    "request_id": request_id if request_id is not None else f"request-{identity}",
                }
            },
            "visual_files" if kind == "image" else "files": [{"path": path}],
        }

    images = [
        item("queued", "queued", "/media/adult/queued.jpg"),
        item("running", "running", "/media/adult/running.jpg"),
        item("done", "done", "/media/adult/done.jpg"),
        item("outside", "queued", "/media/family/private.jpg"),
        item("bad-id", "queued", "/media/adult/bad.jpg", request_id=""),
    ]
    scene = item("scene", "queued", "/media/adult/scene.mp4", kind="scene")

    class FakeStash:
        def items(self, kind):
            return images if kind == "image" else [scene]

    settings = SimpleNamespace(allowed=lambda path: path.startswith("/media/adult/"))

    assert pending_requests(FakeStash(), settings) == [
        QueueRequest("image", "queued", "request-queued"),
        QueueRequest("image", "running", "request-running"),
        QueueRequest("scene", "scene", "request-scene"),
    ]


@pytest.mark.parametrize(
    "record",
    [
        {"request_id": "newer", "status": "queued"},
        {"request_id": "request-1", "status": "cancelled"},
    ],
)
def test_update_request_does_not_overwrite_newer_or_cancelled_request(record):
    class FakeStash:
        def __init__(self):
            self.updates = []

        def item(self, _kind, _identity):
            return {"custom_fields": {QUEUE_FIELD: record}}

        def update(self, *args):
            self.updates.append(args)

    stash = FakeStash()
    request = QueueRequest("image", "image-1", "request-1")

    assert update_request(stash, request, "done") is False
    assert stash.updates == []


def test_update_request_writes_only_queue_custom_field_partial():
    current = {"request_id": "request-1", "status": "running", "keep": "value"}

    class FakeStash:
        def __init__(self):
            self.updates = []

        def item(self, _kind, _identity):
            return {"custom_fields": {QUEUE_FIELD: current}}

        def update(self, *args):
            self.updates.append(args)

    stash = FakeStash()
    request = QueueRequest("image", "image-1", "request-1")

    assert update_request(stash, request, "done") is True
    assert len(stash.updates) == 1
    kind, identity, values = stash.updates[0]
    assert (kind, identity) == ("image", "image-1")
    assert set(values) == {"custom_fields"}
    assert set(values["custom_fields"]) == {"partial"}
    assert set(values["custom_fields"]["partial"]) == {QUEUE_FIELD}
    updated = json.loads(values["custom_fields"]["partial"][QUEUE_FIELD])
    assert updated["status"] == "done"
    assert updated["request_id"] == "request-1"
    assert updated["keep"] == "value"
    assert "updated_at" in updated
