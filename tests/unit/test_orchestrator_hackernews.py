from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from discovery.orchestrator.hackernews import _routing_for, _time_window_epoch


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
