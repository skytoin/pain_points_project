from __future__ import annotations

from typing import Any

import pytest

from discovery.sources.base import RawRecord
from discovery.sources.hackernews import build_search_url, hit_to_raw_record, keep_hit


def _query(
    endpoint: str = "search",
    query: str = "Personal CRM",
    tags: str = "story",
    numeric_filters: str = "created_at_i>1700000000,points>5,num_comments>3",
    hits_per_page: int = 30,
) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "query": query,
        "tags": tags,
        "numeric_filters": numeric_filters,
        "hits_per_page": hits_per_page,
    }


class TestBuildSearchUrl:
    def test_routes_search_endpoint(self) -> None:
        url = build_search_url(_query(endpoint="search"))
        assert url.startswith("https://hn.algolia.com/api/v1/search?")

    def test_routes_search_by_date_endpoint(self) -> None:
        url = build_search_url(
            _query(endpoint="search_by_date", tags="show_hn", numeric_filters="created_at_i>1")
        )
        assert url.startswith("https://hn.algolia.com/api/v1/search_by_date?")

    def test_serializes_tags(self) -> None:
        url = build_search_url(_query(tags="story"))
        assert "tags=story" in url

    def test_serializes_numeric_filters_as_camelcase(self) -> None:
        # snake_case key in the compiled dict; Algolia expects camelCase in the URL.
        url = build_search_url(_query(numeric_filters="points>5,num_comments>3"))
        assert "numericFilters=" in url
        assert "numeric_filters" not in url

    def test_hits_per_page_passes_through(self) -> None:
        url = build_search_url(_query(hits_per_page=30))
        assert "hitsPerPage=30" in url

    def test_page_pinned_to_zero(self) -> None:
        url = build_search_url(_query())
        # Spec §11: always pin page=0 in code, do not rely on Algolia's
        # default. Top 30 by relevance/date is what we want, no paging.
        assert "page=0" in url

    def test_query_url_encoded(self) -> None:
        url = build_search_url(_query(query="Personal CRM"))
        # urlencode encodes space as `+`.
        assert "query=Personal+CRM" in url

    def test_numeric_filters_url_encoded(self) -> None:
        # `>` becomes %3E in URL encoding.
        url = build_search_url(_query(numeric_filters="created_at_i>1700000000"))
        assert "created_at_i%3E1700000000" in url

    def test_unknown_endpoint_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown HN endpoint"):
            build_search_url(_query(endpoint="search_by_relevance"))


class TestKeepHit:
    def test_keeps_normal_hit(self) -> None:
        assert keep_hit({"objectID": "12345", "title": "x"})

    def test_drops_hit_without_object_id(self) -> None:
        """Defensive -- Algolia always returns objectID, but if a hit
        ever lacked it we couldn't dedupe and Bronze would break."""
        assert not keep_hit({"title": "x"})


class TestHitToRawRecord:
    def test_external_id_is_object_id_string(self) -> None:
        hit = {
            "objectID": "12345",
            "title": "x",
            "url": "https://example.com",
            "points": 100,
            "num_comments": 20,
        }
        rec = hit_to_raw_record(hit)
        assert rec.external_id == "12345"
        assert rec.source == "hackernews"

    def test_body_is_verbatim_no_trimming(self) -> None:
        """Locked decision (spec §3): Bronze stores raw, Wave 2 parses.
        Adapter MUST NOT modify, trim, or normalize the hit."""
        long_title = "x" * 500
        hit = {
            "objectID": "1",
            "title": long_title,
            "_tags": ["story", "ask_hn"],
            "story_text": "y" * 1000,
        }
        rec = hit_to_raw_record(hit)
        assert rec.body == hit
        assert rec.body["title"] == long_title  # no trimming
        assert rec.body["story_text"] == "y" * 1000

    def test_ask_hn_post_with_null_url_still_yields_valid_external_id(self) -> None:
        """Ask HN / Show HN text posts often carry a null `url`. We
        rely on objectID for external_id, so dedupe still works.
        Wave 2 handles the permalink fallback."""
        hit = {"objectID": "9876", "title": "Ask HN: ...", "url": None}
        rec = hit_to_raw_record(hit)
        assert rec.external_id == "9876"
        assert rec.body["url"] is None  # verbatim -- no fallback in the adapter

    def test_returns_real_raw_record_instance(self) -> None:
        rec = hit_to_raw_record({"objectID": "7", "title": "x"})
        assert isinstance(rec, RawRecord)
