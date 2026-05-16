"""Tests for `discovery.llm.schemas` — RedditQuerySpec, JobPlan."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from discovery.llm.schemas import JobPlan, RedditQuerySpec, SubredditSearchPhrases


def _good_query(q: str = '"I would pay"') -> RedditQuerySpec:
    return RedditQuerySpec(
        endpoint="site_wide",
        q=q,
        sort="top",
        t="month",
        limit=100,
        rationale="picks high-signal posts on willingness to pay",
    )


class TestRedditQuerySpec:
    def test_minimal_valid_spec(self) -> None:
        spec = _good_query()
        assert spec.endpoint == "site_wide"
        assert spec.sort == "top"

    def test_q_has_min_length(self) -> None:
        with pytest.raises(ValidationError):
            RedditQuerySpec(endpoint="site_wide", q="", rationale="x")

    def test_q_has_max_length(self) -> None:
        """Schema caps q at 3900 chars — Pydantic-level early rejection of URL-busters."""
        with pytest.raises(ValidationError):
            RedditQuerySpec(endpoint="site_wide", q="x" * 3901, rationale="x")

    def test_rationale_is_required(self) -> None:
        """Forces the LLM to explain itself — improves quality, logged for debugging."""
        with pytest.raises(ValidationError):
            RedditQuerySpec(endpoint="site_wide", q="x", rationale="")

    def test_endpoint_must_be_one_of_two(self) -> None:
        with pytest.raises(ValidationError):
            RedditQuerySpec(
                endpoint="bogus",  # type: ignore[arg-type]
                q="x",
                rationale="x",
            )

    def test_limit_clamped_to_1_100(self) -> None:
        with pytest.raises(ValidationError):
            RedditQuerySpec(endpoint="site_wide", q="x", limit=101, rationale="x")


class TestJobPlan:
    def test_rejects_fewer_than_25_queries(self) -> None:
        with pytest.raises(ValidationError):
            JobPlan(reddit_queries=[_good_query() for _ in range(24)])

    def test_accepts_25_queries(self) -> None:
        plan = JobPlan(reddit_queries=[_good_query() for _ in range(25)])
        assert len(plan.reddit_queries) == 25

    def test_accepts_30_queries(self) -> None:
        plan = JobPlan(reddit_queries=[_good_query() for _ in range(30)])
        assert len(plan.reddit_queries) == 30

    def test_rejects_more_than_30_queries(self) -> None:
        with pytest.raises(ValidationError):
            JobPlan(reddit_queries=[_good_query() for _ in range(31)])

    def test_extra_fields_round_trip(self) -> None:
        """extra='allow' — future prompts can emit extra fields and they
        stay on the model (and on Job.job_plan JSON) without losing them.
        model_validate re-validates, so the list must satisfy the 25 floor."""
        plan = JobPlan.model_validate(
            {
                "reddit_queries": [_good_query().model_dump() for _ in range(25)],
                "youtube_queries": ["a", "b"],  # not a typed field yet
            }
        )
        dumped = plan.model_dump()
        assert "youtube_queries" in dumped
        assert dumped["youtube_queries"] == ["a", "b"]

    def test_reddit_subreddits_defaults_to_empty(self) -> None:
        plan = JobPlan(reddit_queries=[_good_query() for _ in range(25)])
        assert plan.reddit_subreddits == []


class TestRedditQuerySpecSubreddit:
    """`subreddit` field is required for per_sub endpoint and forbidden
    for site_wide (where subreddit clauses live inside `q`).
    """

    def test_per_sub_requires_subreddit(self) -> None:
        with pytest.raises(ValidationError):
            RedditQuerySpec(endpoint="per_sub", q='"x"', rationale="r")

    def test_per_sub_with_subreddit_validates(self) -> None:
        spec = RedditQuerySpec(
            endpoint="per_sub",
            q='"frustrated with"',
            subreddit="WeddingPhotography",
            rationale="r",
        )
        assert spec.subreddit == "WeddingPhotography"

    def test_site_wide_must_not_set_subreddit(self) -> None:
        """site_wide queries put subreddit clauses inside `q`; the dedicated
        field is reserved for per_sub."""
        with pytest.raises(ValidationError):
            RedditQuerySpec(
                endpoint="site_wide",
                q='(subreddit:a) AND "x"',
                subreddit="WeddingPhotography",
                rationale="r",
            )

    def test_site_wide_with_subreddit_none_validates(self) -> None:
        spec = RedditQuerySpec(
            endpoint="site_wide",
            q='(subreddit:a) AND "x"',
            rationale="r",
        )
        assert spec.subreddit is None


class TestSubredditSearchPhrases:
    def test_accepts_three_to_eight_phrases(self) -> None:
        m = SubredditSearchPhrases(phrases=["a", "b", "c"])
        assert m.phrases == ["a", "b", "c"]

    def test_rejects_fewer_than_three(self) -> None:
        with pytest.raises(ValidationError):
            SubredditSearchPhrases(phrases=["a", "b"])

    def test_accepts_eight_phrases(self) -> None:
        m = SubredditSearchPhrases(phrases=[str(i) for i in range(8)])
        assert len(m.phrases) == 8

    def test_rejects_more_than_eight(self) -> None:
        with pytest.raises(ValidationError):
            SubredditSearchPhrases(phrases=[str(i) for i in range(9)])
