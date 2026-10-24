"""Command-line entry point for the PC worker."""

import argparse
import logging
import os
import sys
from pathlib import Path

from observability import events

from .config import Settings
from .queueing import load_requests
from .tunnel import api_tunnel

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--upstream", default=str(ROOT.parent))
    parser.add_argument("--limit", type=int, help="Pilot batch item limit")
    parser.add_argument("--kind", choices=["all", "image", "scene"], default="all")
    parser.add_argument("--queue-file", type=Path, help="Locally approved request snapshot")
    parser.add_argument("--worker-process", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(events.SafeFormatter("%(asctime)s %(levelname)s %(message)s"))
    for name in ("httpx", "httpcore", "paramiko", "pyvips"):
        logging.getLogger(name).setLevel(logging.WARNING)
    settings = Settings.load(args.config)
    requests = load_requests(args.queue_file) if args.queue_file else None
    if not args.worker_process:
        from .supervisor import supervise
        raise SystemExit(supervise(settings, sys.argv[1:], requests))
    # Child-only imports keep the run supervisor free of inference libraries.
    from .worker import run
    writer = events.configure(os.environ["HOME_MEDIA_TELEMETRY_DIR"],
                              os.environ["HOME_MEDIA_RUN_ID"])
    logging.getLogger().addHandler(events.StructuredLogHandler())
    connected = False
    try:
        events.checkpoint("connecting", force=True)
        with api_tunnel(settings):
            connected = True
            _, failed = run(settings, args.upstream, args.limit, args.kind, requests)
        if failed:
            raise SystemExit(1)
    except BaseException as error:
        terminal = events.read_json(settings.state.parent / "observability" /
                                    f"outcome-{writer.run_id}.json")
        if not terminal:
            result = "deferred_offline" if not connected else "failed"
            events.atomic_json(settings.state.parent / "observability" /
                               f"outcome-{writer.run_id}.json",
                               {"run_id": writer.run_id, "outcome": result})
            events.emit("worker_start_failed", "error", error_code=type(error).__name__,
                        message=events.redact(error)[:500])
            logging.exception("Worker could not complete")
        raise
    finally:
        writer.close()


if __name__ == "__main__":
    main()
