import json
import sqlite3
from datetime import datetime

from home_media import progress
from home_media.config import Settings


def make_settings(tmp_path):
    return Settings(
        stash_url="http://localhost",
        stash_key="test-key",
        state=tmp_path / "state.sqlite",
        adult_roots=("/media/adult",),
    )


def write_manifest(directory, name, started_at, *, expected_images=0, expected_videos=0):
    path = directory / name
    path.write_text(json.dumps({
        "started_at": started_at,
        "expected_images": expected_images,
        "expected_videos": expected_videos,
        "stderr": f"{name}.log",
    }), encoding="utf-8")
    return path


def create_jobs(path, rows):
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE jobs (entity TEXT, status TEXT, error TEXT, updated REAL)"
        )
        database.executemany("INSERT INTO jobs VALUES(?,?,?,?)", rows)


def epoch(value):
    return datetime.fromisoformat(value).timestamp()


def test_snapshot_uses_newest_manifest_and_excludes_jobs_before_run(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    write_manifest(
        tmp_path, "current-run.json", "2026-10-18T10:00:00+00:00",
        expected_images=1, expected_videos=2,
    )
    write_manifest(
        tmp_path, "worker-control.json", "2026-10-18T11:00:00+00:00",
        expected_images=2, expected_videos=5,
    )
    create_jobs(settings.state, [
        ("old-done", "done", None, epoch("2026-10-18T10:59:59+00:00")),
        ("new-done", "done", None, epoch("2026-10-18T11:00:01+00:00")),
        ("new-failed", "failed", "decode error", epoch("2026-10-18T11:00:02+00:00")),
    ])
    monkeypatch.setattr(progress, "worker_available", lambda _settings: True)

    result = progress.snapshot(settings)

    assert result["started"] == epoch("2026-10-18T11:00:00+00:00")
    assert result["expected"] == 7
    assert result["log"] == "worker-control.json.log"
    assert result["known"] == 3
    assert result["counts"] == {"done": 1, "failed": 1}
    assert result["overall_counts"] == {"done": 2, "failed": 1}
    assert result["failures"] == [("new-failed", "decode error")]


def test_snapshot_without_database_reports_zero_progress(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    write_manifest(tmp_path, "current-run.json", "2026-10-18T10:00:00+00:00",
                   expected_images=1, expected_videos=2)
    monkeypatch.setattr(progress, "worker_available", lambda _settings: True)

    result = progress.snapshot(settings)

    assert result["known"] == 0
    assert result["counts"] == {}
    assert result["overall_counts"] == {}
    assert result["expected"] == 3
    assert "Latest batch completed: 0 / 3 (0.0%)" in progress.format_report(result)


def test_stopped_worker_with_running_job_is_unfinished(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    write_manifest(tmp_path, "current-run.json", "2026-10-18T10:00:00+00:00",
                   expected_images=0, expected_videos=1)
    create_jobs(settings.state, [
        ("scene-7", "running", None, epoch("2026-10-18T10:01:00+00:00")),
    ])
    monkeypatch.setattr(progress, "worker_available", lambda _settings: True)

    report = progress.format_report(progress.snapshot(settings))

    assert "Worker: NOT RUNNING" in report
    assert "Interrupted/unfinished: 1" in report
    assert "All recorded work in this run completed successfully." not in report
    assert "Work remains unfinished." in report


def test_failures_are_not_counted_as_done_or_reported_complete(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    write_manifest(tmp_path, "current-run.json", "2026-10-18T10:00:00+00:00",
                   expected_images=0, expected_videos=3)
    create_jobs(settings.state, [
        ("scene-1", "done", None, epoch("2026-10-18T10:01:00+00:00")),
        ("scene-2", "failed", "decode error", epoch("2026-10-18T10:02:00+00:00")),
    ])
    monkeypatch.setattr(progress, "worker_available", lambda _settings: True)

    report = progress.format_report(progress.snapshot(settings))

    assert "Latest batch completed: 1 / 3 (33.3%)" in report
    assert "Latest batch failed: 1 (unfinished; eligible for retry)" in report
    assert "Not started in this run: 1" in report
    assert "All recorded work in this run completed successfully." not in report
    assert "Work remains unfinished." in report


def test_report_distinguishes_overall_latest_job_counts_from_latest_batch(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    started_at = "2026-10-18T11:00:00+00:00"
    write_manifest(
        tmp_path,
        "current-run.json",
        started_at,
        expected_images=7,
        expected_videos=8,
    )
    rows = [
        (f"older-done-{index}", "done", None, epoch("2026-10-18T10:59:00+00:00"))
        for index in range(285)
    ]
    rows.extend(
        (f"batch-done-{index}", "done", None, epoch("2026-10-18T11:01:00+00:00"))
        for index in range(7)
    )
    rows.extend(
        (f"batch-failed-{index}", "failed", "decode error", epoch("2026-10-18T11:02:00+00:00"))
        for index in range(8)
    )
    create_jobs(settings.state, rows)
    monkeypatch.setattr(progress, "worker_available", lambda _settings: True)

    result = progress.snapshot(settings)
    report = progress.format_report(result)

    assert result["counts"] == {"done": 7, "failed": 8}
    assert result["overall_counts"] == {"done": 292, "failed": 8}
    assert "Latest batch completed: 7 / 15 (46.7%)" in report
    assert "Latest batch failed: 8 (unfinished; eligible for retry)" in report
    assert "Overall completed: 292 / 300 tracked files" in report
    assert "Overall failed: 8 (eligible for retry)" in report
