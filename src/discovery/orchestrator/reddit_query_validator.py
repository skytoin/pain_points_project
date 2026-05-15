"""Validate LLM-built Reddit search queries against the source's rules.

Pure function. Returns a list of human-readable violation strings -
empty list means the query passes. The Wave 0 station drops any
`RedditQuerySpec` whose validator returns a non-empty list.

Each check is keyed to a numbered item in
`.claude/skills/reddit-source/SKILL.md` so reviewers can trace rule ->
code -> test.
"""

from __future__ import annotations

import re

from discovery.llm.schemas import RedditQuerySpec

# Skill item 10: subreddit names - 3 to 21 chars, ASCII letters/digits/underscore.
_VALID_SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{3,21}$")
_MAX_SUBS_SITE_WIDE = 6  # skill item 7


def validate_reddit_query(spec: RedditQuerySpec) -> list[str]:
    """Return a list of skill-rule violations. Empty list = valid."""
    errors: list[str] = []

    q_stripped = _strip_quoted_substrings(spec.q)

    _check_uppercase_operators(q_stripped, errors)
    _check_subreddit_names(spec.q, errors)
    _check_endpoint_subreddit_count(spec, errors)

    return errors


def _strip_quoted_substrings(q: str) -> str:
    """Remove text inside double-quoted phrases so checks for `or` / `and`
    don't false-positive on words like "oranges" or "candy and chips"."""
    return re.sub(r'"[^"]*"', "", q)


def _check_uppercase_operators(q_stripped: str, errors: list[str]) -> None:
    """Skill item 6: OR / AND must be uppercase outside of quoted phrases."""
    # Word-boundaried lowercase or/and, not adjacent to an alphabetic char
    # (which would mean it's part of another word like "factor" or "android").
    if re.search(r"(?<![A-Za-z])(or|and)(?![A-Za-z])", q_stripped):
        errors.append(
            "Reddit operators OR/AND must be uppercase outside of quoted "
            "phrases (skill item 6)."
        )


def _check_subreddit_names(q: str, errors: list[str]) -> None:
    """Skill item 10: subreddit names must match [A-Za-z0-9_]{3,21}.

    Two failure modes:
      - The token itself is malformed (contains hyphens, slashes, etc).
      - The LLM emitted a multi-word name like `subreddit:Small Business`
        where Reddit will parse only `Small` and treat `Business` as a
        free search term - usually NOT what the LLM intended.
    """
    for match in re.finditer(r"subreddit:(\S+?)(?=[\s\)]|$)", q):
        name = match.group(1)
        if not _VALID_SUBREDDIT.match(name):
            errors.append(
                f"Invalid subreddit name '{name}' (skill item 10: "
                f"3-21 chars, [A-Za-z0-9_])."
            )

    # Catches `subreddit:Small Business` - a `subreddit:` token whose
    # next sibling token is a bare word, not an operator/paren/quote.
    multi_word_pattern = re.compile(
        r"subreddit:\S+\s+(?!(?:OR|AND)\b)([A-Za-z][^\s\)\"]*)"
    )
    for m in multi_word_pattern.finditer(q):
        bare = m.group(1)
        errors.append(
            f"Bare word '{bare}' follows a subreddit: clause - looks like "
            f"a multi-word subreddit name attempt; Reddit names cannot "
            f"contain spaces (skill item 10)."
        )


def _check_endpoint_subreddit_count(
    spec: RedditQuerySpec, errors: list[str]
) -> None:
    """Skill items 7 (cap site_wide at ~6) and 16 (per_sub uses endpoint)."""
    sub_count = len(re.findall(r"\bsubreddit:", spec.q))
    if spec.endpoint == "per_sub" and sub_count > 0:
        errors.append(
            "per_sub queries must not include a subreddit: clause - the "
            "subreddit comes from the endpoint (skill item 16)."
        )
    if spec.endpoint == "site_wide":
        if sub_count == 0:
            errors.append(
                "site_wide queries must include at least one subreddit: "
                "clause to avoid scanning all of Reddit (skill item 16)."
            )
        if sub_count > _MAX_SUBS_SITE_WIDE:
            errors.append(
                f"site_wide query has {sub_count} subreddits; cap is "
                f"{_MAX_SUBS_SITE_WIDE} (skill item 7)."
            )
