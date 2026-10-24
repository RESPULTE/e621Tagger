"""Keep Stash API credentials inside an authenticated SSH connection."""

import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


@contextmanager
def api_tunnel(settings):
    endpoint = urlsplit(settings.stash_url)
    if endpoint.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Worker stash_url must use a local SSH tunnel")
    port = endpoint.port or 19999
    args = [
        "ssh",
        "-N",
        "-i",
        str(Path(settings.ssh_key).expanduser()),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-L",
        f"127.0.0.1:{port}:127.0.0.1:9999",
        f"{settings.ssh_user}@{settings.ssh_host}",
    ]
    process = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError(
                    "SSH API tunnel could not start: " + process.stderr.read().decode()
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("SSH API tunnel timed out")
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        process.stderr.close()
