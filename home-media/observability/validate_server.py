"""Bounded tmpfs OpenObserve API smoke test; never touches central log storage."""

import base64
import json
import secrets
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from .api import ApiError, request

IMAGE = "public.ecr.aws/zinclabs/openobserve@sha256:dae91bc1f9d4c11db54e924d8d25bac7f6c50e080a8f06fc90d9468eaab81b67"


def main():
    name = "homelab-openobserve-smoke"
    if subprocess.run(["docker", "ps", "-aq", "--filter", f"name=^/{name}$"],
                      capture_output=True, text=True, check=True).stdout.strip():
        raise RuntimeError("A smoke-test container already exists; refusing to replace it")
    username, password = "smoke@homelab.invalid", secrets.token_urlsafe(32)
    endpoint = "http://127.0.0.1:15080"
    auth = (username, password)
    with tempfile.TemporaryDirectory(prefix="homelab-smoke-", dir="/run") as temporary:
        env = Path(temporary) / "credentials.env"
        env.write_text(f"ZO_ROOT_USER_EMAIL={username}\nZO_ROOT_USER_PASSWORD={password}\n")
        env.chmod(0o600)
        command = ["docker", "run", "-d", "--name", name, "--user", "10001:10001",
                   "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                   "--memory", "768m", "--cpus", "1", "--pids-limit", "256",
                   "--log-opt", "max-size=1m", "--log-opt", "max-file=2",
                   "--tmpfs", "/data:rw,size=256m,uid=10001,gid=10001", "--tmpfs", "/tmp:rw,size=32m",
                   "-p", "127.0.0.1:15080:5080", "--env-file", str(env),
                   "-e", "ZO_DATA_DIR=/data", "-e", "ZO_TELEMETRY=false",
                   "-e", "ZO_MEMORY_CACHE_ENABLED=false", "-e", "ZO_MEMORY_CACHE_DATAFUSION_MAX_SIZE=128",
                   "-e", "ZO_MEM_TABLE_MAX_SIZE=32", "-e", "ZO_MAX_FILE_SIZE_IN_MEMORY=8",
                   "-e", "ZO_DISK_CACHE_ENABLED=false", "-e", "ZO_HTTP_WORKER_NUM=2",
                   "-e", "ZO_QUERY_THREAD_NUM=2", "-e", "ZO_QUERY_INDEX_THREAD_NUM=1", IMAGE]
        subprocess.run(command, capture_output=True, check=True)
        try:
            for _ in range(45):
                try:
                    request(endpoint + "/healthz", timeout=2)
                    break
                except ApiError:
                    time.sleep(1)
            else:
                raise RuntimeError("Smoke OpenObserve failed startup")
            token = request(endpoint + "/api/default/ingestion-tokens",
                            {"name": "smoke-ingest", "description": "Disposable verification"}, auth=auth)["data"]["token"]
            result = request(endpoint + "/api/default/smoke/_json",
                             [{"message": "fixture-only", "level": "info", "source_epoch": time.time()}],
                             auth=("default", token))
            assert not any(row.get("failed") for row in result.get("status", []))
            denied = False
            try:
                request(endpoint + "/api/default/streams", auth=("default", token))
            except ApiError as error:
                denied = error.code in {401, 403}
            assert denied, "Ingestion credential unexpectedly read management API"
            payload = {"email": "checker@homelab.invalid", "first_name": "Fixture", "last_name": "Checker"}
            request(endpoint + "/api/default/service_accounts", payload, auth=auth)
            operation = urllib.request.Request(
                endpoint + "/api/default/service_accounts/checker@homelab.invalid?rotateToken=true",
                data=json.dumps({"first_name": "Fixture", "last_name": "Checker"}).encode(), method="PUT",
                headers={"Content-Type": "application/json", "Authorization": "Basic " +
                         base64.b64encode(":".join(auth).encode()).decode()})
            with urllib.request.urlopen(operation, timeout=5) as response:
                service_token = json.load(response)["token"]
            body = {"query": {"sql": 'SELECT message FROM "smoke"',
                              "start_time": int((time.time() - 60) * 1e6),
                              "end_time": int((time.time() + 60) * 1e6), "from": 0, "size": 10}}
            result = request(endpoint + "/api/default/_search", body,
                             auth=("checker@homelab.invalid", service_token))
            assert any(row.get("message") == "fixture-only" for row in result.get("hits", []))
            for path in ("/web/logs", "/logs"):
                try:
                    request(endpoint + path)
                    print("ui_path_available=" + path)
                except ApiError:
                    pass
            print(json.dumps({"server_smoke": "passed", "ingestion_token_is_write_only": True,
                              "service_account_search": True, "persistent_disk_data": False}))
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("server_smoke_failed=" + type(error).__name__ + ": " + str(error)[:150])
        raise SystemExit(1) from None
