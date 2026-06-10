"""
Fetch posts from VC watchlist by filtering the regular LinkedIn feed.

Instead of trying to hit profile-specific endpoints (rejected by Unipile),
we fetch a large feed page and keep only posts from people in the watchlist.
"""
import json
import os
import re
import requests
from datetime import datetime, timezone
from config import UNIPILE_API_KEY, UNIPILE_DSN, UNIPILE_ACCOUNT_ID, DATA_DIR

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "vc_watchlist.json")
VC_STATE_FILE = os.path.join(DATA_DIR, "vc_state.json")

MAX_POST_AGE_HOURS = 72  # wider window — VCs post less often


def load_watchlist() -> list[dict]:
    if not os.path.exists(WATCHLIST_FILE):
        return []
    with open(WATCHLIST_FILE) as f:
        return json.load(f)


def load_vc_state() -> dict:
    if not os.path.exists(VC_STATE_FILE):
        return {}
    with open(VC_STATE_FILE) as f:
        return json.load(f)


def save_vc_state(state: dict):
    with open(VC_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def record_vc_interaction(linkedin_url: str, post_url: str):
    state = load_vc_state()
    key = _url_to_key(linkedin_url)
    entry = state.get(key, {"interaction_count": 0, "commented_urls": []})
    entry["interaction_count"] = entry.get("interaction_count", 0) + 1
    entry["last_commented_at"] = datetime.now(timezone.utc).isoformat()
    commented = entry.get("commented_urls", [])
    if post_url not in commented:
        commented.append(post_url)
    entry["commented_urls"] = commented[-50:]
    state[key] = entry
    save_vc_state(state)


def _url_to_key(linkedin_url: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", linkedin_url)
    return m.group(1).lower() if m else linkedin_url.lower()


def _build_vanity_index(watchlist: list[dict]) -> dict:
    """Return {vanity_name_lower: vc_dict} for fast lookup."""
    index = {}
    for vc in watchlist:
        key = _url_to_key(vc.get("linkedin_url", ""))
        if key:
            index[key] = vc
    return index


def fetch_vc_posts(account_id: str = None) -> list[dict]:
    """
    Fetch recent posts from VC watchlist by filtering a large feed pull.
    Returns posts sorted by engagement score, skipping already-commented ones.
    """
    from fetch_posts import fetch_feed_posts

    account_id = account_id or UNIPILE_ACCOUNT_ID
    watchlist = load_watchlist()
    if not watchlist:
        print("  VC watchlist is empty.")
        return []

    vanity_index = _build_vanity_index(watchlist)
    vc_state = load_vc_state()

    print(f"  [VC] Fetching feed to find posts from {len(watchlist)} watched VCs...")
    # Fetch a large batch — no min_likes filter
    feed_posts = fetch_feed_posts(target=50, account_id=account_id, min_likes=0)
    print(f"  [VC] Feed returned {len(feed_posts)} posts, scanning for VC authors...")

    vc_posts = []
    for post in feed_posts:
        author_url = post.get("author_url", "")
        vanity = _url_to_key(author_url) if author_url else ""
        if not vanity or vanity not in vanity_index:
            continue

        vc = vanity_index[vanity]
        key = vanity
        already_commented = set(vc_state.get(key, {}).get("commented_urls", []))
        if post["url"] in already_commented:
            continue

        if post.get("posted_at"):
            try:
                age_h = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(post["posted_at"])
                ).total_seconds() / 3600
                if age_h > MAX_POST_AGE_HOURS:
                    continue
            except Exception:
                pass

        # Tag with VC metadata and mark source
        post["source"] = "vc_watchlist"
        post["vc_fund"] = vc.get("fund", "")
        vc_posts.append(post)
        print(f"  [VC] Found: {post['author']} ({vc.get('fund', '')})")

    vc_posts.sort(key=lambda p: p.get("engagement_score", 0), reverse=True)
    print(f"  [VC] Total new VC posts: {len(vc_posts)}")
    return vc_posts
