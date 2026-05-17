"""Abstract base for all source adapters.

A "source" is anything that brings raw data into the Bronze layer:
Reddit, YouTube, Apollo, Yelp, etc. Every source subclasses `BaseSource`
and implements one async `fetch` method.

See `.claude/skills/source-adapter/SKILL.md` for the full contract.
"""

from __future__ import annotations

import abc
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RawRecord(BaseModel):
    """One raw record from an external source.

    Lives in the Bronze layer (`raw_records` table). The `body` field is
    the API's response shape, unprocessed. Don't normalize here — that's
    Wave 2's job.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(description="The source name, e.g. 'reddit'.")
    external_id: str = Field(description="The natural ID inside that source.")
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    body: dict[str, Any] = Field(description="The raw response object.")


class BaseSource(abc.ABC):
    """Contract every source adapter must implement.

    Subclasses MUST set:
        - `name`        — short slug, used as the partition key
        - `rate_limit`  — `(max_requests, per_seconds)` tuple

    Subclasses MUST implement:
        - `async fetch(params) -> list[RawRecord]`
    """

    name: str
    rate_limit: tuple[int, int]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not hasattr(cls, "name") or not cls.name:
            raise TypeError(f"{cls.__name__} must define a non-empty `name`.")
        if not hasattr(cls, "rate_limit"):
            raise TypeError(f"{cls.__name__} must define `rate_limit`.")

    @abc.abstractmethod
    async def fetch(self, params: dict[str, Any]) -> list[RawRecord]:
        """Fetch records for the given parameters.

        Parameters
        ----------
        params : dict
            Source-specific parameters from the `JobPlan` (e.g. a Reddit
            subreddit + sort, a Yelp category + location).

        Returns
        -------
        list[RawRecord]
            Zero or more records. Return `[]`, never `None`.
        """
        ...

    async def aclose(self) -> None:
        """Release any resources the adapter owns (e.g. an HTTP client).

        Default is a no-op; adapters that own a client (like
        `RedditSource`) override this. Called once per worker process at
        shutdown via `discovery.workers.aclose_registry`.
        """
        return None
