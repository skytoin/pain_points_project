"""Tests for `discovery.orchestrator.reddit_query_validator`.

Each test pins one skill rule from
`.claude/skills/reddit-source/SKILL.md`. The validator returns a list
of violation strings - empty list means valid.
"""

from __future__ import annotations

from typing import Any

from discovery.llm.schemas import RedditQuerySpec
from discovery.orchestrator.reddit_query_validator import validate_reddit_query


def _spec(q: str, endpoint: str = "site_wide", **kw: Any) -> RedditQuerySpec:
    return RedditQuerySpec(
        endpoint=endpoint,  # type: ignore[arg-type]
        q=q,
        rationale="x",
        **kw,
    )


class TestValidateRedditQuery:
    def test_well_formed_query_has_no_errors(self) -> None:
        spec = _spec('(subreddit:startups OR subreddit:smallbusiness) AND "I would pay"')
        assert validate_reddit_query(spec) == []

    def test_lowercase_or_is_flagged_skill_item_6(self) -> None:
        spec = _spec("(subreddit:a or subreddit:b)")
        errors = validate_reddit_query(spec)
        assert any("uppercase" in e.lower() for e in errors)

    def test_lowercase_and_is_flagged_skill_item_6(self) -> None:
        spec = _spec('subreddit:a and "phrase"')
        errors = validate_reddit_query(spec)
        assert any("uppercase" in e.lower() for e in errors)

    def test_invalid_subreddit_name_is_flagged_skill_item_10(self) -> None:
        # space inside name
        spec = _spec('subreddit:Small Business AND "x"')
        errors = validate_reddit_query(spec)
        assert any("subreddit" in e.lower() for e in errors)

    def test_hyphen_in_subreddit_name_is_flagged(self) -> None:
        spec = _spec('subreddit:my-sub AND "x"')
        errors = validate_reddit_query(spec)
        assert any("subreddit" in e.lower() for e in errors)

    def test_too_many_subreddits_in_site_wide_is_flagged_skill_item_7(self) -> None:
        subs = " OR ".join(f"subreddit:s{i}" for i in range(8))
        spec = _spec(f'({subs}) AND "x"')
        errors = validate_reddit_query(spec)
        assert any("subreddits" in e.lower() and "6" in e for e in errors)

    def test_per_sub_must_have_no_subreddit_clause_skill_item_16(self) -> None:
        """per_sub means the subreddit comes from the endpoint, not the q string."""
        spec = _spec('subreddit:a AND "x"', endpoint="per_sub")
        errors = validate_reddit_query(spec)
        assert any("per_sub" in e.lower() for e in errors)

    def test_site_wide_must_have_at_least_one_subreddit_clause(self) -> None:
        spec = _spec('"phrase only, no subreddit"', endpoint="site_wide")
        errors = validate_reddit_query(spec)
        flat = " ".join(e.lower() for e in errors)
        assert "site_wide" in flat or "subreddit" in flat

    def test_word_or_inside_a_quoted_phrase_is_not_flagged(self) -> None:
        """`"oranges"` contains the substring 'or' - we must not false-positive."""
        spec = _spec('(subreddit:cooking OR subreddit:food) AND "oranges or apples"')
        # The `or` inside the quoted phrase isn't an operator. Should be valid.
        errors = validate_reddit_query(spec)
        assert errors == [], f"unexpected errors: {errors}"
