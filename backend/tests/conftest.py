"""Suite-wide rules.

Tests marked ``@pytest.mark.live`` talk to real Questrade (read-only). They are skipped unless
``AURELIUS_LIVE_TESTS=1``, and CI additionally deselects them with ``-m "not live"`` and pins
``AURELIUS_LIVE_TESTS=0``, so they can never run in CI.
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("AURELIUS_LIVE_TESTS") == "1" and not os.environ.get("CI"):
        return
    skip = pytest.mark.skip(reason="live Questrade test: set AURELIUS_LIVE_TESTS=1 locally (never in CI)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
