"""Keep disposable synthetic profiles outside the code tree."""

import os
from pathlib import Path
import uuid

import pytest


def _runtime_root() -> Path:
    # Chromium profiles are data (~2 MB per test), not code: keep them under
    # ~/.agents unless BROWSER_WORKER_TEST_ROOT points elsewhere.
    configured = os.environ.get("BROWSER_WORKER_TEST_ROOT")
    return Path(configured) if configured else Path.home() / ".agents" / "browser-worker-tests"


@pytest.fixture
def tmp_path():
    # pytest's Windows mkdir(mode=0700) can lock a restricted token out of its
    # own temp directory. Let the product apply its own real owner-only ACLs.
    # Keep evidence, and never remove a prior run's files during a later test.
    path = _runtime_root() / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path
