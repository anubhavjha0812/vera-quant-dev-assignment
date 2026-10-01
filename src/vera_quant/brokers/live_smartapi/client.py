"""The subset of the SmartAPI Python SDK this adapter calls, as our own
Protocol — tests exercise a fake implementing it, never the real network.

**Exact SDK method names/signatures here are this adapter's best-effort
mapping, not confirmed against the installed `smartapi-python` package.**
Only `generateSession`, `placeOrder`, `position`, `orderBook` and
`getCandleData` are directly evidenced by `reference/01-v6.py`; the rest
(`generateTokens`, modify/cancel, the margin calculator call) must be
verified against the SDK's actual API before a real `SmartConnect`
instance is wired in here — same "confirm in the docs" caveat the dev
plan already flags for ordertag length and the order-update stream.
"""
from __future__ import annotations

from typing import Protocol


class RateLimitError(Exception):
    """A 403 from SmartAPI — means the rate limit was hit, NOT an expired
    session. The adapter backs off on this, it never re-logs-in."""


class SessionExpiredError(Exception):
    """The session/token is genuinely invalid — the adapter re-logs-in on
    this, and only this."""


class SmartApiClient(Protocol):
    def generate_session(
        self, client_code: str, password: str, totp: str
    ) -> dict[str, object]: ...

    def generate_tokens(self, refresh_token: str) -> dict[str, object]: ...

    def place_order(self, params: dict[str, str]) -> dict[str, object]: ...

    def get_order_book(self) -> dict[str, object]: ...

    def get_margin(self, params: dict[str, object]) -> dict[str, object]: ...
