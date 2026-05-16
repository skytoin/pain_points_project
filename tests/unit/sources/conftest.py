"""Autouse: each `sources` test starts with a fresh shared Reddit
limiter. The limiter is now a process-wide singleton
(discovery.sources.reddit_ratelimit). Today's reddit tests stay under
the 10/60.1s budget even sharing it, but once the sub-search client
tests land (Task 4) the per-test request count against the SAME
singleton exceeds the budget — without a per-test reset a later test
would block on a real ~60s sleep.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

from discovery.sources.reddit_ratelimit import reset_reddit_limiter


@pytest.fixture(autouse=True)
def _fresh_reddit_limiter() -> Generator[None, None, None]:
    reset_reddit_limiter()
    yield
    reset_reddit_limiter()
