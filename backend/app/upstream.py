"""Shared admission gate for metadata, previews and explicit downloads."""

import asyncio
import itertools
import time

from .telemetry import Telemetry


class UpstreamLane:
    """A scheduling responsibility sharing the conservative aggregate budget."""

    def __init__(self, gate, name):
        self.gate, self.name = gate, name

    async def acquire(self, priority=0):
        await self.gate.acquire(priority, lane=self.name)

    async def defer(self, seconds):
        await self.gate.defer(seconds)


class UpstreamGate:
    def __init__(self, interval):
        self.interval = interval
        self.condition = asyncio.Condition()
        self.sequence = itertools.count()
        self.waiters = set()
        self.timings = Telemetry()
        self.next = 0

    def lane(self, name):
        return UpstreamLane(self, name)

    async def defer(self, seconds):
        async with self.condition:
            self.next = max(self.next, time.monotonic() + seconds)
            self.condition.notify_all()

    async def acquire(self, priority=0, *, lane="shared"):
        started = time.monotonic()
        ticket = (priority, next(self.sequence))
        async with self.condition:
            self.waiters.add(ticket)
            self.condition.notify_all()
            try:
                while True:
                    remaining = self.next - time.monotonic()
                    if ticket == min(self.waiters) and remaining <= 0:
                        self.next = time.monotonic() + self.interval
                        self.timings.record(f"{lane}_wait_ms", (time.monotonic() - started) * 1000)
                        return
                    try:
                        # Release the lock while waiting so new interactive work
                        # can outrank background work before the next admission.
                        await asyncio.wait_for(self.condition.wait(), timeout=max(0.001, remaining))
                    except asyncio.TimeoutError:
                        pass  # Recheck the deadline even when timers wake early.
            finally:
                self.waiters.remove(ticket)
                self.condition.notify_all()
