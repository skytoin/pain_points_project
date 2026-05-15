"""Wave 0 - Query Expansion station.

Takes a `JobSpec`, returns a Pydantic-validated `JobPlan` with 10-15
Reddit search queries the LLM brainstormed for this industry.

Flow:
    1. Compute cache key over (spec, prompt VERSION, model).
    2. Cache hit? Return cached JobPlan.
    3. Cache miss? Call OpenAI with the query-expansion prompt.
    4. instructor enforces the JobPlan schema; bad JSON -> exception.
    5. Run `validate_reddit_query` over each query; drop violators.
    6. If too few queries survive, raise `QueryExpansionError` -
       callers fall back to the deterministic template.
    7. Cache the validated, filtered plan.

Notes on temperature
--------------------
The skill default for stations is `temperature=0`. We deviate slightly
(0.2) because the LLM is brainstorming creative query designs, not
classifying anything. Determinism here would just echo the few-shot
examples. The skill contract is updated in the same slice that ships
this station - see `.claude/skills/llm-station/SKILL.md`.
"""

from __future__ import annotations

from loguru import logger

from discovery.config.settings import settings
from discovery.jobs import JobSpec
from discovery.llm.cache import cache_key, get_cached, make_cache, put_cached
from discovery.llm.client import call_openai
from discovery.llm.prompts import query_expansion
from discovery.llm.schemas import JobPlan, RedditQuerySpec
from discovery.orchestrator.reddit_query_validator import validate_reddit_query

MODEL: str = "gpt-5.4"
TEMPERATURE: float = 0.2
MIN_VALID_QUERIES: int = 10

# Skill item 9 — profile-agnostic baseline subreddits, merged into every
# JobPlan as defense in depth. Even if the LLM picks garbage domain-
# specific subs, these keep the adapter useful. Order matches the skill.
_BASELINE_SUBREDDITS: tuple[str, ...] = ("startups", "microsaas", "smallbusiness")


class QueryExpansionError(Exception):
    """Raised when the station can't produce a valid JobPlan."""


_cache = make_cache(settings.llm_cache_dir)


async def run_query_expansion(spec: JobSpec) -> JobPlan:
    """Return a `JobPlan` for `spec`, brainstormed by gpt-5.4 and
    validated against the Reddit search rules.

    Raises `QueryExpansionError` if the LLM call fails or too few
    queries survive validation. The caller (`plan_job`) catches this
    and falls back to the deterministic template.
    """
    key = cache_key(
        spec=spec.model_dump(mode="json"),
        prompt_version=query_expansion.VERSION,
        model=MODEL,
    )
    cached = get_cached(_cache, key, JobPlan)
    if cached is not None:
        logger.debug("query_expansion cache hit for {}", key[:12])
        return cached

    logger.info("query_expansion cache miss; calling {}", MODEL)
    try:
        raw_plan = await call_openai(
            system=query_expansion.SYSTEM_PROMPT,
            user=query_expansion.build_user_message(spec),
            response_model=JobPlan,
            model=MODEL,
            temperature=TEMPERATURE,
        )
    except Exception as e:
        raise QueryExpansionError(f"LLM call failed: {type(e).__name__}: {e}") from e

    filtered_plan = _drop_invalid_queries(raw_plan)
    if len(filtered_plan.reddit_queries) < MIN_VALID_QUERIES:
        raise QueryExpansionError(
            f"Only {len(filtered_plan.reddit_queries)} of "
            f"{len(raw_plan.reddit_queries)} queries passed validation; "
            f"need at least {MIN_VALID_QUERIES}."
        )

    aligned_plan = _force_time_window(filtered_plan, spec.time_window)
    final_plan = _merge_baseline_subreddits(aligned_plan)
    put_cached(_cache, key, final_plan)
    return final_plan


def _force_time_window(plan: JobPlan, time_window: str) -> JobPlan:
    """Override every query's `t` field to match the user's chosen window.

    The LLM is told to align via the prompt, but we don't trust it — a
    deterministic override here means the user's CLI choice is honored
    even if the LLM picks differently. Skill item 11.
    """
    new_queries = [q.model_copy(update={"t": time_window}) for q in plan.reddit_queries]
    return JobPlan.model_construct(
        reddit_queries=new_queries,
        reddit_subreddits=plan.reddit_subreddits,
    )


def _merge_baseline_subreddits(plan: JobPlan) -> JobPlan:
    """Append the skill's baseline subs (item 9) to the LLM-picked list.

    LLM picks keep their original order at the front; baselines that the
    LLM didn't already include are appended in skill order. No duplicates.
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
    """Return a new JobPlan keeping only queries that pass validation.

    Uses `model_construct` so the result skips the `min_length=10`
    check on `reddit_queries` - the caller is responsible for handling
    the "too few survived" case.
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
