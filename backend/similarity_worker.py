"""Run GPU image retrieval on the processing laptop with authenticated outbound polling."""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from .app.image_jobs import ImageJob


def run(server, token, image_engine, *, once=False):
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
    worker_id = str(uuid4())
    pending, previous, work = None, None, None
    with ThreadPoolExecutor(max_workers=1) as executor, httpx.Client(
        base_url=server.rstrip("/") + "/api/processing-machine/",
        headers={"Authorization": "Bearer " + token, "Origin": server.rstrip("/")},
        timeout=10,
        follow_redirects=False,
    ) as client:
        while True:
            failed = False
            try:
                if work and work.done():
                    pending, work = work.result(), None
                if pending:
                    response = client.post("results", json=pending)
                    if response.status_code in {401, 403}:
                        raise PermissionError("Worker authorization rejected; update the token file and restart")
                    if 400 <= response.status_code < 500:
                        if response.status_code != 409:
                            print("Image result rejected; discarded. Check server/worker versions.", flush=True)
                    else:
                        response.raise_for_status()
                    pending = None
                response = client.post("poll", json={
                    "protocol_version": 2, "worker_id": worker_id,
                    "image_capability": image_engine.capability, "busy": work is not None,
                })
                if response.status_code in {401, 403}:
                    raise PermissionError("Worker authorization rejected; update the token file and restart")
                if response.status_code == 409:
                    response.raise_for_status()
                if 400 <= response.status_code < 500:
                    raise ValueError("Worker poll rejected; check server/worker versions")
                response.raise_for_status()
                state = "Online (GPU image retrieval)"
                assignment = response.json().get("job")
                if assignment:
                    ImageJob.model_validate(assignment)
                    if work:
                        raise ValueError("Unexpected image assignment while busy")
                    work = executor.submit(image_engine.process, client, assignment, worker_id)
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
    parser.add_argument("--vector-index", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--broad-index", type=Path, help="Validated immutable full image-vector generation")
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args()
    image_engine = None
    try:
        token = args.token_file.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ValueError("Worker token must contain at least 32 characters")
        from .image_retrieval_worker import ImageEngine

        image_engine = ImageEngine(args.checkpoint, args.vector_index, broad_index=args.broad_index)
        run(args.server, token, image_engine)
    except KeyboardInterrupt:
        print("Processing worker stopped")
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc))
        raise SystemExit(1) from None
    finally:
        if image_engine:
            image_engine.close()


if __name__ == "__main__":
    main()
