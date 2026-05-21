"""Wave 0 — Query Expansion station (grounded subreddit discovery).

Public entry `run_query_expansion(spec) -> JobPlan` is UNCHANGED.
Internally it is now a multi-step process (spec §3):

    1. Combined cache key over (spec, sp.VERSION+qe.VERSION, model).
       Cache hit → return cached JobPlan (skips everything below).
    2. LLM Call #1 (subreddit_phrases) → semantic search phrases.
    3. Reddit /subreddits/search per phrase → SubredditCandidate DTOs.
    4. Deterministic middle (no LLM): dedupe+consensus → drop
       non-public → drop NSFW → median → drop drastically-below-median
       → activity_ratio.
    5. LLM Call #2 (query_expansion v5) → JobPlan: selects ONLY from the
       supplied table and designs the 25-30 content queries.
    6. Defensive off-table reject + overflow trim (≤30, LLM order).
    7. EXISTING deterministic tail, UNCHANGED and order-preserved:
       _drop_invalid_queries → MIN_VALID_QUERIES → _force_time_window
       → _merge_baseline_subreddits.
    8. Cache the final JobPlan under the combined key.

Any failure raises `QueryExpansionError`; `plan_job` already catches it
and the Reddit orchestrator falls back to the deterministic template
(spec §10 — no new fallback branches).

Temperature 0.2 (not the skill default 0): Call #2 brainstorms creative
query designs; Call #1 brainstorms phrases. Documented in
`.claude/skills/llm-station/SKILL.md`'s per-station deviation table.
"""

from __future__ import annotations

from loguru import logger

from discovery.config.settings import settings
from discovery.jobs import JobSpec
from discovery.llm.cache import cache_key, get_cached, make_cache, put_cached
from discovery.llm.client import call_openai
from discovery.llm.prompts import query_expansion, subreddit_phrases
from discovery.llm.schemas import (
    HackerNewsKeywordSpec,
    JobPlan,
    RedditQuerySpec,
    SubredditSearchPhrases,
)
from discovery.llm.stations.subreddit_selection import (
    dedupe_and_count,
    drop_below_median,
    drop_non_public,
    drop_nsfw,
    reject_off_table,
    subscriber_median,
    trim_overflow,
    with_activity_ratio,
)
from discovery.orchestrator.reddit_query_validator import validate_reddit_query
from discovery.sources.reddit_subreddits import (
    PhraseResult,
    SubredditCandidate,
    search_subreddits,
)

MODEL: str = "gpt-5.4"
TEMPERATURE: float = 0.2
MIN_VALID_QUERIES: int = 10

# Skill item 9 — profile-agnostic baseline subreddits merged into every
# JobPlan as defense in depth (independent of discovery — spec §13).
_BASELINE_SUBREDDITS: tuple[str, ...] = ("startups", "microsaas", "smallbusiness")


class QueryExpansionError(Exception):
    """Raised when the station can't produce a valid JobPlan."""


_cache = make_cache(settings.llm_cache_dir)


async def run_query_expansion(spec: JobSpec) -> JobPlan:
    """Return a grounded `JobPlan` for `spec`. See module docstring.

    Raises `QueryExpansionError` on any failure in the chain; the caller
    (`plan_job`) catches it and falls back to the deterministic
    template.
    """
    key = cache_key(
        spec=spec.model_dump(mode="json"),
        prompt_version=f"{subreddit_phrases.VERSION}+{query_expansion.VERSION}",
        model=MODEL,
    )
    cached = get_cached(_cache, key, JobPlan)
    if cached is not None:
        logger.debug("query_expansion cache hit for {}", key[:12])
        return cached

    logger.info("query_expansion cache miss; running grounded discovery")
    phrases = await _generate_phrases(spec)
    candidates = await _discover_subreddits(phrases)
    raw_plan = await _select_and_design(spec, candidates)

    hn_queries = list(raw_plan.hn_queries)  # capture once
    grounded = _ground_selection(raw_plan, candidates)
    final_plan = _finalize(grounded, spec)
    final_plan = _attach_hn_queries(final_plan, hn_queries)  # restore once
    put_cached(_cache, key, final_plan)
    return final_plan


async def _generate_phrases(spec: JobSpec) -> SubredditSearchPhrases:
    """LLM Call #1 — semantic subreddit-search phrases (spec §6 #1)."""
    try:
        return await call_openai(
            system=subreddit_phrases.SYSTEM_PROMPT,
            user=subreddit_phrases.build_user_message(spec),
            response_model=SubredditSearchPhrases,
            model=MODEL,
            temperature=TEMPERATURE,
        )
    except Exception as e:
        raise QueryExpansionError(f"phrase generation failed: {type(e).__name__}: {e}") from e


async def _discover_subreddits(
    phrases: SubredditSearchPhrases,
) -> list[SubredditCandidate]:
    """Reddit sub-search + the deterministic middle (spec §7 steps 1-7).

    Raises `QueryExpansionError` on total search wipeout or when nothing
    survives filtering.
    """
    try:
        results: list[PhraseResult] = await search_subreddits(
            list(phrases.phrases),
            user_agent=settings.reddit_user_agent,
        )
    except Exception as e:
        raise QueryExpansionError(f"subreddit search failed: {type(e).__name__}: {e}") from e

    candidates = dedupe_and_count(results)
    candidates = drop_non_public(candidates)
    candidates = drop_nsfw(candidates)
    if not candidates:
        raise QueryExpansionError("no public subreddits surfaced for any phrase")

    median = subscriber_median(candidates)
    candidates = drop_below_median(candidates, median)
    # Defensive guard. In practice unreachable: drop_below_median keeps
    # every sub with subscribers >= median/10, and the max-subscriber
    # candidate is always >= median >= median/10, so a non-empty list
    # can't be emptied here. Kept (and placed right after the drop it
    # guards) to fail safe if the pipeline is ever reordered.
    if not candidates:
        raise QueryExpansionError("all candidates dropped by the median floor")
    candidates = with_activity_ratio(candidates)

    logger.info(
        "subreddit discovery: {} candidates survived (median subs={})",
        len(candidates),
        median,
    )
    return candidates


async def _select_and_design(spec: JobSpec, candidates: list[SubredditCandidate]) -> JobPlan:
    """LLM Call #2 — grounded selection + query design (spec §6 #2)."""
    try:
        return await call_openai(
            system=query_expansion.SYSTEM_PROMPT,
            user=query_expansion.build_user_message(spec, candidates),
            response_model=JobPlan,
            model=MODEL,
            temperature=TEMPERATURE,
        )
    except Exception as e:
        raise QueryExpansionError(f"selection/query design failed: {type(e).__name__}: {e}") from e


def _ground_selection(plan: JobPlan, candidates: list[SubredditCandidate]) -> JobPlan:
    """Spec §7 step 9 + §10 defensive filter: drop off-table picks, then
    keep the LLM's first 30 in its emitted order.
    """
    selected = reject_off_table(list(plan.reddit_subreddits), candidates)
    selected = trim_overflow(selected)
    return JobPlan.model_construct(
        reddit_queries=plan.reddit_queries,
        reddit_subreddits=selected,
    )


def _finalize(plan: JobPlan, spec: JobSpec) -> JobPlan:
    """The EXISTING deterministic tail, UNCHANGED and order-preserved
    (spec §7 step 10): drop invalid queries → MIN_VALID_QUERIES check →
    force time window → merge baseline subs.
    """
    filtered_plan = _drop_invalid_queries(plan)
    if len(filtered_plan.reddit_queries) < MIN_VALID_QUERIES:
        raise QueryExpansionError(
            f"Only {len(filtered_plan.reddit_queries)} of "
            f"{len(plan.reddit_queries)} queries passed validation; "
            f"need at least {MIN_VALID_QUERIES}."
        )
    aligned_plan = _force_time_window(filtered_plan, spec.time_window)
    return _merge_baseline_subreddits(aligned_plan)


def _force_time_window(plan: JobPlan, time_window: str) -> JobPlan:
    """Override every query's `t` to the user's chosen window (skill
    item 11) — deterministic, even if the LLM picked differently.
    """
    new_queries = [q.model_copy(update={"t": time_window}) for q in plan.reddit_queries]
    return JobPlan.model_construct(
        reddit_queries=new_queries,
        reddit_subreddits=plan.reddit_subreddits,
    )


def _merge_baseline_subreddits(plan: JobPlan) -> JobPlan:
    """Append the skill's baseline subs (item 9) after the LLM picks;
    no duplicates; LLM order preserved at the front.
    """
    merged = list(plan.reddit_subreddits)
    seen = set(merged)
    for sub in _BASELINE_SUBREDDITS:
        if sub not in seen:
            merged.append(sub)
            seen.add(sub)
    return JobPlan.model_construct(
        reddit_queries=plan.reddit_queries,
        reddit_subreddits=merged,
    )


def _drop_invalid_queries(plan: JobPlan) -> JobPlan:
    """Keep only queries that pass `validate_reddit_query`. Uses
    `model_construct` so the result skips the `min_length=25` check —
    the caller handles the "too few survived" case.
    """
    kept: list[RedditQuerySpec] = []
    for q in plan.reddit_queries:
        errors = validate_reddit_query(q)
        if errors:
            logger.warning("dropping invalid LLM query: errors={} q={!r}", errors, q.q)
            continue
        kept.append(q)
    return JobPlan.model_construct(
        reddit_queries=kept,
        reddit_subreddits=plan.reddit_subreddits,
    )


def _attach_hn_queries(plan: JobPlan, hn_queries: list[HackerNewsKeywordSpec]) -> JobPlan:
    """Single point that re-attaches `hn_queries` to a post-tail plan.

    The locked Reddit tail (`_ground_selection`, `_force_time_window`,
    `_merge_baseline_subreddits`, `_drop_invalid_queries`) uses
    `JobPlan.model_construct(reddit_queries=..., reddit_subreddits=...)`
    at four sites and silently drops any non-Reddit fields. This helper
    is the carry-through: capture `hn_queries` once at the top of
    `run_query_expansion` (right after `_select_and_design`), let the
    locked tail run untouched, then call this helper exactly once to
    restore them before caching.

    Uses `model_construct` (skips validation) so the post-pruning
    Reddit fields -- which may be below the 25-30 band after
    `_drop_invalid_queries` -- still survive. The "too few survived"
    case is already enforced inside `_finalize`.

    See `docs/specs/2026-05-20-hackernews-source-design.md` §6.
    """
    return JobPlan.model_construct(
        reddit_queries=plan.reddit_queries,
        reddit_subreddits=plan.reddit_subreddits,
        hn_queries=hn_queries,
    )
