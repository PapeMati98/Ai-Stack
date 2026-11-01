"""Every test owns a retained temporary store; no real subprocess may run."""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import idea


def retained_test_root():
    source = Path(__file__).resolve().parents[1]
    repository = next((parent for parent in source.parents if (parent / ".git").exists()), source)
    root = Path(os.environ.get("AGENT_TEST_ROOT") or
                Path.home() / ".agents" / "test-runs" / "idea-maker").resolve()
    if root.is_relative_to(repository):
        raise pytest.UsageError("AGENT_TEST_ROOT must be outside the repository")
    return root


@pytest.fixture
def source_snapshot():
    def snapshot():
        source = Path(__file__).resolve().parents[1]
        return {str(path.relative_to(source)): path.read_bytes() if path.is_file() else None
                for path in source.rglob("*")
                if "__pycache__" not in path.relative_to(source).parts}
    return snapshot


@pytest.fixture(autouse=True)
def sandbox(monkeypatch):
    test_root = retained_test_root()
    test_root.mkdir(parents=True, exist_ok=True)
    # Python 3.13's Windows mode 0700 creates a DACL excluding the sandbox's
    # restricted token. Use ordinary inherited directory permissions at creation,
    # scoped only to this test-owned mkdtemp (no ACL edits to existing paths).
    mkdir = os.mkdir
    with patch("tempfile._os.mkdir", side_effect=lambda path, mode: mkdir(path, 0o777)):
        root = Path(tempfile.mkdtemp(prefix="run-", dir=test_root))
    environment = {"root": root, "home": root / "idea", "helpdesk": root / "helpdesk",
                   "now": dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)}
    monkeypatch.setenv("IDEA_MAKER_HOME", str(environment["home"]))
    monkeypatch.setenv("RSI_HELPDESK_HOME", str(environment["helpdesk"]))
    monkeypatch.setenv("IDEA_MAKER_TELEMETRY", str(root / "telemetry"))
    monkeypatch.setenv("IDEA_MAKER_CATALOG", str(root / "missing-catalog.py"))
    monkeypatch.setenv("IDEA_MAKER_AUDIT_CLI", str(root / "missing-audit.py"))
    monkeypatch.setenv("TESTY_AB_HOME", str(root / "missing-testy-ab"))
    monkeypatch.delenv("IDEA_MAKER_RUN_ID", raising=False)
    monkeypatch.setattr(idea, "now", lambda: environment["now"])

    def forbidden(*args, **kwargs):
        raise AssertionError("real subprocess forbidden in offline tests")

    monkeypatch.setattr(idea.subprocess, "run", forbidden)
    monkeypatch.setattr(idea.subprocess, "Popen", forbidden)
    monkeypatch.setattr(idea, "probe", lambda pid: ("gone", None))
    return environment
