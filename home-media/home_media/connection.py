"""One-shot network-connect queue check; never runs a resident listener or poll loop."""

import argparse
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from observability.events import SafeFormatter

from .config import Settings
from .queueing import pending_requests, save_requests
from .stash import Stash
from .state import worker_available, worker_lock
from .transient import cleanup_idle_media
from .tunnel import api_tunnel

ROOT = Path(__file__).resolve().parent.parent
LOG = logging.getLogger(__name__)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def prompt_for_batch(count: int, video_frames: int) -> bool:
    if os.name != "nt":
        raise RuntimeError("The approval prompt requires an interactive Windows desktop")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-STA", "-WindowStyle", "Hidden",
         "-File", str(ROOT / "scripts/prompt-tagging.ps1"), "-Count", str(count),
         "-VideoFrames", str(video_frames)],
        capture_output=True, text=True, creationflags=NO_WINDOW, check=True,
    )
    return result.stdout.strip() == "APPROVED"


def launch_approved(settings: Settings, config: Path, upstream: Path, requests) -> int:
    """Start only the fixed, approved batch; the one-shot request client then exits."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    snapshot = settings.state.parent / f"approved-queue-{stamp}.json"
    save_requests(snapshot, requests)
    executable = Path(sys.executable).with_name("python.exe")
    log_path = settings.state.parent / f"queued-run-{stamp}.log"
    with log_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [str(executable), "-u", "-m", "home_media.cli", "--config", str(config),
             "--upstream", str(upstream), "--queue-file", str(snapshot)],
            cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
        )
    LOG.info("Approved %d requests; worker PID %d, log %s", len(requests), process.pid, log_path.name)
    return process.pid


def process_once(settings: Settings, config: Path, upstream: Path, connection_id: str,
                 previous: dict, *, check_only: bool = False, manual: bool = False) -> dict:
    if not check_only:
        cleanup_idle_media(settings.state)
    if not manual and previous.get("connection_id") == connection_id:
        return {**previous, "status": "already_handled"}
    state = {"connection_id": connection_id}
    if not worker_available(settings):
        return {**state, "status": "worker_busy"}
    with api_tunnel(settings):
        stash = Stash(settings.stash_url, settings.stash_key)
        try:
            if not check_only:
                # Reuse the server task to discover newly indexed backlog on demand.
                stash.query(
                    'mutation{runPluginOperation(plugin_id:"home-media",args:{mode:"pending"})}'
                )
            requests = pending_requests(stash, settings)
        finally:
            stash.client.close()
    if not requests or check_only:
        return {**state, "status": "queued" if requests else "idle", "queued": len(requests)}
    if not prompt_for_batch(len(requests), settings.video_frames):
        LOG.info("Server request declined for this network connection")
        return {**state, "status": "declined", "queued": len(requests)}
    # Requests arriving after the prompt are excluded from this approval snapshot.
    pid = launch_approved(settings, config, upstream, requests)
    return {**state, "status": "started", "approved": len(requests), "pid": pid}


def write_status(path: Path, state: dict):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({**state, "checked_at": datetime.now(timezone.utc).isoformat()},
                                   indent=2), encoding="utf-8")
    temporary.replace(path)


def describe_result(state: dict) -> str:
    messages = {
        "worker_busy": "Tagging is already running. Open 'Check tagging progress' to see its progress.",
        "idle": "No unprocessed tagging requests are waiting on the home server.",
        "declined": "Processing deferred until the next network connection, or a manual request check.",
        "started": f"Started {state.get('approved', 0)} approved requests on the RTX 4070.",
        "unavailable": "The request check failed. See data/connection-request.log for details.",
        "already_handled": "Requests were already checked on this connection.",
    }
    return messages.get(state["status"], f"{state.get('queued', 0)} requests waiting.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--upstream", type=Path, default=ROOT.parent)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--manual", action="store_true", help="Explicitly check again on this connection")
    parser.add_argument("--check-only", action="store_true", help="Inspect; never prompt or process")
    args = parser.parse_args()
    config, upstream = args.config.resolve(), args.upstream.resolve()
    settings = Settings.load(str(config))
    settings.state.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(settings.state.parent / "connection-request.log",
                                  maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(message)s"))
    for name in ("httpx", "httpcore", "paramiko"):
        logging.getLogger(name).setLevel(logging.WARNING)
    status_path = settings.state.parent / "connection-request-state.json"
    with worker_lock(settings.state.with_name("connection-request.lock")):
        try:
            previous = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        try:
            state = process_once(settings, config, upstream, args.connection_id, previous,
                                 check_only=args.check_only, manual=args.manual)
        except Exception:
            LOG.exception("Server unavailable or request failed; no unapproved work started")
            state = {"status": "unavailable"}
        if args.check_only:
            print(json.dumps(state))
        elif state["status"] != "already_handled":
            write_status(status_path, state)
        if args.manual and not args.check_only:
            print(describe_result(state))


if __name__ == "__main__":
    main()
