"""Install only the three pinned KNN arrays, then validate them before use."""

import argparse
import hashlib
from pathlib import Path

import httpx

from .app.hydra_knn import validate
from .app.neighbor_index import BASE, FILES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    if not args.validate_only:
        with httpx.Client(follow_redirects=True, timeout=60) as client:
            for kind in FILES:
                name = f"hydra35_embed_{kind}.npy"
                destination = args.directory / name
                if destination.exists():
                    continue
                partial = destination.with_suffix(".partial")
                print(f"Downloading {name}", flush=True)
                digest, received = hashlib.sha256(), 0
                with client.stream("GET", BASE + name) as response, partial.open("wb") as stream:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1024 * 1024):
                        received += len(chunk)
                        if received > FILES[kind][2]:
                            raise ValueError(f"Download exceeds pinned file size: {name}")
                        digest.update(chunk)
                        stream.write(chunk)
                if received != FILES[kind][2] or digest.hexdigest() != FILES[kind][3]:
                    raise ValueError(f"Download checksum/size mismatch: {name}")
                partial.replace(destination)
    validate(args.directory)
    print("Hydra index verified. Use this directory with python -m backend.similarity_worker on the processing PC.")


if __name__ == "__main__":
    main()
