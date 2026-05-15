"""Pydantic schemas for LLM station outputs.

One model per station's output, plus shared sub-models. The output of
the Wave 0 (Query Expansion) station is `JobPlan`.

NOTE TO FUTURE SESSIONS
-----------------------
`JobPlan` uses `extra="allow"` so future prompts can emit additional
source fields (`youtube_queries`, `news_keywords`, `apollo_params`,
etc.) and they will round-trip through `Job.job_plan` JSON without any
change here. BUT — to actually CONSUME those fields in app code (e.g.
wire YouTube queries into a YouTubeSource adapter), you MUST add a
typed field on this model AND wire the orchestrator to read from it.
Don't reach into `plan.model_extra["youtube_queries"]` from app code;
that's a bug-magnet because the field isn't validated. Add the field,
then use it.

The fields below are the only ones Wave 0 needs today: Reddit-shaped
because Reddit is the only source built so far.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RedditQuerySpec(BaseModel):
    """One LLM-built Reddit search query.

    The LLM fills `q` with a complete OR-compressed Reddit search string
    per the rules in `.claude/skills/reddit-source/SKILL.md` (items 6,
    7, 8, 10, 12, 13). Python validation in
    `discovery.orchestrator.reddit_query_validator` catches the rules
    the LLM might still slip on (uppercase operators, URL ceiling,
    valid subreddit names). Queries that don't pass validation are
    dropped before being sent to Reddit.
    """

    model_config = ConfigDict(frozen=True)

    endpoint: Literal["per_sub", "site_wide"]
    q: str = Field(min_length=1, max_length=3900)
    sort: Literal["top", "hot", "new"] = "top"
    t: Literal["hour", "day", "week", "month", "year", "all"] = "month"
    limit: int = Field(default=100, ge=1, le=100)
    rationale: str = Field(
        min_length=1,
        description=(
            "Why this query is worth running. Forces the LLM to "
            "explain itself; logged with the query for debugging "
            "bad plans."
        ),
    )


class JobPlan(BaseModel):
    """LLM-produced query plan for one Job. Wave 0's output.

    See module docstring for why `extra="allow"` and how future
    sessions should extend it.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    reddit_queries: list[RedditQuerySpec] = Field(min_length=10, max_length=15)
    reddit_subreddits: list[str] = Field(default_factory=list)
