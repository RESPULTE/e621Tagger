"""Run on the processing PC: checksum-verified index, outbound lookup polling only."""

import argparse
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from .app.hydra_knn import HydraIndex
from .app.processing_machine import LookupJob


def run(server, directory, token, *, once=False):
    origin = urlsplit(server)
    if (
        origin.scheme not in {"http", "https"}
        or not origin.netloc
        or origin.username
        or origin.password
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
    ):
        raise ValueError("Server must be an exact HTTP(S) origin without credentials or a path")
    if len(token) < 32:
        raise ValueError("Worker token must contain at least 32 characters")
    index = HydraIndex(directory)
    print("Verifying processing machine index…", flush=True)
    index.open()
    worker_id = str(uuid4())
    pending, previous = None, None
    with httpx.Client(
        base_url=server.rstrip("/") + "/api/processing-machine/",
        headers={"Authorization": "Bearer " + token, "Origin": server.rstrip("/")},
        timeout=10,
        follow_redirects=False,
    ) as client:
        while True:
            failed = False
            try:
                if pending:
                    response = client.post("results", json=pending)
                    if response.status_code in {401, 403}:
                        raise PermissionError("Worker authorization rejected; update the token file and restart")
                    if 400 <= response.status_code < 500:
                        if response.status_code != 409:
                            print("Lookup result rejected; discarded. Check server/worker versions.", flush=True)
                    else:
                        response.raise_for_status()
                    pending = None
                response = client.post(
                    "poll",
                    json={
                        "worker_id": worker_id,
                        "ready": not bool(index.error),
                        "index_revision": index.revision,
                    },
                )
                if response.status_code in {401, 403}:
                    raise PermissionError("Worker authorization rejected; update the token file and restart")
                if response.status_code == 409:
                    # A restarted process must wait out its predecessor's heartbeat lease.
                    response.raise_for_status()
                if 400 <= response.status_code < 500:
                    raise ValueError("Worker poll rejected; check server/worker versions")
                response.raise_for_status()
                state = "Index unavailable; install/validate it, then restart this worker" if index.error else "Online"
                assignment = response.json().get("job")
                if assignment:
                    job = LookupJob.model_validate(assignment)
                    evidence = index.lookup_sync(job.post_id)
                    pending = {
                        **job.model_dump(mode="json"),
                        "worker_id": worker_id,
                        "coverage": evidence["coverage"],
                        "neighbors": evidence["neighbors"],
                    }
            except httpx.HTTPError as exc:
                failed = True
                state = (
                    "Another processing worker is online; waiting for its heartbeat to expire"
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409
                    else "Server unavailable; reconnecting"
                )
            if state != previous:
                print(state, flush=True)
                previous = state
            if once:
                return
            if not pending or failed:
                time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.server, args.index, args.token_file.read_text(encoding="utf-8").strip())
    except KeyboardInterrupt:
        print("Processing worker stopped")
    except (PermissionError, ValueError) as exc:
        print(str(exc))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
