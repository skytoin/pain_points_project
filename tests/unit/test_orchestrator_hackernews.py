from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from discovery.jobs import JobSpec
from discovery.llm.schemas import HackerNewsKeywordSpec
from discovery.orchestrator.hackernews import (
    MAX_HN_QUERIES,
    _compile_hn_queries,
    _routing_for,
    _time_window_epoch,
    hn_keyword_candidates_for_spec,
)


def _epoch(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp())


class TestTimeWindowEpoch:
    def test_hour_window(self) -> None:
        anchor = date(2026, 5, 20)
        # Anchor at 2026-05-20 00:00 UTC; subtract 1 hour -> 2026-05-19 23:00 UTC.
        assert _time_window_epoch("hour", anchor) == _epoch(2026, 5, 19, 23)

    def test_day_window(self) -> None:
        anchor = date(2026, 5, 20)
        assert _time_window_epoch("day", anchor) == _epoch(2026, 5, 19)

    def test_week_window(self) -> None:
        anchor = date(2026, 5, 20)
        assert _time_window_epoch("week", anchor) == _epoch(2026, 5, 13)

    def test_month_window_30_days(self) -> None:
        anchor = date(2026, 5, 20)
        assert _time_window_epoch("month", anchor) == _epoch(2026, 4, 20)

    def test_year_window_365_days(self) -> None:
        anchor = date(2026, 5, 20)
        assert _time_window_epoch("year", anchor) == _epoch(2025, 5, 20)

    def test_all_returns_none(self) -> None:
        """`all` -> None signals 'omit created_at_i entirely from numericFilters'."""
        assert _time_window_epoch("all", date(2026, 5, 20)) is None

    def test_unknown_window_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown time window"):
            _time_window_epoch("decade", date(2026, 5, 20))


class TestRoutingFor:
    def test_launch_routes_to_search_by_date_show_hn_relaxed(self) -> None:
        endpoint, tags, extra = _routing_for("launch")
        assert endpoint == "search_by_date"
        assert tags == "show_hn"
        assert extra == []  # no points/comments floor -- recency is the signal

    def test_context_routes_to_search_story_with_quality_floor(self) -> None:
        endpoint, tags, extra = _routing_for("context")
        assert endpoint == "search"
        assert tags == "story"
        assert extra == ["points>5", "num_comments>3"]

    def test_unknown_intent_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            _routing_for("unknown")


def _kw(keyword: str, intent: str = "launch") -> HackerNewsKeywordSpec:
    return HackerNewsKeywordSpec(
        keyword=keyword,
        intent=intent,  # type: ignore[arg-type]
        rationale="test",
    )


def _spec(industry: str = "test industry", time_window: str = "month") -> JobSpec:
    return JobSpec(
        industry=industry,
        as_of=date(2026, 5, 20),
        time_window=time_window,  # type: ignore[arg-type]
    )


class TestCompileHnQueries:
    def test_decomposes_each_keyword_to_two_tokens(self) -> None:
        out = _compile_hn_queries([_kw("Personal CRM local-first")], _spec())
        assert len(out) == 1
        # "local-first" at position 3 is dropped by decompose_keyword.
        assert out[0]["query"] == "Personal CRM"

    def test_drops_empty_decomposition(self) -> None:
        # All-stopwords keyword decomposes to [] -> dropped silently.
        out = _compile_hn_queries([_kw("the a an")], _spec())
        assert out == []

    def test_dedupes_on_token_tuple(self) -> None:
        # Same keyword twice -> compiled once.
        out = _compile_hn_queries([_kw("MCP server"), _kw("MCP server")], _spec())
        assert len(out) == 1

    def test_dedup_is_case_sensitive(self) -> None:
        # `MCP` and `mcp` are different on HN (acronym casing matters).
        out = _compile_hn_queries([_kw("MCP server"), _kw("mcp server")], _spec())
        assert len(out) == 2

    def test_routes_launch_to_search_by_date_show_hn(self) -> None:
        out = _compile_hn_queries([_kw("CRM CLI", intent="launch")], _spec())
        assert out[0]["endpoint"] == "search_by_date"
        assert out[0]["tags"] == "show_hn"

    def test_routes_context_to_search_with_quality_floor(self) -> None:
        out = _compile_hn_queries([_kw("CRM founder", intent="context")], _spec())
        assert out[0]["endpoint"] == "search"
        assert out[0]["tags"] == "story"
        assert "points>5" in out[0]["numeric_filters"]
        assert "num_comments>3" in out[0]["numeric_filters"]

    def test_includes_created_at_i_when_time_window_is_not_all(self) -> None:
        out = _compile_hn_queries([_kw("CRM CLI")], _spec(time_window="month"))
        assert "created_at_i>" in out[0]["numeric_filters"]

    def test_omits_all_filters_for_launch_plus_all_time_window(self) -> None:
        """Launch intent + time_window='all' -> no quality floor and no
        recency floor -> numeric_filters is the empty string."""
        out = _compile_hn_queries([_kw("CRM CLI", intent="launch")], _spec(time_window="all"))
        assert out[0]["numeric_filters"] == ""

    def test_context_with_all_time_keeps_quality_floor(self) -> None:
        out = _compile_hn_queries([_kw("CRM founder", intent="context")], _spec(time_window="all"))
        # No created_at_i but points/num_comments still apply.
        assert "created_at_i" not in out[0]["numeric_filters"]
        assert "points>5" in out[0]["numeric_filters"]
        assert "num_comments>3" in out[0]["numeric_filters"]

    def test_caps_at_max_hn_queries_preserving_llm_order(self) -> None:
        kws = [_kw(f"kw{i} tok") for i in range(15)]
        out = _compile_hn_queries(kws, _spec())
        assert len(out) == MAX_HN_QUERIES == 6
        # Order preserved -- LLM ranking signal (spec §8).
        assert out[0]["query"] == "kw0 tok"
        assert out[5]["query"] == "kw5 tok"

    def test_hits_per_page_is_30(self) -> None:
        out = _compile_hn_queries([_kw("CRM CLI")], _spec())
        assert out[0]["hits_per_page"] == 30

    def test_query_is_space_joined_tokens(self) -> None:
        # Tokens joined by single space -- HN treats whitespace as token-AND.
        out = _compile_hn_queries([_kw("CRM CLI")], _spec())
        assert out[0]["query"] == "CRM CLI"

    def test_created_at_i_appears_first_in_filter_string(self) -> None:
        out = _compile_hn_queries(
            [_kw("CRM founder", intent="context")], _spec(time_window="month")
        )
        # Convention: time filter first, quality filters after.
        filters = out[0]["numeric_filters"]
        assert filters.startswith("created_at_i>")


class TestHnKeywordCandidatesForSpec:
    def test_returns_at_least_one_compiled_query(self) -> None:
        out = hn_keyword_candidates_for_spec(_spec(industry="cleaning"))
        # Template has 4 candidates -- all should compile cleanly for a
        # single-word industry.
        assert len(out) >= 1

    def test_capability_first_survives_decomposition_for_multiword_industry(self) -> None:
        """For 'commercial cleaning', the template uses `CLI commercial
        cleaning` etc. so the capability word lands in position 1 and
        survives the 2-token cap."""
        out = hn_keyword_candidates_for_spec(_spec(industry="commercial cleaning"))
        queries = [q["query"] for q in out]
        # Every query starts with a capability word (CLI/OSS/API/workflow)
        # followed by the first industry word.
        assert any(q.startswith("CLI ") for q in queries)
        assert any(q.startswith("OSS ") for q in queries)
        assert any(q.startswith("API ") for q in queries)
        assert any(q.startswith("workflow ") for q in queries)

    def test_includes_both_launch_and_context_queries(self) -> None:
        out = hn_keyword_candidates_for_spec(_spec(industry="cleaning"))
        endpoints = {q["endpoint"] for q in out}
        # CLI/OSS/API are launch, workflow is context.
        assert "search_by_date" in endpoints  # at least one launch
        assert "search" in endpoints  # at least one context

    def test_each_query_carries_the_time_window_filter(self) -> None:
        out = hn_keyword_candidates_for_spec(_spec(industry="cleaning", time_window="year"))
        for q in out:
            # year window -> created_at_i floor present on every query.
            assert "created_at_i>" in q["numeric_filters"]

    def test_template_output_is_deterministic(self) -> None:
        """Same spec, same compiled output -- the template + compile
        pipeline are pure. (Dedup behavior for collision cases is
        exercised separately in TestCompileHnQueries.)"""
        spec = _spec(industry="cleaning")
        assert hn_keyword_candidates_for_spec(spec) == hn_keyword_candidates_for_spec(spec)
