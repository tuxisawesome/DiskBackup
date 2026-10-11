"""Speed and time-remaining estimates for the copy phase.

Copy time is modelled as bytes / throughput + files x per-file overhead. On an
HDD the overhead (creating the file, updating folders) dominates for small
files, so a bytes-only estimate is badly wrong for them.
"""

import time
from collections import deque

HALF_LIFE = 30.0     # seconds of streaming over which throughput is averaged
WINDOW = 5.0         # seconds behind the "current speed" figure


class Estimator:
    def __init__(self, total_bytes, total_files, prior_rate=None, prior_overhead=None,
                 clock=time.monotonic):
        self.total_bytes = total_bytes
        self.total_files = total_files
        self.done_bytes = 0
        self.done_files = 0
        self.prior_rate = prior_rate
        self.prior_overhead = prior_overhead
        self._clock = clock
        self._start = clock()
        self._paused_at = None
        self._paused_total = 0.0
        # decayed sums over chunk-to-chunk intervals: pure streaming, no per-file cost
        self._stream_bytes = 0.0
        self._stream_time = 0.0
        # totals over finished files, to separate out the per-file overhead
        self._closed_time = 0.0
        self._closed_bytes = 0
        self._file_start = None
        self._file_bytes = 0
        self._last_chunk = None
        self._recent = deque()

    # ------------------------------------------------------------------ clock

    def now(self):
        """Seconds of actual work since the start; pauses do not count."""
        moment = self._paused_at if self._paused_at is not None else self._clock()
        return moment - self._start - self._paused_total

    def pause(self):
        if self._paused_at is None:
            self._paused_at = self._clock()

    def resume(self):
        if self._paused_at is not None:
            self._paused_total += self._clock() - self._paused_at
            self._paused_at = None

    # ------------------------------------------------------------------ feeding

    def file_start(self):
        self._file_start = self.now()
        self._file_bytes = 0
        self._last_chunk = None

    def add_bytes(self, count):
        moment = self.now()
        if self._last_chunk is not None:
            gap = moment - self._last_chunk
            decay = 0.5 ** (gap / HALF_LIFE)
            self._stream_bytes = self._stream_bytes * decay + count
            self._stream_time = self._stream_time * decay + gap
        self._last_chunk = moment
        self._file_bytes += count
        self.done_bytes += count
        self._recent.append((moment, self.done_bytes))
        while len(self._recent) > 2 and moment - self._recent[0][0] > WINDOW:
            self._recent.popleft()

    def file_end(self):
        if self._file_start is not None:
            self._closed_time += self.now() - self._file_start
            self._closed_bytes += self._file_bytes
        self.done_files += 1
        self._file_start = None

    def discount(self, size, files=1):
        """Work that turned out not to be needed (skipped or already there)."""
        self.total_bytes = max(self.done_bytes, self.total_bytes - size)
        self.total_files = max(self.done_files, self.total_files - files)
        self._file_start = None

    def add_work(self, size, files=1):
        self.total_bytes += size
        self.total_files += files

    # ------------------------------------------------------------------ reading

    @property
    def measured_rate(self):
        """Streaming throughput in bytes/s seen in this run, or None if too little data."""
        if self._stream_time >= 0.5:
            return self._stream_bytes / self._stream_time
        return None

    @property
    def rate(self):
        return self.measured_rate or self.prior_rate

    @property
    def overhead(self):
        """Seconds per file that are not explained by its size."""
        rate = self.rate
        if rate and self.done_files:
            return max(0.0, self._closed_time - self._closed_bytes / rate) / self.done_files
        return self.prior_overhead

    def speed(self):
        """Bytes/s over the last few seconds."""
        if len(self._recent) >= 2:
            (t0, b0), (t1, b1) = self._recent[0], self._recent[-1]
            if t1 - t0 >= 0.3:
                return (b1 - b0) / (t1 - t0)
        return self.average()

    def average(self):
        elapsed = self.now()
        return self.done_bytes / elapsed if elapsed > 0 else 0.0

    def fraction(self):
        if self.total_bytes > 0:
            return min(1.0, self.done_bytes / self.total_bytes)
        if self.total_files > 0:
            return min(1.0, self.done_files / self.total_files)
        return 1.0

    def eta(self):
        """Seconds of work left, or None if there is nothing to go on yet."""
        left_bytes = max(0, self.total_bytes - self.done_bytes)
        left_files = max(0, self.total_files - self.done_files)
        if not left_bytes and not left_files:
            return 0.0
        rate = self.rate
        if rate:
            return left_bytes / rate + left_files * (self.overhead or 0.0)
        elapsed = self.now()
        if self.done_bytes and self.done_files:
            share = 0.5 * (left_bytes / self.done_bytes + left_files / self.done_files)
            return elapsed * share
        if self.done_bytes:
            return elapsed * left_bytes / self.done_bytes
        return None


def estimate(size, files, rate, overhead):
    """Rough duration of a copy before it starts, from the last run's figures."""
    if not rate:
        return None
    return size / rate + files * (overhead or 0.0)
