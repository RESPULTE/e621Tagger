"""Deploy reviewed artifacts using strict SSH host-key checking."""

import json
import os
import secrets
import shlex
import sys
from pathlib import Path

import bcrypt
import httpx
import paramiko

ROOT = Path(__file__).resolve().parent.parent


def connect():
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys(str(Path.home() / ".ssh/known_hosts"))
    ssh.connect(
        "192.168.0.15",
        username="vector",
        key_filename=str(Path.home() / ".ssh/home_media_ed25519"),
        look_for_keys=False,
        allow_agent=False,
        timeout=15,
    )
    return ssh


def execute(ssh, command):
    _, out, err = ssh.exec_command(command)
    result = out.read().decode()
    error = err.read().decode()
    code = out.channel.recv_exit_status()
    if code:
        raise RuntimeError(f"Remote command failed ({code}): {error}")
    return result + error


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    private = ROOT / "data"
    private.mkdir(exist_ok=True)
    credential_path = private / "access.json"
    if credential_path.exists():
        credentials = json.loads(credential_path.read_text())
    else:
        credentials = {
            "username": "vector",
            "password": secrets.token_urlsafe(24),
            "api_key": secrets.token_hex(32),
        }
        credential_path.write_text(json.dumps(credentials, indent=2))
        if os.name != "nt":
            credential_path.chmod(0o600)
    release = httpx.get("https://api.github.com/repos/stashapp/stash/releases/tags/v0.31.1").json()
    asset = next(a for a in release["assets"] if a["name"] == "stash-linux")
    digest = asset.get("digest", "")
    if not digest.startswith("sha256:"):
        raise RuntimeError("GitHub release has no SHA256 digest; verify download before deploying")
    roots = ["/media/vector/jellyfin-data"]
    config = {
        "host": "0.0.0.0",
        "port": 9999,
        "username": credentials["username"],
        "password": bcrypt.hashpw(credentials["password"].encode(), bcrypt.gensalt()).decode(),
        "api_key": credentials["api_key"],
        "jwt_secret_key": secrets.token_hex(32),
        "database": "/var/lib/home-media/stash.sqlite",
        "generated": "/var/lib/home-media/generated",
        "metadata": "/var/lib/home-media/metadata",
        "cache": "/var/lib/home-media/cache",
        "plugins_path": "/var/lib/home-media/plugins",
        "blobs_path": "/var/lib/home-media/blobs",
        "stash": [{"path": p} for p in roots],
        "parallel_tasks": 1,
    }
    request = {
        "download_url": asset["browser_download_url"],
        "sha256": digest[7:],
        "config": config,
    }
    with connect() as ssh, ssh.open_sftp() as sftp:
        stage = "/home/vector/.home-media-deploy"
        execute(ssh, "mkdir -p " + shlex.quote(stage) + " && chmod 700 " + shlex.quote(stage))
        sftp.put(str(ROOT / "scripts/install_stash.py"), stage + "/install_stash.py")
        with sftp.open(stage + "/request.json", "w") as target:
            target.write(json.dumps(request))
        sftp.chmod(stage + "/request.json", 0o600)
        print(execute(ssh, f"sudo -n python3 {stage}/install_stash.py {stage}/request.json"))
        # The API key is written privately and never printed to logs.
        worker_config = (
            (ROOT / "config.example.toml")
            .read_text()
            .replace("REPLACE_WITH_STASH_API_KEY", credentials["api_key"])
        )
        (ROOT / "config.toml").write_text(worker_config)
    print("Login credentials saved privately to data/access.json")


if __name__ == "__main__":
    main()
