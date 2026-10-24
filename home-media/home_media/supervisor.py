"""On-demand telemetry for explicitly approved workers. Never runs while idle."""

import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from observability import events

from .progress import snapshot
from .state import worker_available, worker_lock
from .transient import cleanup_idle_media

ROOT = Path(__file__).resolve().parent.parent


def current_progress(directory, run_id):
    current = events.read_json(Path(directory) / f"progress-{run_id}-current.json")
    if current.get("stage") == "preparing" and current.get("entity"):
        name = re.sub(r"[^a-zA-Z0-9_-]", "_", current["entity"])
        prepared = events.read_json(Path(directory) / f"progress-{run_id}-{name}.json")
        if prepared.get("run_id") == run_id:
            return {**current, **prepared}
    return current


def outcome(directory, run_id, returncode):
    stop = events.read_json(Path(directory) / "worker-stop.json")
    if stop.get("run_id") == run_id:
        return "unknown" if stop.get("reason") == "monitoring_failure" else "cancelled"
    terminal = events.read_json(Path(directory) / "observability" / f"outcome-{run_id}.json")
    if terminal.get("run_id") == run_id:
        return terminal["outcome"]
    return "failed" if returncode else "unknown"


def start_collector(config):
    binary, path = Path(config["binary"]), Path(config["config"])
    if not binary.is_file() or not path.is_file():
        raise FileNotFoundError("Collector is not configured")
    options = {}
    if os.name == "nt":
        from observability.windows import startup_options
        options = startup_options()
    collector_env = {**os.environ, "HOME_MEDIA_RUN_ID": config.get("run_id", ""),
                     "HOME_MEDIA_MONITOR_SINCE": events.utc_now().replace("+00:00", "Z")}
    diagnostic = path.parent / "collector-startup.log"
    if diagnostic.exists() and diagnostic.stat().st_size > 2 * 1024**2:
        diagnostic.unlink()
    with diagnostic.open("ab") as output:
        process = subprocess.Popen([str(binary), "-c", str(path)], stdin=subprocess.DEVNULL, env=collector_env,
                                   stdout=output, stderr=subprocess.STDOUT, **options)
    job = None
    try:
        if os.name == "nt":
            from observability.windows import OwnedProcessJob
            job = OwnedProcessJob(process)
        if process.poll() is not None:
            raise RuntimeError("Collector exited during startup")
        return process, job
    except Exception:
        process.kill()
        process.wait(timeout=5)
        raise


def prepare_spool(config, directory):
    spool = Path(config.get("spool", directory / "monitoring/spool")).resolve()
    if not spool.is_relative_to(directory.resolve()):
        raise ValueError("Collector spool must stay inside the worker data directory")
    spool.mkdir(parents=True, exist_ok=True)
    log = spool / "fluent-bit.log"
    if log.exists() and log.stat().st_size >= 4 * 1024**2:
        (spool / "fluent-bit.log.2").unlink(missing_ok=True)
        older = spool / "fluent-bit.log.1"
        if older.exists():
            older.replace(spool / "fluent-bit.log.2")
        log.replace(older)
    if shutil.disk_usage(directory).free < 5 * 1024**3:
        raise OSError("Less than five GiB free for collector buffering")
    return spool


def stop_collector(process, job, grace=30):
    if process is None:
        return
    deadline = time.monotonic() + grace
    try:
        # Give the last source events one flush interval before graceful shutdown.
        if process.poll() is None:
            time.sleep(min(2, grace / 4))
            if os.name == "nt":
                from observability.windows import interrupt
                interrupt(process)
            else:
                process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic() - 1))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
    finally:
        if job is not None:
            job.close()


def supervise(settings, arguments, requests, *, interval=60, collector_config=None):
    """Called only by an explicit CLI start or an approved fixed queue launch."""
    directory = settings.state.parent
    directory.mkdir(parents=True, exist_ok=True)
    with worker_lock(directory / "monitoring-run.lock"):
        if not worker_available(settings):
            raise RuntimeError("Another worker is already running")
        telemetry = directory / "observability"
        telemetry.mkdir(exist_ok=True)
        events.cleanup(telemetry)
        run_id = uuid.uuid4().hex
        writer = events.configure(telemetry, run_id, producer="supervisor")
        # These are references to the already-approved snapshot, not a second queue.
        approved = [{"kind": r.kind, "identity": r.identity, "request_id": r.request_id}
                    for r in requests or []]
        events.atomic_json(telemetry / f"approval-{run_id}.json",
                           {"run_id": run_id, "approved_at": events.utc_now(),
                            "requests": approved, "manual": requests is None})
        writer.emit("approved", approved_count=len(approved), manual=requests is None)
        for request in approved:
            writer.emit("approved_request", entity=request["kind"] + ":" + request["identity"], **request)
        collector, job, worker, worker_job = None, None, None, None
        returncode = 1
        interrupted_outcome = None
        try:
            config = collector_config or events.read_json(directory / "monitoring.json")
            config = {**config, "run_id": run_id}
            try:
                prepare_spool(config, directory)
                collector, job = start_collector(config)
            except (OSError, ValueError, KeyError, RuntimeError) as error:
                writer.emit("collector_unavailable", "warning", error_code=type(error).__name__)
                logging.warning("Monitoring collector unavailable; evidence will remain local")
            child_env = {**os.environ, "HOME_MEDIA_RUN_ID": run_id,
                         "HOME_MEDIA_TELEMETRY_DIR": str(telemetry.resolve())}
            command = [sys.executable, "-u", "-m", "home_media.cli", *arguments,
                       "--worker-process"]
            worker = subprocess.Popen(command, cwd=ROOT, env=child_env,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.name == "nt":
                from observability.windows import OwnedProcessJob
                worker_job = OwnedProcessJob(worker)
            writer.emit("worker_launched", pid=worker.pid)
            while worker.poll() is None:
                # Reuse the existing DB queries without opening the worker lock.
                try:
                    progress = snapshot(settings, active=True)
                except (OSError, ValueError, sqlite3.Error):
                    progress = {"counts": {}}
                    writer.emit("snapshot_unavailable", "warning")
                checkpoint = current_progress(telemetry, run_id)
                writer.emit("heartbeat", worker_present=True, counts=progress["counts"],
                            **checkpoint)
                if collector is not None and collector.poll() is not None:
                    writer.emit("collector_unavailable", "warning", error_code="exited")
                    collector = None
                    if job is not None:
                        job.close()
                        job = None
                try:
                    worker.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    pass
            returncode = worker.returncode
            result = outcome(directory, run_id, returncode)
            writer.emit("run_finished", "error" if result == "failed" else "info",
                        outcome=result, returncode=returncode)
            return returncode
        except BaseException as error:
            interrupted_outcome = "cancelled" if isinstance(error, KeyboardInterrupt) else "unknown"
            writer.emit("supervisor_error", "error", error_code=type(error).__name__)
            if worker is not None and worker.poll() is None:
                events.atomic_json(directory / "worker-stop.json", {
                    "run_id": run_id, "requested_at": events.utc_now(),
                    "reason": "user" if interrupted_outcome == "cancelled" else "monitoring_failure"})
                try:
                    worker.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    if worker_job is not None:
                        worker_job.close()
                    else:
                        worker.kill()
                    worker.wait(timeout=3)
            raise
        finally:
            if worker_job is not None:
                worker_job.close()
            # Forced worker exits cannot execute their TemporaryDirectory finalizers.
            # Never touch a newly started worker's files if it already owns the lock.
            try:
                cleanup_idle_media(settings.state)
            except (OSError, RuntimeError, ValueError) as error:
                writer.emit("media_cleanup_failed", "error", error_code=type(error).__name__)
                logging.error("Temporary media cleanup needs attention")
            writer.emit("monitoring_stopped", outcome=interrupted_outcome or outcome(directory, run_id, returncode))
            stop_collector(collector, job)
            writer.close()
