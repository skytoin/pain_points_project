"""Tests for `discovery.llm.prompts.query_expansion` — the Wave 0 prompt.

These tests pin the *shape* of the prompt module (VERSION present,
build_user_message renders the spec, system prompt mentions the key
Reddit syntax rules). They don't pin the prompt wording — that's
expected to evolve via VERSION bumps.
"""

from __future__ import annotations

from datetime import date

from discovery.jobs import JobSpec
from discovery.llm.prompts import query_expansion as qe


class TestPromptModule:
    def test_has_version(self) -> None:
        assert isinstance(qe.VERSION, str)
        assert qe.VERSION
        # cache key includes VERSION — bump it when prompt changes
        assert qe.VERSION.startswith("v")

    def test_system_prompt_mentions_core_reddit_rules(self) -> None:
        sp = qe.SYSTEM_PROMPT
        assert "OR" in sp  # item 6: uppercase operators
        assert "subreddit:" in sp  # item 6: scope-to-sub syntax
        assert "quote" in sp.lower() or "quoted" in sp.lower()  # item 6
        assert "rationale" in sp.lower()  # the model has to explain itself
        assert "10" in sp  # min queries
        assert "15" in sp  # max queries

    def test_few_shot_examples_are_present(self) -> None:
        assert len(qe.FEW_SHOT_EXAMPLES) >= 2
        for ex in qe.FEW_SHOT_EXAMPLES:
            assert "input" in ex
            assert "output" in ex
            assert "reddit_queries" in ex["output"]
            assert len(ex["output"]["reddit_queries"]) >= 10


class TestBuildUserMessage:
    def test_renders_spec_fields(self) -> None:
        spec = JobSpec(
            industry="commercial cleaning",
            as_of=date(2026, 6, 1),
            location="NY",
            size="medium",
        )
        msg = qe.build_user_message(spec)
        assert "commercial cleaning" in msg
        assert "NY" in msg
        assert "medium" in msg
        assert "2026-06-01" in msg

    def test_handles_optional_fields(self) -> None:
        spec = JobSpec(industry="bakery", as_of=date(2026, 6, 1))
        msg = qe.build_user_message(spec)
        assert "bakery" in msg
        assert "None" not in msg

    def test_includes_time_window(self) -> None:
        """The LLM is told the search-window choice so it can match it
        in each query's `t` field."""
        spec = JobSpec(industry="x", as_of=date(2026, 6, 1), time_window="year")
        msg = qe.build_user_message(spec)
        assert "year" in msg.lower()

    def test_version_is_v3(self) -> None:
        """v3 added the time_window field; bump captured here."""
        assert qe.VERSION == "v3"
