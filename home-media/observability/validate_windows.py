"""Exercise the actual Windows collector offline, without starting inference."""

import json
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

import certifi

from home_media.supervisor import start_collector, stop_collector

from .configure_windows import ROOT, build_config
from .events import EventWriter, redact


def main():
    base = (ROOT / "data/monitoring").resolve()
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="validation-", dir=base) as temporary:
        directory = Path(temporary).resolve()
        assert directory.is_relative_to(base)
        private, spool, source = [directory / name for name in ("private", "spool", "source")]
        for path in (private, spool, source):
            path.mkdir()
        shutil.copyfile(certifi.where(), private / "ca.crt")
        shutil.copyfile(ROOT / "observability/deploy/windows-events.lua", private / "windows-events.lua")
        (private / "parsers.conf").write_text("[PARSER]\n    Name json_record\n    Format json\n")
        text = build_config(private, spool, source, "o2oi_fixture", "default")
        text = text.replace("Host 192.168.0.15", "Host 127.0.0.1").replace("Port 5080", "Port 9")
        text = text.replace("Grace 20", "Grace 1")
        text = text.replace("Log_Level warning", "Log_Level debug")
        config = private / "fluent-bit.conf"
        config.write_text(text, encoding="utf-8")
        binary = ROOT / "data/monitoring/fluent-bit/fluent-bit-5.1.3-win64/bin/fluent-bit.exe"
        writer = EventWriter(source, "a" * 32)
        writer.emit("approved")
        writer.emit("run_finished", outcome="completed")
        writer.close()
        options = {"binary": str(binary), "config": str(config), "run_id": "a" * 32}
        process, job = start_collector(options)
        try:
            time.sleep(5)
            if process.poll() is not None:
                diagnostic = (private / "collector-startup.log").read_text(errors="replace")
                print(redact(diagnostic[-4000:]))
                raise RuntimeError("Fixture collector exited during startup")
        finally:
            stop_collector(process, job, grace=5)
        db = spool / "events.sqlite"
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as connection:
            offset = connection.execute("SELECT sum(offset) FROM in_tail_files").fetchone()[0]
        if not offset:
            print(redact((spool / "fluent-bit.log").read_text(errors="replace")[-7000:]))
        assert offset and offset > 0, "Source offsets did not survive stop"
        chunks = list(spool.rglob("*.flb"))
        assert chunks, "Offline filesystem backlog did not persist"
        process, job = start_collector(options)
        try:
            time.sleep(2)
            assert process.poll() is None, "Collector could not restart with backlog"
        finally:
            stop_collector(process, job, grace=5)
        print(json.dumps({"windows_collector": "passed", "gpu_started": False,
                          "persistent_offsets": True, "persistent_backlog_restart": True,
                          "collector_exited": process.poll() is not None}))


if __name__ == "__main__":
    main()
