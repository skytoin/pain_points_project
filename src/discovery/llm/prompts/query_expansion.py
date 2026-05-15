"""System prompt and helpers for the Wave 0 Query Expansion station.

The LLM (OpenAI gpt-5.4) sees this prompt plus a rendered user message
describing the JobSpec, and returns a `JobPlan` validated against the
Pydantic schema in `discovery.llm.schemas`.

Bumping VERSION
---------------
Bump VERSION whenever the system prompt, few-shot examples, or the
intended schema changes. The cache key includes VERSION; old results
stay in cache but are no longer hit (a fresh call is forced).

Versioning:
    v1 — initial release. gpt-5.4, 10-15 reddit_queries, structured
    `RedditQuerySpec` with rationale-per-query.
"""

from __future__ import annotations

from typing import Any

from discovery.jobs import JobSpec

VERSION: str = "v1"


SYSTEM_PROMPT: str = """\
You are a Reddit search query designer. Your job is to brainstorm
between 10 and 15 high-signal Reddit search queries for a given
industry, plus a short list of domain-specific subreddits worth
scanning.

These queries are aimed at finding posts where real practitioners
discuss pain points, willingness to pay, frustration, unmet needs,
and adjacent signals in this industry. Each query you produce will
be executed against Reddit's search API.

# How Reddit search works

You can search site-wide or scope to a single subreddit. The two endpoints
correspond to the `endpoint` field on each query you emit:

- `per_sub` — searches inside one specific subreddit. The query string
  should NOT include a `subreddit:` clause; the subreddit is implied
  by the endpoint. Use this for high-value niche subs.
- `site_wide` — searches across all of Reddit. The query string MUST
  include one or more `subreddit:NAME` clauses joined with `OR` to
  scope the search; otherwise you'll get noise from all of Reddit.

# Reddit search query syntax — the rules you MUST follow

1. **Quote multi-word phrases.** `"I would pay"` matches the literal
   phrase. Without quotes, Reddit splits it into separate word matches
   and you lose ~70% of real signals.

2. **OR / AND must be UPPERCASE.** Lowercase `or` is just a word.

3. **Parenthesize aggressively.** Make precedence explicit:
   `(subreddit:a OR subreddit:b) AND ("phrase1" OR "phrase2")`.

4. **Subreddit names: 3-21 chars, ASCII letters/digits/underscore only.**
   No spaces, no hyphens, no slashes. Strip any leading `r/`.
   Invalid examples that will be rejected: `"r/Small Business"`,
   `"AI/ML"`, `"my-sub"`.

5. **Cap subreddits per site_wide query.** Up to ~6 in one OR-clause
   per query. More than that blows past Reddit's ~4 KB URL ceiling.

6. **Cap pain-phrase variants.** Each pain category should have 3-4
   close paraphrases, OR'd together. Longer lists dilute precision
   and bloat the URL.

7. **Total query length must stay under 3900 characters.**

# Pain-phrase categories worth combining (ranked by signal strength)

These are guidelines, not a fixed list. Brainstorm beyond them where
it makes sense — but each query should be built around one CATEGORY
of pain expression, not a single keyword. Variants are PARAPHRASES,
not synonyms. `"I would buy"` is NOT a variant of `"I would pay"`
(one-time purchase vs. recurring willingness).

1. Willingness to pay: `"I would pay"`, `"I'd pay"`, `"would pay for"`
2. Unmet need: `"wish there was"`, `"wish someone would"`
3. Frustration: `"frustrated with"`, `"fed up with"`, `"tired of"`
4. Looking for alternatives: `"alternative to"`, `"replacement for"`
5. Market gap: `"why is there no"`, `"why does no one"`
6. Builder signals: `"built a tool"`, `"made a tool"`
7. Switching: `"switched from"`, `"moving away from"`
8. Dead competitor signals: `"shut down"`, `"killed off"`

# How to combine subreddit choice with phrase choice

Subreddits give you the DOMAIN; phrases give you the SIGNAL. A nurse
looking for product ideas searches the same phrases a DevOps founder
uses — just in different subs. Don't try to make domain-specific
phrase lists; you'll lose generality.

For each query, you choose:

- Which subreddits to scope to (1 for per_sub; 1-6 for site_wide)
- Which pain category and variants to OR together
- Whether to anchor on the industry literal (e.g. `"commercial cleaning"`)

# What to emit

You will emit a JSON object validated as `JobPlan` with two fields:

- `reddit_queries` — between 10 and 15 `RedditQuerySpec` objects.
  Each has `endpoint`, `q`, `sort`, `t`, `limit`, and a one-sentence
  `rationale` explaining why this query is worth running.
- `reddit_subreddits` — your shortlist of domain-relevant subreddits
  (without the `r/` prefix). Up to ~12. These complement the queries
  themselves; Python code may use this list to seed per-sub queries
  or rank subs for follow-up.

Each `rationale` is mandatory and visible to the engineer reviewing
plans. Be concrete: "scopes to nurse community for willingness-to-pay
signals on documentation tools" beats "looking for pain".

# Defaults

- `sort=top` unless you have a specific reason (`new` for emerging
  trends; `hot` for current discussion).
- `t=month` unless the spec's `as_of` date implies a narrower window.
- `limit=100` — it's the max Reddit allows; smaller wastes rate budget.

# What NOT to do

- Don't repeat near-identical queries. Each one should pull a
  meaningfully different slice.
- Don't put more than ~6 subreddits in a single site_wide query.
- Don't write pain phrases without quotes — Reddit will treat the
  words separately.
- Don't return fewer than 10 or more than 15 queries.
- Don't invent subreddits that obviously won't exist (e.g.
  `r/commercialcleaning2026`); stick to names that real communities
  actually use.
"""


def _example_queries(qs: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    """Helper to build a list of 10+ valid RedditQuerySpec-shaped dicts
    from compact tuples (endpoint, q, rationale)."""
    return [
        {
            "endpoint": endpoint,
            "q": q,
            "sort": "top",
            "t": "month",
            "limit": 100,
            "rationale": rationale,
        }
        for endpoint, q, rationale in qs
    ]


FEW_SHOT_EXAMPLES: list[dict[str, Any]] = [
    {
        "input": {
            "industry": "commercial cleaning",
            "as_of": "2026-06-01",
            "location": "NY",
            "size": "medium",
        },
        "output": {
            "reddit_queries": _example_queries(
                [
                    (
                        "site_wide",
                        '(subreddit:smallbusiness OR subreddit:Entrepreneur OR subreddit:startups) AND "commercial cleaning" AND ("I would pay" OR "I\'d pay" OR "would pay for")',
                        "Cross-sub willingness-to-pay scan anchored on the industry literal; baseline business subs.",
                    ),
                    (
                        "per_sub",
                        '"commercial cleaning" AND ("frustrated with" OR "fed up with" OR "tired of")',
                        "Scoped to r/CleaningTips for frustration signals from actual practitioners.",
                    ),
                    (
                        "site_wide",
                        '(subreddit:Janitorial OR subreddit:OfficeCleaners) AND ("wish there was" OR "wish someone would")',
                        "Unmet-need scan inside janitorial-focused subs.",
                    ),
                    (
                        "site_wide",
                        '(subreddit:smallbusiness OR subreddit:Entrepreneur) AND "cleaning business" AND ("alternative to" OR "replacement for")',
                        "Picks up posts looking to swap out their current cleaning vendor or tool.",
                    ),
                    (
                        "site_wide",
                        '(subreddit:startups OR subreddit:microsaas) AND "cleaning" AND ("built a tool" OR "made a tool")',
                        "Builder signals — devs who've built something cleaning-adjacent worth studying.",
                    ),
                    (
                        "per_sub",
                        '"commercial cleaning" AND ("switched from" OR "moving away from")',
                        "Scoped to r/Janitorial for switching signals between vendors/products.",
                    ),
                    (
                        "site_wide",
                        '(subreddit:smallbusiness OR subreddit:Entrepreneur) AND ("why is there no" OR "why does no one") AND "cleaning"',
                        "Market-gap questions — explicit articulations of what doesn't exist yet.",
                    ),
                    (
                        "site_wide",
                        '(subreddit:OfficeCleaners OR subreddit:CleaningTips) AND ("shut down" OR "killed off")',
                        "Dead-competitor signals — recent failures point to attempts and missed needs.",
                    ),
                    (
                        "per_sub",
                        '("scheduling" OR "billing" OR "payroll") AND ("frustrated with" OR "tired of")',
                        "Inside r/smallbusiness — operational pain points cleaners actually run into.",
                    ),
                    (
                        "site_wide",
                        '(subreddit:Entrepreneur OR subreddit:smallbusiness OR subreddit:startups) AND "janitorial" AND ("I would pay" OR "would pay for")',
                        "Second willingness-to-pay scan using the synonym 'janitorial' to catch posts using different terminology.",
                    ),
                ]
            ),
            "reddit_subreddits": [
                "CleaningTips",
                "Janitorial",
                "smallbusiness",
                "Entrepreneur",
                "OfficeCleaners",
            ],
        },
    },
    {
        "input": {"industry": "indie game development", "as_of": "2026-06-01"},
        "output": {
            "reddit_queries": _example_queries(
                [
                    (
                        "site_wide",
                        '(subreddit:gamedev OR subreddit:IndieDev OR subreddit:Unity3D) AND ("wish there was" OR "wish someone would")',
                        "Unmet-need scan across the three biggest indie gamedev subs.",
                    ),
                    (
                        "per_sub",
                        '("I would pay" OR "I\'d pay" OR "would pay for")',
                        "Inside r/gamedev — willingness-to-pay signals for tools.",
                    ),
                    (
                        "site_wide",
                        '(subreddit:gamedev OR subreddit:Godot) AND ("frustrated with" OR "fed up with")',
                        "Frustration in gamedev + Godot specifically — engine-side pain points.",
                    ),
                    (
                        "site_wide",
                        '(subreddit:IndieDev OR subreddit:gamedesign) AND ("alternative to" OR "replacement for")',
                        "Tooling alternatives inside design-focused subs.",
                    ),
                    (
                        "site_wide",
                        '(subreddit:gamedev OR subreddit:Unity3D OR subreddit:Unreal) AND ("built a tool" OR "made a tool")',
                        "Builder signals across the three major engine subs.",
                    ),
                    (
                        "per_sub",
                        '("switched from" OR "moving away from") AND ("Unity" OR "Unreal" OR "Godot")',
                        "Engine-switching narratives inside r/gamedev.",
                    ),
                    (
                        "site_wide",
                        '(subreddit:gamedev OR subreddit:IndieDev) AND ("why is there no" OR "why does no one")',
                        "Market-gap questions in indie gamedev.",
                    ),
                    (
                        "site_wide",
                        '(subreddit:gamedev OR subreddit:Godot OR subreddit:Unity3D) AND ("shut down" OR "killed off")',
                        "Dead-tool / dead-engine-feature signals.",
                    ),
                    (
                        "site_wide",
                        '(subreddit:IndieDev OR subreddit:gamedev) AND ("marketing" OR "wishlist") AND ("frustrated with" OR "tired of")',
                        "Marketing-specific pain — the #1 indie complaint after engine pain.",
                    ),
                    (
                        "per_sub",
                        '("playtest" OR "QA") AND ("wish there was" OR "wish someone would")',
                        "Inside r/gamedev — pre-release feedback pain.",
                    ),
                ]
            ),
            "reddit_subreddits": [
                "gamedev",
                "IndieDev",
                "Unity3D",
                "Godot",
                "gamedesign",
            ],
        },
    },
]


def build_user_message(spec: JobSpec) -> str:
    """Render the JobSpec into a user message the LLM sees.

    Includes only the fields that are set (location and size are
    optional). The `as_of` date is rendered as ISO format so the LLM
    can reason about a coarse `t` parameter choice.
    """
    lines: list[str] = []
    lines.append(f"Industry: {spec.industry}")
    lines.append(f"As of: {spec.as_of.isoformat()}")
    if spec.location is not None:
        lines.append(f"Location: {spec.location}")
    if spec.size is not None:
        lines.append(f"Company size: {spec.size}")
    lines.append("")
    lines.append(
        "Produce a JobPlan with 10-15 reddit_queries and a shortlist "
        "of reddit_subreddits for this industry. Follow the system-"
        "prompt rules; explain each query's rationale."
    )
    return "\n".join(lines)
