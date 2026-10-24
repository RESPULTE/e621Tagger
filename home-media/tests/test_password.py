"""Password changes require SSH and a fresh Stash session to verify."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import paramiko
import pytest

from home_media import password
from home_media.config import Settings


def make_settings(tmp_path):
    return Settings(
        stash_url="http://localhost:19999",
        stash_key="existing-api-key",
        state=tmp_path / "state.sqlite",
        adult_roots=("/media/adult",),
        ssh_host="stash-host",
        ssh_user="stash-user",
    )


def access_record(path, password_value="old-password"):
    record = {"username": "stash-user", "password": password_value, "api_key": "keep-me"}
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def test_empty_password_is_rejected_before_reading_or_mutating(tmp_path, monkeypatch):
    monkeypatch.setattr(
        password,
        "verify_ssh_password",
        lambda *_args: pytest.fail("empty input must be rejected before SSH"),
    )

    with pytest.raises(ValueError, match="nonempty SSH password"):
        password.change_password(make_settings(tmp_path), "", tmp_path / "missing.json", lambda _: None)


def test_rejected_ssh_password_never_touches_stash_or_local_record(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    access_path = tmp_path / "access.json"
    original = access_record(access_path)
    before = access_path.read_bytes()
    stages = []
    monkeypatch.setattr(
        password,
        "verify_ssh_password",
        lambda *_args: (_ for _ in ()).throw(paramiko.AuthenticationException()),
    )
    monkeypatch.setattr(
        password,
        "api_tunnel",
        lambda *_args: pytest.fail("rejected SSH password must not open Stash tunnel"),
    )
    monkeypatch.setattr(
        password,
        "Stash",
        lambda *_args: pytest.fail("rejected SSH password must not access Stash"),
    )

    with pytest.raises(paramiko.AuthenticationException):
        password.change_password(settings, "wrong-password", access_path, stages.append)

    assert stages == ["verifying_ssh"]
    assert json.loads(access_path.read_text(encoding="utf-8")) == original
    assert access_path.read_bytes() == before


def test_verified_password_updates_stash_then_fresh_login_then_local_record(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    access_path = tmp_path / "access.json"
    access_record(access_path)
    events = []

    class FakeStash:
        def __init__(self, url, api_key):
            assert url == settings.stash_url
            assert api_key == "existing-api-key"
            self.client = SimpleNamespace(close=lambda: events.append("stash_closed"))

        def query(self, document, variables=None):
            if variables is None:
                events.append("read_username")
                return {"configuration": {"general": {"username": "stash-user"}}}
            events.append("stash_mutation")
            assert variables == {"input": {"password": "new-password"}}
            return {"configureGeneral": {"username": "stash-user"}}

    @contextmanager
    def tunnel(_settings):
        events.append("tunnel_open")
        try:
            yield
        finally:
            events.append("tunnel_closed")

    def verify_ssh(_settings, value):
        assert value == "new-password"
        events.append("ssh_verified")

    def verify_login(url, username, value):
        assert (url, username, value) == (
            settings.stash_url,
            "stash-user",
            "new-password",
        )
        events.append("fresh_login_verified")

    monkeypatch.setattr(password, "verify_ssh_password", verify_ssh)
    monkeypatch.setattr(password, "api_tunnel", tunnel)
    monkeypatch.setattr(password, "Stash", FakeStash)
    monkeypatch.setattr(password, "verify_stash_login", verify_login)

    stages = []
    password.change_password(settings, "new-password", access_path, stages.append)

    assert events == [
        "ssh_verified",
        "tunnel_open",
        "read_username",
        "stash_mutation",
        "fresh_login_verified",
        "stash_closed",
        "tunnel_closed",
    ]
    assert stages == [
        "verifying_ssh",
        "updating_stash",
        "verifying_stash_login",
        "complete",
    ]
    assert json.loads(access_path.read_text(encoding="utf-8")) == {
        "username": "stash-user",
        "password": "new-password",
        "api_key": "keep-me",
    }


def test_failed_fresh_login_keeps_old_record_and_closes_stash(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    access_path = tmp_path / "access.json"
    original = access_record(access_path)
    before = access_path.read_bytes()
    events = []

    class FakeStash:
        def __init__(self, *_args):
            self.client = SimpleNamespace(close=lambda: events.append("stash_closed"))

        def query(self, _document, variables=None):
            if variables is None:
                return {"configuration": {"general": {"username": "stash-user"}}}
            events.append("stash_mutation")
            return {}

    @contextmanager
    def tunnel(_settings):
        yield

    monkeypatch.setattr(password, "verify_ssh_password", lambda *_args: None)
    monkeypatch.setattr(password, "api_tunnel", tunnel)
    monkeypatch.setattr(password, "Stash", FakeStash)
    monkeypatch.setattr(
        password,
        "verify_stash_login",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("fresh login failed")),
    )
    stages = []

    with pytest.raises(RuntimeError, match="fresh login failed"):
        password.change_password(settings, "new-password", access_path, stages.append)

    assert events == ["stash_mutation", "stash_closed"]
    assert stages == ["verifying_ssh", "updating_stash", "verifying_stash_login"]
    assert "complete" not in stages
    assert json.loads(access_path.read_text(encoding="utf-8")) == original
    assert access_path.read_bytes() == before


def test_ssh_verification_requires_password_and_known_hosts(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    calls = []

    class FakeSSH:
        def load_system_host_keys(self, path):
            calls.append(("known_hosts", path))

        def connect(self, *args, **kwargs):
            calls.append(("connect", args, kwargs))

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(password.paramiko, "SSHClient", FakeSSH)
    password.verify_ssh_password(settings, "secret")

    assert calls[0] == ("known_hosts", str(password.Path.home() / ".ssh/known_hosts"))
    assert calls[1] == (
        "connect",
        ("stash-host",),
        {
            "username": "stash-user",
            "password": "secret",
            "allow_agent": False,
            "look_for_keys": False,
            "timeout": 15,
            "auth_timeout": 20,
            "banner_timeout": 15,
        },
    )
    assert calls[-1] == ("close",)
