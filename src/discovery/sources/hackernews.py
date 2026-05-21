"""HackerNews source adapter via the Algolia HN Search API.

See `.claude/skills/source-adapter/SKILL.md` for the umbrella contract
and `docs/specs/2026-05-20-hackernews-source-design.md` for the HN-
specific design. Once the `hackernews-source` project skill lands in
Chunk 5, it becomes the operational reference for this file.

This module grows in three tasks (Chunk 2):

1. `build_search_url` -- pure URL builder for both Algolia endpoints.
2. `keep_hit`, `hit_to_raw_record` -- pure hit conversion helpers.
3. `HackerNewsSource(BaseSource)` -- the adapter class.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"


def build_search_url(query: dict[str, Any]) -> str:
    """Build an Algolia HN Search URL from a compiled query spec.

    Required keys in `query`:

    - `endpoint`        -- `"search"` or `"search_by_date"`
    - `query`           -- full-text search string (already
      decomposed to <=2 content tokens by the orchestrator)
    - `tags`            -- Algolia tag filter (`"story"` or `"show_hn"`)
    - `numeric_filters` -- comma-AND filter string (e.g.
      `"created_at_i>1715040000,points>5,num_comments>3"`)
    - `hits_per_page`   -- int; the orchestrator sets this to 30 (no-
      pagination policy, spec §11)

    The output URL ALWAYS pins `page=0` (Algolia's first page). The
    caller must not pass a `page` key -- the no-pagination policy is
    enforced here, not deferred to Algolia's default, per spec §11.
    """
    endpoint = query["endpoint"]
    if endpoint not in ("search", "search_by_date"):
        raise ValueError(f"unknown HN endpoint: {endpoint!r}")
    params = {
        "query": query["query"],
        "tags": query["tags"],
        "numericFilters": query["numeric_filters"],
        "hitsPerPage": str(query["hits_per_page"]),
        "page": "0",
    }
    return f"{_ALGOLIA_BASE}/{endpoint}?{urlencode(params)}"
