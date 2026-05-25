"""Pydantic schemas for LLM station outputs.

One model per station's output, plus shared sub-models. Wave 0 has two
LLM calls: Call #1 emits `SubredditSearchPhrases` (intermediate — NOT
cached separately, see spec §8) and Call #2 emits `JobPlan` (the
station's final, cached output).

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

The fields below are what Wave 0 needs today: Reddit fields
(`reddit_queries`, `reddit_subreddits`) and HN field (`hn_queries`).
Each is consumed by its respective orchestrator under
`discovery.orchestrator.`. Adding a new source means: add a typed
field here AND wire its orchestrator there.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    subreddit: str | None = Field(
        default=None,
        description=(
            "Required for endpoint='per_sub' — the single subreddit to "
            "scope into (no `r/` prefix). Must be None for "
            "endpoint='site_wide' (where subreddit clauses live inside "
            "the `q` string)."
        ),
    )
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

    @model_validator(mode="after")
    def _check_subreddit_matches_endpoint(self) -> Self:
        if self.endpoint == "per_sub" and self.subreddit is None:
            raise ValueError(
                "per_sub queries require a `subreddit` value "
                "(the single sub the endpoint scopes into)."
            )
        if self.endpoint == "site_wide" and self.subreddit is not None:
            raise ValueError(
                "site_wide queries must not set `subreddit` — list "
                "subreddits inside `q` with subreddit:NAME clauses."
            )
        return self


class HackerNewsKeywordSpec(BaseModel):
    """Wave 0 LLM HN keyword candidate. Python downstream decomposes,
    routes by intent, and compiles to an Algolia URL. Schema lives in
    spec §7; deterministic routing table is in §10. See
    `docs/specs/2026-05-20-hackernews-source-design.md`.
    """

    model_config = ConfigDict(frozen=True)

    keyword: str = Field(
        min_length=1,
        max_length=80,
        description=(
            "Raw HN-suitable phrase, 2-4 words, casing preserved. "
            "Python keeps the first 2 surviving content tokens after "
            "stopword stripping; long phrases lose their tail tokens."
        ),
    )
    intent: Literal["launch", "context"] = Field(
        description=(
            "launch -> fired against /search_by_date with tags=show_hn "
            "and a relaxed quality floor (recency is the signal). "
            "context -> fired against /search with tags=story and the "
            "standard points/num_comments floor."
        ),
    )
    rationale: str = Field(
        min_length=1,
        description="Why this HN candidate is worth running.",
    )


class YouTubeQuerySpec(BaseModel):
    """Wave 0 LLM YouTube search candidate. Python downstream normalizes,
    dedupes, applies the time-window publishedAfter floor, caps at
    MAX_YT_QUERIES, and runs the three-step fetch. See
    `docs/specs/2026-05-22-youtube-source-design.md` sections 8-10.
    """

    model_config = ConfigDict(frozen=True)

    query: str = Field(
        min_length=1,
        max_length=120,
        description=(
            "Full-text YouTube search phrase, emotion/pain-shaped and "
            "re-derived for THIS industry (e.g. 'why I quit commercial "
            "cleaning', 'Jobber vs Housecall Pro'). Used near-verbatim as "
            "the `q` parameter; YouTube is full-text relevance search, "
            "NOT token-AND, so no decomposition is applied."
        ),
    )
    intent: Literal["complaint", "discussion"] = Field(
        description=(
            "complaint -> the video itself is the pain (why-I-quit, "
            "horror stories, rant, worst-part, wish-I-knew). discussion "
            "-> the pain is in the comments and the video reveals "
            "tools/workflows (tutorials, tips, reviews, A-vs-B, "
            "day-in-the-life). Used for LLM generation balance and "
            "downstream Wave 2 tagging; does NOT route API params."
        ),
    )
    rationale: str = Field(
        min_length=1,
        description="Why this YouTube candidate is worth running.",
    )


class JobPlan(BaseModel):
    """LLM-produced query plan for one Job. Wave 0's output.

    See module docstring for why `extra="allow"` and how future
    sessions should extend it.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    reddit_queries: list[RedditQuerySpec] = Field(min_length=25, max_length=30)
    reddit_subreddits: list[str] = Field(default_factory=list)
    hn_queries: list[HackerNewsKeywordSpec] = Field(
        default_factory=list,
        description=(
            "Wave 0 HN keyword candidates. Permissive default (no "
            "min_length) is deliberate: a strict floor would let HN "
            "under-production raise QueryExpansionError and sink the "
            "Reddit grounded plan. HN sparsity must degrade gracefully "
            "to the no-LLM template in orchestrator/hackernews.py."
        ),
    )
    youtube_queries: list[YouTubeQuerySpec] = Field(
        default_factory=list,
        description=(
            "Wave 0 YouTube search candidates. Permissive default (no "
            "min_length) is deliberate: a strict floor would let YouTube "
            "under-production raise QueryExpansionError and sink the "
            "Reddit grounded plan. Sparsity degrades to the no-LLM "
            "template in orchestrator/youtube.py."
        ),
    )


class SubredditSearchPhrases(BaseModel):
    """Wave 0 LLM Call #1 output: semantic phrases to SEARCH Reddit's
    subreddit index with — NOT subreddit names (spec §6 prompt #1).
    Strict + frozen by design: this is an ephemeral intermediate,
    consumed immediately within the station and never persisted to the
    DB, so (unlike `JobPlan`'s `extra="allow"`) an unexpected extra
    field should fail loudly rather than be silently round-tripped.
    """

    model_config = ConfigDict(frozen=True)

    phrases: list[str] = Field(min_length=3, max_length=8)
