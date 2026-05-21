"""Wave 1 orchestration for HackerNews.

Bridges the Wave 0 LLM output (`JobPlan.hn_queries`) and the HN adapter's
fetch-params dict. Every brittle mechanical rule from the design spec
lives here in tested Python:

- Token decomposition (delegated to `discovery.sources.keyword_tokens`).
- Endpoint + tag routing from the LLM's per-candidate `intent` flag.
- Server-side `numericFilters` from `JobSpec.time_window` and `as_of`.
- The `MAX_HN_QUERIES=6` cap.

When `Job.job_plan` is null (Wave 0 failed) or fails validation, falls
back to the deterministic capability-first template so HN keeps working
with `OPENAI_API_KEY` unset -- mirroring Reddit's template fallback.

See `docs/specs/2026-05-20-hackernews-source-design.md` §10.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

_TIME_WINDOW_SECONDS: dict[str, int] = {
    "hour": 3_600,
    "day": 86_400,
    "week": 604_800,
    "month": 30 * 86_400,  # 2,592,000
    "year": 365 * 86_400,  # 31,536,000
}

# Routing table -- the deterministic 2:1 launch/context split that
# Python owns. Each entry maps an intent flag to (endpoint, tags,
# extra_numeric_filters). Created_at_i is layered on top by
# `_compile_hn_queries` from the JobSpec time window.
_ROUTING: dict[str, tuple[str, str, list[str]]] = {
    "launch": ("search_by_date", "show_hn", []),
    "context": ("search", "story", ["points>5", "num_comments>3"]),
}


def _time_window_epoch(time_window: str, as_of: date) -> int | None:
    """Compute the unix-seconds floor for `created_at_i` from the job's
    time window, anchored at `as_of` midnight UTC.

    `all` -> None (caller omits `created_at_i` entirely from
    numericFilters; the rest of the filter list still applies).

    `hour | day | week | month | year` -> integer epoch seconds.
    """
    if time_window == "all":
        return None
    if time_window not in _TIME_WINDOW_SECONDS:
        raise ValueError(f"unknown time window: {time_window!r}")
    anchor = datetime.combine(as_of, time.min, tzinfo=UTC)
    floor = anchor - timedelta(seconds=_TIME_WINDOW_SECONDS[time_window])
    return int(floor.timestamp())


def _routing_for(intent: str) -> tuple[str, str, list[str]]:
    """Map an intent flag to (endpoint, tags, extra_numeric_filters).

    Raises KeyError on unknown intent -- the LLM contract is enforced
    by the `HackerNewsKeywordSpec.intent` Literal, so an unknown value
    indicates a contract violation upstream.
    """
    return _ROUTING[intent]
