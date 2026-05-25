"""Shape tests for the Wave 0 Call #2 prompt (query_expansion v8).

Pins module shape, not wording. v8 adds a fourth output (youtube_queries)
via the "Kind 4 -- YouTube keyword candidates" section. All v7 content
retained.
"""

from __future__ import annotations

from datetime import date

from discovery.jobs import JobSpec
from discovery.llm.prompts import query_expansion as qe
from discovery.llm.prompts.query_expansion import (
    SYSTEM_PROMPT,
    VERSION,
    build_user_message,
)
from discovery.sources.reddit_subreddits import SubredditCandidate


def _table() -> list[SubredditCandidate]:
    return [
        SubredditCandidate(
            name="CleaningTips",
            subscribers=12000,
            active_user_count=80,
            activity_ratio=0.0067,
            public_description="tips for cleaning professionals",
            matched_phrases=3,
        )
    ]


class TestPromptModule:
    def test_version_is_v8(self) -> None:
        assert qe.VERSION == "v8"

    def test_system_prompt_keeps_core_reddit_rules(self) -> None:
        sp = qe.SYSTEM_PROMPT
        assert "OR" in sp
        assert "subreddit:" in sp
        assert "quote" in sp.lower() or "quoted" in sp.lower()
        assert "rationale" in sp.lower()
        assert "25" in sp
        assert "30" in sp

    def test_system_prompt_has_grounding_section(self) -> None:
        s = qe.SYSTEM_PROMPT.lower()
        assert "only" in s
        assert "table" in s
        assert "never" in s
        assert "invent" in s or "memory" in s
        assert "matched_phrases" in s
        assert "public_description" in s
        assert "activity_ratio" in s

    def test_system_prompt_has_two_kinds_and_industry_brainstorm(self) -> None:
        s = qe.SYSTEM_PROMPT.lower()
        assert "two kinds" in s
        assert "industry-specific" in s
        assert "standard" in s
        assert "wedding photography" in s
        assert "do not reuse" in s
        assert "re-derive" in s or "never copy" in s

    def test_few_shot_examples_still_present(self) -> None:
        assert len(qe.FEW_SHOT_EXAMPLES) >= 2
        for ex in qe.FEW_SHOT_EXAMPLES:
            assert len(ex["output"]["reddit_queries"]) >= 10


class TestBuildUserMessage:
    def test_renders_spec_and_table(self) -> None:
        msg = qe.build_user_message(
            JobSpec(
                industry="commercial cleaning",
                as_of=date(2026, 6, 1),
                location="NY",
                size="medium",
            ),
            _table(),
        )
        assert "commercial cleaning" in msg
        assert "NY" in msg
        assert "medium" in msg
        assert "2026-06-01" in msg
        assert "CleaningTips" in msg
        assert "matched_phrases" in msg
        assert "25" in msg
        assert "30" in msg

    def test_handles_optional_fields(self) -> None:
        msg = qe.build_user_message(JobSpec(industry="bakery", as_of=date(2026, 6, 1)), _table())
        assert "bakery" in msg
        assert "None" not in msg

    def test_includes_time_window(self) -> None:
        msg = qe.build_user_message(
            JobSpec(industry="x", as_of=date(2026, 6, 1), time_window="year"),
            _table(),
        )
        assert "year" in msg.lower()


class TestPromptV7Additions:
    """v7 = v6 plus the 12-query cap wording. Raises HN query cap from 6
    to 12 (MAX_HN_QUERIES). Spec §8. Retained under v8.
    """

    def test_kind_3_section_present(self) -> None:
        assert "Kind 3" in SYSTEM_PROMPT
        assert "Hacker News" in SYSTEM_PROMPT

    def test_hn_capability_framing_taught(self) -> None:
        assert "CAPABILITY and LAUNCH framing" in SYSTEM_PROMPT

    def test_tag_redundancy_rule_present(self) -> None:
        assert (
            "tag-redundant" in SYSTEM_PROMPT.lower()
            or 'Don\'t write\n   "Show HN"' in SYSTEM_PROMPT
        )

    def test_first_two_positions_rule_present(self) -> None:
        assert "first two positions" in SYSTEM_PROMPT

    def test_quality_over_quota_sparsity_clause_present(self) -> None:
        assert "Quality over quota" in SYSTEM_PROMPT

    def test_strongest_first_ranking_signal_present(self) -> None:
        assert "STRONGEST CANDIDATES FIRST" in SYSTEM_PROMPT

    def test_python_does_not_enforce_ratio_clarifier_present(self) -> None:
        assert "Python does NOT enforce the ratio" in SYSTEM_PROMPT

    def test_build_user_message_includes_hn_nudge(self) -> None:
        spec = JobSpec(industry="x", as_of=date(2026, 5, 20), time_window="month")
        table = [
            SubredditCandidate(
                name="startups",
                subscribers=5000,
                active_user_count=120,
                subreddit_type="public",
                public_description="x",
            )
        ]
        msg = build_user_message(spec, table)

        assert "hn_queries" in msg
        assert "HackerNews keyword candidates" in msg
        assert "capability/launch framing" in msg


class TestPromptV8Additions:
    """v8 = v7 plus the fourth output (youtube_queries) via the "Kind 4 --
    YouTube keyword candidates" section. Spec §8.
    """

    def test_version_is_v8(self) -> None:
        assert VERSION == "v8"

    def test_kind_4_section_present(self) -> None:
        assert "youtube_queries" in SYSTEM_PROMPT
        assert "Kind 4" in SYSTEM_PROMPT
        assert "complaint" in SYSTEM_PROMPT
        assert "discussion" in SYSTEM_PROMPT
        assert "day in the life" in SYSTEM_PROMPT

    def test_youtube_intent_tagging_taught(self) -> None:
        assert "intent" in SYSTEM_PROMPT
        # complaint = the video IS the pain; discussion = pain in comments.
        assert "horror stories" in SYSTEM_PROMPT
        assert "why I quit" in SYSTEM_PROMPT

    def test_youtube_graceful_sparsity_present(self) -> None:
        assert "Quality over quota" in SYSTEM_PROMPT

    def test_youtube_re_derive_guard_present(self) -> None:
        assert "do NOT translate the reddit" in SYSTEM_PROMPT or "Re-derive" in SYSTEM_PROMPT

    def test_master_what_to_emit_lists_four_fields(self) -> None:
        assert "JobPlan` with FOUR fields" in SYSTEM_PROMPT
        assert "reddit_queries" in SYSTEM_PROMPT
        assert "reddit_subreddits" in SYSTEM_PROMPT
        assert "hn_queries" in SYSTEM_PROMPT
        assert "youtube_queries" in SYSTEM_PROMPT

    def test_build_user_message_includes_youtube_nudge(self) -> None:
        spec = JobSpec(industry="x", as_of=date(2026, 5, 20), time_window="month")
        table = [
            SubredditCandidate(
                name="startups",
                subscribers=5000,
                active_user_count=120,
                subreddit_type="public",
                public_description="x",
            )
        ]
        msg = build_user_message(spec, table)

        assert "youtube_queries" in msg
        assert "complaint" in msg
        assert "discussion" in msg
