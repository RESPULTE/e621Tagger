from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from home_media import connection
from home_media.config import Settings
from home_media.queueing import QUEUE_FIELD, QueueRequest, load_requests


def make_settings(tmp_path):
    return Settings(
        stash_url="http://localhost",
        stash_key="test-key",
        state=tmp_path / "state.sqlite",
        adult_roots=("/media/adult",),
    )


def queued_image(identity="image-1"):
    return {
        "id": identity,
        "custom_fields": {
            QUEUE_FIELD: {"request_id": f"request-{identity}", "status": "queued"}
        },
        "visual_files": [{"path": f"/media/adult/{identity}.jpg"}],
    }


class FakeStash:
    def __init__(self, images=(), scenes=()):
        self.media = {"image": list(images), "scene": list(scenes)}
        self.close_calls = 0
        self.query_calls = []
        self.events = None
        self.client = SimpleNamespace(close=self.close)

    def close(self):
        self.close_calls += 1
        if self.events is not None:
            self.events.append("stash_closed")

    def items(self, kind):
        if self.events is not None:
            self.events.append(f"items:{kind}")
        return self.media[kind]

    def query(self, query):
        self.query_calls.append(query)
        if self.events is not None:
            self.events.append("server_pending")


def install_connection_check(monkeypatch, stash, events=None):
    monkeypatch.setattr(connection, "worker_available", lambda _settings: True)
    monkeypatch.setattr(connection, "Stash", lambda *_args: stash)
    stash.events = events

    @contextmanager
    def tunnel(_settings):
        if events is not None:
            events.append("tunnel_open")
        try:
            yield
        finally:
            if events is not None:
                events.append("tunnel_closed")

    monkeypatch.setattr(connection, "api_tunnel", tunnel)


def test_same_connection_is_suppressed_before_network_access(monkeypatch, tmp_path):
    monkeypatch.setattr(
        connection,
        "api_tunnel",
        lambda *_args: pytest.fail("same connection must not check the server"),
    )
    monkeypatch.setattr(
        connection,
        "prompt_for_batch",
        lambda *_args: pytest.fail("same connection must not prompt"),
    )

    result = connection.process_once(
        make_settings(tmp_path), Path("config.toml"), Path("upstream"), "conn-1",
        {"connection_id": "conn-1", "last_value": "preserved"},
    )

    assert result == {
        "connection_id": "conn-1", "last_value": "preserved", "status": "already_handled"
    }


def test_new_connection_checks_server(monkeypatch, tmp_path):
    stash = FakeStash()
    install_connection_check(monkeypatch, stash)
    monkeypatch.setattr(
        connection,
        "prompt_for_batch",
        lambda *_args: pytest.fail("empty queue must not prompt"),
    )

    result = connection.process_once(
        make_settings(tmp_path), Path("config.toml"), Path("upstream"), "conn-2",
        {"connection_id": "conn-1"},
    )

    assert result == {"connection_id": "conn-2", "status": "idle", "queued": 0}
    assert stash.close_calls == 1
    assert stash.query_calls == [
        'mutation{runPluginOperation(plugin_id:"home-media",args:{mode:"pending"})}'
    ]


def test_busy_worker_skips_network_prompt_and_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(connection, "worker_available", lambda _settings: False)
    for name in ("api_tunnel", "prompt_for_batch", "launch_approved"):
        monkeypatch.setattr(
            connection, name,
            lambda *_args, _name=name: pytest.fail(f"busy worker must skip {_name}"),
        )

    result = connection.process_once(
        make_settings(tmp_path), Path("config.toml"), Path("upstream"), "conn-1", {}
    )

    assert result == {"connection_id": "conn-1", "status": "worker_busy"}


@pytest.mark.parametrize(
    ("check_only", "items", "expected"),
    [
        (False, [], {"status": "idle", "queued": 0}),
        (True, [queued_image()], {"status": "queued", "queued": 1}),
    ],
)
def test_empty_or_check_only_queue_never_prompts_or_launches(
    monkeypatch, tmp_path, check_only, items, expected
):
    stash = FakeStash(images=items)
    install_connection_check(monkeypatch, stash)
    monkeypatch.setattr(
        connection,
        "prompt_for_batch",
        lambda *_args: pytest.fail("empty/check-only queue must not prompt"),
    )
    monkeypatch.setattr(
        connection,
        "launch_approved",
        lambda *_args: pytest.fail("empty/check-only queue must not launch"),
    )

    result = connection.process_once(
        make_settings(tmp_path), Path("config.toml"), Path("upstream"), "conn-1", {},
        check_only=check_only,
    )

    assert result == {"connection_id": "conn-1", **expected}
    assert stash.query_calls == ([] if check_only else [
        'mutation{runPluginOperation(plugin_id:"home-media",args:{mode:"pending"})}'
    ])


def test_declined_request_is_recorded_for_this_connection_without_launch(
    monkeypatch, tmp_path
):
    stash = FakeStash(images=[queued_image()])
    install_connection_check(monkeypatch, stash)
    prompt_calls = []
    monkeypatch.setattr(
        connection,
        "prompt_for_batch",
        lambda count, video_frames: prompt_calls.append((count, video_frames)) or False,
    )
    monkeypatch.setattr(
        connection,
        "launch_approved",
        lambda *_args: pytest.fail("declined request must not launch"),
    )

    result = connection.process_once(
        make_settings(tmp_path), Path("config.toml"), Path("upstream"), "conn-1", {}
    )

    assert prompt_calls == [(1, 8)]
    assert result == {"connection_id": "conn-1", "status": "declined", "queued": 1}
    assert len(stash.query_calls) == 1


def test_approved_batch_launches_fixed_requests_after_tunnel_closes(monkeypatch, tmp_path):
    events = []
    stash = FakeStash(images=[queued_image()])
    install_connection_check(monkeypatch, stash, events)
    prompt_calls = []
    monkeypatch.setattr(
        connection,
        "prompt_for_batch",
        lambda count, frame_count: prompt_calls.append((count, frame_count)) or True,
    )
    approved = [QueueRequest("image", "image-1", "request-image-1")]

    def launch(_settings, _config, _upstream, requests):
        events.append(("launch", requests))
        return 4242

    monkeypatch.setattr(connection, "launch_approved", launch)

    result = connection.process_once(
        make_settings(tmp_path), Path("config.toml"), Path("upstream"), "conn-2",
        {"connection_id": "conn-1"},
    )

    assert prompt_calls == [(1, 8)]
    assert events == [
        "tunnel_open",
        "server_pending",
        "items:image",
        "items:scene",
        "stash_closed",
        "tunnel_closed",
        ("launch", approved),
    ]
    assert stash.close_calls == 1
    assert result == {
        "connection_id": "conn-2", "status": "started", "approved": 1, "pid": 4242
    }


def test_server_unavailable_never_prompts_or_launches(monkeypatch, tmp_path):
    monkeypatch.setattr(connection, "worker_available", lambda _settings: True)

    @contextmanager
    def offline(_settings):
        raise ConnectionError("server offline")
        yield

    monkeypatch.setattr(connection, "api_tunnel", offline)
    monkeypatch.setattr(
        connection,
        "Stash",
        lambda *_args: pytest.fail("offline connection must not query the server"),
    )
    monkeypatch.setattr(
        connection,
        "prompt_for_batch",
        lambda *_args: pytest.fail("offline check must not prompt"),
    )
    monkeypatch.setattr(
        connection,
        "launch_approved",
        lambda *_args: pytest.fail("offline check must not launch"),
    )

    with pytest.raises(ConnectionError, match="server offline"):
        connection.process_once(
            make_settings(tmp_path), Path("config.toml"), Path("upstream"), "conn-1", {}
        )


def test_launch_approved_writes_exact_snapshot_and_uses_argv_without_shell(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    config = tmp_path / "settings.toml"
    upstream = tmp_path / "upstream"
    executable = tmp_path / "environment" / "python.exe"
    approved = [
        QueueRequest("image", "image-1", "request-1"),
        QueueRequest("scene", "scene-2", "request-2"),
    ]
    monkeypatch.setattr(connection.sys, "executable", str(executable))
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        snapshot = Path(argv[argv.index("--queue-file") + 1])
        assert load_requests(snapshot) == approved
        return SimpleNamespace(pid=9876)

    monkeypatch.setattr(connection.subprocess, "Popen", fake_popen)

    assert connection.launch_approved(settings, config, upstream, approved) == 9876

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert isinstance(argv, list)
    assert argv[0] == str(executable)
    assert argv[1:4] == ["-u", "-m", "home_media.cli"]
    assert argv[argv.index("--config") + 1] == str(config)
    assert argv[argv.index("--upstream") + 1] == str(upstream)
    assert Path(argv[argv.index("--queue-file") + 1]).is_file()
    assert "shell" not in kwargs
    assert kwargs["cwd"] == connection.ROOT
