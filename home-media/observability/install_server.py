"""Idempotent, gated deployment of the isolated home-server monitoring stack."""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from .api import ApiError, request
from .checks import container_mapping

CONFIG = Path("/etc/homelab-monitor")
STORAGE = Path("/srv/observability")
SOURCE = Path(__file__).resolve().parent / "deploy"
ROOT = Path("/opt/homelab-monitor")
STATE = Path("/var/lib/homelab-monitor")


def run(*argv, timeout=120):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # stderr can contain private paths/headers. Keep deployment output metadata-only.
        raise RuntimeError(f"{Path(argv[0]).name} failed (exit {result.returncode})")
    return result.stdout


def write(path, text, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), mode)
        output.write(text)


def install_storage():
    backup = json.loads(Path("/var/lib/family-backup/state.json").read_text())
    entry = backup.get("ssd-latest", {})
    if not entry.get("last_success") or entry.get("error") or not entry.get("snapshot"):
        raise RuntimeError("SSD backup has not recorded verified success; deployment remains gated")
    STATE.mkdir(parents=True, exist_ok=True)
    image = STATE / "telemetry.ext4"
    if image.is_symlink():
        raise RuntimeError("Refusing a symlink storage image")
    if not image.exists():
        if shutil.disk_usage("/").free < 64 * 1024**3:
            raise RuntimeError("Need at least 64 GiB root free before allocating 24 GiB")
        run("fallocate", "-l", str(24 * 1024**3), str(image))
        image.chmod(0o600)
        run("mkfs.ext4", "-F", "-E", "nodiscard", "-L", "homelab-monitor", str(image))
    elif run("blkid", "-s", "LABEL", "-o", "value", str(image)).strip() != "homelab-monitor":
        raise RuntimeError("Existing image is not the managed monitoring filesystem")
    if image.stat().st_size != 24 * 1024**3:
        raise RuntimeError("Existing telemetry image has an unexpected size; refusing to mount or resize")
    STORAGE.mkdir(parents=True, exist_ok=True)
    write("/etc/systemd/system/srv-observability.mount", f"""[Unit]
Description=Bounded SSD storage for home-server telemetry
Before=homelab-openobserve.service fluent-bit.service homelab-monitor.service
[Mount]
What={image}
Where={STORAGE}
Type=ext4
Options=loop,nodev,nosuid,noexec
[Install]
WantedBy=multi-user.target
""")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "srv-observability.mount")
    if not STORAGE.is_mount():
        raise RuntimeError("Storage mount did not become available")
    for name in ("openobserve", "collector", "events", "checker"):
        (STORAGE / name).mkdir(exist_ok=True)
    os.chown(STORAGE / "openobserve", 10001, 10001)
    unit("homelab-monitor-storage.service", """[Unit]
Description=Reserve telemetry image and disable discard on its loop device
RequiresMountsFor=/srv/observability
After=srv-observability.mount
Before=homelab-openobserve.service fluent-bit.service homelab-monitor.service
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/homelab-monitor/source
ExecStart=/usr/bin/python3 -m observability.storage_guard
[Install]
WantedBy=multi-user.target
""")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "homelab-monitor-storage.service")


def install_tls():
    pki = CONFIG / "pki"
    pki.mkdir(exist_ok=True)
    os.chown(pki, 0, 10001)
    pki.chmod(0o750)
    if not (pki / "ca.crt").exists():
        run("openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "3650",
            "-subj", "/CN=Home server monitoring CA", "-keyout", str(pki / "ca.key"),
            "-out", str(pki / "ca.crt"))
        (pki / "ca.key").chmod(0o600)
    if not (pki / "server.crt").exists():
        run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=192.168.0.15",
            "-keyout", str(pki / "server.key"), "-out", str(pki / "server.csr"))
        write(pki / "server.ext", "subjectAltName=IP:192.168.0.15,IP:127.0.0.1,DNS:thinkserver\n"
              "extendedKeyUsage=serverAuth\n")
        run("openssl", "x509", "-req", "-in", str(pki / "server.csr"), "-CA", str(pki / "ca.crt"),
            "-CAkey", str(pki / "ca.key"), "-CAcreateserial", "-days", "365",
            "-extfile", str(pki / "server.ext"), "-out", str(pki / "server.crt"))
        os.chown(pki / "server.key", 0, 10001)
        (pki / "server.key").chmod(0o640)


def unit(name, text):
    write(Path("/etc/systemd/system") / name, text)


def install_backend():
    CONFIG.mkdir(exist_ok=True)
    CONFIG.chmod(0o750)
    install_tls()
    secret_path = CONFIG / "secrets.json"
    if not secret_path.exists():
        data = {"root_user": "monitor-admin@homelab.invalid", "root_password": secrets.token_urlsafe(32)}
        write(secret_path, json.dumps(data), 0o600)
    data = json.loads(secret_path.read_text())
    write(CONFIG / "openobserve.env", f"ZO_ROOT_USER_EMAIL={data['root_user']}\n"
          f"ZO_ROOT_USER_PASSWORD={data['root_password']}\n", 0o600)
    shutil.copyfile(SOURCE / "compose.yaml", ROOT / "compose.yaml")
    write(CONFIG / "firewall.nft", """table inet homelab_monitor {
  chain input {
    type filter hook input priority -10; policy accept;
    tcp dport 5080 ip saddr != { 127.0.0.0/8, 192.168.0.0/24, 10.15.39.0/24 } drop
    meta nfproto ipv6 tcp dport 5080 drop
  }
  chain forward {
    type filter hook forward priority -10; policy accept;
    tcp dport 5080 ip saddr != { 127.0.0.0/8, 192.168.0.0/24, 10.15.39.0/24 } drop
    meta nfproto ipv6 tcp dport 5080 drop
  }
}
""")
    unit("homelab-monitor-firewall.service", """[Unit]
Description=Private LAN/VPN access to monitoring
Before=homelab-openobserve.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/usr/sbin/nft delete table inet homelab_monitor
ExecStart=/usr/sbin/nft -f /etc/homelab-monitor/firewall.nft
[Install]
WantedBy=multi-user.target
""")
    unit("homelab-openobserve.service", """[Unit]
Description=Single-node OpenObserve on bounded SSD storage
After=docker.service srv-observability.mount homelab-monitor-firewall.service homelab-monitor-storage.service
Requires=docker.service srv-observability.mount homelab-monitor-firewall.service homelab-monitor-storage.service
StartLimitIntervalSec=300
StartLimitBurst=3
[Service]
Type=simple
WorkingDirectory=/opt/homelab-monitor
ExecStart=/usr/bin/docker compose -f /opt/homelab-monitor/compose.yaml up --abort-on-container-exit --exit-code-from openobserve
ExecStop=/usr/bin/docker compose -f /opt/homelab-monitor/compose.yaml stop -t 20
Restart=on-failure
RestartSec=15
TimeoutStopSec=30
[Install]
WantedBy=multi-user.target
""")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "homelab-monitor-firewall.service", "homelab-openobserve.service")
    endpoint = "https://192.168.0.15:5080"
    ca = str(CONFIG / "pki/ca.crt")
    for _ in range(30):
        try:
            request(endpoint + "/healthz", cafile=ca, timeout=2)
            break
        except ApiError:
            time.sleep(1)
    else:
        raise RuntimeError("OpenObserve did not become healthy")
    auth = (data["root_user"], data["root_password"])
    for platform in ("linux", "windows"):
        if f"{platform}_token" not in data:
            result = request(endpoint + "/api/default/ingestion-tokens",
                             {"name": f"fluent-bit-{platform}", "description": "Dedicated log ingestion"},
                             auth=auth, cafile=ca)
            data[f"{platform}_token"] = result["data"]["token"]
            write(secret_path, json.dumps(data), 0o600)
    if "query_user" not in data:
        email = "checker@homelab.invalid"
        payload = {"email": email, "first_name": "Home server", "last_name": "Checker"}
        request(endpoint + "/api/default/service_accounts", payload, auth=auth, cafile=ca)
        # Service-account rotation returns the token, unlike the create confirmation.
        result = request(endpoint + f"/api/default/service_accounts/{email}?rotateToken=true",
                         {"first_name": "Home server", "last_name": "Checker"}, method="PUT",
                         auth=auth, cafile=ca)
        data.update(query_user=email, query_password=result["token"])
        write(secret_path, json.dumps(data), 0o600)
    data["ingest_user"] = "default"
    write(secret_path, json.dumps(data), 0o600)
    schema = {"source_epoch": time.time(), "source_time": "", "service": "observability",
              "host": "thinkserver", "event_id": "bootstrap", "event_type": "bootstrap",
              "run_id": "", "outcome": "", "stage": "", "entity": "", "request_id": "",
              "progress_at": 0.0, "progress_value": 0, "worker_present": False,
              "failed": 0, "completed_images": 0, "completed_videos": 0, "completed_bytes": 0,
              "level": "info", "message": "Monitoring stream initialized"}
    for stream, days in (("homelab_logs", 7), ("homelab_events", 30)):
        result = request(endpoint + f"/api/default/{stream}/_json", [schema],
                         auth=(data["ingest_user"], data["linux_token"]), cafile=ca)
        if any(row.get("failed") for row in result.get("status", [])):
            raise RuntimeError("Stream bootstrap reported rejected records")
        request(endpoint + f"/api/default/streams/{stream}/settings", {"data_retention": days},
                method="PUT", auth=auth, cafile=ca)
    # Prove ingestion tokens cannot query before distributing the Windows credential.
    try:
        request(endpoint + "/api/default/streams", auth=(data["ingest_user"], data["windows_token"]), cafile=ca)
    except ApiError as error:
        if error.code not in {401, 403}:
            raise
    else:
        raise RuntimeError("Ingestion-only credential unexpectedly read management API")
    return data


def collector_output(tag, stream, token, limit):
    return f"""[OUTPUT]
    Name http
    Match {tag}
    Host 192.168.0.15
    Port 5080
    URI /api/default/{stream}/_json
    Format json
    Json_date_key _timestamp
    Json_date_format iso8601
    HTTP_User default
    HTTP_Passwd {token}
    tls On
    tls.verify On
    tls.verify_hostname On
    tls.ca_file /etc/homelab-monitor/pki/ca.crt
    Retry_Limit False
    storage.total_limit_size {limit}
    Workers 1
"""


def install_collector(data):
    if not Path("/opt/fluent-bit/bin/fluent-bit").exists():
        package = STATE / "fluent-bit_5.1.3_amd64.deb"
        url = "https://packages.fluentbit.io/ubuntu/noble/pool/main/f/fluent-bit/fluent-bit_5.1.3_amd64.deb"
        run("curl", "-fsSL", "--retry", "3", "--max-time", "180", "-o", str(package), url, timeout=190)
        if hashlib.sha256(package.read_bytes()).hexdigest() != "0ad7c832865380b1e4d2b26226b492046c327c8f879d0e6449a807ef1ac5a60a":
            raise RuntimeError("Fluent Bit package checksum mismatch")
        run("apt-get", "install", "-y", str(package), timeout=180)
        run("systemctl", "stop", "fluent-bit.service")
        package.unlink()
    for name in ("normalize.lua", "parsers.conf"):
        shutil.copyfile(SOURCE / name, CONFIG / name)
    settings = json.loads((SOURCE / "server-config.json").read_text())
    write(CONFIG / "containers.map", container_mapping(settings["containers"]))
    write(CONFIG / "config.json", json.dumps(settings, indent=2))
    logging = """[SERVICE]
    Flush 2
    Grace 20
    Log_Level warning
    Parsers_File /etc/homelab-monitor/parsers.conf
    storage.path /srv/observability/collector
    storage.sync full
    storage.checksum On
    storage.max_chunks_up 16
    storage.backlog.mem_limit 8M
    scheduler.cap 300
    HTTP_Server On
    HTTP_Listen 127.0.0.1
    HTTP_Port 2020
    storage.metrics On

[INPUT]
    Name tail
    Tag logs.docker.*
    Path /var/lib/docker/containers/*/*-json.log
    Path_Key file
    multiline.parser docker
    DB /srv/observability/collector/docker.sqlite
    Read_from_Head Off
    Refresh_Interval 5
    Rotate_Wait 30
    Buffer_Max_Size 256K
    Skip_Long_Lines On
    storage.type filesystem

[INPUT]
    Name systemd
    Tag logs.journal
    DB /srv/observability/collector/journal.sqlite
    Read_From_Tail On
    Max_Entries 500
    storage.type filesystem
"""
    units = list(dict.fromkeys(settings["units"] + [
        "ac-eth-shutdown.service", "thermald.service", "acpid.service"]))
    logging += "    Systemd_Filter _TRANSPORT=kernel\n"
    logging += "".join(f"    Systemd_Filter _SYSTEMD_UNIT={name}\n" for name in units)
    logging += """
[INPUT]
    Name tail
    Tag logs.unmanic
    Path /DATA/AppData/unmanic/.unmanic/logs/unmanic.log
    DB /srv/observability/collector/unmanic.sqlite
    Read_from_Head Off
    Buffer_Max_Size 256K
    Skip_Long_Lines On
    storage.type filesystem

[INPUT]
    Name tail
    Tag logs.jellyfin
    Path /DATA/AppData/jellyfin/config/log/log_*.log
    DB /srv/observability/collector/jellyfin.sqlite
    Read_from_Head Off
    Buffer_Max_Size 256K
    Skip_Long_Lines On
    storage.type filesystem

[INPUT]
    Name tail
    Tag health.events
    Path /srv/observability/events/*.ndjson
    Parser json_record
    DB /srv/observability/collector/events.sqlite
    Read_from_Head On
    Buffer_Max_Size 256K
    Skip_Long_Lines On
    storage.type filesystem

[FILTER]
    Name multiline
    Match logs.docker.*
    multiline.key_content log
    multiline.parser python_trace
    buffer On
    emitter_storage.type filesystem
    emitter_mem_buf_limit 8M

[FILTER]
    Name lua
    Match logs.*
    Script /etc/homelab-monitor/normalize.lua
    Call normalize

"""
    logging += collector_output("logs.*", "homelab_logs", data["linux_token"], "1792M")
    logging += collector_output("health.*", "homelab_events", data["linux_token"], "256M")
    write(CONFIG / "fluent-bit.conf", logging, 0o600)
    dropin = Path("/etc/systemd/system/fluent-bit.service.d/monitoring.conf")
    write(dropin, """[Unit]
RequiresMountsFor=/srv/observability
Requires=homelab-monitor-storage.service
After=srv-observability.mount homelab-monitor-storage.service homelab-openobserve.service
[Service]
ExecStart=
ExecStart=/opt/fluent-bit/bin/fluent-bit -c /etc/homelab-monitor/fluent-bit.conf
Restart=on-failure
RestartSec=10
MemoryMax=192M
CPUQuota=25%
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/srv/observability/collector
TimeoutStopSec=30
""")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "fluent-bit.service")
    time.sleep(5)
    # Initial offsets skip old 402-MiB logs. Subsequently new files start at byte zero.
    write(CONFIG / "fluent-bit.conf", logging.replace("Read_from_Head Off", "Read_from_Head On"), 0o600)
    run("systemctl", "restart", "fluent-bit.service")


def install_checker():
    unit("homelab-monitor.service", """[Unit]
Description=Home-server checks and Telegram incidents
RequiresMountsFor=/srv/observability
Requires=homelab-monitor-storage.service
After=srv-observability.mount homelab-monitor-storage.service homelab-openobserve.service fluent-bit.service
StartLimitIntervalSec=300
StartLimitBurst=3
[Service]
Type=notify
NotifyAccess=main
WorkingDirectory=/opt/homelab-monitor/source
ExecStart=/usr/bin/python3 -m observability.checker
Restart=on-failure
RestartSec=15
WatchdogSec=120
TimeoutStartSec=120
MemoryMax=128M
CPUQuota=15%
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/srv/observability/checker /srv/observability/events /etc/homelab-monitor/containers.map
[Install]
WantedBy=multi-user.target
""")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "homelab-monitor.service")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("storage", "backend", "collectors", "all"), default="all")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run through the existing authorized sudo connection")
    ROOT.mkdir(parents=True, exist_ok=True)
    if args.phase in {"storage", "all"}:
        install_storage()
        print("storage_ready")
    if args.phase in {"backend", "all"}:
        install_backend()
        print("backend_ready")
    if args.phase in {"collectors", "all"}:
        install_collector(json.loads((CONFIG / "secrets.json").read_text()))
        install_checker()
        print("collectors_ready")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never print request objects, credential-bearing JSON, or stack locals.
        print(f"deployment_failed: {type(error).__name__}: {str(error)[:160]}")
        raise SystemExit(1) from None
