"""Process-wide Reddit rate limiter (skill item 3).

Sub-discovery (Wave 0) and content-fetch (Wave 1) run in the SAME
process and share ONE 10-requests/minute unauthenticated Reddit budget.
If each component default-constructed its own `AsyncLimiter`, the real
request rate would silently double and earn 429s. This module owns the
single shared limiter; both `RedditSource` and the subreddit-search
client default to it.

Memoized into a module dict (not a `global`) — the same pattern as the
lazy OpenAI client singleton in `discovery.llm.client`.
`reset_reddit_limiter()` exists only so tests start each case with a
fresh budget; production never calls it.
"""

from __future__ import annotations

from aiolimiter import AsyncLimiter

# Skill item 3: ~10 requests/min unauthenticated. 60.1s (not 60.0) so
# clock skew can't bunch two requests into the same wall-clock second.
REDDIT_RATE: tuple[int, float] = (10, 60.1)

_singleton: dict[str, AsyncLimiter] = {}


def get_reddit_limiter() -> AsyncLimiter:
    """Return the process-wide shared Reddit `AsyncLimiter` (memoized)."""
    if "limiter" not in _singleton:
        _singleton["limiter"] = AsyncLimiter(REDDIT_RATE[0], REDDIT_RATE[1])
    return _singleton["limiter"]


def reset_reddit_limiter() -> None:
    """Drop the memoized limiter so the next `get_reddit_limiter()`
    builds a fresh one. Test-only.
    """
    _singleton.clear()
