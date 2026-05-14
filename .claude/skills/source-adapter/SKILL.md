---
name: source-adapter
description: Contract for adding a new external data source — REST/GraphQL/scrape. Read this before writing any file under src/discovery/sources/.
---

# Source Adapter Pattern

Sources are the bottom of the pipeline. They hit external APIs (or scrape
when there's no API) and write raw responses to the Bronze layer
(`raw_records` table). No normalization, no parsing beyond what's needed
to hand the response off.

## The contract

Each source is one file under `src/discovery/sources/<name>.py` and
implements `BaseSource`:

```python
from discovery.sources.base import BaseSource, RawRecord

class RedditSource(BaseSource):
    name = "reddit"
    rate_limit = (60, 60)   # 60 requests / 60 seconds

    async def fetch(self, params: dict) -> list[RawRecord]:
        ...
```

## Required behaviors

- **Async only.** Use `httpx.AsyncClient` or the source's async SDK.
  Never block the event loop.
- **Rate-limited.** Wrap the call with `aiolimiter.AsyncLimiter` matching
  the API's documented quota. If unknown, start conservative (1 req/sec).
- **Retried.** Wrap the network call with `@tenacity.retry` —
  exponential backoff, max 3 attempts, retry on `httpx.HTTPError` and
  `httpx.TimeoutException`. Do NOT retry on 4xx (except 429).
- **Validated.** Wrap the JSON response in a Pydantic model before
  returning. Even if you don't normalize, validate the shape you got.
- **Stored verbatim.** The raw `bytes` of the response go into
  `raw_records.body`. Don't pre-parse.
- **Idempotent.** Running the same `fetch` twice should not write
  duplicate rows. Use the response's natural key in the upsert.

## Auth

- API keys come from `discovery.config.settings` (pydantic-settings),
  never hardcoded.
- If auth is OAuth, the token-refresh logic lives in the adapter file,
  not scattered.
- If the API has no auth, document that in the file header.

## Tests

Every source needs at least one test:

```python
# tests/unit/sources/test_reddit.py
import pytest

@pytest.mark.vcr           # records the response once into a cassette
async def test_reddit_fetch_returns_records():
    source = RedditSource()
    records = await source.fetch({"subreddit": "Commercialcleaning"})
    assert len(records) > 0
    assert all(r.source == "reddit" for r in records)
```

- Cassettes go under `tests/fixtures/_recorded/`.
- Regenerate them by deleting the cassette file and re-running. Don't
  edit them by hand.

## Common gotchas

- **`requests` vs `httpx`.** This project uses `httpx`. Don't `uv add
  requests` — it'll work but breaks the async story and adds a dep we
  don't need.
- **SDK clients that are sync-only.** Wrap them in `asyncio.to_thread()`
  or find the async variant. Don't block the loop.
- **Pagination.** Most APIs paginate. The adapter is responsible for
  walking pages until exhausted or until a `max_results` is hit.
- **Date parsing.** APIs return dates in 20 different formats. Use
  `python-dateutil` or `datetime.fromisoformat()`; never roll your own
  parser.
- **Empty results.** Return `[]`, never `None`. Workers expect a list.
