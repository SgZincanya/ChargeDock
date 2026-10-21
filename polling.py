"""Per-device retry deadlines. State stays in the UI process, never on disk."""
import time


class RetrySchedule:
    def __init__(self, keys, clock=time.monotonic):
        self.keys = tuple(keys)
        self.clock = clock
        self.failures = dict.fromkeys(self.keys, 0)
        self.deadlines = dict.fromkeys(self.keys, 0.0)

    def due(self, now=None):
        now = self.clock() if now is None else now
        return [key for key in self.keys if self.deadlines[key] <= now]

    def delay_ms(self, now=None):
        now = self.clock() if now is None else now
        return max(1, int((min(self.deadlines.values()) - now) * 1000) + 1)

    def record(self, readings, attempted, interval, now=None):
        now = self.clock() if now is None else now
        by_key = {r.key: r for r in readings}
        for key in attempted:
            reading = by_key.get(key)
            if reading and reading.level is not None and not reading.error:
                self.failures[key] = 0
                delay = interval
            else:
                self.failures[key] = min(7, self.failures[key] + 1)
                delay = min(300, 5 * 2 ** (self.failures[key] - 1))
            self.deadlines[key] = now + delay

    def reset(self, keys=None):
        for key in self.keys if keys is None else keys:
            self.deadlines[key] = 0.0
            self.failures[key] = 0

    def change_interval(self, interval, now=None):
        now = self.clock() if now is None else now
        for key in self.keys:
            if not self.failures[key]:
                self.deadlines[key] = now + interval
