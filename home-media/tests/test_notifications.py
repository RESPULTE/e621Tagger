from types import SimpleNamespace

import pytest

from observability.checker import Checker
from observability.incidents import Incidents
from observability.notifications import Notifications, readable_size


@pytest.mark.parametrize("size,expected", [
    (0, "0 KB"), (1, "0.001 KB"), (1000, "1 KB"), (999_999, "1 MB"),
    (2_500_000, "2.5 MB"), (1_700_000_000, "1.7 GB"),
])
def test_size_units(size, expected):
    assert readable_size(size) == expected


def completion(identity, *, at=1100, service="immich", size=2_500_000):
    return {"id": identity, "service": service, "completed_at": at,
            "images": 1, "videos": 0, "bytes": size}


def test_new_policy_discards_pending_legacy_notices_and_baselines_old_library(tmp_path):
    store = Incidents(tmp_path / "state.sqlite")
    store.enqueue("old", "recovery", "long old message", 1000)
    notices = Notifications(store, now=1000)
    assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert not notices.completed(completion("historic", at=999))
    assert notices.completed(completion("new"))
    assert notices.flush(now=1300) == 1
    assert store.db.execute("SELECT body FROM outbox").fetchone()[0] == (
        "Immich upload complete\n1 photo · 2.5 MB")
    store.close()


def test_duplicate_events_restart_and_quiet_period_do_not_resend_or_send_early(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Incidents(path)
    notices = Notifications(store, now=1000)
    assert notices.completed(completion("one", at=1100))
    assert not notices.completed(completion("one", at=1100))
    assert notices.flush(now=1150) == 0
    assert notices.completed(completion("two", at=1190))
    assert notices.flush(now=1300) == 0
    store.close()
    store = Incidents(path)
    notices = Notifications(store, now=1300)
    assert notices.since == 1000
    assert notices.flush(now=1320, busy={"immich"}) == 0
    assert notices.flush(now=1320) == 1
    assert notices.flush(now=1500) == 0
    assert not notices.completed(completion("one", at=1100))
    assert store.db.execute("SELECT body FROM outbox").fetchone()[0] == (
        "Immich upload complete\n2 photos · 5 MB")
    store.close()


def test_hydra_only_successful_terminal_event_with_actual_counts_is_a_completion(tmp_path):
    store = Incidents(tmp_path / "state.sqlite")
    notices = Notifications(store, now=1000)
    event = {"event_type": "worker_outcome", "outcome": "completed", "run_id": "a" * 32,
             "source_epoch": 1100, "failed": 0, "completed_images": 2,
             "completed_videos": 1, "completed_bytes": 1_500_000_000}
    for override in ({"outcome": "failed"}, {"outcome": "cancelled"},
                     {"event_type": "run_finished"}, {"failed": 1},
                     {"completed_bytes": None}, {"source_epoch": 999}):
        assert not notices.hydra({**event, **override})
    assert notices.hydra(event)
    assert not notices.hydra(event)
    assert notices.flush(now=1101) == 1
    assert store.db.execute("SELECT body FROM outbox").fetchone()[0] == (
        "Hydra tagging complete\n2 images, 1 video · 1.5 GB")
    store.close()


def test_one_docker_error_per_episode_and_silent_recovery(tmp_path):
    store = Incidents(tmp_path / "state.sqlite")
    notices = Notifications(store, now=1000)
    for _ in range(3):
        notices.docker("immich-server", "unhealthy", "Container stopped or unhealthy.")
    rows = store.db.execute("SELECT kind,body FROM outbox").fetchall()
    assert rows == [("error", "Docker error · immich-server\nContainer stopped or unhealthy.")]
    for _ in range(5):
        notices.docker("immich-server", "unhealthy", "Container stopped or unhealthy.")
    assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    for _ in range(3):
        notices.docker("immich-server", "healthy", "Healthy.")
    assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    store.observe("backup/family", "unhealthy", "Private diagnosis", failures=1)
    assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    store.close()


def test_unmanic_shows_verified_input_and_output_bytes(tmp_path):
    store = Incidents(tmp_path / "state.sqlite")
    notices = Notifications(store, now=1000)
    notices.completed({"id": "task", "service": "unmanic", "completed_at": 1100,
                       "images": 0, "videos": 1, "bytes": 400_000_000,
                       "input_bytes": 1_200_000_000})
    assert notices.flush(now=1300, busy={"unmanic"}) == 0
    assert notices.flush(now=1300) == 1
    body = store.db.execute("SELECT body FROM outbox").fetchone()[0]
    assert body == "Unmanic processing complete\n1 video · 1.2 GB → 400 MB"
    assert len(body) < 100 and "http" not in body
    store.close()


def test_disabling_source_holds_its_pending_summaries(tmp_path):
    store = Incidents(tmp_path / "state.sqlite")
    notices = Notifications(store, now=1000)
    assert notices.completed(completion("one", service="jellyfin"))
    assert notices.flush(now=1400, enabled={"hydra", "immich", "unmanic"}) == 0
    assert notices.flush(now=1400, enabled={"jellyfin"}) == 1
    store.close()


def test_stale_docker_log_feed_cannot_clear_pending_error(tmp_path, monkeypatch):
    monkeypatch.setattr("observability.checker.time.time", lambda: 5000)
    store = Incidents(tmp_path / "state.sqlite")
    notices = Notifications(store, now=1000)
    notices.docker("app", "unhealthy", "Log errors.", failures=1)
    store.set_state("notification_error/app", 1000)
    checker = Checker.__new__(Checker)
    checker.config = {"containers": ["app"]}
    checker.incidents, checker.notifications = store, notices
    checker.docker_health = {"app": ("healthy", "Healthy.")}
    checker.docker_errors, checker.health, checker.maintenance = {}, {}, {}
    latest = 4000
    checker.api = SimpleNamespace(search=lambda sql, *_args, **_kwargs:
                                  [{"latest": latest * 1e6}] if "AS latest FROM" in sql else [])
    for _ in range(3):
        checker.log_errors()
        checker.docker_notifications()
    assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    latest = 4990
    for _ in range(3):
        checker.log_errors()
        checker.docker_notifications()
    assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    store.close()
