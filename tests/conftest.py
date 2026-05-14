"""Shared pytest fixtures and configuration.

Pytest auto-loads this file for every test session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator

import pytest


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Session-scoped event loop so async fixtures aren't torn down per-test."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
