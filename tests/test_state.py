"""Tests for state.py — JSON dedup store."""
import pytest
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from src.state import load_state, is_recently_alerted, mark_alerted, save_state


class TestLoadState:
    """Loading state from JSON file."""

    def test_loads_existing_file(self, tmp_path):
        """Loads state from an existing JSON file."""
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"AAPL": "2024-01-15"}))
        state = load_state(str(state_file))
        assert state == {"AAPL": "2024-01-15"}

    def test_returns_empty_on_missing_file(self, tmp_path):
        """Returns empty dict when file doesn't exist."""
        state = load_state(str(tmp_path / "nonexistent.json"))
        assert state == {}

    def test_returns_empty_on_invalid_json(self, tmp_path):
        """Returns empty dict on malformed JSON."""
        state_file = tmp_path / "state.json"
        state_file.write_text("not json")
        state = load_state(str(state_file))
        assert state == {}


class TestIsRecentlyAlerted:
    """Scenario 6: Dedup suppresses repeat alerts."""

    def test_recently_alerted_is_true(self):
        """Ticker alerted within dedup window returns True."""
        state = {"JKL": (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")}
        assert is_recently_alerted("JKL", state, dedup_days=10) is True

    def test_old_alert_is_not_recent(self):
        """Ticker alerted beyond dedup window returns False."""
        state = {"JKL": (datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%d")}
        assert is_recently_alerted("JKL", state, dedup_days=10) is False

    def test_unknown_ticker_is_not_recent(self):
        """Ticker not in state returns False."""
        assert is_recently_alerted("XYZ", {}, dedup_days=10) is False

    def test_exact_boundary_day(self):
        """Ticker alerted exactly dedup_days ago is still recent."""
        state = {"JKL": (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")}
        assert is_recently_alerted("JKL", state, dedup_days=10) is True


class TestMarkAlerted:
    """Marking a ticker as alerted."""

    def test_adds_new_ticker(self):
        """New ticker is added with today's date."""
        state = {}
        updated = mark_alerted("AAPL", state)
        assert "AAPL" in updated
        assert updated["AAPL"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def test_updates_existing_ticker(self):
        """Existing ticker's date is updated."""
        state = {"AAPL": "2024-01-01"}
        updated = mark_alerted("AAPL", state)
        assert updated["AAPL"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def test_does_not_mutate_original(self):
        """Original state dict is not modified."""
        state = {"AAPL": "2024-01-01"}
        updated = mark_alerted("MSFT", state)
        assert "MSFT" not in state
        assert "MSFT" in updated


class TestSaveState:
    """Saving state to JSON file."""

    def test_saves_to_file(self, tmp_path):
        """State is persisted as valid JSON."""
        state_file = tmp_path / "state.json"
        state = {"AAPL": "2024-06-01", "MSFT": "2024-06-02"}
        save_state(state, str(state_file))
        loaded = json.loads(state_file.read_text())
        assert loaded == state

    def test_creates_file_if_missing(self, tmp_path):
        """Creates the file if it doesn't exist."""
        state_file = tmp_path / "state.json"
        save_state({}, str(state_file))
        assert state_file.exists()
