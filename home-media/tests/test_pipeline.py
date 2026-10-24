from concurrent.futures import TimeoutError
from threading import Event

import pytest

from home_media.control import StopRequested
from home_media.pipeline import prefetched


def test_preparation_overlaps_and_stays_bounded_until_consumer_advances():
    started = [Event() for _ in range(4)]
    paths = []

    def prepare(index, directory, check):
        paths.append(directory)
        started[index].set()
        check()
        return index

    pipeline = prefetched(range(4), prepare, lambda: None)
    first, future = next(pipeline)
    assert first == future.result(timeout=5) == 0
    assert started[1].wait(5), "next file should prepare while caller holds current file"
    assert not started[2].is_set(), "lookahead must be bounded to one file"
    assert len(paths) == 2
    assert all(path.exists() for path in paths)
    second, future = next(pipeline)
    assert second == future.result(timeout=5) == 1
    assert not paths[0].exists(), "finished item's temporary media should be removed"
    pipeline.close()
    assert all(not path.exists() for path in paths)


def test_one_bad_file_does_not_discard_next_prepared_file():
    paths = []

    def prepare(index, directory, check):
        paths.append(directory)
        if index == 0:
            raise ValueError("bad media")
        return index

    pipeline = prefetched([0, 1], prepare, lambda: None)
    _, first = next(pipeline)
    with pytest.raises(ValueError, match="bad media"):
        first.result(timeout=5)
    identity, second = next(pipeline)
    assert identity == second.result(timeout=5) == 1
    with pytest.raises(StopIteration):
        next(pipeline)
    assert all(not path.exists() for path in paths)


def test_closing_cancels_preparation_before_removing_its_files():
    ready = Event()
    release = Event()
    paths = []
    cancelled = Event()

    def prepare(index, directory, check):
        paths.append(directory)
        if index == 0:
            return 0
        ready.set()
        assert release.wait(5)
        try:
            check()
        except StopRequested:
            assert directory.exists(), "cleanup must wait until writer has stopped"
            cancelled.set()
            raise

    pipeline = prefetched([0, 1], prepare, lambda: None)
    _, first = next(pipeline)
    assert first.result(timeout=5) == 0
    assert ready.wait(5)
    # Trigger cancellation in the generator's finally before allowing its writer to exit.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as closer:
        closing = closer.submit(pipeline.close)
        with pytest.raises(TimeoutError):
            closing.result(timeout=0.05)
        release.set()
        closing.result(timeout=5)
    assert cancelled.is_set()
    assert all(not path.exists() for path in paths)


def test_skipped_future_stops_its_writer_before_advancing_and_cleanup():
    from concurrent.futures import ThreadPoolExecutor

    ready, release, cancelled = Event(), Event(), Event()
    paths = {}

    def prepare(index, directory, check):
        paths[index] = directory
        if index == 0:
            ready.set()
            assert release.wait(5)
            try:
                check()
            except StopRequested:
                assert directory.exists()
                cancelled.set()
                raise
        return index

    pipeline = prefetched([0, 1], prepare, lambda: None)
    identity, _ = next(pipeline)  # Consumer skips the future after a cancelled queue claim.
    assert identity == 0
    assert ready.wait(5)
    with ThreadPoolExecutor(max_workers=1) as consumer:
        advancing = consumer.submit(next, pipeline)
        with pytest.raises(TimeoutError):
            advancing.result(timeout=0.05)
        release.set()
        identity, future = advancing.result(timeout=5)
    assert identity == future.result(timeout=5) == 1
    assert cancelled.is_set()
    assert not paths[0].exists()
    pipeline.close()
    assert not paths[1].exists()
