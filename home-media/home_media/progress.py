"""One-shot local progress report. No network requests, media reads, or GPU loading."""

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .state import worker_available

ROOT = Path(__file__).resolve().parent.parent
LOCAL_TIME = timezone(timedelta(hours=8))


def read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def timestamp(raw) -> float:
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (TypeError, ValueError):
        return 0


def snapshot(settings: Settings, *, active: bool | None = None) -> dict:
    directory = settings.state.parent
    manifests = [read_json(directory / name)
                 for name in ("current-run.json", "worker-control.json")]
    manifest = max(manifests, key=lambda item: timestamp(item.get("started_at")))
    started = timestamp(manifest.get("started_at"))
    expected = (manifest["expected_images"] + manifest["expected_videos"]
                if "expected_images" in manifest and "expected_videos" in manifest else None)
    result = {"active": not worker_available(settings) if active is None else active,
              "started": started, "expected": expected,
              "counts": {}, "overall_counts": {}, "current": None, "failures": [], "known": 0,
              "log": manifest.get("stderr")}
    if not settings.state.exists():
        return result
    # Opening in read-only mode cannot create a fresh DB or modify processing state.
    with closing(sqlite3.connect(settings.state.resolve().as_uri() + "?mode=ro", uri=True)) as database:
        result["known"] = database.execute("SELECT count(*) FROM jobs").fetchone()[0]
        result["overall_counts"] = dict(database.execute(
            "SELECT status,count(*) FROM jobs GROUP BY status"
        ))
        result["counts"] = dict(database.execute(
            "SELECT status,count(*) FROM jobs WHERE updated>=? GROUP BY status", (started,)
        ))
        row = database.execute(
            "SELECT entity FROM jobs WHERE status='running' AND updated>=? ORDER BY updated DESC LIMIT 1",
            (started,),
        ).fetchone()
        result["current"] = row[0] if row else None
        result["failures"] = database.execute(
            "SELECT entity,error FROM jobs WHERE status='failed' AND updated>=? ORDER BY entity",
            (started,),
        ).fetchall()
    return result


def format_report(progress: dict) -> str:
    counts = progress["counts"]
    done, failed = counts.get("done", 0), counts.get("failed", 0)
    running = counts.get("running", 0)
    overall = progress.get("overall_counts", counts)
    lines = ["HOME MEDIA TAGGING PROGRESS", "",
             "Worker: RUNNING" if progress["active"] else "Worker: NOT RUNNING"]
    if progress["started"]:
        lines.append("Run started: " + datetime.fromtimestamp(progress["started"], LOCAL_TIME)
                     .strftime("%d %b %Y, %I:%M:%S %p (Malaysia)"))
    total = progress["expected"]
    lines.append("Latest batch completed: " + str(done)
                 + (f" / {total} ({done / total:.1%})" if total else ""))
    lines.append(f"Latest batch failed: {failed} (unfinished; eligible for retry)")
    known = progress.get("known", sum(overall.values()))
    lines.append(f"Overall completed: {overall.get('done', 0)} / {known} tracked files")
    lines.append(f"Overall failed: {overall.get('failed', 0)} (eligible for retry)")
    if progress["active"] and progress["current"]:
        lines.append("Processing: " + progress["current"])
    unfinished = counts.get("interrupted", 0) + (running if not progress["active"] else 0)
    if unfinished:
        lines.append(f"Interrupted/unfinished: {unfinished}")
    if total is not None:
        lines.append(f"Not started in this run: {max(0, total - sum(counts.values()))}")
    if not progress["active"]:
        if failed or unfinished or (total is not None and done < total):
            lines.append("Work remains unfinished. Start or resume tagging to retry it.")
        elif done:
            lines.append("All recorded work in this run completed successfully.")
        else:
            lines.append("No processing progress has been recorded yet.")
    if progress["failures"]:
        lines.extend(["", "Failed items:"])
        lines.extend(f"  {identity}: {error or 'Unknown error'}"
                     for identity, error in progress["failures"])
    if progress["log"]:
        lines.extend(["", "Detailed log: " + progress["log"]])
    lines.extend(["", "This is a snapshot. Reopen this shortcut to refresh."])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.toml"))
    args = parser.parse_args()
    print(format_report(snapshot(Settings.load(args.config))))


if __name__ == "__main__":
    main()
