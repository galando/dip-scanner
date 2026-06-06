"""State / dedup store — persist alerted tickers in a JSON file."""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def load_state(path: str = "state.json") -> dict[str, str]:
    """Load dedup state from JSON file. Returns empty dict on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.warning("Invalid state file %s: %s", path, e)
        return {}


def is_recently_alerted(ticker: str, state: dict[str, str], dedup_days: int) -> bool:
    """Check if ticker was alerted within the dedup window."""
    if ticker not in state:
        return False

    try:
        last_date = datetime.strptime(state[ticker], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_date).days <= dedup_days
    except (ValueError, TypeError):
        return False


def mark_alerted(ticker: str, state: dict[str, str]) -> dict[str, str]:
    """Mark a ticker as alerted with today's date. Returns a new dict."""
    updated = dict(state)
    updated[ticker] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return updated


def save_state(state: dict[str, str], path: str = "state.json") -> None:
    """Save dedup state to JSON file."""
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    logger.info("Saved state with %d tickers to %s", len(state), path)
