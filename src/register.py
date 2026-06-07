"""Register Telegram users who message the bot.

Polls getUpdates, collects any chat_id that appears in a message or command,
and appends new IDs to data/users.json. Designed to run as a scheduled
GitHub Actions job and commit the updated file back to the repo.
"""
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

USERS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "users.json")


def load_users(path: str = USERS_FILE) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"chat_ids": [], "last_update_id": 0}


def save_users(data: dict, path: str = USERS_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _fetch_updates(token: str, offset: int) -> list[dict]:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        response = requests.get(url, params={"offset": offset + 1, "timeout": 0}, timeout=30)
        if response.status_code != 200:
            logger.error("getUpdates failed %d: %s", response.status_code, response.text)
            return []
        return response.json().get("result", [])
    except Exception as e:
        logger.error("Failed to fetch Telegram updates: %s", e)
        return []


def register_users() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return

    users_data = load_users()
    existing = set(users_data["chat_ids"])
    last_update_id = users_data.get("last_update_id", 0)

    updates = _fetch_updates(token, last_update_id)
    added = []

    for update in updates:
        last_update_id = max(last_update_id, update.get("update_id", 0))
        msg = update.get("message") or update.get("channel_post")
        if not msg:
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id and chat_id not in existing:
            existing.add(chat_id)
            added.append(chat_id)
            logger.info("New user registered: %s", chat_id)

    users_data["chat_ids"] = sorted(existing)
    users_data["last_update_id"] = last_update_id
    save_users(users_data)

    logger.info("Registration done. Added %d new user(s). Total: %d", len(added), len(existing))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    register_users()
