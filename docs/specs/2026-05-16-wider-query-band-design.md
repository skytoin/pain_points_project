# Wider Query Band + Industry-Specific Brainstorm — Design Spec

**Date:** 2026-05-16
**Status:** Approved design (brainstormed + user-approved 2026-05-16). Ready for
implementation planning (build via the `superpowers:writing-plans` skill).
**Branch when written:** `claude/jovial-poitras-fd083f`

> **For the build session:** this is the design — *what* and *why*, plus the
> concrete decided parameters. Turn it into a task-by-task plan with
> `superpowers:writing-plans`. Do not start coding from this document directly.

**Supersedes (deliberate, user-authorized):** this spec overrides three
previously locked-in decisions from `docs/specs/2026-05-15-subreddit-discovery-design.md`
and `docs/handoff.md` ("Decisions locked in"):

- `JobPlan.reddit_queries` `max_length=15` → now `max_length=30`.
- The query floor `min_length=10` → now `min_length=25`.
- "Do NOT relax the floor / thin tables still yield 10–15" (2026-05-15 §10/§13)
  → the band is now intentionally **25–30**.

These reversals are intentional and chosen by the user. A future session must
NOT "restore" the 10–15 band as a regression — this document is the newer
authority on the query band.

**Related skills (policy — read before building):**
- `.claude/skills/llm-station/SKILL.md` — the prompt is a station prompt; the
  `VERSION` bump + cache behaviour must follow the contract.
- `.claude/skills/reddit-source/SKILL.md` — item 9 (generality) is
  *deliberately and partially* deviated from here; items 3/5/7/23 (rate budget,
  ≤6 subs per site-wide query) are unchanged and still bind.

---

## 1. Problem & why

On a live run (`wedding photography`, US, default 1-month window) the pipeline
returned only 2 stored posts. Root causes were the short time window plus a
narrow plan: **10–15 queries built only from the generic 8-pain-category ×
subreddit grid**. The generic grid is deliberately industry-*agnostic* (skill
item 9) — good for generality, but it never asks about an industry's *specific*
tools, workflows, jargon, or recurring operational problems, which is where a
lot of real, findable pain lives.

The user wants two things:

1. **A much wider sweep every run** — 25–30 queries, not 10–15.
2. **Industry-aware queries** — in addition to the standard grid, the LLM should
   reason about the *specific* industry in the request and brainstorm queries
   that hunt that industry's particular pain points and problems by name.

The time-window lever (`--time-window`) already exists and is the other half of
the recall story; it is **out of scope here** (no code change — operators pass
`-t year`/`all`). This spec only widens and enriches the *query plan*.

---

## 2. Locked decisions (from the 2026-05-16 brainstorming dialogue)

1. **Query band = 25–30.** `JobPlan.reddit_queries`:
   `Field(min_length=25, max_length=30)` (was `min_length=10, max_length=15`).
   Every run is a wide sweep; the schema rejects a Call-#2 plan with fewer
   than 25 queries.
2. **Fallback floor stays 10, decoupled from the schema floor.**
   `MIN_VALID_QUERIES` in the station remains `10`. Rationale: the schema
   forces the LLM to *emit* ≥25, but the deterministic validator may prune a
   few; we want to run the ~20-something survivors, NOT collapse to the old
   ~4-query deterministic template. Template fallback triggers ONLY if fewer
   than 10 queries survive validation (a genuine failure), exactly as today.
3. **Composition = standard grid (kept) + industry-specific brainstorm
   (added).** The existing generic 8-pain-category × subreddit grid is
   retained verbatim (preserves the generality skill item 9 protects for that
   portion). The LLM *additionally* brainstorms industry-specific queries for
   the specific industry in the user message. The **mix/ratio is the LLM's
   discretion**, but it MUST include a substantial share of *both* kinds;
   neither kind alone satisfies the 25–30 band.
4. **Teaching method = instruction + one fenced worked illustration.** In
   `SYSTEM_PROMPT`: a clear explanation of the two-kinds requirement, PLUS a
   compact worked illustration for **one** example industry (wedding
   photography — roughly 6 example industry-specific query *ideas*), wrapped
   in explicit guard text stating these examples are that-industry-only,
   shown only to demonstrate the *kind* of domain reasoning, and MUST NOT be
   reused or lightly edited — for the actual industry in the user message the
   model must derive its own unique, high-quality industry-specific queries
   the same way.
5. **`FEW_SHOT_EXAMPLES` is NOT wired in.** It is currently never sent to the
   model (only `system` + `user` go to `call_openai`). Wiring it in remains a
   separate, pre-existing deferred follow-up (recorded in `docs/handoff.md`)
   and is explicitly OUT OF SCOPE here. All behaviour change is driven through
   `SYSTEM_PROMPT`, the only effective lever. `FEW_SHOT_EXAMPLES` is left
   untouched.
6. **Runtime cost accepted.** 25–30 content queries run through the shared
   process-wide 10-req/min Reddit limiter (~6.1 s apart) → Wave-1 fetch ≈ ~3
   min, full cold run ≈ ~3–4 min (vs ~2 today). No Wave-1 fetch cap is added
   (the user explicitly declined that option). Cached re-runs of the same
   spec stay instant.
7. **Scoped deviation from reddit-source skill item 9.** Item 9 says keep
   phrases industry-agnostic for generality. The *standard grid half* still
   honours this. The *added industry-specific half* deliberately does not —
   this is a conscious, user-chosen recall trade-off, documented here and to
   be repeated in the prompt's own comments.

---

## 3. Exact changes (file-by-file)

### `src/discovery/llm/schemas.py`
- `JobPlan.reddit_queries: list[RedditQuerySpec] = Field(min_length=25, max_length=30)`
  (was `min_length=10, max_length=15`). One-line change. This is the locked
  decision in §2.1 and the supersession noted in the header.
- The `JobPlan` shape and `RedditQuerySpec` are otherwise unchanged.

### `src/discovery/llm/prompts/query_expansion.py`
- `VERSION` `"v4"` → `"v5"`. The combined Wave-0 cache key
  `f"{subreddit_phrases.VERSION}+{query_expansion.VERSION}"` becomes `"v1+v5"`;
  every cached Wave-0 plan misses once and re-runs (accepted one-time cost).
- Docstring `Versioning:` block: add a `v5` entry summarising: 25–30 band;
  standard grid retained + industry-specific brainstorm added; fenced
  one-industry illustration with a "do not reuse — re-derive" guard.
- `SYSTEM_PROMPT` edits:
  - Every count phrase updated: all "10–15" → "25–30"; "Do NOT emit fewer
    than 10" → "fewer than 25"; the grounding section's "STILL produce 10–15
    content queries" → "STILL produce 25–30"; the "even 3 subs comfortably
    yield 10–15" line → "…25–30". No other grounding/Reddit-syntax rule text
    changes.
  - Add a new section (e.g. `# Two kinds of queries — produce BOTH (25–30
    total)`):
    - **(a) Standard pain-grid** — the existing 8 pain categories × subreddit
      scoping, rules kept verbatim. Generality preserved here.
    - **(b) Industry-specific** — reason about the SPECIFIC industry in the
      user message: its tools, software, workflows, roles, jargon, and
      recurring operational problems; build queries that hunt those exact
      pain points/problems by name.
    - State the mix is the model's discretion but BOTH kinds must be
      well-represented; neither alone fills 25–30.
  - Add a clearly-fenced **illustration for one industry** (wedding
    photography), ~6 example industry-specific query ideas, immediately
    followed by guard text: these are wedding-photography-only, shown ONLY to
    illustrate the kind of domain reasoning, MUST NOT be reused or lightly
    edited; for the actual industry in the user message derive your own
    unique, high-quality industry-specific queries the same way.
  - All v3/v4 Reddit-syntax rules (uppercase OR/AND, quoted phrases, ≤6
    subreddits per site-wide query, per_sub vs site_wide, mandatory
    per-query rationale, honour the time window) and the v4 GROUNDING
    section (select ONLY from the supplied real-subreddit table, never
    invent/memory, ceiling 30 on subreddits, column/trap guide) are retained
    unchanged.
- **`build_user_message` user-turn string — SECOND count lever, MUST also
  change.** `build_user_message(spec, table)` ends with a hard-coded
  instruction sent in Call #2's *user* message: `"Produce a JobPlan with
  10-15 reddit_queries using ONLY the subreddits above…"`. Change `10-15`
  → `25-30`. This is distinct from `SYSTEM_PROMPT`; if only the system
  prompt is updated the model receives contradictory counts (system: 25–30,
  user: 10–15) in the same request. The `build_user_message(spec, table)`
  signature and the rest of its rendering are unchanged. (The prompt
  shape-test `test_prompts_query_expansion.py` must therefore also assert
  the rendered user message contains the new count, not the old.)
- `FEW_SHOT_EXAMPLES`: untouched (§2.5). Note: each example still lists ~10
  queries and will look inconsistent with the 25–30 band, but the examples
  are plain dicts never sent to the model, so this is cosmetic only; record
  it in the handoff alongside the existing "few-shot not wired in" follow-up.

### `src/discovery/llm/stations/query_expansion.py`
- **No change.** `MIN_VALID_QUERIES = 10` stays (§2.2). The flow
  (Call #1 → search → deterministic middle → Call #2 → off-table reject +
  ≤30-subreddit trim → `_drop_invalid_queries` → `MIN_VALID_QUERIES` check →
  `_force_time_window` → `_merge_baseline_subreddits` → combined-key cache)
  is unchanged. The tail builds via `JobPlan.model_construct`, so a pruned
  survivor set (e.g. 22) is NOT re-validated against `min_length=25` — this
  is already correct and is why the station needs no edit.

### Tests (mechanical fan-out — the schema floor breaks every <25-query fixture)

**General rule:** any test that materialises a `JobPlan` *through validation*
— a `JobPlan(...)` constructor call OR `JobPlan.model_validate(...)` — with
fewer than 25 `reddit_queries` raises `ValidationError` at materialisation
once `min_length=25`. Only the station tail's `JobPlan.model_construct(...)`
is exempt (it bypasses validation by design — do NOT change those). The plan
MUST `grep -rn "JobPlan(" tests/` AND `grep -rn "JobPlan.model_validate" tests/`
to re-confirm the full set before finishing, so no site is missed.

**Complete grep-verified site list (2026-05-16 — 5 files, 11 sites):**

- `tests/unit/llm/test_schemas.py`: update the `TestJobPlan` count
  tests/helpers — reject <25, accept exactly 25, accept exactly 30, reject
  >30 (currently asserts around 9/10/16 at lines ~58/61/66/82) **and**
  `test_extra_fields_round_trip` at line ~71 which uses
  `JobPlan.model_validate({... range(10) ...})` (also re-validates → bump to
  ≥25). Also rename the now-misleading `test_rejects_more_than_15_queries`
  method → `test_rejects_more_than_30_queries` and change its `range(16)` →
  `range(31)`; similarly the `test_requires_min_10_queries` /
  `test_accepts_10_queries` names should reflect the new 25 floor.
- `tests/unit/llm/test_prompts_query_expansion.py`: this is a REWRITE of the
  affected assertions, not additive. Specifically: the existing
  `assert "10" in sp` and `assert "15" in sp` (lines ~40–41) MUST be removed/
  replaced (the v5 prompt no longer contains the "10"/"15" count language, so
  leaving them produces a false failure); the version assertion
  (`test_version_is_v4`-style) MUST become `VERSION == "v5"`. Then add shape
  assertions that "25"/"30" are present, plus the two-kinds instruction, the
  industry-specific brainstorm requirement, and the "do not reuse the
  illustration / re-derive for the real industry" guard, AND that the
  rendered `build_user_message(...)` user message contains the new count
  (the second lever). Shape only — do NOT pin exact wording.
- `tests/unit/llm/stations/test_query_expansion.py`: **every direct
  `JobPlan(reddit_queries=[...])` constructor call in this file (NOT
  `model_construct`) must build ≥25 queries** or it raises `ValidationError`
  at test-setup time once the floor is 25. This is exhaustive — the plan
  must cover all of these, not just the helpers:
  - the `_plan` / `_query` helpers (default `range(10)` → ≥25);
  - `TestValidationDropsInvalidQueries.test_drops_lowercase_or_query_via_existing_tail`
    — builds `JobPlan([*good, bad])` inline with 11 queries; rework to ≥25
    emitted (e.g. 25 good + 1 bad) and assert the bad one is dropped by the
    tail, survivors ≥ `MIN_VALID_QUERIES`;
  - all `TestFallbackTable` cases, esp. "too few valid after tail": rework so
    ≥25 are emitted but survivors fall below `MIN_VALID_QUERIES=10` (e.g. 9
    valid + ≥16 invalid) to still exercise template fallback;
  - `TestCacheHit` / `TestCacheMiss`: these `put_cached(...)` a `_plan()` and
    `get_cached` re-validates via `JobPlan.model_validate_json`, so the
    stored plan must ALSO be ≥25 (covered by bumping `_plan`, but call this
    dependency out so it isn't missed);
  - `TestOffTableRejection`, `TestTimeWindowOverride`,
    `TestBaselineSubredditMerge` — all build `_plan(...)`/`JobPlan(...)`;
    bump accordingly.
  The combined-cache-key helper derives the key from `qe.VERSION`, so it
  auto-tracks v5 with no literal edit.
- `tests/unit/test_orchestrator_jobs.py`: bump the `_valid_plan()` helper
  (line ~42) from 10 → ≥25 queries.
- `tests/unit/test_orchestrator_reddit.py`: line ~134 builds
  `JobPlan(reddit_queries=llm_queries).model_dump()` where `llm_queries` is
  `range(10)`; bump that list to ≥25.
- `tests/unit/test_view.py`: (a) the `_plan(n: int = 10) -> JobPlan` helper
  (line ~33) — change the default to `n=25`; (b) the explicit caller
  `_plan(n=11)` at line ~113 — change to `n=25`; (c) the matching
  `assert len(detail.plan.reddit_queries) == 11` at line ~125 — change to
  `== 25`. (Naming the explicit override + its assertion, not just "any
  caller", so the builder can't miss it.)

(`tests/unit/test_orchestrator_jobs.py`, `test_orchestrator_reddit.py`, and
`test_view.py` were all untouched through the prior feature; the schema-floor
change forces these purely-mechanical helper bumps. All five files above must
land in the SAME commit as the `schemas.py`/prompt change — see §8.)

---

## 4. Flow, caching, fallback (mostly unchanged)

- **Flow:** identical to the shipped grounded Wave-0 flow except the query
  count and the added industry-specific instruction. Two LLM calls,
  deterministic middle, grounded selection (LLM still picks subreddits ONLY
  from the supplied real-subreddit table — unchanged), `_force_time_window`
  and `_merge_baseline_subreddits` unchanged.
- **Caching:** one combined Wave-0 entry via the EXISTING
  `discovery.llm.cache.cache_key`; only the `prompt_version` string changes
  (because `query_expansion.VERSION` is now `v5`). The cache module is NOT
  modified. `SubredditSearchPhrases` (Call #1) is still not separately cached.
- **Fallback:** unchanged path. If gpt-5.4 returns fewer than 25 queries,
  `instructor`/Pydantic rejects the Call-#2 `JobPlan` → the station wraps it
  in `QueryExpansionError` → `plan_job` (unchanged) leaves `job_plan` null →
  the Reddit orchestrator uses the deterministic template (exactly as today).
  After the deterministic validator prunes invalid queries, fewer than
  `MIN_VALID_QUERIES=10` survivors → same `QueryExpansionError` → template.
  No new fallback branches.

---

## 5. Risks & trade-offs (accepted)

- **Higher schema floor (25) raises template-fallback risk on very thin
  niches** if gpt-5.4 under-produces. Mitigation: the generic 8-pain-category
  grid across the available subreddits already comfortably exceeds 25
  combinations, and the industry-specific block adds more; the prompt states
  the model can always reach 25–30 by combining both kinds. Residual risk
  accepted (the user wants the wide sweep).
- **Industry-specific queries are tighter/jargon-anchored** → each may match
  fewer posts individually. Net effect should still be more total signal
  (many more queries + domain-targeted), but volume also depends heavily on
  `--time-window`; operators should run niche topics with `-t year`/`all`.
  This remains a Bronze-layer collection stage; relevance filtering is a
  later wave.
- **Deliberate skill item-9 deviation** (generality) — scoped to the added
  half only; documented in §2.7 and to be repeated in prompt comments so
  reviewers/future sessions don't flag it as an error.
- **Cache invalidation (v5):** first run per spec pays the full cold cost
  again. One-time per spec; expected.
- **Runtime ≈ 3–4 min cold** — accepted (§2.6); no Wave-1 cap added.

---

## 6. Testing strategy

All unit tests stay fully offline (no real network, no real LLM), mirroring
existing patterns:

- **Schema bounds:** reject <25, accept 25, accept 30, reject >30.
- **Prompt shape:** `VERSION == "v5"`; "25"/"30" present and "10"/"15" count
  language gone; the two-kinds section, the industry-specific brainstorm
  instruction, and the "illustration is one-industry-only — re-derive, don't
  copy" guard are all present as substrings. Do not pin prose.
- **Station:** all JobPlan fixtures bumped to ≥25 valid queries; cache-hit
  still skips both LLM calls + the sub-search client; the §10 fallback table
  still each raises `QueryExpansionError` (the "too few valid" case reworked
  so survivors < 10); off-table rejection, time-window override, and baseline
  merge behaviour unchanged.
- **Orchestrator-jobs:** `_valid_plan()` helper bumped; the three plan_job
  tests stay green (signature unchanged).
- **Manual smoke (post-build):** re-run `wedding photography` (with
  `-t year`), inspect the saved plan via the DB-read one-liner, and confirm
  the 25–30 plan visibly contains BOTH standard pain-grid queries AND
  distinct industry-specific (wedding-photography-derived, NOT the prompt's
  illustration verbatim) queries. Read the Item-21 logs to confirm
  `wave 0: planned` (not fallback).

---

## 7. Open / deferred (not part of this spec)

- **Wiring `FEW_SHOT_EXAMPLES` into the LLM call** — pre-existing follow-up,
  still deferred (§2.5). This spec does not depend on it.
- **Runtime mitigation (Wave-1 fetch cap)** — explicitly declined by the user;
  not added.
- **Industry-specific recall quality tuning** — to be observed on real runs;
  tunable purely via the prompt (VERSION bump) with no code change, like the
  existing prompt-tunable knobs.
- **`--time-window` UX** — unchanged; out of scope.

---

## 8. Suggested build sequence (high level — detailed plan via writing-plans)

The schema floor change breaks every existing 10-query test fixture, so the
schema change, the prompt v5 change, and all dependent test-fixture bumps must
land together as one coherent green commit (same atomicity reasoning used for
the prior slice's prompt+station change). Rough order for the plan:

1. `schemas.py` band → 25/30; `query_expansion.py` prompt → v5 (SYSTEM_PROMPT
   count rewrite + two-kinds section + fenced one-industry illustration +
   re-derive guard + docstring v5 entry) **and** the `build_user_message`
   user-turn "10-15" → "25-30" string (the second count lever); plus the
   full test fan-out across ALL FIVE files — `test_schemas.py` (bounds +
   the `model_validate` round-trip), `test_prompts_query_expansion.py`
   (shape: v5, counts, two-kinds/guard substrings, rendered user-message
   count), `tests/unit/llm/stations/test_query_expansion.py` (every
   `JobPlan(...)` site incl. `TestValidationDropsInvalidQueries`,
   `TestFallbackTable`, `TestTimeWindowOverride`, `_plan`, cache tests),
   `test_orchestrator_jobs.py` (`_valid_plan`), `test_orchestrator_reddit.py`
   (line ~134), `test_view.py` (`_plan` default). TDD, ONE coherent commit
   (the schema floor breaks every <25 fixture, so all of the above must land
   together to stay green), full `/run-checks` green.
2. `/run-checks` + manual smoke (`wedding photography`, `-t year`), inspect
   the plan via the DB-read one-liner, confirm `wave 0: planned` and a
   visible standard + industry-specific mix; update `docs/handoff.md`
   (record the band supersession + the v5 prompt + the scoped item-9
   deviation + this spec as the new authority on the query band).

Each step ends green (tests + ruff + mypy) per the project's existing TDD
cadence.
