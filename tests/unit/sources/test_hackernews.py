from __future__ import annotations

from typing import Any

import pytest

from discovery.sources.hackernews import build_search_url


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
