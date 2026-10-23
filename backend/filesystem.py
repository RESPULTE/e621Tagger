"""Durability helpers for the local collection (POSIX deployment)."""

import asyncio
import os


def sync_directory(path):
    # Windows does not expose directory fsync through os.open. Linux production
    # requires it before acknowledging names added to or removed from a folder.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


async def disk(function, *args):
    """Join filesystem threads even on timeout/shutdown, retaining writer ownership."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        # Retrieve any thread exception before propagating cancellation. The
        # caller's lock and open file handles remain alive until work has ended.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


async def cancel_and_join(task):
    """Keep ownership while a cancelled operation finishes its disk cleanup."""
    task.cancel()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if not task.cancelled():
        task.exception()
