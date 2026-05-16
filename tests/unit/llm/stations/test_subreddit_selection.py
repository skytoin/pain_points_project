"""Tests for the deterministic subreddit pipeline (spec §7).

Each function is pure; boundary cases per spec §12.
"""

from __future__ import annotations

from discovery.llm.stations.subreddit_selection import (
    DRASTIC_FLOOR_DIVISOR,
    SELECTION_CEILING,
    dedupe_and_count,
    drop_below_median,
    drop_non_public,
    drop_nsfw,
    reject_off_table,
    subscriber_median,
    trim_overflow,
    with_activity_ratio,
)
from discovery.sources.reddit_subreddits import PhraseResult, SubredditCandidate


def _c(name: str, **kw: object) -> SubredditCandidate:
    base: dict[str, object] = {"name": name}
    base.update(kw)
    return SubredditCandidate(**base)  # type: ignore[arg-type]


class TestDedupeAndCount:
    def test_collapses_to_unique_name_case_insensitive(self) -> None:
        results = [
            PhraseResult(phrase="p1", candidates=[_c("Startups"), _c("saas")]),
            PhraseResult(phrase="p2", candidates=[_c("startups")]),
        ]
        out = dedupe_and_count(results)
        names = sorted(c.name for c in out)
        assert names == ["Startups", "saas"]  # first-seen casing kept

    def test_matched_phrases_counts_distinct_phrases(self) -> None:
        results = [
            PhraseResult(phrase="p1", candidates=[_c("startups")]),
            PhraseResult(phrase="p2", candidates=[_c("startups")]),
            PhraseResult(phrase="p2", candidates=[_c("startups")]),  # dup phrase
        ]
        out = dedupe_and_count(results)
        assert len(out) == 1
        assert out[0].matched_phrases == 2  # p1, p2 — not 3

    def test_first_occurrence_wins_for_other_fields(self) -> None:
        results = [
            PhraseResult(phrase="p1", candidates=[_c("x", subscribers=100)]),
            PhraseResult(phrase="p2", candidates=[_c("x", subscribers=999)]),
        ]
        out = dedupe_and_count(results)
        assert out[0].subscribers == 100

    def test_output_order_is_first_seen(self) -> None:
        results = [
            PhraseResult(phrase="p1", candidates=[_c("C"), _c("A")]),
            PhraseResult(phrase="p2", candidates=[_c("B"), _c("C")]),
        ]
        out = dedupe_and_count(results)
        assert [c.name for c in out] == ["C", "A", "B"]


class TestDropNonPublic:
    def test_keeps_public_and_restricted_only(self) -> None:
        cands = [
            _c("a", subreddit_type="public"),
            _c("b", subreddit_type="restricted"),
            _c("c", subreddit_type="private"),
            _c("d", subreddit_type="archived"),
            _c("e", subreddit_type="quarantined"),
        ]
        kept = {c.name for c in drop_non_public(cands)}
        assert kept == {"a", "b"}


class TestDropNsfw:
    def test_drops_over18(self) -> None:
        cands = [_c("a", over18=False), _c("b", over18=True)]
        assert [c.name for c in drop_nsfw(cands)] == ["a"]


class TestSubscriberMedian:
    def test_odd_count(self) -> None:
        assert (
            subscriber_median(
                [_c("a", subscribers=1), _c("b", subscribers=3), _c("c", subscribers=2)]
            )
            == 2.0
        )

    def test_even_count(self) -> None:
        assert subscriber_median([_c("a", subscribers=1), _c("b", subscribers=3)]) == 2.0

    def test_empty_is_zero(self) -> None:
        assert subscriber_median([]) == 0.0


class TestDropBelowMedian:
    def test_drops_strictly_below_floor_keeps_equal(self) -> None:
        # median 1000 → floor = 1000 / 10 = 100. 100 kept, 99 dropped.
        cands = [_c("keep", subscribers=100), _c("drop", subscribers=99)]
        kept = {c.name for c in drop_below_median(cands, 1000.0)}
        assert kept == {"keep"}

    def test_zero_median_keeps_all(self) -> None:
        # list(cands) makes a fresh list object, so callers cannot mutate
        # the result and affect the input. Assert identity differs AND
        # values match — the `is not` is what actually pins no-aliasing.
        cands = [_c("a", subscribers=0)]
        result = drop_below_median(cands, 0.0)
        assert result is not cands  # fresh list, not the input
        assert result == cands  # same contents

    def test_divisor_constant_is_ten(self) -> None:
        assert DRASTIC_FLOOR_DIVISOR == 10


class TestWithActivityRatio:
    def test_normal_ratio_rounded_4dp(self) -> None:
        out = with_activity_ratio([_c("a", subscribers=3000, active_user_count=10)])
        assert out[0].activity_ratio == round(10 / 3000, 4)

    def test_zero_active_user_count_ratio_is_zero(self) -> None:
        out = with_activity_ratio([_c("a", subscribers=1000, active_user_count=0)])
        assert out[0].activity_ratio == 0.0

    def test_zero_subscribers_guarded_to_zero(self) -> None:
        out = with_activity_ratio([_c("a", subscribers=0, active_user_count=5)])
        assert out[0].activity_ratio == 0.0


class TestTrimOverflow:
    def test_passthrough_at_or_below_ceiling(self) -> None:
        names = [f"s{i}" for i in range(SELECTION_CEILING)]
        assert trim_overflow(names) == names

    def test_keeps_first_30_in_order_when_over(self) -> None:
        names = [f"s{i}" for i in range(45)]
        out = trim_overflow(names)
        assert out == [f"s{i}" for i in range(30)]

    def test_ceiling_constant_is_30(self) -> None:
        assert SELECTION_CEILING == 30


class TestRejectOffTable:
    def test_drops_names_not_in_table_case_insensitive(self) -> None:
        table = [_c("Startups"), _c("saas")]
        assert reject_off_table(["startups", "ghost", "SAAS"], table) == [
            "startups",
            "SAAS",
        ]

    def test_preserves_selection_order(self) -> None:
        table = [_c("a"), _c("b"), _c("c")]
        assert reject_off_table(["c", "a", "b"], table) == ["c", "a", "b"]
