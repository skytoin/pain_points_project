"""Token decomposition for token-AND search APIs (HN Algolia).

Splits a raw keyword phrase into the small set of high-signal content
tokens HN's strict token-AND search will accept. Long phrases starve
the source, so we keep only the first 2 surviving tokens after a
small stopword strip, with original casing preserved (acronyms like
MCP, CLI, RAG, LLM matter on HN).

Reusable later by other token-AND backends (GitHub code search, arXiv,
etc.) -- kept here in the HN-adopting slice without pre-generalization
for unbuilt sources.
"""

from __future__ import annotations

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "for",
        "with",
        "to",
        "of",
        "in",
        "on",
        "and",
        "or",
    }
)

MAX_TOKENS: int = 2


def decompose_keyword(keyword: str) -> list[str]:
    """Return up to 2 content tokens from a raw HN keyword phrase.

    - Whitespace-split (no punctuation surgery -- HN's tokenizer is
      simple; we feed it as-is once stopwords are gone).
    - Filter tokens whose LOWERCASED form is in the stopword set
      (so the comparison is case-insensitive but surviving tokens
      retain their ORIGINAL casing).
    - Keep the first MAX_TOKENS surviving tokens.
    - Return [] if nothing survives (caller drops the query).
    """
    out: list[str] = []
    for tok in keyword.split():
        if tok.lower() in _STOPWORDS:
            continue
        out.append(tok)
        if len(out) == MAX_TOKENS:
            break
    return out
