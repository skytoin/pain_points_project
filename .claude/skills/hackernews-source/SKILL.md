# HackerNews Source Adapter — Operational Playbook

This file is the project's policy on HackerNews. Read it end-to-end
before writing or modifying `src/discovery/sources/hackernews.py`,
`src/discovery/orchestrator/hackernews.py`, or planning HN queries
from a `JobPlan`. The numbered items below are cross-referenced by
number in commits and reviews — don't renumber them.

The `source-adapter` skill is the umbrella contract (async, rate-
limited, retried, Pydantic-validated, idempotent, stored verbatim).
This file is the HN-specific layer on top — and where HN deliberately
diverges, it says so.

The companion design doc is
`docs/specs/2026-05-20-hackernews-source-design.md`.

---

## 1. Use the Algolia HN Search API. Don't scrape news.ycombinator.com.

Hacker News has an official search backend hosted by Algolia. It's
free, needs no API key, no auth, no User-Agent requirement, and is
generous on rate limits (~10,000 requests/hour — effectively
unlimited for a scanner).

Base URLs:
- `https://hn.algolia.com/api/v1/search` — relevance-ranked
- `https://hn.algolia.com/api/v1/search_by_date` — reverse-chronological

Never parse HTML from `news.ycombinator.com`. The Algolia API returns
clean JSON with `points`, `num_comments`, `author`, `created_at`,
`objectID`, and `_tags` already structured.

## 2. Two endpoints, two purposes — transport flag at plan time.

This is the single most important design decision in the adapter.

- `/search` ranks by relevance (points + text match + freshness). Use
  for broad topic/discussion queries (`intent=context`).
- `/search_by_date` is strict reverse-chronological. Use for fresh
  product launches that haven't accumulated points yet
  (`intent=launch`).

Project queries carry a transport flag `endpoint: "search" |
"search_by_date"`. `build_search_url` prepends the base URL at fetch
time.

## 3. Tags taxonomy + AND/OR semantics.

Algolia HN tags: `story`, `comment`, `ask_hn`, `show_hn`, `poll`,
`front_page`, `author_{username}`, `story_{id}`. Comma between tag
values = AND; parentheses = OR.

- Project uses `tags=show_hn` for launch queries.
- Project uses `tags=story` for context queries.

Empirically verified (2026-05-20) that Ask HN and Show HN posts
carry BOTH `story` AND their subtype tag in `_tags`, so `tags=story`
is a true superset that catches Ask HN's pain-shaped "how do you
handle X?" threads — the closest HN gets to Reddit-style problem
discussion. No need to OR `(story,ask_hn)`.

## 4. Strict token-AND on the `query` parameter — no OR operator.

This is the #1 cause of "why am I getting zero HN results." Unlike
Reddit, you cannot OR phrases together. Every content token in the
`query` parameter must co-occur in the matched story.

Consequence: a 4+ word keyword like "privacy preserving data
collection library" demands all five words appear in a short HN
title. Almost never happens. Long keywords starve the source.

The fix: decompose every keyword to its first ~2 content tokens
before querying. See item 5.

## 5. Decomposition policy.

Pure helper `discovery.sources.keyword_tokens.decompose_keyword`:

1. Whitespace-split the keyword.
2. Drop tokens whose lowercased form is in the small stopword set
   (`a, an, the, for, with, to, of, in, on, and, or`).
3. Keep the first 2 surviving tokens.
4. Preserve ORIGINAL casing — HN's vocabulary is acronym-heavy
   (MCP, CLI, RAG, LLM, WASM, ETL, CRDT, OSS) and lowercasing them
   loses signal.
5. Return `[]` if nothing survives (caller drops the query).

Stopword set is deliberately small per the guide ("a big list starts
eating real content"). This is a CODE change with tests if it ever
needs tuning, not a runtime knob.

Reusable later for GitHub code search / arXiv (all token-AND), but
kept HN-only here without pre-generalization.

## 6. Server-side `numericFilters` IS the quality floor.

Algolia supports server-side `numericFilters` (comma=AND between
clauses). The project uses:

- `created_at_i>{epoch}` — recency floor from `JobSpec.time_window`
  (`_time_window_epoch` in the orchestrator computes the floor at
  midnight UTC; `all` → omit the filter).
- For context queries: ALSO `points>5,num_comments>3` — server-side
  quality floor.
- For launch queries: RELAXED — no points/comments floor. Fresh
  Show HN launches legitimately sit at 0–3 points for hours, and
  the recency is the signal.

Client-side `keep_hit` is a near-noop (only drops hits missing
`objectID`, which Algolia never actually omits). All quality work
happens server-side.

## 7. Cap total queries per task at ~12.

Even though the rate limit is generous, more queries = more downstream
LLM cost + duplicate noise, with diminishing returns. `MAX_HN_QUERIES
= 12` (in `orchestrator/hackernews.py`). The Wave 0 LLM emits 15–20
candidates; Python decomposes, dedupes, and truncates to the first 12
in the LLM's emitted order (a ranking signal).

## 8. Set `hitsPerPage=30` explicitly. No pagination.

Algolia's default `hitsPerPage` is small (~20). Set it to 30 so each
request returns enough candidates without paging. The top 30 by
relevance or date is what matters for a scanner — don't build paging
you won't use.

## 9. Per-instance limiter, NOT a process-wide singleton.

Reddit uses `reddit_ratelimit.py`'s shared singleton because TWO
consumers (Wave 0 sub-search + Wave 1 content fetch) must share ONE
10-req/min budget. HN has exactly ONE consumer (Wave 1 fetch — there
is no HN subreddit-discovery analog) and Algolia's ceiling is
~10k/hr (effectively unlimited).

Each `HackerNewsSource` instance gets a fresh `AsyncLimiter(5, 1)`
(5 req/s polite, far under Algolia's actual ceiling).

**Documented divergence from `reddit-source` skill.** Don't "fix"
this by adding an `hn_ratelimit.py` singleton — there's nothing for
it to coordinate.

## 10. No retry, partial success across queries.

The HN guide is emphatic: HN does NOT need Reddit's retry dance. No
429/Retry-After machinery, no exponential backoff, no skill-item-4
analog. One GET per query; non-2xx or `httpx.HTTPError` records that
query's error and the loop continues to the next.

Project-locked partial-success contract still applies: if `fetch`
batches ~12 queries and some succeed, return what worked; only when
EVERY query fails does `fetch` raise (the first error) so the worker
marks the task failed.

**Documented divergence from `source-adapter` umbrella.** The umbrella
says "wrap the network call with @tenacity.retry — exponential
backoff, max 3 attempts." HN deliberately does not. This is the
single largest deliberate divergence; don't add retry by stealth.

## 11. Bronze stores raw — Wave 2 parses.

`hit_to_raw_record` sets `external_id = str(hit["objectID"])` and
`body = hit` verbatim. No snippet construction, no permalink fallback,
no body trimming. The HN guide discusses these as adapter-side
concerns; in THIS project they are explicitly **deferred to Wave 2**
because the locked Bronze contract is "store raw."

When Wave 2 lands and needs the permalink for an Ask/Show HN text
post whose `url` is null, it constructs
`https://news.ycombinator.com/item?id={objectID}` from the verbatim
body.

## 12. HN signals are CAPABILITY, not pain. (Downstream / Wave 2.)

Tag the signals downstream as "capability/launch" — the opposite of
Reddit's "pain/adoption". The Wave 0 prompt teaches the LLM the
framing for keyword generation (capability/launch phrasings, NOT
pain phrasings). The adapter itself just stores raw; tagging is a
Wave 2 concern.

## 13. Things that will tempt you and shouldn't.

- Don't try to OR keywords into one big query. The API doesn't
  support it. Run separate queries (Python compiles the candidate
  list into ≤12 separate queries).
- Don't send long multi-word phrases. The decomposition cap is 2
  content tokens; longer phrases lose their tail tokens silently.
  This is the #1 silent-failure mode.
- Don't write "Show HN" / "HN" / "Ask HN" inside a keyword. Those
  are tag filters, not content. Putting them in the query burns
  both content slots on tag-redundant words (next-most-common
  failure mode). The v7 prompt explicitly forbids this.
- Don't lowercase or stem tokens. Acronyms are high-signal on HN
  and casing/exact-match matter.
- Don't use only `/search`. You'll systematically miss fresh
  launches — exactly the signal you most want for idea generation.
  Always include `/search_by_date` for the launch queries.
- Don't paginate. Top 30 by relevance/date is plenty.
- Don't filter NSFW or do heavy body cleaning. HN doesn't need it.
- Don't add retry logic by stealth. The "no retry" decision is
  load-bearing (item 10 above).
- Don't mirror Reddit's process-wide singleton limiter (item 9).

## 14. The mental model.

Reddit = where people complain (pain signals). HN = where people ship
and discuss tech (capability signals). They're complementary halves
of the same research question. Mix the two as separate `_tags`
downstream; don't dump them into one bucket.

For HN, optimize for:

1. Catching fresh launches — `show_hn` + `/search_by_date` with
   relaxed quality filters is the killer combo.
2. Query construction discipline — short token-AND queries, always.
   This is where 90% of HN bugs live.
3. Server-side engagement filtering — `points + num_comments` for
   context queries; relaxed for launches.

The API will rarely fight you. Your own query strings will.

---

## Divergences from related skills (single point of truth)

The HN adapter deliberately diverges from `source-adapter` (umbrella)
and from `reddit-source` (sister adapter) on three points:

- **No retry / backoff** (item 10). `source-adapter` umbrella mandates
  `@tenacity.retry` with exponential backoff. HN deliberately does
  not; project-locked partial-success across queries is preserved.
- **Per-instance limiter** (item 9). `reddit-source` uses the
  `reddit_ratelimit.py` process-wide singleton because two Reddit
  consumers share a budget. HN has one consumer and a generous
  ceiling.
- **HN guide's adapter-side normalization** (snippet, permalink
  fallback, missing-`url` handling, capability tagging) is **deferred
  to Wave 2** in this project (item 11). The HN guide describes these
  as adapter-side because it was written for a different downstream;
  here Bronze stores raw and Wave 2 owns parsing.

All three divergences are user-approved, surfaced in the spec
(`docs/specs/2026-05-20-hackernews-source-design.md` §16), and
documented inline above so future sessions don't "fix" them.
