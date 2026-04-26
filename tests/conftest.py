"""Ensure project root is importable inside pytest."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _test_fx_rate(monkeypatch):
    """Keep USD-quoted legacy tests deterministic without app hardcoding FX."""
    monkeypatch.setenv("GBP_USD_RATE", "2.0")
