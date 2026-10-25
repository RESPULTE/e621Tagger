"""Shared admission gate for metadata, previews and explicit downloads."""

import asyncio
import time


class UpstreamGate:
    def __init__(self, interval):
        self.interval = interval
        self.lock = asyncio.Lock()
        self.next = 0

    async def acquire(self):
        async with self.lock:
            # Event-loop timers can wake early (notably with Windows clock resolution).
            while (remaining := self.next - time.monotonic()) > 0:
                await asyncio.sleep(remaining)
            self.next = time.monotonic() + self.interval
