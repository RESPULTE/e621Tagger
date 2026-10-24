"""Bounded CPU preparation while the caller runs inference on the GPU."""

import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from .control import StopRequested


def prefetched(items, prepare, stop_check, temporary_root=None):
    """Yield ordered futures, holding at most the current and next temporary item.

    Preparation owns no GPU objects. Exceptions stay attached to their item so one
    bad file does not discard the rest of the batch. Close this generator on exit.
    """
    items = iter(items)
    cancelled = Event()
    pending = deque()
    current = None
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-prepare")

    def check():
        if cancelled.is_set():
            raise StopRequested("Preparation cancelled")
        stop_check()

    def submit():
        item = next(items, None)
        if item is None:
            return
        directory = tempfile.TemporaryDirectory(prefix="home-media-", dir=temporary_root)
        item_cancelled = Event()

        def item_check():
            if item_cancelled.is_set():
                raise StopRequested("Prepared item no longer needed")
            check()

        future = pool.submit(prepare, item, Path(directory.name), item_check)
        pending.append((item, future, directory, item_cancelled))

    try:
        submit()
        submit()
        while pending:
            current = pending.popleft()
            item, future, directory, item_cancelled = current
            yield item, future
            # A cancelled queue claim may skip future.result() in the consumer.
            # Stop that preparation and join its writer before deleting its files.
            item_cancelled.set()
            future.cancel()
            try:
                future.result()
            except Exception:
                pass  # The consumer handles item failures; skipped items publish nothing.
            directory.cleanup()
            current = None
            submit()
    finally:
        cancelled.set()
        # Finish/cancel CPU work before removing files it could still be writing.
        pool.shutdown(wait=True, cancel_futures=True)
        if current:
            current[2].cleanup()
        for _, _, directory, _ in pending:
            directory.cleanup()
