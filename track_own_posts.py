"""
Fetches Nick's own LinkedIn posts via Unipile and tracks their engagement over time.
Saves to own_posts_db.json. Refreshes stats for posts checked more than 24h ago.

Run: python track_own_posts.py
Auto-run: called after each LinkedIn session and from analyze_viral_posts.py
"""
import json
import os
import requests
from datetime import datetime, timezone, timedelta

from config import UNIPILE_API_KEY, UNIPILE_DSN, UNIPILE_ACCOUNT_ID, LINKEDIN_PROFILE_ID, DATA_DIR

OWN_POSTS_PATH = os.path.join(DATA_DIR, "own_posts_db.json")
REFRESH_AFTER_HOURS = 24
FETCH_LIMIT = 20  # posts per fetch


def _headers() -> dict:
    return {"X-API-KEY": UNIPILE_API_KEY, "Content-Type": "application/json"}


def _load_db() -> list[dict]:
    if not os.path.exists(OWN_POSTS_PATH):
        return []
    with open(OWN_POSTS_PATH) as f:
        return json.load(f)


def _save_db(db: list[dict]):
    db.sort(key=lambda p: p.get("likes", 0), reverse=True)
    with open(OWN_POSTS_PATH, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def _get_profile_id() -> str:
    """Return LINKEDIN_PROFILE_ID from env, or try to discover it via Unipile."""
    if LINKEDIN_PROFILE_ID:
        return LINKEDIN_PROFILE_ID
    # Try to get profile from account info
    try:
        resp = requests.get(
            f"{UNIPILE_DSN}/api/v1/accounts/{UNIPILE_ACCOUNT_ID}",
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            uid = data.get("linkedin_identifier") or data.get("identifier") or data.get("id")
            if uid:
                return str(uid)
    except Exception:
        pass
    return ""


def fetch_own_posts() -> list[dict]:
    """Fetch recent posts from Nick's LinkedIn profile."""
    profile_id = _get_profile_id()
    if not profile_id:
        print("  track_own_posts: LINKEDIN_PROFILE_ID not set and could not auto-discover.")
        return []

    try:
        resp = requests.get(
            f"{UNIPILE_DSN}/api/v1/users/{profile_id}/posts",
            headers=_headers(),
            params={"account_id": UNIPILE_ACCOUNT_ID, "limit": FETCH_LIMIT},
            timeout=20,
        )
        resp.raise_for_status()
        items = resp.json().get("items", resp.json() if isinstance(resp.json(), list) else [])
    except Exception as e:
        print(f"  track_own_posts: fetch failed: {e}")
        return []

    posts = []
    for item in items:
        social_id = item.get("social_id") or item.get("id", "")
        text = (item.get("text") or item.get("commentary") or "").strip()
        if not text:
            continue
        posts.append({
            "social_id": social_id,
            "text": text[:600],
            "likes": item.get("reaction_counter", item.get("likes", 0)) or 0,
            "comments": item.get("comment_counter", item.get("comments", 0)) or 0,
            "reposts": item.get("repost_counter", 0) or 0,
            "impressions": item.get("impressions_counter", 0) or 0,
            "posted_at": item.get("created_at") or item.get("published_at") or "",
            "last_checked": datetime.now(timezone.utc).isoformat(),
        })
    return posts


def refresh_post_stats(post: dict) -> dict:
    """Re-fetch engagement stats for a single post."""
    social_id = post.get("social_id", "")
    if not social_id:
        return post
    try:
        resp = requests.get(
            f"{UNIPILE_DSN}/api/v1/posts/{social_id}",
            headers=_headers(),
            params={"account_id": UNIPILE_ACCOUNT_ID},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            post["likes"] = data.get("reaction_counter", post["likes"]) or post["likes"]
            post["comments"] = data.get("comment_counter", post["comments"]) or post["comments"]
            post["reposts"] = data.get("repost_counter", post.get("reposts", 0)) or 0
            post["impressions"] = data.get("impressions_counter", post.get("impressions", 0)) or 0
            post["last_checked"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        print(f"  refresh failed for {social_id}: {e}")
    return post


def _needs_refresh(post: dict) -> bool:
    last = post.get("last_checked", "")
    if not last:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
        return age > timedelta(hours=REFRESH_AFTER_HOURS)
    except Exception:
        return True


def run(silent: bool = False) -> list[dict]:
    """
    Main entry point. Fetches new posts and refreshes stale stats.
    Returns updated DB.
    """
    db = _load_db()
    existing_ids = {p["social_id"] for p in db}

    if not silent:
        print("  Fetching own LinkedIn posts...")

    new_posts = fetch_own_posts()
    added = 0
    for p in new_posts:
        if p["social_id"] and p["social_id"] not in existing_ids:
            db.append(p)
            existing_ids.add(p["social_id"])
            added += 1

    # Refresh stale stats for existing posts
    refreshed = 0
    for i, post in enumerate(db):
        if _needs_refresh(post):
            db[i] = refresh_post_stats(post)
            refreshed += 1

    _save_db(db)

    if not silent:
        print(f"  Own posts: {added} new, {refreshed} refreshed, {len(db)} total → own_posts_db.json")

    return db


def load_own_posts_for_prompt() -> str:
    """Return formatted string of Nick's top performing posts for injection into prompts."""
    db = _load_db()
    if not db:
        return ""

    top = sorted(db, key=lambda p: p.get("likes", 0) + 3 * p.get("comments", 0), reverse=True)[:10]

    lines = ["## Nick's Own Top-Performing Posts (for style reference)\n"]
    for p in top:
        likes = p.get("likes", 0)
        comments = p.get("comments", 0)
        text = p.get("text", "").replace("\n", " ").strip()[:300]
        lines.append(f"- {likes}L {comments}C | {text}")

    return "\n".join(lines)


if __name__ == "__main__":
    run()
