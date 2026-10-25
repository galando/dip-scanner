"""Shared test setup."""
import pytest

import src.sec as sec


@pytest.fixture(autouse=True)
def _no_sec_network(monkeypatch):
    """No test may reach SEC, even on a machine where SEC_USER_AGENT is set.

    Tests that exercise the SEC path set their own User-Agent and replace
    sec._get_json with canned responses.
    """
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    sec.clear_cache()
    yield
    sec.clear_cache()
