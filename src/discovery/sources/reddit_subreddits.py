"""Reddit subreddit-discovery client and its DTOs.

NOT a `BaseSource`. This hits Reddit's `/subreddits/search.json`
endpoint to find *real, currently-existing* subreddits for Wave 0
query planning. The result is a planning artifact, never Bronze
`raw_records` data — so it returns `SubredditCandidate` DTOs, not
`RawRecord`s. It still obeys the source-adapter contract (async httpx,
shared rate limiter, retry, Pydantic-validated response) and the
reddit-source skill (User-Agent, 6.1s pacing, 401/403 raise, partial
success, per-request logging).

See `.claude/skills/reddit-source/SKILL.md` (items 2,3,4,10,17,20,21)
and `docs/specs/2026-05-15-subreddit-discovery-design.md`.

This task adds the DTOs + pure helpers; `search_subreddits` (the async
client) is added in the next task.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

_DESCRIPTION_LIMIT = 300

# The 6 columns the LLM (Call #2) sees, in order. `subreddit_type` and
# `over18` are carried on the DTO for deterministic filtering only and
# are intentionally NOT in this projection (spec §5).
_TABLE_COLUMNS: tuple[str, ...] = (
    "name",
    "subscribers",
    "active_user_count",
    "activity_ratio",
    "public_description",
    "matched_phrases",
)


def clean_description(raw: str) -> str:
    """Collapse whitespace runs and truncate to ~300 chars.

    `public_description` is the LLM's primary relevance signal (spec
    §6); a few hundred chars is plenty and keeps the rendered table
    compact (spec §5: 25 raw t5 objects ≈ 80k tokens, the projection a
    few hundred).
    """
    collapsed = re.sub(r"\s+", " ", raw).strip()
    if len(collapsed) <= _DESCRIPTION_LIMIT:
        return collapsed
    return collapsed[:_DESCRIPTION_LIMIT] + "…"


class SubredditCandidate(BaseModel):
    """One deduped, surviving subreddit considered for Wave 0 selection.

    Six fields are projected into the table the LLM sees;
    `subreddit_type`/`over18` are filter-only and dropped before the LLM
    (spec §5). `matched_phrases` and `activity_ratio` are populated by
    the deterministic pipeline, not at client parse time.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    subscribers: int = 0
    active_user_count: int = 0
    activity_ratio: float = 0.0
    public_description: str = ""
    matched_phrases: int = 0
    subreddit_type: str = "public"
    over18: bool = False


class PhraseResult(BaseModel):
    """Raw per-phrase search result. One entry per phrase request that
    succeeded (failed phrases omitted — partial success, skill item 17).
    Candidates carry raw fields only; the pipeline sets `matched_phrases`
    and `activity_ratio` later.
    """

    model_config = ConfigDict(frozen=True)

    phrase: str
    candidates: list[SubredditCandidate] = Field(default_factory=list)


def render_candidate_table(candidates: list[SubredditCandidate]) -> str:
    """Render candidates as a compact tab-delimited table — header line
    plus one row per subreddit, exactly the 6 columns in
    `_TABLE_COLUMNS` (spec §5). NOT raw JSON: compaction is mandatory,
    not an optimization. Tabs/newlines inside the description are
    replaced with spaces so the column count stays exactly 6.
    """
    lines = ["\t".join(_TABLE_COLUMNS)]
    for c in candidates:
        desc = c.public_description.replace("\t", " ").replace("\n", " ").replace("\r", " ")
        lines.append(
            "\t".join(
                [
                    c.name,
                    str(c.subscribers),
                    str(c.active_user_count),
                    str(c.activity_ratio),
                    desc,
                    str(c.matched_phrases),
                ]
            )
        )
    return "\n".join(lines)
