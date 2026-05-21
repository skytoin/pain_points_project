"""Few-shot examples for the Wave 0 Query Expansion station.

Extracted from query_expansion.py to keep that file under the 600-line
hard cap. Import FEW_SHOT_EXAMPLES from here, or via the re-export in
query_expansion.py.
"""

from __future__ import annotations

from typing import Any


def _example_queries(
    qs: list[tuple[str, str | None, str, str]],
) -> list[dict[str, Any]]:
    """Helper to build a list of 10+ valid RedditQuerySpec-shaped dicts
    from compact tuples `(endpoint, subreddit_or_None, q, rationale)`.
    Sets `subreddit` only for per_sub queries; site_wide omits it."""
    out: list[dict[str, Any]] = []
    for endpoint, subreddit, q, rationale in qs:
        entry: dict[str, Any] = {
            "endpoint": endpoint,
            "q": q,
            "sort": "top",
            "t": "month",
            "limit": 100,
            "rationale": rationale,
        }
        if subreddit is not None:
            entry["subreddit"] = subreddit
        out.append(entry)
    return out


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
                        None,
                        '(subreddit:smallbusiness OR subreddit:Entrepreneur OR subreddit:startups) AND "commercial cleaning" AND ("I would pay" OR "I\'d pay" OR "would pay for")',
                        "Cross-sub willingness-to-pay scan anchored on the industry literal; baseline business subs.",
                    ),
                    (
                        "per_sub",
                        "CleaningTips",
                        '"commercial cleaning" AND ("frustrated with" OR "fed up with" OR "tired of")',
                        "Scoped to r/CleaningTips for frustration signals from actual practitioners.",
                    ),
                    (
                        "site_wide",
                        None,
                        '(subreddit:Janitorial OR subreddit:OfficeCleaners) AND ("wish there was" OR "wish someone would")',
                        "Unmet-need scan inside janitorial-focused subs.",
                    ),
                    (
                        "site_wide",
                        None,
                        '(subreddit:smallbusiness OR subreddit:Entrepreneur) AND "cleaning business" AND ("alternative to" OR "replacement for")',
                        "Picks up posts looking to swap out their current cleaning vendor or tool.",
                    ),
                    (
                        "site_wide",
                        None,
                        '(subreddit:startups OR subreddit:microsaas) AND "cleaning" AND ("built a tool" OR "made a tool")',
                        "Builder signals — devs who've built something cleaning-adjacent worth studying.",
                    ),
                    (
                        "per_sub",
                        "Janitorial",
                        '"commercial cleaning" AND ("switched from" OR "moving away from")',
                        "Scoped to r/Janitorial for switching signals between vendors/products.",
                    ),
                    (
                        "site_wide",
                        None,
                        '(subreddit:smallbusiness OR subreddit:Entrepreneur) AND ("why is there no" OR "why does no one") AND "cleaning"',
                        "Market-gap questions — explicit articulations of what doesn't exist yet.",
                    ),
                    (
                        "site_wide",
                        None,
                        '(subreddit:OfficeCleaners OR subreddit:CleaningTips) AND ("shut down" OR "killed off")',
                        "Dead-competitor signals — recent failures point to attempts and missed needs.",
                    ),
                    (
                        "per_sub",
                        "smallbusiness",
                        '("scheduling" OR "billing" OR "payroll") AND ("frustrated with" OR "tired of")',
                        "Inside r/smallbusiness — operational pain points cleaners actually run into.",
                    ),
                    (
                        "site_wide",
                        None,
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
                        None,
                        '(subreddit:gamedev OR subreddit:IndieDev OR subreddit:Unity3D) AND ("wish there was" OR "wish someone would")',
                        "Unmet-need scan across the three biggest indie gamedev subs.",
                    ),
                    (
                        "per_sub",
                        "gamedev",
                        '("I would pay" OR "I\'d pay" OR "would pay for")',
                        "Inside r/gamedev — willingness-to-pay signals for tools.",
                    ),
                    (
                        "site_wide",
                        None,
                        '(subreddit:gamedev OR subreddit:Godot) AND ("frustrated with" OR "fed up with")',
                        "Frustration in gamedev + Godot specifically — engine-side pain points.",
                    ),
                    (
                        "site_wide",
                        None,
                        '(subreddit:IndieDev OR subreddit:gamedesign) AND ("alternative to" OR "replacement for")',
                        "Tooling alternatives inside design-focused subs.",
                    ),
                    (
                        "site_wide",
                        None,
                        '(subreddit:gamedev OR subreddit:Unity3D OR subreddit:Unreal) AND ("built a tool" OR "made a tool")',
                        "Builder signals across the three major engine subs.",
                    ),
                    (
                        "per_sub",
                        "gamedev",
                        '("switched from" OR "moving away from") AND ("Unity" OR "Unreal" OR "Godot")',
                        "Engine-switching narratives inside r/gamedev.",
                    ),
                    (
                        "site_wide",
                        None,
                        '(subreddit:gamedev OR subreddit:IndieDev) AND ("why is there no" OR "why does no one")',
                        "Market-gap questions in indie gamedev.",
                    ),
                    (
                        "site_wide",
                        None,
                        '(subreddit:gamedev OR subreddit:Godot OR subreddit:Unity3D) AND ("shut down" OR "killed off")',
                        "Dead-tool / dead-engine-feature signals.",
                    ),
                    (
                        "site_wide",
                        None,
                        '(subreddit:IndieDev OR subreddit:gamedev) AND ("marketing" OR "wishlist") AND ("frustrated with" OR "tired of")',
                        "Marketing-specific pain — the #1 indie complaint after engine pain.",
                    ),
                    (
                        "per_sub",
                        "gamedev",
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
