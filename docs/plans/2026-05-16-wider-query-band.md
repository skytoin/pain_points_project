# Wider Query Band + Industry-Specific Brainstorm — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every Wave-0 run produces 25–30 Reddit queries (was 10–15), composed of the unchanged generic pain-grid PLUS LLM-brainstormed industry-specific queries, with a fenced one-industry illustration the model must re-derive (not copy) for the actual industry.

**Architecture:** A pure schema-bound + prompt change. `JobPlan.reddit_queries` floor/ceiling becomes 25/30. The `query_expansion` prompt (v4→v5) rewrites all count language to 25–30, rescopes the "stay generic" rule to the standard grid only, adds a "two kinds of queries" section + a fenced wedding-photography illustration with an explicit re-derive guard, and `build_user_message`'s user-turn count string changes too (second lever). No station code changes (`MIN_VALID_QUERIES=10` stays, decoupled — pruning never collapses to template unless <10 survive; the tail uses `model_construct` so a pruned set isn't re-validated). Because the schema floor breaks every <25-query test fixture, the schema + prompt + the full grep-verified 5-file test fan-out land in **one atomic green commit** (spec §8).

**Tech Stack:** Python 3.12 (uv), Pydantic v2, OpenAI gpt-5.4 via instructor, pytest, ruff (strict select — watch `PLC0415` no in-function imports, `PT018` no compound asserts, `RUF001` no ambiguous unicode like `×`), mypy strict on `src/`.

**Spec (source of truth):** `docs/specs/2026-05-16-wider-query-band-design.md` (approved, reviewer-verified 4 passes). This plan transcribes it; if any conflict, the spec wins.

---

## Settled decisions carried from the spec (do not re-litigate)

- Band = `min_length=25, max_length=30`. This **consciously supersedes** the prior locked 10–15 decision (recorded in the spec header + to be recorded in handoff). A future session must NOT "restore" 10–15.
- `MIN_VALID_QUERIES` stays **10**, decoupled. Station code unchanged.
- Keep the generic pain-grid; ADD industry-specific brainstorm; LLM's discretion on the mix; both kinds required.
- One fenced illustration for ONE industry (wedding photography) with an explicit "these are illustration-only — re-derive your own, never copy" guard.
- `FEW_SHOT_EXAMPLES` untouched (never sent to the model; the separate deferred follow-up). Each still has ~10 queries — cosmetic only.
- Runtime ~3–4 min cold accepted; no Wave-1 cap.
- Deliberate, scoped deviation from reddit-source skill item 9 (generality): only the industry-specific half; the prompt's own "stay generic" line is rescoped to the standard grid so the prompt does not self-contradict.

## File structure (created / modified)

| File | Change |
|---|---|
| `src/discovery/llm/schemas.py` | `JobPlan.reddit_queries` Field `min_length=10,max_length=15` → `25,30` (line 92). |
| `src/discovery/llm/prompts/query_expansion.py` | `VERSION` v4→v5; docstring +v5 entry; SYSTEM_PROMPT all "10/15" count language → "25/30"; rescope the "don't make domain-specific phrase lists" line to the standard grid; add the "Two kinds" section + fenced wedding-photography illustration + re-derive guard; `build_user_message` user-turn "10-15"→"25-30". `FEW_SHOT_EXAMPLES` untouched. |
| `src/discovery/llm/stations/query_expansion.py` | **No change** (verify only). |
| `tests/unit/llm/test_schemas.py` | Rework `TestJobPlan` to the 25–30 band (incl. the `model_validate` round-trip + method renames). |
| `tests/unit/llm/test_prompts_query_expansion.py` | Rewrite affected assertions: v5, drop "10"/"15" presence asserts, add 25/30 + two-kinds/guard/illustration + rendered-user-message-count asserts. |
| `tests/unit/llm/stations/test_query_expansion.py` | Bump `_plan` default → 25; fix `TestCacheHit` len assertion; rework `TestValidationDropsInvalidQueries` + `TestFallbackTable.test_too_few_valid_queries_after_tail` + `TestTimeWindowOverride`. |
| `tests/unit/test_orchestrator_jobs.py` | `_valid_plan` `range(10)`→`range(25)`. |
| `tests/unit/test_orchestrator_reddit.py` | `llm_queries` `range(10)`→`range(25)` (~line 132) + matching `== 10`→`== 25` assertion (~line 139). |
| `tests/unit/test_view.py` | `_plan` default `n=10`→`n=25` (~33); `_plan(n=11)` caller (~113)→`n=25`; `assert ... == 11` (~125)→`== 25`. |

## Pre-flight (run once, do not commit)

- [ ] Health-check baseline green:
```
uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/
```
Expected: `229 passed`; `All checks passed!`; `…already formatted`; `Success: no issues found`. If not green, STOP and report.

---

## Chunk 1: Atomic band + prompt + test fan-out (spec §8 — ONE commit)

### Task 1: Widen the band to 25–30 and add industry-specific brainstorm

This is one atomic task ending in **one commit**. The schema floor change breaks every <25-query fixture, so the schema, the prompt, and all five test files MUST land together to keep the suite green (spec §8). TDD ordering is preserved by changing each contract's tests first and watching them fail before the implementation step.

**Files:** all nine in the table above.

#### Step 1: Rewrite `tests/unit/llm/test_schemas.py` `TestJobPlan` to the new band

- [ ] Replace the entire `class TestJobPlan:` block (currently lines ~55–83) with:

```python
class TestJobPlan:
    def test_rejects_fewer_than_25_queries(self) -> None:
        with pytest.raises(ValidationError):
            JobPlan(reddit_queries=[_good_query() for _ in range(24)])

    def test_accepts_25_queries(self) -> None:
        plan = JobPlan(reddit_queries=[_good_query() for _ in range(25)])
        assert len(plan.reddit_queries) == 25

    def test_accepts_30_queries(self) -> None:
        plan = JobPlan(reddit_queries=[_good_query() for _ in range(30)])
        assert len(plan.reddit_queries) == 30

    def test_rejects_more_than_30_queries(self) -> None:
        with pytest.raises(ValidationError):
            JobPlan(reddit_queries=[_good_query() for _ in range(31)])

    def test_extra_fields_round_trip(self) -> None:
        """extra='allow' — future prompts can emit extra fields and they
        stay on the model (and on Job.job_plan JSON) without losing them.
        model_validate re-validates, so the list must satisfy the 25 floor."""
        plan = JobPlan.model_validate(
            {
                "reddit_queries": [_good_query().model_dump() for _ in range(25)],
                "youtube_queries": ["a", "b"],  # not a typed field yet
            }
        )
        dumped = plan.model_dump()
        assert "youtube_queries" in dumped
        assert dumped["youtube_queries"] == ["a", "b"]

    def test_reddit_subreddits_defaults_to_empty(self) -> None:
        plan = JobPlan(reddit_queries=[_good_query() for _ in range(25)])
        assert plan.reddit_subreddits == []
```

#### Step 2: Run the schema tests — verify they FAIL against the old schema

- [ ] Run: `uv run pytest tests/unit/llm/test_schemas.py -v`
Expected: FAIL — `test_accepts_25_queries` and `test_accepts_30_queries` fail (old `max_length=15` rejects 25/30) and `test_rejects_fewer_than_25_queries` fails (24 ≥ old `min_length=10`, so no error is raised). Note `test_rejects_more_than_30_queries` will PASS even pre-change (31 > old `max_length=15` still raises) — that is fine and expected; the three failing tests above are what proves the new contract is pinned.

#### Step 3: Change the schema floor/ceiling

- [ ] In `src/discovery/llm/schemas.py` line 92, change:
```python
    reddit_queries: list[RedditQuerySpec] = Field(min_length=10, max_length=15)
```
to:
```python
    reddit_queries: list[RedditQuerySpec] = Field(min_length=25, max_length=30)
```

#### Step 4: Run the schema tests — verify they PASS (rest of suite now red, expected)

- [ ] Run: `uv run pytest tests/unit/llm/test_schemas.py -v`
Expected: PASS (all `TestJobPlan` + the rest of `test_schemas.py`).
- [ ] (Optional sanity) `uv run pytest -q` will now show many failures in the other test files — that is EXPECTED and fixed in the steps below. Do not commit yet.

#### Step 5: Rewrite `tests/unit/llm/test_prompts_query_expansion.py` to the v5 contract

- [ ] Replace the file body from `class TestPromptModule:` through the end with:

```python
class TestPromptModule:
    def test_version_is_v5(self) -> None:
        assert qe.VERSION == "v5"

    def test_system_prompt_keeps_core_reddit_rules(self) -> None:
        sp = qe.SYSTEM_PROMPT
        assert "OR" in sp
        assert "subreddit:" in sp
        assert "quote" in sp.lower() or "quoted" in sp.lower()
        assert "rationale" in sp.lower()
        # new band — the old "10"/"15" presence asserts are removed on purpose
        assert "25" in sp
        assert "30" in sp

    def test_system_prompt_has_grounding_section(self) -> None:
        s = qe.SYSTEM_PROMPT.lower()
        assert "only" in s
        assert "table" in s
        assert "never" in s
        assert "invent" in s or "memory" in s
        assert "matched_phrases" in s
        assert "public_description" in s
        assert "activity_ratio" in s

    def test_system_prompt_has_two_kinds_and_industry_brainstorm(self) -> None:
        s = qe.SYSTEM_PROMPT.lower()
        assert "two kinds" in s
        assert "industry-specific" in s
        assert "standard" in s
        # the fenced one-industry illustration + re-derive guard
        assert "wedding photography" in s
        assert "do not reuse" in s
        assert "re-derive" in s or "never copy" in s

    def test_few_shot_examples_still_present(self) -> None:
        assert len(qe.FEW_SHOT_EXAMPLES) >= 2
        for ex in qe.FEW_SHOT_EXAMPLES:
            assert len(ex["output"]["reddit_queries"]) >= 10


class TestBuildUserMessage:
    def test_renders_spec_and_table(self) -> None:
        msg = qe.build_user_message(
            JobSpec(
                industry="commercial cleaning",
                as_of=date(2026, 6, 1),
                location="NY",
                size="medium",
            ),
            _table(),
        )
        assert "commercial cleaning" in msg
        assert "NY" in msg
        assert "medium" in msg
        assert "2026-06-01" in msg
        assert "CleaningTips" in msg
        assert "matched_phrases" in msg
        # second count lever: the user-turn instruction must state the new band
        assert "25" in msg
        assert "30" in msg

    def test_handles_optional_fields(self) -> None:
        msg = qe.build_user_message(JobSpec(industry="bakery", as_of=date(2026, 6, 1)), _table())
        assert "bakery" in msg
        assert "None" not in msg

    def test_includes_time_window(self) -> None:
        msg = qe.build_user_message(
            JobSpec(industry="x", as_of=date(2026, 6, 1), time_window="year"),
            _table(),
        )
        assert "year" in msg.lower()
```

Also update the module docstring line 1–4 from `(query_expansion v4)` / "v4 adds the grounding section…" to reference v5 (cosmetic, keeps the file honest):
```python
"""Shape tests for the Wave 0 Call #2 prompt (query_expansion v5).

Pins module shape, not wording. v5 widens the band to 25-30 and adds
the two-kinds + industry-specific-brainstorm instruction with a fenced
one-industry illustration. The grounding section (v4) is retained.
"""
```

#### Step 6: Run the prompt shape tests — verify they FAIL against v4

- [ ] Run: `uv run pytest tests/unit/llm/test_prompts_query_expansion.py -v`
Expected: FAIL — `test_version_is_v5` (still "v4"), `test_system_prompt_keeps_core_reddit_rules` ("25"/"30" absent), `test_system_prompt_has_two_kinds_and_industry_brainstorm` (section absent), `TestBuildUserMessage::test_renders_spec_and_table` ("25"/"30" absent in user message).

#### Step 7: Edit `src/discovery/llm/prompts/query_expansion.py`

Apply ALL of the following exact edits. Keep new prose ASCII-only (no `×`, no smart quotes — ruff `RUF001`; use "x" or "by"). Do not touch `FEW_SHOT_EXAMPLES` or `_example_queries`.

- [ ] **7a — VERSION.** Line 38: `VERSION: str = "v4"` → `VERSION: str = "v5"`.

- [ ] **7b — docstring v5 entry.** After the v4 block (ends line 25 `…retained unchanged.`) and before the closing `"""` (line 26), add:
```
    v5 — wider band (25-30, was 10-15) and a second kind of query: the
    LLM keeps the generic pain-grid AND additionally brainstorms
    industry-specific queries for the specific industry in the request.
    Adds a fenced one-industry illustration (wedding photography) with
    an explicit "re-derive, never copy" guard. build_user_message's
    user-turn count string also moves to 25-30. All v4 grounding and
    Reddit-syntax rules retained.
```

- [ ] **7c — opening count.** Lines 42–43:
```
You are a Reddit search query designer. Your job is to brainstorm
between 10 and 15 high-signal Reddit search queries for a given
```
→
```
You are a Reddit search query designer. Your job is to brainstorm
between 25 and 30 high-signal Reddit search queries for a given
```

- [ ] **7d — rescope the generality rule (remove the self-contradiction).** Lines 110–113:
```
Subreddits give you the DOMAIN; phrases give you the SIGNAL. A nurse
looking for product ideas searches the same phrases a DevOps founder
uses — just in different subs. Don't try to make domain-specific
phrase lists; you'll lose generality.
```
→
```
Subreddits give you the DOMAIN; phrases give you the SIGNAL. A nurse
looking for product ideas searches the same phrases a DevOps founder
uses — just in different subs. For the STANDARD pain-grid queries
(kind 1, defined in "Two kinds of queries" below) keep phrasing
industry-agnostic — domain-specific phrasing there loses generality.
Industry-specific phrasing is the explicit, separate job of the kind-2
queries described in that section.
```

- [ ] **7e — insert the "Two kinds" section.** Immediately AFTER the block edited in 7d (the line ending `…described in that section.`) and BEFORE the next line `For each query, you choose:` (line 115), insert a blank line then:
```
# Two kinds of queries — produce BOTH (25-30 total)

Your 25-30 queries MUST draw from BOTH of these, each well represented:

1. STANDARD pain-grid queries — the generic pain categories above
   (willingness-to-pay, unmet need, frustration, alternatives, market
   gap, builder, switching, dead-competitor) crossed with the supplied
   subreddits. Keep these industry-AGNOSTIC in phrasing; do not bolt
   industry jargon onto them.

2. INDUSTRY-SPECIFIC queries — reason about THIS specific industry (the
   one in the user message). Think about its real tools, software,
   workflows, roles, recurring operational headaches, money and billing
   pain, client or vendor friction, and the words practitioners in that
   trade actually use. Then build queries that hunt those concrete
   problems by name, still combined with a pain category and the
   supplied subreddits.

The mix and ratio are YOUR judgement, but neither kind alone is
acceptable: include a substantial share of each, and let the
industry-specific set carry most of the breadth that gets you to 25-30.

## Illustration — ONE example industry only (do NOT reuse these)

The list below is illustrative ONLY, for the example industry "wedding
photography". It shows the KIND of domain reasoning expected; it is NOT
a template to copy. For the ACTUAL industry in the user message you
must re-derive your own, different, unique industry-specific angles the
same way. Do not reuse, lightly edit, or anchor on these
wedding-photography terms unless the user's industry genuinely is
wedding photography.

Example industry-specific angles for "wedding photography":
- editing and culling backlog, turnaround-time complaints
- client gallery, proofing, and delivery-platform pain
- booking, contracts, deposits, payment-collection friction
- second-shooter and associate-coordination problems
- album design and print-vendor frustration
- pricing, packaging, and client-ghosting pain

For any other industry these would be entirely different terms drawn
from THAT industry's real workflow. Re-derive; never copy.
```

- [ ] **7f — "What to emit" count.** Line 127:
```
- `reddit_queries` — between 10 and 15 `RedditQuerySpec` objects.
```
→
```
- `reddit_queries` — between 25 and 30 `RedditQuerySpec` objects.
```

- [ ] **7g — grounding count language.** Lines 170–175:
```
If the table is thin, use FEWER distinct subreddits — but you must
STILL produce 10-15 content queries by varying the pain-phrase angle
across the available subs (per_sub and site_wide combinations). Do NOT
emit fewer than 10 queries. Query count is driven by subreddit x
pain-category combinations, not 1:1 with subreddit count -- even 3 subs
comfortably yield 10-15 queries.
```
→
```
If the table is thin, use FEWER distinct subreddits — but you must
STILL produce 25-30 content queries by varying the pain-phrase angle
AND the industry-specific angle across the available subs (per_sub and
site_wide combinations). Do NOT emit fewer than 25 queries. Query count
is driven by subreddit x (pain-category + industry-specific) angle
combinations, not 1:1 with subreddit count -- even 3 subs comfortably
yield well over 25 queries.
```

- [ ] **7h — "What NOT to do" count.** Line 202:
```
- Don't return fewer than 10 or more than 15 queries.
```
→
```
- Don't return fewer than 25 or more than 30 queries.
```

- [ ] **7i — build_user_message user-turn string (second lever).** Lines 416–420:
```
    lines.append(
        "Produce a JobPlan with 10-15 reddit_queries using ONLY the "
        "subreddits above. Follow the system-prompt rules; explain each "
        "query's rationale."
    )
```
→
```
    lines.append(
        "Produce a JobPlan with 25-30 reddit_queries using ONLY the "
        "subreddits above — a substantial share STANDARD pain-grid and a "
        "substantial share INDUSTRY-SPECIFIC (re-derived for THIS "
        "industry, not the prompt's wedding-photography illustration). "
        "Follow the system-prompt rules; explain each query's rationale."
    )
```

#### Step 8: Run the prompt shape tests — verify they PASS

- [ ] Run: `uv run pytest tests/unit/llm/test_prompts_query_expansion.py -v`
Expected: PASS (v5, core rules incl. 25/30, grounding retained, two-kinds/illustration/guard present, user message carries 25/30).

#### Step 9: Bump the station test fixtures

- [ ] In `tests/unit/llm/stations/test_query_expansion.py`:
  - Line 42: `def _plan(subs: list[str] | None = None, n: int = 10) -> JobPlan:` → `n: int = 25`.
  - `TestCacheHit` line ~119: `assert len(result.reddit_queries) == 10` → `== 25`.
  - `TestValidationDropsInvalidQueries.test_drops_lowercase_or_query_via_existing_tail` (~145–159): change `good = [_query(f"g{i}") for i in range(10)]` → `range(25)`; change the final `assert len(result.reddit_queries) == 10` → `== 25` (25 good survive, the 1 bad dropped by the tail).
  - `TestFallbackTable.test_too_few_valid_queries_after_tail` (~217–229): keep `good = [_query(f"g{i}") for i in range(9)]`; change `_make_call_openai(JobPlan(reddit_queries=[*good, *[bad] * 6]))` → `[*good, *[bad] * 17]` (9 + 17 = 26 ≥ 25 passes the schema; the validator drops the 17 lowercase-`or` bad queries; 9 survive < `MIN_VALID_QUERIES`=10 → `QueryExpansionError`). Leave the `pytest.raises(QueryExpansionError)` assertion.
  - `TestTimeWindowOverride` (~249–257): change the `mixed = [ … for i in range(10)]` → `range(25)`.
  - `TestCacheMiss`, `TestOffTableRejection`, `TestBaselineSubredditMerge` call `_plan(...)` and are auto-fixed by the `n=25` default — no literal edit. `_combined_key` derives from `qe.VERSION` → auto-tracks v5; no edit.

#### Step 10: Bump the remaining fixture files

- [ ] `tests/unit/test_orchestrator_jobs.py`: (a) `_valid_plan()` (~line 49) `for i in range(10)` → `range(25)`; (b) the matching count assertion in `test_populates_job_plan_on_success` (~line 68) `assert len(updated.job_plan["reddit_queries"]) == 10` → `== 25`. (Helper-bump-plus-matching-assertion — the same pairing rule applied to the other files; missed in the spec's §3 list, caught in plan review.)
- [ ] `tests/unit/test_orchestrator_reddit.py` (~line 132): `for i in range(10)` (the `llm_queries` list) → `range(25)`; and the matching assertion (~line 139) `assert len(task.params["queries"]) == 10` → `== 25`.
- [ ] `tests/unit/test_view.py`: `def _plan(n: int = 10) -> JobPlan:` (~line 32) → `n: int = 25`; the explicit caller `_plan(n=11)` (~line 113) → `_plan(n=25)`; the matching `assert len(detail.plan.reddit_queries) == 11` (~line 125) → `== 25`.

#### Step 11: Full green gate

- [ ] Run: `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/`
Expected: ALL green. Test count ≈ 229 + 1 (TestJobPlan gains one method: was 5, now 6) + 1 (test_prompts gains the two-kinds test) ≈ **~231 passed** — exact number is not critical; ZERO failures and no regressions are. If `ruff format --check` flags any file you edited, run `uv run ruff format .` and re-stage. If `ruff check` flags `RUF001` in the new prompt prose, replace the offending ambiguous unicode with ASCII and re-run. If `PT018` flags a compound `assert a and b`, split into two `assert` lines (the test code above is already split — keep it that way).
- [ ] Independently re-confirm the no-code-change claim: `git diff --stat` must show `src/discovery/llm/stations/query_expansion.py` is **NOT** modified. If it is, revert that file — the station needs no change.

#### Step 12: One atomic commit

- [ ] ```
git add src/discovery/llm/schemas.py src/discovery/llm/prompts/query_expansion.py tests/unit/llm/test_schemas.py tests/unit/llm/test_prompts_query_expansion.py tests/unit/llm/stations/test_query_expansion.py tests/unit/test_orchestrator_jobs.py tests/unit/test_orchestrator_reddit.py tests/unit/test_view.py
git commit -m "feat(llm): widen query band to 25-30 + industry-specific brainstorm (prompt v5)

JobPlan.reddit_queries 10-15 -> 25-30 (supersedes the prior locked
10-15 decision, per docs/specs/2026-05-16-wider-query-band-design.md).
query_expansion prompt v4->v5: all count language -> 25-30, the
generality rule rescoped to the standard grid, a new two-kinds section
(keep generic pain-grid AND brainstorm industry-specific) with a fenced
wedding-photography illustration + re-derive guard, and the
build_user_message user-turn count string. Station unchanged
(MIN_VALID_QUERIES=10 decoupled). Atomic: the schema floor breaks every
<25 fixture so schema+prompt+the grep-verified 5-file test fan-out land
together (spec section 8).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Chunk 2: Smoke + handoff (separate commit)

### Task 2: Manual smoke + handoff update

**Files:** Modify `docs/handoff.md`.

**CRITICAL — run EVERYTHING in this chunk from the WORKTREE, never the
main checkout.** The Chunk-1 changes live on branch
`claude/jovial-poitras-fd083f` in the worktree and are NOT merged to
`main`. The main checkout (`/mnt/c/Users/skyto/pain_points_poject`) is on
`main` and still has the old 10–15 band — running the smoke or the test
gate there would exercise OLD code (false result) and a commit there
would land on the wrong branch. The worktree is:

- WSL: `/mnt/c/Users/skyto/pain_points_poject/.claude/worktrees/jovial-poitras-fd083f`
- Windows: `C:\Users\skyto\pain_points_poject\.claude\worktrees\jovial-poitras-fd083f`

`uv run` works in the worktree (its own venv) under both WSL and the
Windows shell. Use ONE of those worktree paths for every command below;
`cd` there at the start of each step. The smoke writes/reads the
worktree's own `data/discovery.db`.

#### Step 1: Pre-checks (from the worktree)

- [ ] `cd` to the worktree (WSL or Windows path above), then:
  `uv run python -m discovery.cli.init_db` → expect `alembic upgrade head` (or already-at-head).
- [ ] Confirm OpenAI key resolves (from the worktree):
`uv run python -c "from discovery.config.settings import settings; print('OPENAI set:', settings.openai_api_key is not None)"`
Expected `OPENAI set: True`. If `False`, STOP — a missing key surfaces as `wave 0: fallback` (not a logic bug).

#### Step 2: Smoke — the user's exact case, year window (from the worktree)

- [ ] From the worktree (so the smoke exercises the Chunk-1 feature-branch code, NOT main):
  `cd <worktree-path> && PYTHONIOENCODING=utf-8 uv run discovery run --industry "wedding photography" --location US --time-window year`
Expected: `wave 0: planned` (NOT fallback); `queued task: N  (source=reddit, queries=NN)` with **NN between 25 and 30**; the `subreddit discovery: N candidates survived` line present; ~5 `subreddit search done` Item-21 lines. **Record NN — you need it for Step 4 and Step 5.** If `wave 0: fallback`, capture the logged `QueryExpansionError` reason and STOP — investigate before continuing.

#### Step 3: Inspect the plan — confirm BOTH kinds present (from the worktree)

- [ ] From the worktree, read the saved plan's raw `q` strings (the heredoc reads the worktree's `data/discovery.db`; `uv run discovery jobs` lists ids if needed):
```
cd <worktree-path>
uv run python - <<'PY'
import sqlite3, json
db = sqlite3.connect("data/discovery.db")
row = db.execute("SELECT id, job_plan FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
plan = row[1] if isinstance(row[1], dict) else json.loads(row[1])
qs = plan["reddit_queries"]
print(f"job {row[0]} — {len(qs)} queries, {len(plan.get('reddit_subreddits', []))} subs")
for i, q in enumerate(qs, 1):
    tgt = f" r/{q['subreddit']}" if q.get("subreddit") else ""
    print(f"[{i}] {q['endpoint']}{tgt}: {q['q']}")
    print(f"    why: {q['rationale']}")
PY
```
(The `isinstance(row[1], dict)` branch is defensive — SQLite returns `job_plan` as TEXT, so the `json.loads` path is what runs; both are correct.)
Expected: 25–30 queries; a visible split between generic pain-grid queries (willingness-to-pay / frustration / alternatives phrasing) AND industry-specific ones using real wedding-photography workflow terms **derived for this industry** (e.g. culling/turnaround, client gallery, second shooter, deposits) — NOT the illustration copied verbatim. Confirm none are nonsense and subreddits are all from the discovered set + the baseline trio.

#### Step 4: Update `docs/handoff.md` (in the worktree)

- [ ] Edit `docs/handoff.md` in the worktree (follow its existing dated-slice pattern):
  - Header `Last touched` → today; `Branch` line → note the wider-band slice on top of the prior commits.
  - Test count → the new `N passed` from Chunk 1 Step 11.
  - Commit table → prepend the Chunk-1 commit SHA + the Chunk-2 handoff SHA.
  - Add a dated section "Wider query band + industry brainstorm (2026-05-16)" recording: band now 25–30 (supersedes the prior locked 10–15 — point to `docs/specs/2026-05-16-wider-query-band-design.md` as the new authority so a future session does not "restore" 10–15); prompt v5 (two-kinds + fenced illustration + re-derive guard); **the scoped reddit-source skill item-9 deviation — the industry-specific half deliberately bends item-9 generality; the standard pain-grid half still honours it (the prompt's generality line was rescoped to the standard grid so the prompt does not self-contradict)**; `MIN_VALID_QUERIES=10` decoupled; station unchanged; `build_user_message` second count lever; smoke evidence (the actual NN from Step 2, both kinds visible, `wave 0: planned`).
  - Open follow-ups → reaffirm the still-deferred `FEW_SHOT_EXAMPLES` wiring (now also visibly inconsistent at ~10 vs 25–30 — cosmetic, dead data) and the unchanged `--time-window` recall lever.

#### Step 5: Final gate + commit (from the worktree)

- [ ] From the worktree: `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/`
Expected: all green (unchanged test count from Chunk 1 Step 11; only a doc changed since). Running from the worktree is required — the main checkout is on `main` without these changes and would give a false green.
- [ ] Replace `NN` in the commit message below with the **actual query count observed in Step 2** (e.g. `27`), then from the worktree:
```
git add docs/handoff.md
git commit -m "docs(handoff): wider query band (25-30) + industry brainstorm shipped

Records the band supersession (new authority: the 2026-05-16 spec),
prompt v5 (two-kinds + fenced illustration + re-derive guard; scoped
item-9 deviation), decoupled MIN_VALID_QUERIES, the second
build_user_message count lever, and the live smoke (NN queries, both
kinds present, wave 0: planned). FEW_SHOT inconsistency reaffirmed as
deferred.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```
- [ ] Confirm the commit landed on `claude/jovial-poitras-fd083f` (not `main`): `git branch --show-current` → `claude/jovial-poitras-fd083f`; `git log --oneline -1` shows the handoff commit.

---

## Done criteria

- Two commits: the atomic band+prompt+test-fan-out commit (green), then the handoff commit.
- `uv run pytest` green at the recorded count (≥ baseline 229 + the small net-new tests); `ruff check`, `ruff format --check`, `mypy src/` clean.
- `src/discovery/llm/stations/query_expansion.py` unchanged (verified via `git diff`).
- `JobPlan.reddit_queries` is `min_length=25, max_length=30`; prompt `VERSION == "v5"`; `build_user_message` user-turn says 25-30; the prompt has the two-kinds section + fenced wedding-photography illustration + re-derive guard, and no longer self-contradicts on generality.
- Manual smoke: `wedding photography -t year` → `wave 0: planned`, 25–30 queries, a visible mix of generic pain-grid AND industry-specific (re-derived, not the illustration verbatim) queries.
- `docs/handoff.md` records the slice + the band supersession authority.
