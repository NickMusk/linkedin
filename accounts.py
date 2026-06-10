"""
Multi-account management.

Unipile is the source of truth for connected LinkedIn accounts.
This module lists them and manages per-account local config/state
(knowledge base path, daily cap, active flag, session tracking).
"""
import json
import os
import requests
from config import UNIPILE_API_KEY, UNIPILE_DSN, DATA_DIR

ACCOUNTS_DIR = os.path.join(DATA_DIR, "accounts")


def list_linkedin_accounts() -> list[dict]:
    """Return all LinkedIn accounts connected in Unipile."""
    resp = requests.get(
        f"{UNIPILE_DSN}/api/v1/accounts",
        headers={"X-API-KEY": UNIPILE_API_KEY},
        params={"limit": 100},
        timeout=15,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [a for a in items if a.get("type") == "LINKEDIN"]


def _account_dir(account_id: str) -> str:
    d = os.path.join(ACCOUNTS_DIR, account_id)
    os.makedirs(d, exist_ok=True)
    return d


def get_account_config(account_id: str) -> dict:
    """Load local config for an account. Returns defaults if not yet configured."""
    path = os.path.join(_account_dir(account_id), "config.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {
        "name": account_id,
        "kb_path": None,          # path to knowledge base .md file; None = use default
        "system_prompt_path": None,  # path to system prompt .txt; None = use default
        "daily_cap": 10,
        "min_likes": 0,           # no threshold — comment on any post in feed
        "active": True,
    }


def save_account_config(account_id: str, config: dict):
    path = os.path.join(_account_dir(account_id), "config.json")
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def get_account_state(account_id: str) -> dict:
    path = os.path.join(_account_dir(account_id), "state.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {"date": "", "count": 0, "last_session_ts": 0}


def save_account_state(account_id: str, state: dict):
    path = os.path.join(_account_dir(account_id), "state.json")
    with open(path, "w") as f:
        json.dump(state, f)
