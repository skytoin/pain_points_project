"""Diskcache-backed cache for LLM station outputs.

Why JSON, not pickle:
    Pydantic models pickle fine, but we store JSON instead. JSON survives
    unrelated code reorganization (e.g. moving a model between modules)
    where pickles would break. The cache key already encodes prompt
    VERSION and model name, so a schema change should bump one of those
    and miss the cache anyway — but JSON is safer if someone forgets.

Public surface
--------------
- `cache_key(**parts) -> str` — sha256 hex digest of canonical JSON
  over the kwargs. Use named keys (`spec=`, `prompt_version=`, `model=`)
  for readability.
- `make_cache(dir) -> Cache` — open or create a diskcache rooted at `dir`.
- `get_cached(cache, key, model) -> Model | None` — validated read.
- `put_cached(cache, key, value) -> None` — JSON write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from diskcache import Cache
from pydantic import BaseModel

from discovery.hashing import hash_params


def cache_key(**parts: Any) -> str:
    """Return a sha256 hex digest of the canonical JSON of `parts`.

    Use named arguments at the call site so cache keys read clearly:

        cache_key(
            spec=spec.model_dump(mode="json"),
            prompt_version=prompts.query_expansion.VERSION,
            model="gpt-5.4",
        )
    """
    return hash_params(parts)


def make_cache(directory: Path) -> Cache:
    """Open (or create) a diskcache at `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    return Cache(str(directory))


def get_cached[T: BaseModel](cache: Cache, key: str, model: type[T]) -> T | None:
    """Return a validated `model` instance for `key`, or `None` on miss.

    The stored value is a JSON string; we re-validate on read so a value
    that no longer matches the model raises cleanly (and the caller can
    fall through to a fresh LLM call).
    """
    raw = cache.get(key)
    if raw is None:
        return None
    return model.model_validate_json(raw)


def put_cached(cache: Cache, key: str, value: BaseModel) -> None:
    """Store `value.model_dump_json()` under `key`."""
    cache.set(key, value.model_dump_json())
