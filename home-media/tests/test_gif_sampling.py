import pytest
from PIL import Image

from home_media import worker
from home_media.config import Settings

FRAME_COLORS = ["red", "green", "blue", "yellow"]


def make_settings(tmp_path, *, video_frames=8, max_frames=32):
    return Settings(
        stash_url="http://localhost",
        stash_key="test-key",
        state=tmp_path / "state.sqlite",
        adult_roots=("/media/adult",),
        video_frames=video_frames,
        max_frames=max_frames,
    )


def save_gif(path, colors, durations):
    frames = [Image.new("RGB", (2, 2), color) for color in colors]
    frames[0].save(
        path,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
        optimize=False,
    )
    return path


def sample_colors(samples):
    colors = []
    for _, path in samples:
        with Image.open(path) as image:
            colors.append(image.convert("RGB").getpixel((0, 0)))
    return colors


def test_gif_variable_durations_are_sampled_at_eight_playback_midpoints(
    tmp_path
):
    gif_path = save_gif(
        tmp_path / "variable.gif",
        FRAME_COLORS,
        [100, 300, 100, 500],
    )

    samples = worker.frames(
        gif_path, tmp_path, "gif", 999, make_settings(tmp_path)
    )

    expected_times = worker.sample_times(1.0, 8)
    assert len(samples) == 8
    assert [timestamp for timestamp, _ in samples] == pytest.approx(expected_times)
    assert sample_colors(samples) == [
        (255, 0, 0),
        (0, 128, 0), (0, 128, 0),
        (0, 0, 255),
        (255, 255, 0), (255, 255, 0), (255, 255, 0), (255, 255, 0),
    ]


def test_two_frame_gif_repeats_frames_to_fill_eight_sample_positions(
    tmp_path
):
    gif_path = save_gif(tmp_path / "two-frame.gif", ["red", "blue"], [100, 100])

    samples = worker.frames(gif_path, tmp_path, "gif", 0, make_settings(tmp_path))

    assert len(samples) == 8
    assert [timestamp for timestamp, _ in samples] == pytest.approx(worker.sample_times(0.2))
    assert sample_colors(samples) == [
        (255, 0, 0), (255, 0, 0), (255, 0, 0), (255, 0, 0),
        (0, 0, 255), (0, 0, 255), (0, 0, 255), (0, 0, 255),
    ]


def test_static_gif_repeats_its_only_frame_at_all_eight_positions(tmp_path):
    gif_path = save_gif(tmp_path / "static.gif", ["purple"], [100])

    samples = worker.frames(gif_path, tmp_path, "gif", 0, make_settings(tmp_path))

    assert len(samples) == 8
    assert [timestamp for timestamp, _ in samples] == pytest.approx(worker.sample_times(0.1))
    assert sample_colors(samples) == [(128, 0, 128)] * 8


def test_zero_and_missing_gif_frame_delays_use_100ms_fallback(tmp_path):
    class SavedFrame:
        def __init__(self, marker):
            self.marker = marker

        def save(self, path):
            path.write_text(self.marker, encoding="ascii")

    class FakeGif:
        n_frames = 2

        def __init__(self):
            self.info = {}
            self.index = 0

        def seek(self, index):
            self.index = index
            self.info = {"duration": 0} if index == 0 else {}

        def convert(self, _mode):
            return SavedFrame(str(self.index))

    samples = worker.gif_frames(FakeGif(), tmp_path, 8)

    assert [timestamp for timestamp, _ in samples] == pytest.approx(worker.sample_times(0.2))
    assert [path.read_text(encoding="ascii") for _, path in samples] == [
        "0", "0", "0", "0", "1", "1", "1", "1"
    ]


def test_stop_during_gif_decode_aborts_before_next_sample(tmp_path):
    gif_path = save_gif(tmp_path / "stoppable.gif", ["red", "green", "blue"], [100] * 3)
    checks = 0

    class StopRequested(Exception):
        pass

    def stop_check():
        nonlocal checks
        checks += 1
        if checks == 6:
            raise StopRequested("stop requested")

    with pytest.raises(StopRequested, match="stop requested"):
        worker.frames(
            gif_path, tmp_path, "gif", 0, make_settings(tmp_path), stop_check=stop_check
        )

    assert (tmp_path / "frame-0.png").is_file()
    assert not (tmp_path / "frame-1.png").exists()


@pytest.mark.parametrize("sample_count", [1, 7, 32])
def test_gif_prediction_requires_exact_configured_sample_count(
    tmp_path, sample_count
):
    prediction = {
        "times": list(range(sample_count)),
        "scores": {"tag": 0.9},
        "score_format": "probability-v1",
    }

    assert not worker.prediction_complete(prediction, "gif", make_settings(tmp_path))


def test_gif_prediction_accepts_exactly_eight_samples(tmp_path):
    prediction = {
        "times": list(range(8)),
        "scores": {"tag": 0.9},
        "score_format": "probability-v1",
    }

    assert worker.prediction_complete(prediction, "gif", make_settings(tmp_path))


def test_non_gif_animation_continues_to_use_max_frames(tmp_path):
    pages = [Image.new("RGB", (1, 1), (index % 255, 0, 0)) for index in range(40)]
    source = tmp_path / "animated.tiff"
    pages[0].save(source, format="TIFF", save_all=True, append_images=pages[1:])

    samples = worker.frames(source, tmp_path, "image", 0, make_settings(tmp_path))

    assert len(samples) == 32
    assert len({path for _, path in samples}) == 32
