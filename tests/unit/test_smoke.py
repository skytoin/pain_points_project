"""Smoke tests — verify the project boots.

If these fail, something is structurally wrong with the install. Run
these first before debugging anything else.
"""

from __future__ import annotations

import discovery


def test_package_imports() -> None:
    """The top-level package imports without side effects."""
    assert discovery is not None


def test_version_string_present() -> None:
    """A `__version__` string is exported and looks like a version."""
    assert hasattr(discovery, "__version__")
    parts = discovery.__version__.split(".")
    assert len(parts) >= 2
    assert all(p.isdigit() for p in parts[:2])


def test_settings_module_imports() -> None:
    """The settings module imports — does NOT require .env to exist yet.

    We just verify the import path resolves; we don't instantiate
    `settings` because that would require ANTHROPIC_API_KEY to be set.
    """
    from discovery.config import settings as settings_mod

    assert hasattr(settings_mod, "Settings")
    assert hasattr(settings_mod, "get_settings")
