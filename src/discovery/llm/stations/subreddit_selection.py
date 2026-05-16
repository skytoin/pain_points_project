"""Deterministic subreddit pipeline — the "no LLM" middle of Wave 0.

Pure functions only (no I/O, no LLM). Order is fixed by spec §7:

    dedupe + consensus → drop non-public → drop NSFW → median →
    drop drastically-below-median → activity_ratio
    → (LLM Call #2 selects) → reject off-table → overflow trim

`render_candidate_table` (the projection step) lives in
`discovery.sources.reddit_subreddits` beside the DTO it projects so the
prompt module can import it without inverting the layer direction.
"""

from __future__ import annotations

from discovery.sources.reddit_subreddits import PhraseResult, SubredditCandidate

# Spec §7 step 6: gentle relative floor. subscribers < median/10 → drop.
# Kills dead/junk without decapitating small niche communities the LLM
# should still judge. Tunable later if Item-21 data warrants (spec §13).
DRASTIC_FLOOR_DIVISOR: int = 10

# Spec §2.4 / §7 step 9: adaptive selection, hard ceiling 30.
SELECTION_CEILING: int = 30

_PUBLIC_TYPES: frozenset[str] = frozenset({"public", "restricted"})


def dedupe_and_count(results: list[PhraseResult]) -> list[SubredditCandidate]:
    """Collapse to unique subreddit (case-insensitive name); set
    `matched_phrases` = number of DISTINCT phrases whose result set
    contained it (spec §7 step 2). First occurrence wins for every other
    field. Dedup MUST precede the median — duplicates would skew it.
    """
    first_seen: dict[str, SubredditCandidate] = {}
    phrases_for: dict[str, set[str]] = {}
    for res in results:
        for cand in res.candidates:
            key = cand.name.lower()
            phrases_for.setdefault(key, set()).add(res.phrase)
            if key not in first_seen:
                first_seen[key] = cand
    return [
        cand.model_copy(update={"matched_phrases": len(phrases_for[key])})
        for key, cand in first_seen.items()
    ]


def drop_non_public(cands: list[SubredditCandidate]) -> list[SubredditCandidate]:
    """Spec §7 step 3: keep `subreddit_type ∈ {public, restricted}`.
    `restricted` is READable (only posting is gated) — keep it.
    """
    return [c for c in cands if c.subreddit_type in _PUBLIC_TYPES]


def drop_nsfw(cands: list[SubredditCandidate]) -> list[SubredditCandidate]:
    """Spec §7 step 4: drop `over18` (defense in depth — the request
    also sets `include_over_18=false`; neither alone is fully reliable).
    """
    return [c for c in cands if not c.over18]


def subscriber_median(cands: list[SubredditCandidate]) -> float:
    """Median of `subscribers` over `cands` (spec §7 step 5). Empty →
    0.0; the caller checks emptiness separately and raises.
    """
    if not cands:
        return 0.0
    xs = sorted(c.subscribers for c in cands)
    n = len(xs)
    mid = n // 2
    if n % 2 == 1:
        return float(xs[mid])
    return (xs[mid - 1] + xs[mid]) / 2


def drop_below_median(cands: list[SubredditCandidate], median: float) -> list[SubredditCandidate]:
    """Spec §7 step 6: drop `subscribers < median / DRASTIC_FLOOR_DIVISOR`
    (strictly below; equal is kept). `median <= 0` → no-op (nothing to
    compare against).
    """
    if median <= 0:
        return list(cands)
    floor = median / DRASTIC_FLOOR_DIVISOR
    return [c for c in cands if c.subscribers >= floor]


def with_activity_ratio(
    cands: list[SubredditCandidate],
) -> list[SubredditCandidate]:
    """Spec §7 step 7: `activity_ratio = active_user_count / subscribers`,
    rounded ~4dp. Guards divide-by-zero (`subscribers == 0` → 0.0). A
    missing `active_user_count` defaults to 0 in the DTO, which
    naturally yields a 0.0 ratio (no special-casing needed).
    """
    out: list[SubredditCandidate] = []
    for c in cands:
        ratio = round(c.active_user_count / c.subscribers, 4) if c.subscribers > 0 else 0.0
        out.append(c.model_copy(update={"activity_ratio": ratio}))
    return out


def reject_off_table(selected: list[str], table: list[SubredditCandidate]) -> list[str]:
    """Spec §10 defensive filter: drop any selected sub NOT present in
    the supplied table (case-insensitive — Reddit names are
    case-insensitive). The grounding prompt forbids off-table picks;
    this enforces it deterministically if the LLM slips. Selection
    order is preserved.
    """
    allowed = {c.name.lower() for c in table}
    return [s for s in selected if s.lower() in allowed]


def trim_overflow(selected: list[str]) -> list[str]:
    """Spec §7 step 9: keep the LLM's first `SELECTION_CEILING` in its
    emitted order. `JobPlan.reddit_subreddits` is an ordered list, so
    the spec's "tie-break only if order is ambiguous" branch
    (matched_phrases desc, activity_ratio desc) is unreachable here and
    is intentionally not implemented (YAGNI).
    """
    return selected[:SELECTION_CEILING]
