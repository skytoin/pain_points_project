"""Tests for `discovery.llm.schemas` — RedditQuerySpec, JobPlan."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from discovery.llm.schemas import JobPlan, RedditQuerySpec


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
    def test_requires_min_10_queries(self) -> None:
        with pytest.raises(ValidationError):
            JobPlan(reddit_queries=[_good_query() for _ in range(9)])

    def test_accepts_10_queries(self) -> None:
        plan = JobPlan(reddit_queries=[_good_query() for _ in range(10)])
        assert len(plan.reddit_queries) == 10

    def test_rejects_more_than_15_queries(self) -> None:
        with pytest.raises(ValidationError):
            JobPlan(reddit_queries=[_good_query() for _ in range(16)])

    def test_extra_fields_round_trip(self) -> None:
        """extra='allow' — future prompts can emit extra fields and they
        stay on the model (and on Job.job_plan JSON) without losing them."""
        plan = JobPlan.model_validate(
            {
                "reddit_queries": [_good_query().model_dump() for _ in range(10)],
                "youtube_queries": ["a", "b"],  # not a typed field yet
            }
        )
        dumped = plan.model_dump()
        assert "youtube_queries" in dumped
        assert dumped["youtube_queries"] == ["a", "b"]

    def test_reddit_subreddits_defaults_to_empty(self) -> None:
        plan = JobPlan(reddit_queries=[_good_query() for _ in range(10)])
        assert plan.reddit_subreddits == []
