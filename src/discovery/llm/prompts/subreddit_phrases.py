"""System prompt + helpers for Wave 0 LLM Call #1 (subreddit phrases).

The LLM (OpenAI gpt-5.4) sees this prompt plus a rendered JobSpec and
returns a `SubredditSearchPhrases` (validated in
`discovery.llm.schemas`). These phrases are fed to Reddit's
`/subreddits/search.json` to find REAL, currently-existing subreddits —
the LLM never names subreddits from memory (that was the bug this
feature fixes; see spec §1).

Bumping VERSION
---------------
Bump when the system prompt, few-shot, or intended schema changes. The
combined Wave 0 cache key includes this VERSION (spec §8); bumping it
forces a full fresh re-run (re-phrase, re-search, re-select).

Versioning:
    v1 — initial release. ~5 semantic subreddit-search phrases.
"""

from __future__ import annotations

from typing import Any

from discovery.jobs import JobSpec

VERSION: str = "v1"


SYSTEM_PROMPT: str = """\
You generate SEARCH PHRASES used to discover Reddit communities. You do
NOT name subreddits.

The phrases you return are fed verbatim to Reddit's subreddit-search
index (`/subreddits/search`). Reddit matches them against subreddit
names AND descriptions and returns real, currently-existing
communities. Your job is to maximize the chance that the practitioners,
customers, and adjacent niches of the given industry are surfaced.

# Critical rule

- Output SEARCH PHRASES, NOT subreddit names. `"dog grooming"` is a
  good phrase. `r/doggrooming` is a subreddit NAME — never output that.
  You cannot know which subreddits exist; that is exactly what the
  search step is for.

# Vary the angle

Produce a small set of distinct phrases that approach the industry from
different directions, so the search surfaces a broad, non-redundant set
of communities:

- the trade/practice itself (what insiders call the work)
- practitioner slang or role names
- the customer / buyer side of the same industry
- adjacent or upstream/downstream verticals

# Length

Keep each phrase short (a few words). Reddit's subreddit-search query is
short; long phrases hurt recall. Around 5 phrases is the sweet spot —
enough angles without burning the shared Reddit rate budget.

# Output

A JSON object validated as `SubredditSearchPhrases` with one field:
`phrases` — between 3 and 8 short search phrases. No subreddit names,
no `r/` prefixes, no operators.
"""


FEW_SHOT_EXAMPLES: list[dict[str, Any]] = [
    {
        "input": {"industry": "commercial cleaning", "location": "NY", "size": "medium"},
        "output": {
            "phrases": [
                "commercial cleaning",
                "janitorial services",
                "office cleaning business",
                "facilities maintenance",
                "small business owners",
            ]
        },
    },
    {
        "input": {"industry": "indie game development"},
        "output": {
            "phrases": [
                "indie game development",
                "game dev",
                "solo game developer",
                "game design",
                "game marketing",
            ]
        },
    },
]


def build_user_message(spec: JobSpec) -> str:
    """Render the JobSpec into the Call #1 user message. Only set fields
    are included (location/size are optional). `as_of` and `time_window`
    are intentionally omitted — Call #1 only needs industry/location/size
    to generate discovery phrases; temporal framing is relevant only to
    Call #2's query design (where query_expansion's builder renders them).
    """
    lines: list[str] = [f"Industry: {spec.industry}"]
    if spec.location is not None:
        lines.append(f"Location: {spec.location}")
    if spec.size is not None:
        lines.append(f"Company size: {spec.size}")
    lines.append("")
    lines.append(
        "Produce 3-8 short subreddit-SEARCH phrases for this industry. "
        "Phrases that find communities — never subreddit names."
    )
    return "\n".join(lines)
