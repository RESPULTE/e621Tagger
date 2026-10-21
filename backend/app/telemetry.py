"""Small bounded rolling samples; no images, query strings or identifiers."""

from collections import deque


class Telemetry:
    def __init__(self):
        self.samples = {}

    def record(self, name, value):
        self.samples.setdefault(name, deque(maxlen=256)).append(float(value))

    def snapshot(self):
        result = {}
        for name, samples in self.samples.items():
            ordered = sorted(samples)
            result[name] = {
                "samples": len(samples),
                "latest": round(samples[-1], 2),
                "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
            }
        return result
