"""clock_monotonic — real monotonic clock (ms). The only place that uses time.* on the
py side of the runtime; core does not import it but receives it via Clock."""

from __future__ import annotations

import time


class MonotonicClock:
    def now_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000
