---
name: reddit-source
description: Operational playbook for the Reddit source adapter — no-OAuth .json endpoint, User-Agent rules, 10 req/min pacing, OR-compressed query budgeting, search syntax, pain-phrase variants, junk filtering, retry policy. Read before writing src/discovery/sources/reddit.py or planning Reddit queries from a JobPlan.
---

# Reddit Source Adapter — Operational Playbook

This file is the project's policy on Reddit. Read it end-to-end before
writing or modifying `src/discovery/sources/reddit.py`, and before planning
Reddit-bound query budgets inside Wave 0's `JobPlan`. The numbered items
below are cross-referenced by number in commits and reviews — don't
renumber them.

The `source-adapter` skill is the umbrella contract (async, rate-limited,
retried, Pydantic-validated, idempotent, stored verbatim). This file is
the Reddit-specific layer on top.

---

## 1. Skip OAuth. Use the .json trick.

You do not need a Reddit API key, app registration, or OAuth flow.

Append `.json` to almost any Reddit URL and you get back the same data as the API. Example:

- `reddit.com/r/startups/search.json?q=...`
- `reddit.com/search.json?q=...` (site-wide)

This cuts ~40% of your code: no token cache, no refresh logic, no 401-retry, no expiring credentials.

Trade-off: you're limited to ~10 requests per minute unauthenticated. Plan your query budget around that number, not around what feels natural.

---

## 2. The User-Agent header is not optional.

Reddit silently throttles or blocks requests that use generic User-Agent strings (anything that looks like `curl`, `python-requests`, `axios`, `Go-http-client`, etc.).

Send a descriptive User-Agent like: `your-project-name/1.0 (purpose; contact-url-or-username)`.

Make it configurable via env var so operators can put their own Reddit username in it later (Reddit's docs request the `<platform>:<app>:<version> (by /u/<user>)` shape).

If your adapter mysteriously gets 0 results in production but works locally, always check User-Agent first.

---

## 3. Pace requests at ~6.1 seconds apart.

10 requests/minute = 6.0 seconds between requests at the limit. Use **6.1 seconds** so clock skew doesn't accidentally bunch two requests into the same second.

Don't try to be clever with bursting. Reddit's rate limit is a rolling window — a burst of 5 followed by a long pause will still trigger a 429.

---

## 4. Always handle 429 properly, but only retry 429.

Three failure modes, three different responses:

- **401 / 403** → auth/IP problem. Do not retry. Mark the source as denied and move on.
- **429** → rate-limited. Retry with backoff. Reddit usually returns a `Retry-After` header (integer seconds). When present, honor it exactly. When absent, fall back to exponential backoff (5s, 10s, 20s).
- **5xx / network errors** → treat as transient. One retry, maybe two.

Cap retries at 3 attempts. Clamp any `Retry-After` value to a sane range (1s to 5min) so a misbehaving response can't park your scanner forever.

---

## 5. Budget your queries around the rate limit, not your wishlist.

This was the biggest design shift. Old plan: 6 small queries (one per sub × one per phrase). New plan: **1 fat query + 3 thin queries = 4 total.**

The fat one searches multiple subreddits for multiple pain phrases in a single request, using Reddit's OR syntax. Same recall, ⅓ the rate-limit budget.

The lesson: Reddit's search syntax is more powerful than people realize. Use it to compress N queries into 1.

---

## 6. Reddit search syntax — the actually-useful operators.

These all work in the `q=` parameter on the site-wide `/search.json` endpoint:

- `subreddit:startups` — scope to one sub
- `subreddit:a OR subreddit:b OR subreddit:c` — scope to many
- `"exact phrase here"` — quoted = literal phrase match (mandatory for multi-word!)
- `phraseA OR phraseB OR phraseC` — match any
- `clauseA AND clauseB` — both must match
- `self:yes` — only text posts (self-posts), no link posts
- `nsfw:no` — drop NSFW results
- `flair:"Help"` — filter by flair (less reliable, varies per sub)

Operators must be uppercase (`OR`, `AND`). Parenthesize aggressively to make precedence explicit. Multi-word phrases without quotes will be treated as separate word-search terms — almost never what you want.

---

## 7. Watch the URL length.

A query like `(subreddit:a OR subreddit:b ...) AND (phrase1 OR phrase2 ...)` blows up fast. Reddit's practical limit is around 4 KB. After that you get 414 errors or silent truncation.

Cap each OR list:

- Max ~6 subreddits in the subreddit clause
- Max ~12 phrases in the phrase clause (3 base phrases × 4 variants each is a good rule of thumb)

If you have more subs than the cap, prioritize the LLM-picked ones first, baseline subs last.

---

## 8. Pain phrases need variants, not just the base phrase.

`"I would pay"` (literal match) misses every user who wrote `"I'd pay"`, `"would pay for"`, or `"would gladly pay"`. You'll lose ~70% of real signals.

Build a small lookup: each base phrase → 3-4 close paraphrases. OR them all together in the query.

Guidelines:

- Keep variant lists short (≤4). Longer lists bloat URL and dilute precision.
- Variants are **paraphrases**, not synonyms. `"I would buy"` is NOT a variant of `"I would pay"` — different semantics (one-time purchase vs. recurring willingness).
- Curate by signal type. Group around the meaning (willingness-to-pay, frustration, switching), not around the keyword.

Good base-phrase categories to start from, ranked by signal strength:

1. **Willingness to pay** (`"I would pay"`, `"I'd pay"`, `"would pay for"`)
2. **Unmet need** (`"wish there was"`, `"wish someone would"`)
3. **Frustration** (`"frustrated with"`, `"fed up with"`, `"tired of"`)
4. **Looking for alternatives** (`"alternative to"`, `"replacement for"`)
5. **Market-gap questions** (`"why is there no"`, `"why does no one"`)
6. **Builder signals** (`"built a tool"`, `"made a tool"`)
7. **Switching** (`"switched from"`, `"moving away from"`)
8. **Dead competitor signals** (`"shut down"`, `"killed off"`)

---

## 9. Subreddit + pain phrase is a 2D grid. Mix them deliberately.

Split your concerns:

- The **subreddit** gives you the DOMAIN. (`r/nursing`, `r/devops`, `r/teachers`)
- The **phrase** gives you the PAIN SIGNAL. (`"I would pay"`, `"wish there was"`)

A nurse looking for ideas should search the same phrases a DevOps founder uses — just in different subs. Don't try to make domain-specific phrase lists; you'll lose generality.

Always merge LLM-picked subs with a **profile-agnostic baseline** (`r/startups`, `r/microsaas`, `r/smallbusiness`). Why: if the LLM gives you garbage subreddits, the baseline keeps the adapter useful. Defense in depth.

---

## 10. Validate subreddit names CLIENT-SIDE before sending requests.

Reddit's rule: 3 to 21 characters, ASCII letters/digits/underscore only. Strip leading `r/` or `/r/` first.

LLMs will output names like `"r/Small Business"`, `"subreddit-name"`, or `"AI/ML"`. If you send those to Reddit you get 404s, which pollute your error stats and make the source look broken when it's actually fine.

Filter invalid names **silently at planning time**, never at fetch time.

---

## 11. The `t` (time) parameter is coarse. Use it anyway.

Reddit only accepts: `hour`, `day`, `week`, `month`, `year`, `all`.

When your timeframe is something like "last 17 days", pick the narrowest bucket that still covers it (`month` in this case). Then re-filter precisely on the client side after the data comes back. The `t` param just controls how much Reddit returns before you trim.

---

## 12. Other params worth setting on every request.

- `sort=top` — gets the highest-quality posts first
- `limit=100` — maximum allowed; smaller values waste rate budget
- `restrict_sr=true` — required when hitting `/r/{sub}/search.json` (per-sub), otherwise Reddit silently searches sitewide
- `include_over_18=false` — drops NSFW at the API level
- `raw_json=1` — Reddit otherwise HTML-escapes ampersands, quotes, etc. in text fields. You'll be tracking down `&amp;` bugs forever without this.

For NSFW filtering, use **both** `include_over_18=false` (URL param) AND `nsfw:no` (in the search query). Defense in depth — neither alone is fully reliable.

---

## 13. Filter junk posts BEFORE sending them to your LLM.

Every post you keep costs LLM tokens downstream. Apply a cheap quality floor in the adapter:

- Drop posts with fewer than ~5 upvotes
- Drop posts with fewer than ~2 comments
- Drop NSFW (belt-and-suspenders, even if you filtered server-side)
- Drop deleted/removed posts (check `removed_by_category`, `author == '[deleted]'`)

Tune those numbers to the niche. 5 upvotes on `r/microsaas` is meaningful; 5 upvotes on `r/programming` is nothing. Don't set one global threshold — set one appropriate for the smallest subreddit you'll query, then accept that bigger subs will pass more posts (that's fine).

---

## 14. Use the permalink, never the external URL.

Reddit posts have two URLs: the post body's `url` field (the linked article) and the `permalink` (the Reddit thread itself).

Always use the **permalink**. Reasons:

- Permalinks dedupe reliably (one per thread)
- They preserve the "this is Reddit discussion" semantic
- External URLs collapse cross-sub discussions of the same article into one signal — you lose the discussion context, which is the whole point of scraping Reddit

---

## 15. Trim post bodies to ~200 characters.

Reddit `selftext` can be enormous. Strip whitespace runs (collapse `\s+` to single spaces), trim, and cap at ~200 chars with an ellipsis.

Long bodies cost tokens downstream without proportional information gain — the first 200 chars almost always contain the gist.

---

## 16. Two endpoints, two URL patterns.

- **Per-sub search:** `reddit.com/r/{sub}/search.json` — requires `restrict_sr=true`
- **Site-wide search:** `reddit.com/search.json` — used for cross-sub OR queries; do NOT set `restrict_sr` here

In your code, plan a query with a "transport flag" that says which endpoint it should hit. Don't hard-code the URL at plan time; build it at fetch time from the flag. This makes testing easier and keeps planning logic clean.

---

## 17. Partial success > all-or-nothing.

If your adapter sends 4 queries and #2 times out on a 429, do not throw away the results from #1, #3, and #4.

Pattern: collect results AND errors in a loop. At the end:

- If you have at least one successful response → return what you have, log the errors.
- If every query failed → throw the first error so the orchestrator can mark the source as failed.

A mid-loop rate-limit shouldn't poison earlier successful pulls.

---

## 18. Make sleeps cancelable.

Your top-level scanner probably has a per-source timeout (say 60 seconds). If a 30-second backoff sleep ignores that timeout signal, you'll orphan the adapter past its deadline.

Whatever your language's equivalent of an abort signal / cancellation token is — propagate it into sleep functions. When the signal fires, the sleep should reject/return immediately, not finish its full duration.

---

## 19. Make sleep injectable for tests.

Unit tests should not actually wait 6 seconds between fake requests. Your `fetch` function should accept an optional `sleep` function parameter (defaulting to a real sleep). Tests inject a no-op.

This is a small architectural decision that pays for itself the first time you write a 429-retry test.

---

## 20. Empty results are not failures.

A query that returns zero posts is a valid outcome, not an error. Reddit returns a normal 200 response with an empty `children` array.

Status buckets you'll want:

- `ok` — got at least one signal
- `ok_empty` — query ran fine, nothing matched
- `denied` — 401/403/429-after-retries
- `failed` — 5xx, network errors, JSON parse failures
- `timeout` — exceeded per-source budget

Treating `ok_empty` as `failed` will trash your scanner's quality metrics for no reason.

---

## 21. Logging that will save you hours later.

For each query, log:

- The exact URL hit (after URL-encoding) — sometimes the bug is in your URL builder
- HTTP status code
- Response time
- Number of posts returned (before AND after your engagement floor)
- Number of posts dropped and why (NSFW / score / comments)

When something looks wrong in production, you want to know immediately whether Reddit returned zero, your filter ate everything, or your dedupe killed it later.

---

## 22. Categorize Reddit signals as "adoption" or "pain", not as "new tech".

In your pipeline, Reddit signals represent what real people are saying — community engagement, frustration, willingness to pay. They are NOT new-capability announcements (that's Hacker News / arXiv territory).

Tag them accordingly. Mixing categories confuses downstream scoring.

---

## 23. Things that will tempt you and shouldn't.

- Don't add Reddit OAuth "for higher rate limits." The bump (60/min vs 10/min) isn't worth the auth complexity for a scanner. Pace correctly instead.
- Don't search every sub the LLM suggests. Cap at ~6 in the cross-sub query. More subs = longer URL, more noise, diminishing returns.
- Don't combine pain query and topic query into one mega-query. They want different filters (`self:yes` helps pain queries, hurts topic queries). Keep them separate.
- Don't try to scrape `/comments/` endpoints to get full threads. That doubles your request count and 90% of the signal is in the post title + first 200 chars of selftext.
- Don't dedupe on title alone. Two different threads can have nearly identical titles (`"I built a tool to track habits"`). Dedupe on permalink.

---

## 24. The mental model.

Reddit, at its best, is a corpus of real humans articulating real pain in their own words. Your adapter exists to extract those moments cheaply and reliably.
