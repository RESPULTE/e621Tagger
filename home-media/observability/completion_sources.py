"""Read successful media completions without starting uploads, scans or processing."""

import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from .api import ApiError, request
from .events import read_json


def epoch(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def file_bytes(value):
    if type(value) is int and value >= 0:
        return value
    return None


class CompletionSources:
    def __init__(self, notifications, writer, config=None):
        self.notices, self.writer = notifications, writer
        self.store, self.db = notifications.incidents, notifications.db
        self.config = config if config is not None else read_json("/etc/homelab-monitor/notifications.json")
        self.immich_token = None
        self.db.execute("CREATE TABLE IF NOT EXISTS jellyfin_seen (id TEXT PRIMARY KEY)")
        self.db.commit()

    @property
    def enabled(self):
        return {"hydra"} | {name for name, settings in self.config.items() if settings.get("enabled")}

    def poll(self):
        busy = set()
        for service, operation in (("unmanic", self.unmanic), ("immich", self.immich),
                                   ("jellyfin", self.jellyfin)):
            if not self.config.get(service, {}).get("enabled"):
                continue
            try:
                if operation():
                    busy.add(service)
            except (ApiError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                # Missing visibility must not flush a batch that may still be running.
                busy.add(service)
                self.writer.emit("completion_source_unavailable", "warning", service="observability",
                                 source=service, error_code=type(error).__name__)
        return busy

    def unmanic(self):
        config = self.config["unmanic"]
        feed = Path(config["feed"])
        offsets = self.store.state("completion/unmanic_offsets", {})
        current = {}
        for path in (feed.with_name(feed.name + ".1"), feed):
            if not path.exists():
                continue
            stat = path.stat()
            identity = f"{stat.st_dev}:{stat.st_ino}"
            offset = offsets.get(identity, 0)
            if offset > stat.st_size:
                offset = 0
            with path.open("rb") as stream:
                stream.seek(offset)
                for _ in range(10000):
                    line = stream.readline(65537)
                    if not line or not line.endswith(b"\n"):
                        break
                    if len(line) > 65536:
                        raise ValueError("Completion record too large")
                    row = json.loads(line)
                    self.notices.completed({"service": "unmanic", "id": row.get("job_id"),
                                            "completed_at": row.get("completed_at"),
                                            "images": 0, "videos": 1,
                                            "bytes": row.get("output_bytes"),
                                            "input_bytes": row.get("input_bytes")})
                    offset = stream.tell()
            current[identity] = offset
        self.store.set_state("completion/unmanic_offsets", current)
        # The native success event fires after the final file move. Hold the
        # summary while any queued/active tasks remain in Unmanic's own state.
        with closing(sqlite3.connect(Path(config["database"]).as_uri() + "?mode=ro", uri=True)) as db:
            queued = db.execute("SELECT count(*) FROM tasks").fetchone()[0]
        return queued > 0

    def immich_request(self, path, payload=None):
        config = self.config["immich"]
        if not self.immich_token:
            account = read_json(config["credentials"])[config["account"]]
            self.immich_token = request(config["url"] + "/auth/login",
                                        {"email": account["username"], "password": account["password"]})["accessToken"]
        try:
            return request(config["url"] + path, payload,
                           headers={"Authorization": "Bearer " + self.immich_token})
        except ApiError as error:
            if error.code == 401:
                self.immich_token = None
            raise

    def immich_event(self, row):
        library = self.config["immich"]["library"]
        if row.get("libraryId", library) != library:
            return True  # Recovered archives and unrelated libraries are excluded.
        kind = row.get("type")
        if kind not in {"IMAGE", "VIDEO"} or row.get("isTrashed") or row.get("isOffline"):
            return True
        size = file_bytes((row.get("exifInfo") or {}).get("fileSizeInByte"))
        if size is None:
            return False
        self.notices.completed({"service": "immich", "id": row.get("id"),
                                "completed_at": epoch(row.get("createdAt")),
                                "images": int(kind == "IMAGE"), "videos": int(kind == "VIDEO"),
                                "bytes": size})
        return True

    def immich(self):
        now = time.time()
        pending = self.store.state("completion/immich_pending", {})
        for identity in list(pending)[:20]:
            try:
                row = self.immich_request("/assets/" + identity)
            except ApiError as error:
                if error.code == 404:
                    del pending[identity]
                    continue
                raise
            if self.immich_event(row):
                del pending[identity]
            else:
                # Rotate unresolved metadata so one broken asset cannot starve
                # later uploads. Keep its ID until measured or deleted upstream.
                first_seen = pending.pop(identity)
                pending[identity] = first_seen
        if pending and now - min(pending.values()) > 86400:
            warned = self.store.state("completion/immich_pending_warning", 0)
            if now - warned > 86400:
                self.writer.emit("completion_metadata_pending", "warning", service="observability",
                                 source="immich", pending_files=len(pending))
                self.store.set_state("completion/immich_pending_warning", now)
        cursor = self.store.state("completion/immich_scan")
        if not cursor:
            after = self.store.state("completion/immich_cursor", self.notices.since)
            cursor = {"after": max(self.notices.since, after - 300), "end": now - 5, "page": 1}
        if cursor["end"] <= cursor["after"]:
            return bool(pending)
        for _ in range(3):
            response = self.immich_request("/search/metadata", {
                "libraryId": self.config["immich"]["library"],
                "createdAfter": utc(cursor["after"]), "createdBefore": utc(cursor["end"]),
                "withExif": True, "page": cursor["page"], "size": 200})
            assets = response["assets"]
            for row in assets["items"]:
                if self.immich_event(row):
                    pending.pop(row["id"], None)
                else:
                    if row["id"] not in pending and len(pending) >= 1000:
                        raise ApiError("completion_backlog_limit")
                    pending.setdefault(row["id"], now)
            # Persist metadata retries before advancing their source page, so a
            # restart cannot lose accepted-but-not-yet-measurable uploads.
            self.store.set_state("completion/immich_pending", pending)
            next_page = assets.get("nextPage")
            if not next_page:
                self.store.set_state("completion/immich_cursor", cursor["end"])
                self.store.set_state("completion/immich_scan", None)
                break
            cursor["page"] = int(next_page)
            self.store.set_state("completion/immich_scan", cursor)
        self.store.set_state("completion/immich_pending", pending)
        return bool(pending) or bool(self.store.state("completion/immich_scan"))

    def jellyfin_request(self, path, params):
        config = self.config["jellyfin"]
        token = read_json(config["credentials"])["jellyfin_token"]
        return request(config["url"] + path + "?" + urlencode(params),
                       headers={"X-Emby-Token": token})

    def jellyfin_size(self, row):
        sources = row.get("MediaSources") or []
        if sources:
            size = file_bytes(sources[0].get("Size"))
            if size is not None:
                return size
        # Photos may have no MediaSources size. Map the selected library's
        # container path to its read-only host file and stat metadata only.
        path = row.get("Path")
        if isinstance(path, str):
            candidate = Path(path)
            for mapping in self.config["jellyfin"].get("mounts", []):
                try:
                    relative = candidate.relative_to(mapping["container"])
                    root = Path(mapping["host"]).resolve()
                    target = (root / relative).resolve()
                    if target.is_relative_to(root) and target.is_file():
                        return target.stat().st_size
                except (OSError, ValueError):
                    continue
        return None

    def jellyfin(self):
        busy = False
        for library in self.config["jellyfin"].get("libraries", []):
            key = "completion/jellyfin_scan/" + library
            cursor = self.store.state(key, {"offset": 0})
            initialized = self.store.state("completion/jellyfin_ready/" + library, False)
            more_pages = True
            for _ in range(3):
                result = self.jellyfin_request("/Items", {
                    "ParentId": library, "Recursive": "true", "StartIndex": cursor["offset"],
                    "Limit": 200, "Fields": "MediaSources,Path", "SortBy": "SortName",
                    "IncludeItemTypes": "Photo,Video,Movie,Episode"})
                rows = result["Items"]
                for row in rows:
                    identity = row["Id"]
                    if self.db.execute("SELECT 1 FROM jellyfin_seen WHERE id=?", (identity,)).fetchone():
                        continue
                    if initialized:
                        size = self.jellyfin_size(row)
                        if size is None:
                            busy = True
                            continue
                        self.notices.completed({"service": "jellyfin", "id": identity,
                                                "completed_at": time.time(),
                                                "images": int(row["Type"] == "Photo"),
                                                "videos": int(row["Type"] != "Photo"), "bytes": size})
                    self.db.execute("INSERT OR IGNORE INTO jellyfin_seen VALUES (?)", (identity,))
                self.db.commit()
                cursor["offset"] += len(rows)
                if cursor["offset"] >= result["TotalRecordCount"] or not rows:
                    self.store.set_state("completion/jellyfin_ready/" + library, True)
                    self.store.set_state(key, {"offset": 0})
                    more_pages = False
                    break
                self.store.set_state(key, cursor)
            busy = busy or more_pages
        return busy
