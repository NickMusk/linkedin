"""
Fetch recent posts from specific LinkedIn profiles (VC watchlist).

Uses Unipile's LinkedIn proxy to call the Voyager API directly by vanity name,
so no profile ID resolution step is needed.
"""
import json
import os
import re
import requests
from datetime import datetime, timezone
from config import UNIPILE_API_KEY, UNIPILE_DSN, UNIPILE_ACCOUNT_ID, DATA_DIR

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "vc_watchlist.json")
VC_STATE_FILE = os.path.join(DATA_DIR, "vc_state.json")

MAX_POST_AGE_HOURS = 72  # VCs post less often — wider window
MAX_POSTS_PER_VC = 5


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
    """Call this after successfully posting a comment on a VC's post."""
    state = load_vc_state()
    key = _url_to_key(linkedin_url)
    entry = state.get(key, {"interaction_count": 0, "commented_urls": []})
    entry["interaction_count"] = entry.get("interaction_count", 0) + 1
    entry["last_commented_at"] = datetime.now(timezone.utc).isoformat()
    commented = entry.get("commented_urls", [])
    if post_url not in commented:
        commented.append(post_url)
    entry["commented_urls"] = commented[-50:]  # keep last 50
    state[key] = entry
    save_vc_state(state)


def _url_to_key(linkedin_url: str) -> str:
    """Extract vanity name from URL as a stable dict key."""
    m = re.search(r"linkedin\.com/in/([^/?#]+)", linkedin_url)
    return m.group(1).lower() if m else linkedin_url.lower()


def _vanity_from_url(linkedin_url: str) -> str | None:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", linkedin_url)
    return m.group(1).rstrip("/") if m else None


def _proxy_get(request_url: str, account_id: str) -> dict | None:
    """Make a GET request through Unipile's LinkedIn proxy."""
    try:
        resp = requests.post(
            f"{UNIPILE_DSN}/api/v1/linkedin",
            headers={"X-API-KEY": UNIPILE_API_KEY, "Content-Type": "application/json"},
            json={"account_id": account_id, "method": "GET", "request_url": request_url},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  Proxy error for {request_url[:80]}: {e}")
        return None


def _extract_text(node) -> str:
    if isinstance(node, dict):
        return node.get("text", "")
    return ""


def _fetch_profile_posts(vanity: str, account_id: str) -> list[dict]:
    """Fetch recent posts from a LinkedIn profile by vanity name."""
    url = (
        "https://www.linkedin.com/voyager/api/v2/memberDashProfileContributions"
        f"?count={MAX_POSTS_PER_VC}&memberIdentity={vanity}&moduleKey=creator"
        "&q=contributions&start=0"
    )
    data = _proxy_get(url, account_id)
    if not data:
        return []

    elements = (
        data.get("elements")
        or data.get("data", {}).get("elements")
        or []
    )
    return elements


def _normalize_contribution(el: dict, author_name: str, author_url: str) -> dict | None:
    """Parse a profileContributions element into our standard post dict."""
    # The element wraps an updateV2 or similar
    entity = el.get("*contribution") or el.get("contribution") or el

    commentary = entity.get("commentary") or {}
    text = _extract_text(commentary.get("text") or {})
    if not text:
        text = _extract_text(entity.get("text") or {})
    if not text.strip():
        return None

    share_url = (entity.get("socialContent") or {}).get("shareUrl", "")
    if not share_url:
        # try nested
        share_url = entity.get("permalink", "")
    if not share_url:
        return None

    counts = (entity.get("socialDetail") or {}).get("totalSocialActivityCounts") or {}
    likes = counts.get("numLikes", 0) or 0
    comments_count = counts.get("numComments", 0) or 0

    created_ms = (entity.get("created") or {}).get("time") or entity.get("createdAt") or 0
    if created_ms:
        posted_at = datetime.fromtimestamp(int(created_ms) / 1000, tz=timezone.utc).isoformat()
    else:
        posted_at = ""

    clean_url = share_url.split("?")[0]

    return {
        "url": clean_url,
        "author": author_name,
        "author_title": "",
        "author_url": author_url,
        "text": text,
        "likes": likes,
        "comments": comments_count,
        "posted_at": posted_at,
        "engagement_score": likes + 3 * comments_count,
        "hashtags": [],
        "content_type": "text",
        "image_url": "",
        "source": "vc_watchlist",
    }


def fetch_vc_posts(account_id: str = None) -> list[dict]:
    """
    Fetch recent posts from all VCs in the watchlist.
    Skips posts already commented on.
    Returns posts sorted by engagement score.
    """
    account_id = account_id or UNIPILE_ACCOUNT_ID
    watchlist = load_watchlist()
    if not watchlist:
        print("  VC watchlist is empty.")
        return []

    vc_state = load_vc_state()
    all_posts = []

    for vc in watchlist:
        name = vc.get("name", "Unknown")
        li_url = vc.get("linkedin_url", "")
        vanity = _vanity_from_url(li_url)
        if not vanity:
            print(f"  [VC] Could not parse vanity from URL: {li_url}")
            continue

        key = _url_to_key(li_url)
        already_commented = set(vc_state.get(key, {}).get("commented_urls", []))

        print(f"  [VC] Fetching posts for {name} (@{vanity})...")
        elements = _fetch_profile_posts(vanity, account_id)
        if not elements:
            print(f"  [VC] No posts found for {name}")
            continue

        count = 0
        for el in elements:
            post = _normalize_contribution(el, author_name=name, author_url=li_url)
            if not post:
                continue
            if post["url"] in already_commented:
                continue
            if post["posted_at"]:
                try:
                    age_h = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(post["posted_at"])
                    ).total_seconds() / 3600
                    if age_h > MAX_POST_AGE_HOURS:
                        continue
                except Exception:
                    pass
            all_posts.append(post)
            count += 1

        print(f"  [VC] {name}: {count} new post(s)")

    all_posts.sort(key=lambda p: p.get("engagement_score", 0), reverse=True)
    print(f"  [VC] Total new VC posts: {len(all_posts)}")
    return all_posts
