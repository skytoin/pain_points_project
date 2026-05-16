"""Shape tests for the Call #1 prompt (spec §6 prompt #1). Pins the
module shape, not the wording (wording evolves via VERSION bumps).
"""

from __future__ import annotations

from datetime import date

from discovery.jobs import JobSpec
from discovery.llm.prompts import subreddit_phrases as sp


class TestPromptModule:
    def test_has_version_v1(self) -> None:
        assert sp.VERSION == "v1"

    def test_system_prompt_says_phrases_not_names(self) -> None:
        s = sp.SYSTEM_PROMPT.lower()
        assert "phrase" in s
        assert "subreddit" in s
        # the core rule, concept-pinned (survives wording tweaks but
        # fails a prompt that drops the phrases-not-names rule):
        assert "not subreddit name" in s

    def test_few_shot_examples_present_with_phrases(self) -> None:
        assert len(sp.FEW_SHOT_EXAMPLES) >= 2
        for ex in sp.FEW_SHOT_EXAMPLES:
            assert "input" in ex
            assert "output" in ex
            assert "phrases" in ex["output"]
            assert len(ex["output"]["phrases"]) >= 3


class TestBuildUserMessage:
    def test_renders_industry_and_optionals(self) -> None:
        msg = sp.build_user_message(
            JobSpec(
                industry="commercial cleaning",
                as_of=date(2026, 6, 1),
                location="NY",
                size="medium",
            )
        )
        assert "commercial cleaning" in msg
        assert "NY" in msg
        assert "medium" in msg

    def test_omits_unset_optionals(self) -> None:
        msg = sp.build_user_message(JobSpec(industry="bakery", as_of=date(2026, 6, 1)))
        assert "bakery" in msg
        assert "None" not in msg
