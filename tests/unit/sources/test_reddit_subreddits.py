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


# --- Client integration --------------------------------------------------

from collections.abc import Callable  # noqa: E402

import httpx  # noqa: E402
from loguru import logger as _loguru  # noqa: E402

from discovery.sources.reddit_subreddits import _parse_listing, search_subreddits  # noqa: E402


def _t5(name: str, **over: object) -> dict[str, object]:
    data: dict[str, object] = {
        "display_name": name,
        "subscribers": 1000,
        "active_user_count": 50,
        "subreddit_type": "public",
        "over18": False,
        "public_description": f"{name} community",
    }
    data.update(over)
    return {"kind": "t5", "data": data}


def _listing(*names: str) -> dict[str, object]:
    return {"kind": "Listing", "data": {"children": [_t5(n) for n in names], "after": None}}


async def _noop_sleep(_: float) -> None:
    return None


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestSearchSubredditsHappyPath:
    async def test_returns_one_phraseresult_per_phrase(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_listing("startups", "saas"))

        out = await search_subreddits(
            ["a", "b"],
            user_agent="discovery-tests/0.1",
            client=_client(handler),
            sleep=_noop_sleep,
        )
        assert [r.phrase for r in out] == ["a", "b"]
        assert {c.name for c in out[0].candidates} == {"startups", "saas"}
        assert out[0].candidates[0].public_description != ""

    async def test_request_url_has_required_params(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["ua"] = request.headers.get("user-agent", "")
            return httpx.Response(200, json=_listing())

        await search_subreddits(
            ["food truck"],
            user_agent="my-app/1.0 (u/me)",
            client=_client(handler),
            sleep=_noop_sleep,
        )
        assert "/subreddits/search.json" in seen["url"]
        assert "q=food+truck" in seen["url"] or "q=food%20truck" in seen["url"]
        assert "limit=100" in seen["url"]
        assert "raw_json=1" in seen["url"]
        assert "include_over_18=false" in seen["url"]
        assert seen["ua"] == "my-app/1.0 (u/me)"


class TestSearchSubredditsEmpty:
    async def test_empty_children_is_ok_empty_not_failure(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"kind": "Listing", "data": {"children": []}})

        out = await search_subreddits(
            ["x"],
            user_agent="discovery-tests/0.1",
            client=_client(handler),
            sleep=_noop_sleep,
        )
        assert len(out) == 1
        assert out[0].candidates == []


class TestSearchSubredditsRetry:
    async def test_429_then_200_retries_and_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "1"})
            return httpx.Response(200, json=_listing("startups"))

        out = await search_subreddits(
            ["x"],
            user_agent="discovery-tests/0.1",
            client=_client(handler),
            sleep=_noop_sleep,
        )
        assert calls["n"] == 2
        assert out[0].candidates[0].name == "startups"


class TestSearchSubreddits403Raises:
    async def test_403_raises_not_empty(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        with pytest.raises(httpx.HTTPStatusError):
            await search_subreddits(
                ["x"],
                user_agent="discovery-tests/0.1",
                client=_client(handler),
                sleep=_noop_sleep,
            )


class TestSearchSubredditsPartialSuccess:
    async def test_one_phrase_failing_does_not_kill_the_others(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "q=bad" in str(request.url):
                return httpx.Response(500)
            return httpx.Response(200, json=_listing("startups"))

        out = await search_subreddits(
            ["good1", "bad", "good2"],
            user_agent="discovery-tests/0.1",
            client=_client(handler),
            sleep=_noop_sleep,
            max_retries=0,
        )
        assert [r.phrase for r in out] == ["good1", "good2"]

    async def test_total_wipeout_raises_first_error(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        with pytest.raises(httpx.HTTPError):
            await search_subreddits(
                ["a", "b"],
                user_agent="discovery-tests/0.1",
                client=_client(handler),
                sleep=_noop_sleep,
                max_retries=0,
            )


class TestSearchSubredditsLimiterRouting:
    async def test_every_request_goes_through_the_injected_limiter(self) -> None:
        entered = {"n": 0}

        class _CountingLimiter:
            async def __aenter__(self) -> None:
                entered["n"] += 1

            async def __aexit__(self, *exc: object) -> None:
                return None

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_listing("startups"))

        await search_subreddits(
            ["a", "b", "c"],
            user_agent="discovery-tests/0.1",
            client=_client(handler),
            sleep=_noop_sleep,
            limiter=_CountingLimiter(),  # type: ignore[arg-type]
        )
        assert entered["n"] == 3  # one limiter acquisition per phrase


class TestSearchSubredditsLogging:
    async def test_per_request_structured_log(self) -> None:
        captured: list[dict[str, object]] = []

        def sink(message: object) -> None:
            captured.append(dict(message.record["extra"]))  # type: ignore[attr-defined]

        sink_id = _loguru.add(sink, level="DEBUG")
        try:

            def handler(_: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=_listing("startups", "saas"))

            await search_subreddits(
                ["x"],
                user_agent="discovery-tests/0.1",
                client=_client(handler),
                sleep=_noop_sleep,
            )
            logs = [c for c in captured if "url" in c and "count_after_filter" in c]
            assert logs, f"no per-request log; captured: {captured}"
            log = logs[0]
            assert log["status"] == 200
            assert log["count_before_filter"] == 2
            assert log["count_after_filter"] == 2
            assert log["phrase"] == "x"
            assert "/subreddits/search.json" in log["url"]
        finally:
            _loguru.remove(sink_id)


class TestParseListingMalformed:
    def test_children_null_is_empty_not_typeerror(self) -> None:
        cands, count = _parse_listing({"data": {"children": None}})
        assert cands == []
        assert count == 0

    def test_data_null_is_empty(self) -> None:
        cands, count = _parse_listing({"data": None})
        assert cands == []
        assert count == 0

    def test_children_non_list_is_empty(self) -> None:
        cands, count = _parse_listing({"data": {"children": "oops"}})
        assert cands == []
        assert count == 0


class TestSearchSubreddits401Raises:
    async def test_401_raises_not_empty(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        with pytest.raises(httpx.HTTPStatusError):
            await search_subreddits(
                ["x"],
                user_agent="discovery-tests/0.1",
                client=_client(handler),
                sleep=_noop_sleep,
            )
