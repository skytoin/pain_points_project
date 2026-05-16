"""Tests for `discovery.sources.reddit_subreddits`.

Two layers, mirroring test_reddit.py:
1. Pure helpers/DTOs — no HTTP, no async.
2. `search_subreddits` — httpx.MockTransport, injected no-op sleep.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from discovery.sources.reddit_subreddits import (
    PhraseResult,
    SubredditCandidate,
    clean_description,
    render_candidate_table,
)


class TestCleanDescription:
    def test_collapses_whitespace(self) -> None:
        assert clean_description("a   b\n\tc") == "a b c"

    def test_truncates_long_with_ellipsis(self) -> None:
        out = clean_description("x" * 500)
        assert len(out) == 301  # 300 chars + the single ellipsis char
        assert out.endswith("…")

    def test_short_passes_through(self) -> None:
        assert clean_description("  short  ") == "short"

    def test_at_limit_passes_through(self) -> None:
        out = clean_description("x" * 300)
        assert len(out) == 300
        assert not out.endswith("…")


class TestSubredditCandidate:
    def test_defaults(self) -> None:
        c = SubredditCandidate(name="startups")
        assert c.subscribers == 0
        assert c.active_user_count == 0
        assert c.activity_ratio == 0.0
        assert c.public_description == ""
        assert c.matched_phrases == 0
        assert c.subreddit_type == "public"
        assert c.over18 is False

    def test_is_frozen(self) -> None:
        c = SubredditCandidate(name="x")
        with pytest.raises(ValidationError):
            c.name = "y"  # type: ignore[misc]

    def test_extra_fields_are_ignored(self) -> None:
        c = SubredditCandidate(name="x", unknown_reddit_field="ignored", created_utc=123)
        assert not hasattr(c, "unknown_reddit_field")
        assert not hasattr(c, "created_utc")


class TestPhraseResult:
    def test_holds_phrase_and_candidates(self) -> None:
        pr = PhraseResult(
            phrase="cleaning business",
            candidates=[SubredditCandidate(name="CleaningTips")],
        )
        assert pr.phrase == "cleaning business"
        assert pr.candidates[0].name == "CleaningTips"

    def test_candidates_default_empty(self) -> None:
        assert PhraseResult(phrase="p").candidates == []


class TestRenderCandidateTable:
    def _c(self, **kw: object) -> SubredditCandidate:
        base: dict[str, object] = {
            "name": "startups",
            "subscribers": 1000,
            "active_user_count": 50,
            "activity_ratio": 0.05,
            "public_description": "founders talk shop",
            "matched_phrases": 3,
        }
        base.update(kw)
        return SubredditCandidate(**base)  # type: ignore[arg-type]

    def test_header_has_exactly_six_columns_in_order(self) -> None:
        out = render_candidate_table([self._c()])
        header = out.splitlines()[0]
        assert header.split("\t") == [
            "name",
            "subscribers",
            "active_user_count",
            "activity_ratio",
            "public_description",
            "matched_phrases",
        ]

    def test_one_row_per_candidate_six_fields(self) -> None:
        out = render_candidate_table([self._c(), self._c(name="saas")])
        rows = out.splitlines()[1:]
        assert len(rows) == 2
        assert all(len(r.split("\t")) == 6 for r in rows)
        assert rows[1].split("\t")[0] == "saas"

    def test_neutralizes_tabs_and_newlines_in_description(self) -> None:
        out = render_candidate_table([self._c(public_description="a\tb\nc")])
        row = out.splitlines()[1]
        # description column must not introduce extra tab/newline columns
        assert len(row.split("\t")) == 6
        assert "\n" not in row

    def test_neutralizes_carriage_returns(self) -> None:
        out = render_candidate_table([self._c(public_description="a\r\nb\rc")])
        row = out.splitlines()[1]
        assert len(row.split("\t")) == 6
        assert "\r" not in row

    def test_empty_list_renders_header_only(self) -> None:
        out = render_candidate_table([])
        assert out.splitlines() == [
            "name\tsubscribers\tactive_user_count\tactivity_ratio\tpublic_description\tmatched_phrases"
        ]
