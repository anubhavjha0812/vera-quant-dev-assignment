from vera_quant.rate_limiter import RateLimiter


class FakeClock:
    """Deterministic clock: `sleep()` just advances `now` instead of blocking."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_allows_up_to_max_requests_without_waiting() -> None:
    limiter = RateLimiter(max_requests=3, window_seconds=1.0)
    clock = FakeClock()
    for _ in range(3):
        limiter.acquire(clock=clock, sleep=clock.sleep)
    assert clock.now == 0.0


def test_blocks_until_the_window_frees_up() -> None:
    limiter = RateLimiter(max_requests=3, window_seconds=1.0)
    clock = FakeClock()
    for _ in range(3):
        limiter.acquire(clock=clock, sleep=clock.sleep)
    limiter.acquire(clock=clock, sleep=clock.sleep)
    assert clock.now >= 1.0


def test_requests_spread_over_time_do_not_wait() -> None:
    limiter = RateLimiter(max_requests=2, window_seconds=1.0)
    clock = FakeClock()
    limiter.acquire(clock=clock, sleep=clock.sleep)
    clock.now = 0.5
    limiter.acquire(clock=clock, sleep=clock.sleep)
    clock.now = 1.1  # first call has now rolled out of the window
    limiter.acquire(clock=clock, sleep=clock.sleep)
    assert clock.now == 1.1


def test_rejects_non_positive_construction_params() -> None:
    import pytest

    with pytest.raises(ValueError):
        RateLimiter(max_requests=0, window_seconds=1.0)
    with pytest.raises(ValueError):
        RateLimiter(max_requests=1, window_seconds=0)
