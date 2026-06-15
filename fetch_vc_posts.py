"""
Fetch posts from VC watchlist via Apify's LinkedIn profile posts scraper.

Unipile blocks profile-specific Voyager endpoints, and the regular feed
rarely surfaces target VC posts. So we use Apify's harvestapi actor
to pull recent posts directly from each VC profile URL.
"""
from __future__ import annotations
import json
import os
import re
from datetime import datetime, timezone, timedelta
from apify_client import ApifyClient
from config import APIFY_API_TOKEN, UNIPILE_ACCOUNT_ID, DATA_DIR

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "vc_watchlist.json")
VC_STATE_FILE = os.path.join(DATA_DIR, "vc_state.json")
APIFY_CACHE_FILE = os.path.join(DATA_DIR, "vc_apify_cache.json")

MAX_POST_AGE_HOURS = 72       # don't comment on posts older than this
APIFY_CACHE_TTL_HOURS = 4     # reuse Apify results within this window
ACTOR_LINKEDIN_POSTS = "harvestapi/linkedin-profile-posts"
POSTS_PER_PROFILE = 5         # fetch last N posts per VC


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


def _load_apify_cache() -> dict:
    if not os.path.exists(APIFY_CACHE_FILE):
        return {}
    try:
        with open(APIFY_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_apify_cache(data: dict):
    with open(APIFY_CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _normalize_post(item: dict, watchlist_entry: dict) -> dict | None:
    """Map a harvestapi post item to our internal post dict. Skips reposts."""
    if item.get("repostedBy"):
        return None

    text = item.get("content") or ""
    if not text or not text.strip():
        return None

    url = (item.get("linkedinUrl") or "").split("?")[0]
    if not url:
        urn = item.get("shareUrn") or item.get("id") or ""
        m = re.search(r"(\d{10,})", urn)
        if m:
            url = f"https://www.linkedin.com/feed/update/urn:li:activity:{m.group(1)}"
    if not url:
        return None

    engagement = item.get("engagement") or {}
    likes = engagement.get("likes") or engagement.get("reactions") or item.get("reactions") or 0
    comments = engagement.get("comments") or item.get("commentsCount") or 0
    if isinstance(likes, dict):
        likes = likes.get("totalCount", 0) or 0
    try:
        likes = int(likes)
    except (TypeError, ValueError):
        likes = 0
    try:
        comments = int(comments)
    except (TypeError, ValueError):
        comments = 0

    posted = item.get("postedAt") or {}
    if isinstance(posted, dict):
        posted_at = posted.get("date", "")
        if not posted_at and posted.get("timestamp"):
            try:
                posted_at = datetime.fromtimestamp(posted["timestamp"] / 1000, tz=timezone.utc).isoformat()
            except Exception:
                posted_at = ""
    else:
        posted_at = str(posted) if posted else ""

    images = item.get("postImages") or []
    image_url = ""
    content_type = "text"
    if images:
        first = images[0]
        image_url = first if isinstance(first, str) else first.get("url", "")
        if image_url:
            content_type = "image"
    elif item.get("article"):
        content_type = "article"

    return {
        "url": url,
        "author": watchlist_entry.get("name", "Unknown"),
        "author_title": f"{watchlist_entry.get('role','')} @ {watchlist_entry.get('fund','')}".strip(" @"),
        "author_url": watchlist_entry.get("linkedin_url", ""),
        "text": text,
        "likes": likes,
        "comments": comments,
        "posted_at": posted_at,
        "engagement_score": likes + 3 * comments,
        "hashtags": [],
        "content_type": content_type,
        "image_url": image_url,
        "source": "vc_watchlist",
        "vc_fund": watchlist_entry.get("fund", ""),
    }


def _fetch_via_apify(watchlist: list[dict]) -> list[dict]:
    """Run the Apify actor across all VC profile URLs."""
    cache = _load_apify_cache()
    cache_ts = cache.get("fetched_at")
    if cache_ts:
        try:
            age_h = (
                datetime.now(timezone.utc) - datetime.fromisoformat(cache_ts)
            ).total_seconds() / 3600
            if age_h < APIFY_CACHE_TTL_HOURS:
                print(f"  [VC] Using cached Apify results ({age_h:.1f}h old, {len(cache.get('items', []))} items)")
                return cache.get("items", [])
        except Exception:
            pass

    urls = [vc["linkedin_url"] for vc in watchlist if vc.get("linkedin_url")]
    print(f"  [VC] Running Apify actor for {len(urls)} profiles...")

    client = ApifyClient(APIFY_API_TOKEN)
    try:
        run = client.actor(ACTOR_LINKEDIN_POSTS).call(run_input={
            "profileUrls": urls,
            "maxPosts": POSTS_PER_PROFILE,
        }, timeout_secs=900)
    except Exception as e:
        print(f"  [VC] Apify error: {e}")
        return cache.get("items", []) if cache else []

    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    print(f"  [VC] Apify returned {len(items)} raw post items")

    _save_apify_cache({
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    })
    return items


def fetch_vc_posts(account_id: str = None) -> list[dict]:
    """
    Fetch recent posts from VC watchlist via Apify.
    Returns posts sorted by engagement score, skipping already-commented ones.
    """
    watchlist = load_watchlist()
    if not watchlist:
        print("  VC watchlist is empty.")
        return []

    vc_state = load_vc_state()
    by_vanity = {_url_to_key(vc.get("linkedin_url", "")): vc for vc in watchlist}

    raw_items = _fetch_via_apify(watchlist)
    if not raw_items:
        return []

    vc_posts = []
    for item in raw_items:
        # Match by author.publicIdentifier (vanity name) — most reliable
        author = item.get("author") or {}
        vanity = (author.get("publicIdentifier") or "").lower()
        if not vanity:
            vanity = _url_to_key(author.get("linkedinUrl", "") or item.get("query", ""))
        vc = by_vanity.get(vanity)
        if not vc:
            continue

        post = _normalize_post(item, vc)
        if not post:
            continue

        already_commented = set(vc_state.get(vanity, {}).get("commented_urls", []))
        if post["url"] in already_commented:
            continue

        if post["posted_at"]:
            try:
                age_h = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(post["posted_at"].replace("Z", "+00:00"))
                ).total_seconds() / 3600
                if age_h > MAX_POST_AGE_HOURS:
                    continue
            except Exception:
                pass

        vc_posts.append(post)

    vc_posts.sort(key=lambda p: p.get("engagement_score", 0), reverse=True)
    print(f"  [VC] Total publishable VC posts: {len(vc_posts)}")
    return vc_posts
