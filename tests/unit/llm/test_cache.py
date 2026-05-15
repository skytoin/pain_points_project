"""Tests for `discovery.llm.cache` — diskcache wrapper with typed get/put."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from diskcache import Cache
from pydantic import BaseModel

from discovery.llm.cache import cache_key, get_cached, make_cache, put_cached


class _Sample(BaseModel):
    a: int
    b: str


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[Cache]:
    """Open a fresh diskcache rooted at `tmp_path/cache` and close it on teardown.

    Diskcache holds a SQLite connection; not closing it triggers a
    ResourceWarning at gc time (which pytest promotes to an error via
    `filterwarnings = ["error", ...]` in pyproject.toml).
    """
    c = make_cache(tmp_path / "cache")
    yield c
    c.close()


class TestCacheKey:
    def test_key_is_deterministic(self) -> None:
        k1 = cache_key(spec={"x": 1}, prompt_version="v1", model="m")
        k2 = cache_key(spec={"x": 1}, prompt_version="v1", model="m")
        assert k1 == k2
        assert len(k1) == 64  # sha256 hex digest

    def test_key_changes_with_prompt_version(self) -> None:
        k1 = cache_key(spec={"x": 1}, prompt_version="v1", model="m")
        k2 = cache_key(spec={"x": 1}, prompt_version="v2", model="m")
        assert k1 != k2

    def test_key_changes_with_model(self) -> None:
        k1 = cache_key(spec={"x": 1}, prompt_version="v1", model="m1")
        k2 = cache_key(spec={"x": 1}, prompt_version="v1", model="m2")
        assert k1 != k2

    def test_key_is_order_independent_in_spec(self) -> None:
        """hash_params uses sort_keys=True — input dict ordering shouldn't matter."""
        k1 = cache_key(spec={"a": 1, "b": 2}, prompt_version="v", model="m")
        k2 = cache_key(spec={"b": 2, "a": 1}, prompt_version="v", model="m")
        assert k1 == k2


class TestRoundTrip:
    def test_put_then_get_returns_equivalent_model(self, cache: Cache) -> None:
        key = cache_key(spec={"x": 1}, prompt_version="v", model="m")
        put_cached(cache, key, _Sample(a=1, b="hi"))
        got = get_cached(cache, key, _Sample)
        assert got is not None
        assert got.a == 1
        assert got.b == "hi"

    def test_miss_returns_none(self, cache: Cache) -> None:
        got = get_cached(cache, "no-such-key", _Sample)
        assert got is None

    def test_stored_value_revalidates_through_pydantic(self, cache: Cache) -> None:
        """We store JSON, not pickles — read path validates through Pydantic."""
        put_cached(cache, "k", _Sample(a=42, b="x"))
        got = get_cached(cache, "k", _Sample)
        assert isinstance(got, _Sample)
