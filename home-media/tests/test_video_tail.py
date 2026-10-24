import sys
from types import SimpleNamespace

import pytest

from home_media import worker
from home_media.config import Settings


def make_settings(tmp_path, *, video_frames=8):
    return Settings(
        stash_url="http://localhost",
        stash_key="test-key",
        state=tmp_path / "state.sqlite",
        adult_roots=("/media/adult",),
        video_frames=video_frames,
    )


def test_failed_final_midpoint_retries_at_last_video_pts_and_returns_eight_samples(
    monkeypatch, tmp_path
):
    requested = worker.sample_times(2.0, 8)
    decode_calls = []
    tail_probes = []

    def fake_decode(_executable, _source, timestamp, destination):
        decode_calls.append(timestamp)
        if timestamp > 1.75:
            return SimpleNamespace(returncode=1, stderr=b"no frame")
        destination.write_bytes(b"frame")
        return SimpleNamespace(returncode=0, stderr=b"")

    def fake_probe(_executable, _source, duration, stop_check=None):
        tail_probes.append((duration, stop_check))
        return 1.75

    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        SimpleNamespace(get_ffmpeg_exe=lambda: "ffmpeg"),
    )
    monkeypatch.setattr(worker, "decode_video_frame", fake_decode)
    monkeypatch.setattr(worker, "last_video_timestamp", fake_probe)

    samples = worker.frames(
        tmp_path / "source.mp4", tmp_path, "scene", 2.0, make_settings(tmp_path)
    )

    assert len(samples) == 8
    assert [timestamp for timestamp, _ in samples[:-1]] == pytest.approx(requested[:-1])
    assert samples[-1][0] == 1.75
    assert decode_calls == pytest.approx([*requested, 1.75])
    assert tail_probes == [(2.0, None)]
    assert all(path.is_file() for _, path in samples)


def test_successful_midpoints_do_not_probe_the_video_tail(monkeypatch, tmp_path):
    decode_calls = []

    def fake_decode(_executable, _source, timestamp, destination):
        decode_calls.append(timestamp)
        destination.write_bytes(b"frame")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        SimpleNamespace(get_ffmpeg_exe=lambda: "ffmpeg"),
    )
    monkeypatch.setattr(worker, "decode_video_frame", fake_decode)
    monkeypatch.setattr(
        worker,
        "last_video_timestamp",
        lambda *_args, **_kwargs: pytest.fail("valid frames should not trigger a tail probe"),
    )

    samples = worker.frames(
        tmp_path / "source.mp4", tmp_path, "scene", 2.0, make_settings(tmp_path)
    )

    assert len(samples) == 8
    assert decode_calls == pytest.approx(worker.sample_times(2.0, 8))


def test_last_video_timestamp_uses_bounded_copyts_sseof_showinfo_probe(
    monkeypatch, tmp_path
):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stderr=b"frame pts_time:1.500\nframe pts_time:1.750\n",
        )

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker.last_video_timestamp("ffmpeg", tmp_path / "video.mp4", 2.0) == 1.75

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:8] == [
        "ffmpeg", "-v", "info", "-nostdin", "-copyts", "-sseof", "-0.5", "-threads"
    ]
    assert command[command.index("-vf") + 1] == "showinfo"
    assert command[-3:] == ["-f", "null", "-"]
    assert kwargs == {"capture_output": True, "timeout": 120}


def test_decode_failure_at_or_before_last_pts_raises_without_retry_or_sample(
    monkeypatch, tmp_path
):
    decode_calls = []

    def fake_decode(_executable, _source, timestamp, _destination):
        decode_calls.append(timestamp)
        if timestamp != 1.625:
            _destination.write_bytes(b"frame")
            return SimpleNamespace(returncode=0, stderr=b"")
        return SimpleNamespace(returncode=1, stderr=b"broken frame")

    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        SimpleNamespace(get_ffmpeg_exe=lambda: "ffmpeg"),
    )
    monkeypatch.setattr(worker, "decode_video_frame", fake_decode)
    monkeypatch.setattr(worker, "last_video_timestamp", lambda *_args, **_kwargs: 1.75)

    with pytest.raises(RuntimeError, match=r"Unable to decode video at 1\.625s: broken frame"):
        worker.frames(
            tmp_path / "source.mp4", tmp_path, "scene", 2.0,
            make_settings(tmp_path),
        )

    assert decode_calls == pytest.approx([*worker.sample_times(2.0, 8)[:6], 1.625])
    assert not (tmp_path / "frame-6.jpg").exists()


def test_empty_tail_probe_is_bounded_and_returns_none(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stderr=b"no video frames in seek window")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker.last_video_timestamp("ffmpeg", tmp_path / "video.mp4", 95.0) is None

    assert [command[command.index("-sseof") + 1] for command, _ in calls] == [
        "-0.5", "-2.0", "-10.0"
    ]
    assert all(kwargs["timeout"] == 120 for _, kwargs in calls)


def test_stop_check_during_tail_probe_aborts_before_ffmpeg(monkeypatch, tmp_path):
    class StopRequested(Exception):
        pass

    calls = []

    def stop_check():
        calls.append("checked")
        raise StopRequested("stop requested")

    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("stopped probe must not invoke ffmpeg"),
    )

    with pytest.raises(StopRequested, match="stop requested"):
        worker.last_video_timestamp(
            "ffmpeg", tmp_path / "video.mp4", 2.0, stop_check=stop_check
        )

    assert calls == ["checked"]
