"""Tests for the shared `hash_params` helper.

`hash_params` is the project's one and only content-hashing recipe.
It is used by:

- `tasks.content_hash` (idempotency on the task queue)
- `raw_records.content_hash` (second-line dedup of Bronze rows)
- The LLM diskcache key (content + prompt VERSION + model)

The recipe is `sha256(canonical_json)` where canonical_json uses
`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`.
"""

from __future__ import annotations

import hashlib

import pytest

from discovery.hashing import hash_params


def test_hash_params_returns_64_char_hex() -> None:
    """sha256 hex digest is always 64 lowercase hex characters."""
    digest = hash_params({"x": 1})
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_params_is_deterministic() -> None:
    """Same input twice yields the same digest."""
    payload = {"source": "reddit", "action": "fetch", "params": {"sub": "startups"}}
    assert hash_params(payload) == hash_params(payload)


def test_hash_params_dict_key_order_invariant() -> None:
    """Reordering dict keys must not change the digest."""
    a = {"alpha": 1, "beta": 2, "gamma": 3}
    b = {"gamma": 3, "alpha": 1, "beta": 2}
    assert hash_params(a) == hash_params(b)


def test_hash_params_nested_dict_key_order_invariant() -> None:
    """Nested dicts are also canonicalized."""
    a = {"outer": {"a": 1, "b": 2}, "list": [{"k": 1, "j": 2}]}
    b = {"outer": {"b": 2, "a": 1}, "list": [{"j": 2, "k": 1}]}
    assert hash_params(a) == hash_params(b)


def test_hash_params_unicode_preserved() -> None:
    """`ensure_ascii=False` is the project policy; unicode must hash stably."""
    a = hash_params({"name": "café"})
    b = hash_params({"name": "café"})
    assert a == b
    assert len(a) == 64


def test_hash_params_different_inputs_yield_different_hashes() -> None:
    """Sanity: distinct payloads → distinct digests."""
    assert hash_params({"x": 1}) != hash_params({"x": 2})
    assert hash_params({"x": 1}) != hash_params({"y": 1})


def test_hash_params_list_order_matters() -> None:
    """Lists are semantic; reordering items must change the digest.

    `sort_keys=True` only sorts dict keys, not list items. Batch-of-items
    callers (LLM stations) rely on this to distinguish "same items, new
    order".
    """
    assert hash_params([1, 2, 3]) != hash_params([3, 2, 1])


def test_hash_params_compact_separators() -> None:
    """No whitespace inside the canonical JSON.

    We don't expose the JSON, but the digest is sensitive to separator
    bytes. A regression here (e.g., someone forgets `separators=`) would
    change every hash in the database. This test pins the recipe by
    asserting a known fixture digest.
    """
    expected = hashlib.sha256(b'{"a":1,"b":[2,3]}').hexdigest()
    assert hash_params({"a": 1, "b": [2, 3]}) == expected


def test_hash_params_rejects_non_json_serializable() -> None:
    """Non-JSON-serializable values should fail loudly, not silently.

    Callers must convert datetimes/Decimals/sets to JSON-safe forms before
    hashing. We refuse to guess.
    """
    with pytest.raises(TypeError):
        hash_params({"bad": object()})
