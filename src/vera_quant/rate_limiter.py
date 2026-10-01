"""A single, reusable sliding-window rate limiter.

One tested implementation shared by every rate-limited call — historical
candle downloads (step 6) and every SmartAPI REST/WebSocket endpoint
(steps 12-13). Construct one instance per endpoint, sized to that
endpoint's documented limit (dev-plan Appendix A); never bypass it with a
raw call (CLAUDE.md rule 6).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    """Blocks the caller until a new call is allowed under a sliding
    `max_requests` per `window_seconds` limit. Thread-safe.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        if max_requests <= 0:
            raise ValueError(f"max_requests must be positive, got {max_requests}")
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be positive, got {window_seconds}")
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def _evict_expired(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= self._window_seconds:
            self._calls.popleft()

    def acquire(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Call before every request to the rate-limited endpoint.

        `clock`/`sleep` are injectable for deterministic tests; production
        callers use the defaults (real wall-clock time).
        """
        with self._lock:
            now = clock()
            self._evict_expired(now)
            if len(self._calls) >= self._max_requests:
                wait = self._window_seconds - (now - self._calls[0])
                if wait > 0:
                    sleep(wait)
                now = clock()
                self._evict_expired(now)
            self._calls.append(now)
