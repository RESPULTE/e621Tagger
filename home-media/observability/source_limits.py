"""Apply source-log limits without pulling images or recreating dependencies."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

from .events import atomic_json

BASELINE = Path("/var/lib/homelab-monitor/baseline")


def run(*arguments, cwd=None, timeout=60):
    result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{arguments[0]} operation failed ({result.returncode}); private output suppressed")
    return result.stdout


def details(name):
    # Selected properties only, never environment, labels unrelated to Compose, or credentials.
    expression = ('{{json .Config.Image}}|{{json .Image}}|'
                  '{{json (index .Config.Labels "com.docker.compose.project.config_files")}}|'
                  '{{json (index .Config.Labels "com.docker.compose.project")}}|'
                  '{{json (index .Config.Labels "com.docker.compose.service")}}')
    image, image_id, file, project, service = run("docker", "inspect", "--format", expression, name).strip().split("|", 4)
    path = Path(json.loads(file)).resolve()
    if path.name != "docker-compose.yml" or not path.is_relative_to(Path("/var/lib/casaos/apps")):
        raise RuntimeError("Compose path is outside the expected CasaOS source directories")
    return {"container": name, "image": json.loads(image), "image_id": json.loads(image_id),
            "path": path, "project": json.loads(project), "service": json.loads(service)}


def apply_config(info):
    path = info["path"]
    original = path.read_bytes()
    document = yaml.safe_load(original)
    desired = {"driver": "json-file", "options": {"max-size": "20m", "max-file": "3"}}
    service = document["services"][info["service"]]
    if service.get("logging") == desired:
        return
    BASELINE.mkdir(parents=True, exist_ok=True)
    BASELINE.chmod(0o700)
    backup = BASELINE / (hashlib.sha256(str(path).encode()).hexdigest()[:16] + ".compose")
    if not backup.exists():
        shutil.copyfile(path, backup)
        backup.chmod(0o600)
    service["logging"] = desired
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".monitoring-",
                                         suffix=".yml", delete=False) as output:
            temporary = Path(output.name)
            yaml.safe_dump(document, output, sort_keys=False)
            output.flush()
            os.fsync(output.fileno())
        run("docker", "compose", "-p", info["project"], "--project-directory", str(path.parent),
            "-f", str(temporary), "config", "--quiet", cwd=path.parent)
        if path.read_bytes() != original:
            raise RuntimeError("Compose changed concurrently; refusing to overwrite it")
        metadata = path.stat()
        os.chmod(temporary, metadata.st_mode & 0o777)
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restart(info):
    document = yaml.safe_load(info["path"].read_bytes())
    if document["services"][info["service"]].get("logging", {}).get("options", {}).get("max-size") != "20m":
        raise RuntimeError("Apply the validated logging policy before recreating a container")
    current = run("docker", "image", "inspect", "--format", "{{.Id}}", info["image"]).strip()
    if current != info["image_id"]:
        raise RuntimeError("Local image tag changed; refusing an unintended application upgrade")
    # Short, expiring maintenance covers normal transient dependency reconnects.
    affected = (["immich-server", "immich-postgres", "immich-redis", "immich-machine-learning"]
                if info["project"] == "big-bear-immich" else [info["container"]])
    atomic_json(Path("/var/lib/homelab-monitor/maintenance.json"),
                {"until": time.time() + 600, "services": affected})
    run("docker", "compose", "-p", info["project"], "-f", str(info["path"]), "up", "-d",
        "--no-deps", "--force-recreate", "--pull", "never", info["service"],
        cwd=info["path"].parent, timeout=120)
    actual = run("docker", "inspect", "--format", "{{.Image}}|{{json .HostConfig.LogConfig}}",
                 info["container"]).strip().split("|", 1)
    if actual[0] != info["image_id"] or json.loads(actual[1]).get("Config", {}).get("max-file") != "3":
        raise RuntimeError("Recreated container did not match expected image/log policy")


def apply_journal():
    path = Path("/etc/systemd/journald.conf.d/90-homelab-monitoring.conf")
    policy = "[Journal]\nSystemMaxUse=512M\nSystemKeepFree=40G\nMaxRetentionSec=7day\n"
    if path.exists() and path.read_text() != policy:
        raise RuntimeError("Managed journal policy differs; review it before replacement")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(policy)
    path.chmod(0o644)
    run("systemctl", "restart", "systemd-journald.service")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-config", action="store_true")
    parser.add_argument("--apply-journal", action="store_true", help="Apply the bounded journal policy")
    parser.add_argument("--restart", help="Recreate just this already-configured container")
    args = parser.parse_args()
    if args.apply_journal:
        apply_journal()
        print("journal_policy_applied")
        return
    config = json.loads(Path("/etc/homelab-monitor/config.json").read_text())
    names = [n for n in config["containers"] if n != "homelab-openobserve"]
    if args.restart and args.restart not in names:
        raise RuntimeError("Container is outside the source manifest")
    for name in names:
        if args.restart and name != args.restart:
            continue
        info = details(name)
        if args.apply_config:
            apply_config(info)
        if args.restart:
            restart(info)
        print(json.dumps({"container": name, "config_applied": args.apply_config,
                          "recreated": bool(args.restart)}))


if __name__ == "__main__":
    main()
