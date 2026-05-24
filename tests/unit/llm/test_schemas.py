"""Tests for `discovery.llm.schemas` — RedditQuerySpec, JobPlan."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from discovery.llm.schemas import (
    HackerNewsKeywordSpec,
    JobPlan,
    RedditQuerySpec,
    SubredditSearchPhrases,
    YouTubeQuerySpec,
)


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


def _make_reddit_queries(n: int = 25) -> list[RedditQuerySpec]:
    """Build N valid RedditQuerySpec to satisfy JobPlan's 25-30 band."""
    return [
        RedditQuerySpec(
            endpoint="site_wide",
            q=f'(subreddit:startups) AND "test{i}"',
            sort="top",
            t="month",
            limit=100,
            rationale="test",
        )
        for i in range(n)
    ]


class TestJobPlanHnQueries:
    def test_hn_queries_defaults_to_empty_list(self) -> None:
        plan = JobPlan(reddit_queries=_make_reddit_queries())
        assert plan.hn_queries == []

    def test_hn_queries_accepts_list(self) -> None:
        plan = JobPlan(
            reddit_queries=_make_reddit_queries(),
            hn_queries=[
                HackerNewsKeywordSpec(keyword="CRM CLI", intent="launch", rationale="r"),
            ],
        )
        assert len(plan.hn_queries) == 1
        assert plan.hn_queries[0].keyword == "CRM CLI"

    def test_hn_queries_round_trips_through_model_dump(self) -> None:
        plan = JobPlan(
            reddit_queries=_make_reddit_queries(),
            hn_queries=[
                HackerNewsKeywordSpec(keyword="x", intent="context", rationale="r"),
            ],
        )
        restored = JobPlan.model_validate(plan.model_dump())
        assert len(restored.hn_queries) == 1
        assert restored.hn_queries[0].intent == "context"

    def test_empty_hn_queries_does_not_break_validation(self) -> None:
        """A JobPlan with empty hn_queries must validate cleanly — the
        permissive default is what keeps HN sparsity from sinking the
        Reddit plan."""
        plan = JobPlan(reddit_queries=_make_reddit_queries(), hn_queries=[])
        assert plan.hn_queries == []


class TestHackerNewsKeywordSpec:
    def test_minimal_valid(self) -> None:
        spec = HackerNewsKeywordSpec(
            keyword="local-first CRM",
            intent="launch",
            rationale="Show HN local-first CRM launches",
        )
        assert spec.keyword == "local-first CRM"
        assert spec.intent == "launch"
        assert spec.rationale == "Show HN local-first CRM launches"

    def test_intent_must_be_launch_or_context(self) -> None:
        with pytest.raises(ValidationError):
            HackerNewsKeywordSpec(keyword="x", intent="other", rationale="r")  # type: ignore[arg-type]

    def test_keyword_min_length(self) -> None:
        with pytest.raises(ValidationError):
            HackerNewsKeywordSpec(keyword="", intent="launch", rationale="r")

    def test_rationale_min_length(self) -> None:
        with pytest.raises(ValidationError):
            HackerNewsKeywordSpec(keyword="x", intent="launch", rationale="")

    def test_frozen_blocks_assignment(self) -> None:
        spec = HackerNewsKeywordSpec(keyword="x", intent="launch", rationale="r")
        with pytest.raises(ValidationError):
            spec.keyword = "y"  # type: ignore[misc]


class TestYouTubeQuerySpec:
    def test_minimal_valid(self) -> None:
        spec = YouTubeQuerySpec(
            query="why I quit commercial cleaning",
            intent="complaint",
            rationale="quit-the-industry pain monologue",
        )
        assert spec.query == "why I quit commercial cleaning"
        assert spec.intent == "complaint"

    def test_intent_must_be_complaint_or_discussion(self) -> None:
        with pytest.raises(ValidationError):
            YouTubeQuerySpec(query="x", intent="other", rationale="r")  # type: ignore[arg-type]

    def test_query_min_length(self) -> None:
        with pytest.raises(ValidationError):
            YouTubeQuerySpec(query="", intent="complaint", rationale="r")

    def test_query_max_length(self) -> None:
        with pytest.raises(ValidationError):
            YouTubeQuerySpec(query="x" * 121, intent="discussion", rationale="r")

    def test_rationale_min_length(self) -> None:
        with pytest.raises(ValidationError):
            YouTubeQuerySpec(query="x", intent="complaint", rationale="")

    def test_frozen_blocks_assignment(self) -> None:
        spec = YouTubeQuerySpec(query="x", intent="complaint", rationale="r")
        with pytest.raises(ValidationError):
            spec.query = "y"  # type: ignore[misc]
