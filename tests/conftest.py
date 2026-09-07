"""Shared test fixtures.

Tests exercise the *classifier / triage* behaviour with developer-hiring text.
That behaviour must not be coupled to the product's live targeting config in
``circle_leads/config/requirements.yaml`` -- that file is owned by whoever runs
the product and has since been retargeted (e.g. to founders/CEOs), which would
otherwise silently break these tests.

So tests load a stable, developer-targeting config from
``tests/fixtures/dev_requirements.yaml`` (a snapshot of the original targeting)
instead of ``load_requirements()``. This keeps the tests asserting classifier
behaviour, independent of how the product is currently aimed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from circle_leads.config.settings import Requirements, load_requirements

DEV_REQUIREMENTS_PATH = Path(__file__).parent / "fixtures" / "dev_requirements.yaml"


def load_dev_requirements() -> Requirements:
    """Stable developer-targeting Requirements for tests (not the live config)."""
    return load_requirements(str(DEV_REQUIREMENTS_PATH))


@pytest.fixture
def dev_requirements() -> Requirements:
    # Function-scoped: return a FRESH instance per test. Some tests mutate their
    # Requirements (e.g. set target_roles), so a shared session/module instance
    # would leak that mutation into later tests. Loading is cheap.
    return load_dev_requirements()
