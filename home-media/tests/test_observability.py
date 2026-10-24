import json
import os
import subprocess
import sys
import time

import pytest

from home_media import supervisor
from home_media.config import Settings
from observability import events
from observability.api import ApiError, deliver
from observability.checks import backup_checks
from observability.incidents import Incidents
from observability.worker_state import apply_event, evaluate


def test_unknown_is_neither_failure_nor_recovery_and_state_survives_restart(tmp_path):
    path = tmp_path / "incidents.sqlite"
    store = Incidents(path)
    assert store.observe("job", "unknown", "offline", failures=1, now=1000) is None
    assert store.observe("job", "unhealthy", "no progress", failures=1, now=1001) == "failure"
    store.close()
    store = Incidents(path)
    assert store.observe("job", "unknown", "offline", recoveries=1, now=1002) is None
    assert store.observe("job", "healthy", "progress advanced", recoveries=2, now=1003) is None
    assert store.observe("job", "healthy", "progress advanced", recoveries=2, now=1004) == "recovery"
    assert store.observe("job", "healthy", "still healthy", recoveries=2, now=1005) is None
    assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    store.close()


def test_frozen_progress_is_detected_despite_fresh_heartbeat_and_offline_is_unknown():
    state = {"approved_at": 1000, "started_at": 1001, "progress_at": 1002,
             "heartbeat_at": 2000, "worker_present": True, "stage": "download"}
    assert evaluate(state, 2001)[0] == "unhealthy"
    assert evaluate(state, 2300)[0] == "unknown"
    state["outcome"] = "cancelled"
    assert evaluate(state, 2300)[0] == "cancelled"


def test_old_failure_and_heartbeat_cannot_reopen_completed_run():
    state = apply_event({}, {"source_epoch": 2000, "event_type": "run_finished",
                             "outcome": "completed"})
    apply_event(state, {"source_epoch": 1000, "event_type": "worker_outcome", "outcome": "failed"})
    apply_event(state, {"source_epoch": 1500, "event_type": "heartbeat", "worker_present": True})
    assert evaluate(state, 3000)[0] == "healthy"


def test_backup_requires_verified_success_not_snapshot_creation(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"ssd": {"snapshot": "created-only", "error": None}}))
    assert backup_checks(path, ["ssd"])[0][1] == "unhealthy"
    path.write_text(json.dumps({"ssd": {"snapshot": "verified", "error": None,
                                        "last_success": events.utc_now()}}))
    assert backup_checks(path, ["ssd"])[0][1] == "healthy"


def test_telegram_backoff_honors_server_retry_and_never_loses_outbox(tmp_path, monkeypatch):
    store = Incidents(tmp_path / "incidents.sqlite")
    store.observe("app", "unhealthy", "failed", failures=1, now=1000,
                  notification="Docker error · app\nContainer unhealthy.")

    def limited(*_args, **_kwargs):
        raise ApiError(429, {"parameters": {"retry_after": 1200}})

    monkeypatch.setattr("observability.api.request", limited)
    deliver(store, {"telegram_token": "secret", "telegram_chat_id": "123"}, now=1000)
    due, attempts = store.db.execute("SELECT due,attempts FROM outbox").fetchone()
    assert due == 2200 and attempts == 1
    monkeypatch.setattr("observability.api.request", lambda *_a, **_kw: {"ok": True})
    assert deliver(store, {"telegram_token": "secret", "telegram_chat_id": "123"}, now=2200)["delivered"] == 1
    assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    store.close()


def test_sensitive_values_are_removed_and_event_writer_does_not_dump_media(tmp_path):
    message = events.redact('ApiKey: abc123 password=xyz token=other https://example/?key=x '
                            '/media/private/name.jpg C:\\Users\\someone\\file')
    for secret in ("abc123", "xyz", "other", "example", "name.jpg", "someone"):
        assert secret not in message
    writer = events.configure(tmp_path, "a" * 32)
    with events.item("scene:22", "request-1"):
        events.checkpoint("download", 100, force=True)
    with events.item("scene:23", "request-2", "preparing"):
        events.checkpoint("download", 200, force=True)
    current = supervisor.current_progress(tmp_path, "a" * 32)
    assert current["entity"] == "scene:22" and current["progress_value"] == 100
    writer.close()


def test_worker_outcome_keeps_completion_totals_in_event_and_snapshot(tmp_path):
    run_id = "a" * 32
    writer = events.configure(tmp_path, run_id)
    events.finish(
        "completed", 3, 0, completed_files=3, completed_bytes=12345,
        completed_images=2, completed_videos=1, run_id="spoofed",
    )

    snapshot = events.read_json(tmp_path / f"outcome-{run_id}.json")
    rows = [json.loads(line) for line in (tmp_path / f"worker-{run_id}-{os.getpid()}.ndjson")
            .read_text(encoding="utf-8").splitlines()]
    outcome = next(row for row in rows if row["event_type"] == "worker_outcome")
    expected = {
        "run_id": run_id, "outcome": "completed", "completed": 3, "failed": 0,
        "completed_files": 3, "completed_bytes": 12345,
        "completed_images": 2, "completed_videos": 1,
    }
    assert snapshot == expected
    assert {key: outcome[key] for key in expected} == expected
    writer.close()


@pytest.mark.parametrize("scheme", ["Basic", "Bearer"])
def test_authorization_redaction_removes_scheme_and_credential(scheme):
    assert "private-credential" not in events.redact(
        f'Authorization: {scheme} private-credential, next=value')


@pytest.mark.parametrize("result,exit_code", [("completed", 0), ("failed", 1), ("cancelled", 0)])
def test_run_supervisor_writes_terminal_evidence_exits_and_never_imports_gpu(
    tmp_path, monkeypatch, result, exit_code
):
    settings = Settings("http://localhost", "test", tmp_path / "worker.sqlite", ("/media/test",))
    real_popen = subprocess.Popen
    children = []

    def fixture_worker(arguments, **kwargs):
        assert "--worker-process" in arguments
        code = ("import os; from observability import events; "
                "w=events.configure(os.environ['HOME_MEDIA_TELEMETRY_DIR'],os.environ['HOME_MEDIA_RUN_ID']); "
                f"events.finish('{result}'); w.close(); raise SystemExit({exit_code})")
        child = real_popen([sys.executable, "-c", code], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(supervisor.subprocess, "Popen", fixture_worker)
    monkeypatch.setattr(supervisor, "start_collector", lambda _c: (None, None))
    started = time.monotonic()
    assert supervisor.supervise(settings, [], None, interval=0.05) == exit_code
    assert time.monotonic() - started < 5
    assert all(p.poll() is not None for p in children)
    rows = [json.loads(line) for p in (tmp_path / "observability").glob("*.ndjson")
            for line in p.read_text().splitlines()]
    terminals = [r for r in rows if r["event_type"] == "run_finished"]
    assert len(terminals) == 1 and terminals[0]["outcome"] == result
    assert rows and any(r["event_type"] == "monitoring_stopped" for r in rows)
    assert "torch" not in sys.modules


def test_matching_stop_intent_overrides_forced_termination(tmp_path):
    events.atomic_json(tmp_path / "worker-stop.json", {"run_id": "a" * 32})
    assert supervisor.outcome(tmp_path, "a" * 32, -9) == "cancelled"
    assert supervisor.outcome(tmp_path, "b" * 32, -9) == "failed"


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership")
def test_windows_job_closing_kills_only_its_collector():
    from observability.windows import OwnedProcessJob, startup_options
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               **startup_options())
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 **startup_options())
    try:
        job = OwnedProcessJob(process)
        job.close()
        process.wait(timeout=3)
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=3)


def test_actual_collector_lua_distinguishes_severity_from_message_text(tmp_path):
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    binary = root / "data/monitoring/fluent-bit/fluent-bit-5.1.3-win64/bin/fluent-bit.exe"
    if os.name != "nt" or not binary.exists():
        pytest.skip("Verified Windows collector is not staged")
    fixtures = [
        {"MESSAGE": '{"level":"INFO","message":"query contains fatal and error"}', "PRIORITY": "3"},
        {"MESSAGE": '{"status":200,"error":""}', "PRIORITY": "3"},
        {"log": "ERROR unable to initialize repository"},
        {"log": "panic: failed to allocate"},
    ]
    (tmp_path / "fixture.ndjson").write_text("".join(json.dumps(row) + "\n" for row in fixtures))
    (tmp_path / "parsers.conf").write_text("[PARSER]\n    Name fixture_json\n    Format json\n")
    config = f"""[SERVICE]
    Flush 1
    Grace 1
    Log_Level warning
    Parsers_File {(tmp_path / 'parsers.conf').as_posix()}
[INPUT]
    Name tail
    Tag logs.fixture
    Path {tmp_path}\\fixture.ndjson
    Parser fixture_json
    Read_from_Head On
[FILTER]
    Name lua
    Match logs.*
    Script {(root / 'observability/deploy/normalize.lua').as_posix()}
    Call normalize
[OUTPUT]
    Name file
    Match *
    Path {tmp_path.as_posix()}
    File normalized
    Format plain
"""
    path = tmp_path / "fluent.conf"
    path.write_text(config)
    process = subprocess.Popen([str(binary), "-c", str(path)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 5
        result = tmp_path / "normalized"
        while time.monotonic() < deadline and not result.exists() and process.poll() is None:
            time.sleep(0.1)
        assert result.exists(), "Collector did not produce normalized fixture records"
        rows = [json.loads(line) for line in result.read_text().splitlines()]
        assert [row["level"] for row in rows] == ["info", "info", "error", "fatal"]
    finally:
        process.kill()
        process.wait(timeout=3)
