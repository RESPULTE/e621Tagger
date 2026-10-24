"""Single-node health checks, correlated approved-run incidents and Telegram outbox."""

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode

from . import checks
from .api import ApiError, OpenObserve, deliver
from .completion_sources import CompletionSources
from .events import EventWriter, cleanup, read_json, redact
from .incidents import Incidents
from .notifications import Notifications
from .worker_state import apply_event, evaluate


def notify_watchdog():
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
        connection.connect(address)
        connection.sendall(b"READY=1\nWATCHDOG=1")


class Checker:
    def __init__(self, config, secrets):
        self.config, self.secrets = config, secrets
        storage = Path(config["storage"])
        if not storage.is_mount():
            raise RuntimeError("Monitoring storage is not mounted; refusing bare-directory writes")
        directory = storage / "checker"
        directory.mkdir(parents=True, exist_ok=True)
        self.incidents = Incidents(directory / "incidents.sqlite")
        self.notifications = Notifications(self.incidents)
        self.writer = EventWriter(storage / "events", "server", host="thinkserver", producer="checker")
        self.completions = CompletionSources(self.notifications, self.writer)
        self.api = OpenObserve(config, secrets)
        self.cursor = self.incidents.state("cursor", time.time() - 300)
        self.started = time.monotonic()
        self.health = {}
        self.docker_health = {}
        self.docker_errors = {}
        self.error_search_ok = False
        self.maintenance = {}

    def record(self, key, status, evidence, failures=3):
        if (self.maintenance.get("until", 0) > time.time()
                and key.split("/", 1)[-1] in self.maintenance.get("services", [])):
            status = "maintenance"
        now = time.time()
        stream = "homelab_events" if key.startswith(("hydra/", "monitor/")) else "homelab_logs"
        field, value = ("run_id", key.split("/", 1)[1]) if key.startswith("hydra/") else ("service", key.split("/", 1)[-1])
        value = value.replace("'", "''")
        sql = f'SELECT _timestamp,service,level,message FROM "{stream}" WHERE {field}=\'{value}\' ORDER BY _timestamp DESC LIMIT 100'
        params = {"org_identifier": self.api.organization, "stream_type": "logs", "stream": stream,
                  "from": int((now - 3600) * 1e6), "to": int((now + 300) * 1e6),
                  "query": base64.b64encode(sql.encode()).decode(), "sql_mode": "true"}
        link = self.config["openobserve_url"] + "/web/logs?" + urlencode(params)
        evidence = redact(evidence) + "\nPrivate log search: " + link
        if key.startswith(("docker/", "unit/", "app/")):
            self.health[key.split("/", 1)[-1]] = status
        if key.startswith("docker/"):
            self.docker_health[key.split("/", 1)[1]] = (status, evidence)
        transition = self.incidents.observe(key, status, evidence, failures=failures)
        self.writer.emit("check", level="error" if status == "unhealthy" else "info",
                         service="observability", check=key, status=status,
                         message=evidence, transition=transition)

    def worker_events(self):
        end = time.time()
        sql = ('SELECT event_id,source_epoch,event_type,run_id,outcome,stage,entity,request_id,'
               'progress_at,progress_value,worker_present,level,message,failed,'
               'completed_images,completed_videos,completed_bytes FROM "homelab_events" '
               "WHERE service='hydra' ORDER BY _timestamp ASC")
        scan = self.incidents.state("scan") or {"start": self.cursor - 120, "end": end, "offset": 0}
        for _ in range(10):
            batch = self.api.search(sql, scan["start"], scan["end"], offset=scan["offset"])
            for event in batch:
                if not event.get("event_id") or not self.incidents.unseen(event["event_id"]):
                    continue
                run_id = event.get("run_id", "")
                if len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
                    continue
                key = "run/" + run_id
                state = self.incidents.state(key, {})
                if event.get("source_epoch", 0) > end + 300:
                    self.record("monitor/laptop_clock", "unhealthy", "Laptop clock is ahead; run state is unknown.")
                    continue
                self.notifications.hydra(event)
                self.incidents.set_state(key, apply_event(state, event))
                request_id = event.get("request_id")
                if request_id and event.get("event_type") in {"item_completed", "item_failed"}:
                    previous = self.incidents.state("request/" + request_id, {})
                    if event.get("source_epoch", 0) >= previous.get("at", 0):
                        self.incidents.set_state("request/" + request_id,
                                                 {"at": event["source_epoch"], "status": event["event_type"]})
                    entity = event.get("entity")
                    if entity:
                        latest = self.incidents.state("entity/" + entity, {})
                        if event["source_epoch"] >= latest.get("at", 0):
                            self.incidents.set_state("entity/" + entity,
                                                     {"at": event["source_epoch"], "status": event["event_type"]})
            if len(batch) < 500:
                self.cursor = scan["end"]
                self.incidents.set_state("cursor", self.cursor)
                self.incidents.set_state("scan", None)
                break
            scan["offset"] += len(batch)
            self.incidents.set_state("scan", scan)
        for key, payload in self.incidents.db.execute("SELECT key,payload FROM state WHERE key LIKE 'run/%'"):
            state = json.loads(payload)
            requests = state.get("requests", [])
            def completed_after_retry(request_id):
                if self.incidents.state("request/" + request_id, {}).get("status") == "item_completed":
                    return True
                entity = state.get("entities", {}).get(request_id)
                latest = self.incidents.state("entity/" + entity, {}) if entity else {}
                return latest.get("status") == "item_completed" and latest.get("at", 0) > state.get("terminal_at", 0)

            retry_completed = state.get("outcome") == "failed" and requests and all(
                completed_after_retry(request_id) for request_id in requests)
            status, evidence = evaluate(state, end)
            if retry_completed:
                status, evidence = "healthy", "Every approved request in the failed run completed in an explicitly approved retry."
            self.record("hydra/" + key[4:], status, evidence, failures=1)
            # Retain terminal correlation for 30 days, active/unknown runs for reconciliation.
            if state.get("terminal_at", end) < end - 30 * 86400:
                self.incidents.db.execute("DELETE FROM state WHERE key=?", (key,))
        self.incidents.db.commit()

    def log_errors(self):
        end = time.time()
        sql = ('SELECT service,count(*) AS errors,max(_timestamp) AS latest FROM "homelab_logs" '
               "WHERE source_type='docker' AND level IN ('error','critical','fatal') GROUP BY service")
        groups = self.api.search(sql, end - 300, end, size=200)
        try:
            latest = self.api.search('SELECT max(_timestamp) AS latest FROM "homelab_logs" '
                                     "WHERE source_type='docker'", end - 600, end, size=1)
        except ApiError:
            latest = []
        # A working search endpoint does not prove the Docker feed is flowing.
        # Quiet-but-stale logs leave an active episode unknown until fresh data.
        self.error_search_ok = bool(latest and latest[0].get("latest")
                                    and end - latest[0]["latest"] / 1e6 < 180)
        observed = set()
        for group in groups:
            service = group.get("service")
            if service not in self.config["containers"] or group.get("errors", 0) < 5:
                continue
            self.docker_errors[service] = group["errors"]
            key = "log_errors/" + service
            observed.add(key)
            self.incidents.set_state(key, end)
            self.record(key, "unhealthy", f"{group['errors']} error-level records in five minutes. "
                        "Inspect this service's logs and dependency health.", 1)
        states = self.incidents.db.execute(
            "SELECT key,payload FROM state WHERE key LIKE 'log_errors/%'"
        ).fetchall()
        for key, payload in states:
            if key in observed:
                continue
            service = key.split("/", 1)[1]
            if (self.error_search_ok and end - json.loads(payload) >= 600
                    and self.health.get(service) == "healthy"):
                self.record(key, "healthy", "Service probe is healthy and errors have been quiet for ten minutes.")

    def docker_notifications(self):
        now = time.time()
        for name in self.config["containers"]:
            status, evidence = self.docker_health.get(name, ("unknown", "Container state unavailable."))
            errors = self.docker_errors.get(name, 0)
            last = self.incidents.state("notification_error/" + name, 0)
            failures = 3
            if errors:
                self.incidents.set_state("notification_error/" + name, now)
                status, evidence, failures = "unhealthy", f"{errors} error records in five minutes.", 1
            elif status == "unhealthy":
                evidence = "Container stopped or unhealthy."
            elif last and (not self.error_search_ok or now - last < 600):
                status = "unknown"
            else:
                evidence = "Container healthy; recent error threshold is clear."
            if (self.maintenance.get("until", 0) > now
                    and name in self.maintenance.get("services", [])):
                status = "maintenance"
            self.notifications.docker(name, status, evidence, failures=failures)

    def cycle(self):
        self.health = {}
        self.docker_health = {}
        self.docker_errors = {}
        self.error_search_ok = False
        self.maintenance = read_json("/var/lib/homelab-monitor/maintenance.json")
        # Only identities and names are exported; never container environment/labels.
        try:
            mapping = checks.container_mapping(self.config["containers"])
            Path("/etc/homelab-monitor/containers.map").write_text(mapping)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            self.record("monitor/container_mapping", "unknown",
                        f"Container identities unavailable ({type(error).__name__}); previous mapping retained.")
        self.writer.emit("checker_canary", service="observability", source_type="health")
        sample = checks.resource_sample()
        previous = self.incidents.state("resources", sample)
        total = sum(sample["cpu_ticks"]) - sum(previous["cpu_ticks"])
        if total > 0:
            idle = sample["cpu_ticks"][3] - previous["cpu_ticks"][3]
            wait = sample["cpu_ticks"][4] - previous["cpu_ticks"][4]
            self.writer.emit("resources", service="observability", source_type="health",
                             cpu_percent=round(100 * (total-idle-wait) / total, 2),
                             iowait_percent=round(100 * wait / total, 2),
                             memory_available_gib=sample["memory_available_gib"],
                             root_free_gib=sample["root_free_gib"],
                             telemetry_free_gib=sample["telemetry_free_gib"],
                             swapin_pages=sample["swapin"]-previous["swapin"],
                             swapout_pages=sample["swapout"]-previous["swapout"])
        self.incidents.set_state("resources", sample)
        tasks = [lambda: checks.docker_checks(self.config["containers"]),
                 lambda: checks.unit_checks(self.config["units"]),
                 lambda: checks.backup_checks(self.config["backup_state"], self.config["backup_targets"]),
                 lambda: checks.host_checks(self.config), lambda: [checks.backup_runtime()],
                 lambda: [checks.stash_check(self.secrets)], lambda: [checks.dns_check()]]
        tasks.extend(lambda check=c: [checks.http_check(check)] for c in self.config["http_checks"])
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(task) for task in tasks]
            for future in as_completed(futures):
                try:
                    for key, status, evidence in future.result():
                        if time.monotonic() - self.started < 300 and status == "unhealthy":
                            status = "maintenance"
                        self.record(key, status, evidence)
                except Exception as error:
                    self.record("monitor/probe_execution", "unknown",
                                f"Probe could not complete: {type(error).__name__}. Inspect local journal.")
        try:
            self.worker_events()
            self.log_errors()
            canary = self.api.search('SELECT source_epoch FROM "homelab_events" '
                                     "WHERE event_type='checker_canary' ORDER BY _timestamp DESC LIMIT 1",
                                     time.time() - 600, time.time(), size=1)
            fresh = canary and time.time() - canary[0].get("source_epoch", 0) < 180
            self.record("monitor/ingestion", "healthy" if fresh else "unhealthy",
                        "End-to-end server canary " + ("is searchable." if fresh else "is missing/stale."))
            self.record("monitor/search", "healthy", "OpenObserve search succeeds.")
        except ApiError as error:
            self.record("monitor/search", "unhealthy", f"OpenObserve query failed ({error.code}).")
        self.docker_notifications()
        busy = self.completions.poll()
        self.notifications.flush(busy=busy, enabled=self.completions.enabled)
        # Last-resort hard stop protects the preallocated telemetry filesystem.
        disk = shutil.disk_usage(self.config["storage"])
        if disk.free / disk.total < 0.10:
            self.record("monitor/storage_stop", "unhealthy",
                        "Telemetry volume below 10% free; OpenObserve stopped. Operator recovery required.", 1)
            checks.command("systemctl", "stop", "homelab-openobserve.service", timeout=20)
        result = deliver(self.incidents, self.secrets, now=time.time())
        self.writer.emit("notification_delivery", service="observability", **result)
        self.incidents.prune()
        cleanup(Path(self.config["storage"]) / "events", limit=64 * 1024**2,
                exclude=(self.writer.handler.baseFilename,))
        notify_watchdog()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/homelab-monitor/config.json")
    parser.add_argument("--secrets", default="/etc/homelab-monitor/secrets.json")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    secrets = json.loads(Path(args.secrets).read_text())
    checker = Checker(config, secrets)
    while True:
        started = time.monotonic()
        checker.cycle()
        if args.once:
            return
        time.sleep(max(1, 60 - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
