"""Configure an inactive on-demand collector using the existing pinned SSH identity."""

import base64
import json
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

import paramiko

from home_media.config import Settings

from .events import atomic_json

ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def server_connection(settings):
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys(str(Path.home() / ".ssh/known_hosts"))
    ssh.connect(settings.ssh_host, username=settings.ssh_user,
                key_filename=str(Path(settings.ssh_key).expanduser()), allow_agent=False,
                look_for_keys=False, timeout=15)
    try:
        yield ssh
    finally:
        ssh.close()


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                capture_output=True, text=True, check=True)
        sid = result.stdout.strip().split(',')[-1].strip('"')
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
                       capture_output=True, check=True)


def remote_python(ssh, code, input_data=None):
    encoded = base64.b64encode(code.encode()).decode()
    operation = f"sudo -n python3 -c \"import base64;exec(base64.b64decode('{encoded}'))\""
    stdin, stdout, stderr = ssh.exec_command(operation, timeout=30)
    if input_data:
        stdin.write(input_data)
        stdin.flush()
    stdin.channel.shutdown_write()
    data = stdout.read()
    if stdout.channel.recv_exit_status():
        raise RuntimeError("Private server operation failed; credentials were not printed")
    return data


def build_config(private, spool, source, token, username):
    output = f"""[SERVICE]
    Flush 2
    Grace 20
    Log_Level warning
    Log_File "{(spool / 'fluent-bit.log').as_posix()}"
    Parsers_File "{(private / 'parsers.conf').as_posix()}"
    storage.path "{spool.as_posix()}"
    storage.sync full
    storage.checksum On
    storage.max_chunks_up 16
    storage.backlog.mem_limit 8M
    scheduler.cap 300

[INPUT]
    Name tail
    Tag worker.events
    Path {source}\\worker-*.ndjson,{source}\\supervisor-*.ndjson
    Parser json_record
    DB "{(spool / 'events.sqlite').as_posix()}"
    Read_from_Head On
    Refresh_Interval 1
    Rotate_Wait 5
    Buffer_Max_Size 256K
    Skip_Long_Lines On
    storage.type filesystem

[FILTER]
    Name rewrite_tag
    Match worker.events
    Rule $event_type ^application_log$ worker.logs false
    Emitter_Storage.type filesystem
    Emitter_Mem_Buf_Limit 8M

[INPUT]
    Name winevtlog
    Tag worker.platform
    Channels System,Application,Microsoft-Windows-NetworkProfile/Operational
    DB "{(spool / 'windows.sqlite').as_posix()}"
    Event_Query *[System[TimeCreated[@SystemTime >= '${{HOME_MEDIA_MONITOR_SINCE}}'] and (Provider[@Name='Display'] or Provider[@Name='Application Error'] or Provider[@Name='Windows Error Reporting'] or Provider[@Name='Microsoft-Windows-Kernel-Power'] or Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] or Provider[@Name='Microsoft-Windows-NetworkProfile'])]]
    Read_Existing_Events false
    storage.type filesystem

[FILTER]
    Name lua
    Match worker.platform
    Script "{(private / 'windows-events.lua').as_posix()}"
    Call platform_event
"""
    for tag, stream, limit in (("worker.events", "homelab_events", "240M"),
                               ("worker.platform", "homelab_events", "16M"),
                               ("worker.logs", "homelab_logs", "1792M")):
        output += f"""
[OUTPUT]
    Name http
    Match {tag}
    Host 192.168.0.15
    Port 5080
    URI /api/default/{stream}/_json
    Format json
    Json_date_key _timestamp
    Json_date_format iso8601
    HTTP_User {username}
    HTTP_Passwd {token}
    tls On
    tls.verify On
    tls.verify_hostname On
    tls.ca_file "{(private / 'ca.crt').as_posix()}"
    Retry_Limit False
    storage.total_limit_size {limit}
    Workers 1
"""
    # Classic Fluent Bit config values already extend to end of line; literal
    # quotation marks become part of a Windows path in this parser.
    return output.replace('"', '')


def main():
    settings = Settings.load(str(ROOT / "config.toml"))
    private = ROOT / "data" / "monitoring-private"
    private_directory(private)
    spool = ROOT / "data" / "monitoring" / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    binary = ROOT / "data/monitoring/fluent-bit/fluent-bit-5.1.3-win64/bin/fluent-bit.exe"
    if not binary.exists():
        raise RuntimeError("Verified Fluent Bit binary has not been staged")
    with server_connection(settings) as ssh:
        data = json.loads(remote_python(ssh, "import json;from pathlib import Path;"
                                       "d=json.loads(Path('/etc/homelab-monitor/secrets.json').read_text());"
                                       "print(json.dumps({k:d[k] for k in ('windows_token','ingest_user')}))"))
        token = data["windows_token"]
        if not re.fullmatch(r"o2oi_[a-zA-Z0-9]+", token):
            raise RuntimeError("Unexpected ingestion credential format")
        ca = remote_python(ssh, "from pathlib import Path;"
                           "print(Path('/etc/homelab-monitor/pki/ca.crt').read_text())")
        (private / "ca.crt").write_bytes(ca)
        (private / "parsers.conf").write_text("[PARSER]\n    Name json_record\n    Format json\n")
        (private / "windows-events.lua").write_text(
            (ROOT / "observability/deploy/windows-events.lua").read_text(), encoding="utf-8")
        config_path = private / "fluent-bit.conf"
        config_path.write_text(build_config(private, spool, ROOT / "data/observability",
                                            token, data["ingest_user"]), encoding="utf-8")
        atomic_json(ROOT / "data/monitoring.json", {"binary": str(binary), "config": str(config_path),
                                                   "spool": str(spool)})
        telegram = private / "telegram.json"
        if telegram.exists():
            payload = telegram.read_text()
            remote_python(ssh, "import json,sys,os;from pathlib import Path;"
                          "p=Path('/etc/homelab-monitor/secrets.json');d=json.loads(p.read_text());"
                          "v=json.load(sys.stdin);"
                          "d.update({k:v[k] for k in ('telegram_token','telegram_chat_id')});"
                          "p.write_text(json.dumps(d));p.chmod(0o600)", payload)
        remote_python(ssh, "import json,sys;from pathlib import Path;"
                      "p=Path('/etc/homelab-monitor/secrets.json');d=json.loads(p.read_text());"
                      "d['stash_api_key']=json.load(sys.stdin)['key'];"
                      "p.write_text(json.dumps(d));p.chmod(0o600)",
                      json.dumps({"key": settings.stash_key}))
    if os.name == "nt":
        # Windows may require its certificate consent dialog. Do not suppress
        # interactive UI or block configuration while that dialog is pending.
        subprocess.Popen(["powershell.exe", "-NoProfile", "-File",
                          str(ROOT / "observability/deploy/trust-monitoring-ca.ps1"),
                          "-CertificatePath", str(private / "ca.crt"),
                          "-StatusPath", str(private / "certificate-trust.json")],
                         creationflags=subprocess.CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("windows_collector_configured_inactive")


if __name__ == "__main__":
    main()
