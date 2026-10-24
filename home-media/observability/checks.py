"""Read-only application, host and verified-backup probes."""

import json
import shutil
import socket
import struct
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .api import ApiError, request


def command(*arguments, timeout=5):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{arguments[0]} exited {result.returncode}")
    return result.stdout.strip()


def docker_checks(names):
    raw = command("docker", "inspect", "--format",
                  '{{json .Name}}|{{json .State}}', *names)
    results = []
    for line in raw.splitlines():
        name, raw_state = line.split("|", 1)
        name, state = json.loads(name).lstrip("/"), json.loads(raw_state)
        healthy = state.get("Health", {}).get("Status", "not_configured")
        ok = state.get("Running") and healthy not in {"unhealthy", "starting"}
        results.append((f"docker/{name}", "healthy" if ok else "unhealthy",
                        f"state={state.get('Status')}; health={healthy}; "
                        f"exit={state.get('ExitCode')}; OOM={state.get('OOMKilled')}. "
                        "Inspect this service's correlated logs."))
    return results


def container_mapping(names):
    allowed = set(names)
    raw = command("docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}|{{.Names}}")
    return "\n".join(line for line in raw.splitlines() if line.split("|", 1)[-1] in allowed)


def unit_checks(units):
    results = []
    raw = command("systemctl", "show", *units, "-p", "Id", "-p", "ActiveState", "-p", "SubState")
    for block in raw.split("\n\n"):
        data = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if not data.get("Id"):
            continue
        state = data.get("ActiveState")
        results.append((f"unit/{data['Id']}", "healthy" if state == "active" else "unhealthy",
                        f"systemd state={state}/{data.get('SubState')}. Check its unit journal."))
    return results


def http_check(check):
    try:
        result = request(check["url"], cafile=check.get("ca_file"), timeout=4)
        if check.get("key") and result.get(check["key"]) != check["value"]:
            return check["name"], "unhealthy", "Application response did not match expected health."
        return check["name"], "healthy", "Application returned its expected health response."
    except ApiError as error:
        state = "unknown" if error.code in {401, 403} else "unhealthy"
        return check["name"], state, f"Read-only HTTP probe failed ({error.code}); inspect service health."


def backup_checks(state_path, targets, now=None):
    now = time.time() if now is None else now
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        return [("backup/state", "unknown", "Backup state cannot be read; verification is unknown.")]
    results = []
    for target in targets:
        entry = state.get(target, {})
        success = entry.get("last_success")
        try:
            age = now - datetime.fromisoformat(success).timestamp() if success else None
        except (TypeError, ValueError):
            age = None
        # Errors may refer to an earlier attempt; only a newer verified success clears them.
        error = bool(entry.get("error"))
        raw_error = str(entry.get("error", "")).lower()
        error_class = "none"
        if error:
            if any(word in raw_error for word in ("decrypt", "authentic", "verify", "corrupt")):
                error_class = "integrity or verification failure"
            elif any(word in raw_error for word in ("space", "reserve")):
                error_class = "insufficient free-space reserve"
            elif "expected filesystem" in raw_error:
                error_class = "filesystem identity mismatch"
            else:
                error_class = "runner error; inspect private journal"
        due = age is None or age > (30 * 86400 + 6 * 3600 + 86400)
        ok = not error and not due and bool(entry.get("snapshot"))
        evidence = (f"target={target}; last_verified_success={success or 'none'}; "
                    f"error={error_class}; overdue={due}. "
                    "Inspect family-backup.service; success requires full-file verification.")
        results.append((f"backup/{target}", "healthy" if ok else "unhealthy", evidence))
    return results


def host_checks(config):
    results = []
    for label, path, minimum in [("root", "/", 40),
                                 ("telemetry", config["storage"], 2.4)]:
        disk = shutil.disk_usage(path)
        free = disk.free / 1024**3
        results.append((f"disk/{label}", "healthy" if free >= minimum else "unhealthy",
                        f"free={free:.1f} GiB; reserve={minimum} GiB. Reduce telemetry growth; "
                        "never delete media or backup data automatically."))
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.strip().split()[0])
    available = memory["MemAvailable"] / 1024**2
    results.append(("host/memory", "healthy" if available >= 1 else "unhealthy",
                    f"available={available:.2f} GiB. Check pressure before increasing monitoring limits."))
    return results


def backup_runtime():
    raw = command("systemctl", "show", "family-backup.service", "-p", "ActiveState",
                  "-p", "ExecMainStartTimestampMonotonic")
    data = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    started = int(data.get("ExecMainStartTimestampMonotonic", "0")) / 1e6
    active = data.get("ActiveState") in {"active", "activating"}
    elapsed = time.monotonic() - started if started and active else 0
    return ("backup/runtime", "unhealthy" if elapsed > 86400 else "healthy",
            f"backup active={active}; running_hours={elapsed / 3600:.1f}. "
            "If over 24 hours, inspect snapshot/verification progress; do not terminate automatically.")


def stash_check(secrets):
    if not secrets.get("stash_api_key"):
        return "app/stash", "unknown", "Authenticated Stash probe has not been configured."
    try:
        response = request("http://127.0.0.1:9999/graphql", {"query": "{version{version}}"},
                           headers={"ApiKey": secrets["stash_api_key"]}, timeout=4)
        healthy = bool(response.get("data", {}).get("version")) and not response.get("errors")
        return "app/stash", "healthy" if healthy else "unhealthy", "Read-only authenticated GraphQL version probe."
    except ApiError as error:
        return "app/stash", "unhealthy", f"Stash GraphQL probe failed ({error.code})."


def dns_check():
    transaction = int(time.time_ns()) & 0xFFFF
    question = b"\x02pi\x04hole\x00" + struct.pack("!HH", 1, 1)
    packet = struct.pack("!HHHHHH", transaction, 0x100, 1, 0, 0, 0) + question
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.settimeout(3)
            connection.connect(("127.0.0.1", 53))
            connection.send(packet)
            data = connection.recv(4096)
        reply_id, flags, _questions, answers = struct.unpack("!HHHH", data[:8])
        ok = reply_id == transaction and flags & 0x8000 and not flags & 15 and answers > 0
        return "app/pihole", "healthy" if ok else "unhealthy", "Local pi.hole DNS A-response validation."
    except (OSError, ValueError, struct.error):
        return "app/pihole", "unhealthy", "Pi-hole DNS response missing/invalid; inspect DNS service."


def resource_sample():
    cpu = [int(n) for n in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    vm = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
    return {"cpu_ticks": cpu,
            "memory_available_gib": int(memory["MemAvailable"].strip().split()[0]) / 1024**2,
            "root_free_gib": shutil.disk_usage("/").free / 1024**3,
            "telemetry_free_gib": shutil.disk_usage("/srv/observability").free / 1024**3,
            "swapin": int(vm["pswpin"]), "swapout": int(vm["pswpout"])}
