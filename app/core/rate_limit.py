"""
A small throttle for the doors anyone can knock on without an account:
signing in, signing up and starting a demo.

The counts live in memory, per worker — enough to slow a password guesser
or a script filling the database, without a store of its own. Several
workers each count on their own, so the real ceiling is the limit times the
workers: still a throttle, not an open door.
"""

import math
import time as clock
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import HTTPException, Request, status

from app.core.config import settings

# Past this many keys the ones quiet for a whole window are forgotten, so a
# public form seen by many networks can't grow the worker's memory for ever
SWEEP_AT = 10_000

_every: list["Limiter"] = []


class Limiter:
    """At most `limit()` hits per key in any `window` seconds. The limit is
    read on every check, so a changed setting applies at once."""

    def __init__(self, window: float, limit: Callable[[], int]) -> None:
        self.window = window
        self.limit = limit
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        _every.append(self)

    def _forget_the_quiet(self, now: float) -> None:
        quiet = [
            key for key, times in self.hits.items() if not times or now - times[-1] > self.window
        ]
        for key in quiet:
            del self.hits[key]

    def _recent(self, key: str, now: float) -> deque[float]:
        if len(self.hits) >= SWEEP_AT:
            self._forget_the_quiet(now)
        recent = self.hits[key]
        while recent and now - recent[0] > self.window:
            recent.popleft()
        return recent

    def allows(self, key: str) -> bool:
        """Whether one more would be let through — without counting it."""
        return len(self._recent(key, clock.monotonic())) < self.limit()

    def hit(self, key: str) -> None:
        now = clock.monotonic()
        self._recent(key, now).append(now)

    def take(self, key: str) -> bool:
        """Counts one more when it is let through. A refusal is not counted,
        so being turned away doesn't keep the door shut for longer."""
        now = clock.monotonic()
        recent = self._recent(key, now)
        if len(recent) >= self.limit():
            return False
        recent.append(now)
        return True

    def retry_after(self, key: str) -> int:
        """Seconds until the oldest counted hit leaves the window."""
        now = clock.monotonic()
        recent = self._recent(key, now)
        return max(1, math.ceil(recent[0] + self.window - now)) if recent else 1


def too_many(limiter: Limiter, key: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(limiter.retry_after(key))},
    )


def reset() -> None:
    """Every count forgotten — a fresh start for each test."""
    for limiter in _every:
        limiter.hits.clear()


def client_address(request: Request) -> str:
    """Who is asking. Behind the reverse proxy TRUSTED_PROXY vouches for, it
    is the last address in X-Forwarded-For — the one that proxy added; every
    entry before it the client could have written itself. Without a trusted
    proxy the header is ignored, or anyone could pick a fresh address per try."""
    if settings.TRUSTED_PROXY:
        hops = [hop.strip() for hop in request.headers.get("x-forwarded-for", "").split(",")]
        if hops[-1]:
            return hops[-1]
    return request.client.host if request.client else "unknown"
