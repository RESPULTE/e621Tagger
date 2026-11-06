import asyncio
import time

from backend.app.upstream import UpstreamGate


async def test_interactive_lanes_outrank_downloads_and_cancelled_waiters_leave():
    # Use multiple Windows clock ticks so observing admission outside the lock
    # is not dominated by timer resolution or scheduling jitter under suite load.
    gate = UpstreamGate(0.1)
    await gate.acquire()
    order = []

    async def admit(name, priority):
        await gate.lane(name).acquire(priority)
        order.append((name, time.monotonic()))

    download = asyncio.create_task(admit("download", 10))
    cancelled = asyncio.create_task(admit("cancelled", -20))
    preview = asyncio.create_task(admit("preview", 0))
    seek = asyncio.create_task(admit("seek", -10))
    await asyncio.sleep(0)
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    await asyncio.wait_for(asyncio.gather(download, preview, seek), 2)
    assert [name for name, _ in order] == ["seek", "preview", "download"]
    assert all(b[1] - a[1] >= 0.08 for a, b in zip(order, order[1:]))
    assert not gate.waiters


async def test_retry_after_defers_every_lane():
    gate = UpstreamGate(0)
    started = time.monotonic()
    await gate.lane("media").defer(0.06)
    await gate.lane("metadata").acquire()
    assert time.monotonic() - started >= 0.055


async def test_cooldown_extension_wakes_and_rechecks_pending_admission():
    gate = UpstreamGate(0)
    await gate.defer(0.03)
    pending = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0.01)
    started = time.monotonic()
    await gate.defer(0.06)
    await pending
    assert time.monotonic() - started >= 0.055
