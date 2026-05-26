# YouTube comment-quality floor — design

**Date:** 2026-05-25
**Status:** approved (brainstorm) — pending spec review + user sign-off
**Author:** Claude Opus 4.7 (1M context), in session with the project owner

---

## 1. Goal

Stop the YouTube adapter from storing obvious low-information comment
threads. The first real run (`IoT device management`) harvested 4,536
comment threads, but a large share were junk — `"WOW"`, `"😅😅"`,
`"chupa chups"`, emoji-only reactions, and one-line praise/greetings.
Add a cheap, deterministic **comment-quality floor** so the Bronze layer
keeps only threads that carry some textual signal.

This mirrors the existing Reddit precedent exactly: `keep_post()` in
`src/discovery/sources/reddit.py` already drops low-quality posts (score,
comment, NSFW, removed floors) at fetch time before storing
(reddit-source skill item 13: "quality floor — cheap drop before LLM
tokens get spent"). The YouTube floor is the comment-level analog.

## 2. Non-goals

- **No semantic / on-topic / pain detection.** Judging whether a comment
  is actually *about the industry* or expresses a *pain point* is
  semantic and belongs to Wave 2 (classification). A long, well-formed
  but off-topic comment (`"Add a variety of shapes of orange and white
  pumpkins for chess pieces"`, 116 likes) will pass this floor — that is
  expected. The floor removes the cheap-to-detect garbage only.
- **No non-English exclusion.** The floor drops only true emoji/symbol-
  only comments. Non-English text (Cyrillic, CJK, etc.) is real signal
  and is KEPT — the "has a letter" check uses Unicode `str.isalpha()`,
  which is true for any script's letters and false for emoji/punctuation.
- **No back-fill to preserve count.** Filtering reduces the stored count
  (same quota, fewer rows). We do NOT paginate or harvest more videos to
  restore the ~4,500 number; that would cost quota for diminishing
  returns. Fewer, higher-signal comments is the intended outcome.
- **No change to which videos are harvested.** Still the top
  `COMMENT_TOP_K=50` videos by view count. Smarter (relevance-based)
  video selection is a possible future slice, explicitly out of scope.
- **No new config / settings.** Thresholds are module-level constants in
  the adapter (tunable in code, monkeypatchable in tests), like
  `COMMENT_TOP_K`.

## 3. Decision record + measured impact

Owner-chosen thresholds:

- `MIN_COMMENT_CHARS = 45`
- `MIN_COMMENT_LIKES = 7`

Rule: **keep a thread if** its top-level comment text contains at least
one letter (any script) **AND** (`len(text) >= 45` **OR**
`likeCount >= 7`).

Measured against the 4,536 stored IoT comment threads:

| Outcome | Count | Share |
|---|---|---|
| kept | 2,507 | 55% |
| dropped | 2,029 | 45% |
| (of kept) qualified by length >= 45 | 2,243 | |
| (of kept) rescued by likes >= 7 (short but upvoted) | ~264 | |

The like-rescue keeps short-but-validated complaints (a punchy
`"this bricked my router"` with 30+ likes) that a pure length floor would
drop.

## 4. Design

A pure helper in `src/discovery/sources/youtube.py`:

```python
MIN_COMMENT_CHARS = 45   # keep threads with >= this many chars of text...
MIN_COMMENT_LIKES = 7    # ...OR at least this many likes (rescue short-but-upvoted)


def _comment_text(thread: dict[str, Any]) -> str:
    """Top-level comment text, preferring textOriginal, then textDisplay."""
    snip = thread.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
    text = snip.get("textOriginal") or snip.get("textDisplay") or ""
    return text.strip()


def _comment_likes(thread: dict[str, Any]) -> int:
    snip = thread.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
    raw = snip.get("likeCount")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def keep_comment(thread: dict[str, Any]) -> bool:
    """Cheap deterministic quality floor for a commentThread (analog of
    keep_post). Drops emoji/symbol-only and short low-engagement threads.
    Semantic relevance is Wave 2's job. See
    docs/specs/2026-05-25-youtube-comment-quality-floor.md.
    """
    text = _comment_text(thread)
    if not any(ch.isalpha() for ch in text):   # emoji/symbol/number-only -> drop
        return False
    return len(text) >= MIN_COMMENT_CHARS or _comment_likes(thread) >= MIN_COMMENT_LIKES
```

Applied in `_harvest_comments`, filtering each page before conversion:

```python
for thread in payload.get("items", []):
    if keep_comment(thread):
        records.append(comment_to_raw_record(thread))
```

Everything else in the three-step fetch is unchanged. Video records are
NOT affected (the floor is comment-only). Bronze still stores survivors
verbatim. `viewcount_of`, quota handling, logging all unchanged — except
the per-call comment log line should carry both counts (before/after
floor), mirroring Reddit's `count_before_filter` / `count_after_filter`.

## 5. Edge cases

- **Missing `topLevelComment` / `snippet` / `textOriginal`:** the `.get`
  chain yields `""` → no letter → dropped. Defensive, correct.
- **`likeCount` absent or non-numeric:** treated as 0 (no rescue).
- **`textOriginal` missing but `textDisplay` present:** fall back to
  `textDisplay` (HTML-escaped, but length/letter checks still valid).
- **Whitespace-only / very long whitespace:** stripped before the length
  check.
- **A thread that is all digits/punctuation (e.g. a timestamp):** no
  letter → dropped (intended; no textual signal).

## 6. Testing

Unit tests for `keep_comment` (and the two extractors) in
`tests/unit/sources/test_youtube.py`:

- emoji-only / symbol-only → dropped.
- short (< 45) with low likes → dropped.
- short (< 45) but likes >= 7 → kept (rescue).
- long (>= 45) with 0 likes → kept.
- non-English (Cyrillic) text >= 45 chars → kept (Unicode isalpha).
- missing `textOriginal` (falls back to `textDisplay`) → handled.
- missing `topLevelComment` entirely → dropped, no crash.
- boundary: exactly 45 chars → kept; 44 → dropped (unless liked).

Adapter-level test: `_harvest_comments` (or `fetch`) drops junk threads
and keeps good ones from a mocked `commentThreads.list` payload; assert
only survivors are returned and the per-call log carries before/after
counts.

## 7. Docs

Update `.claude/skills/youtube-source/SKILL.md`: add the comment-quality
floor to the comment-harvest item (thresholds, the emoji-only drop, the
"semantic relevance is Wave 2" boundary), mirroring how the reddit-source
skill documents `keep_post`.

## 8. Risks / notes

- **Aggressiveness:** 45/7 drops ~45% of comments. That is deliberate
  (owner-chosen) and tunable via the two constants if it proves too
  strict on other industries.
- **Re-running existing jobs:** the floor only affects NEW fetches.
  Already-stored job rows keep their unfiltered comments until re-run.
- **`textDisplay` HTML entities:** when falling back to `textDisplay`,
  the length check counts entity markup (`&#39;`), a minor over-count;
  acceptable, and `textOriginal` is present on virtually all comments.

---

End of spec.
