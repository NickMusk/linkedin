"""LinkedIn growth lane fetcher: fresh posts (<90 min) of big authors.

Reuses the harvestapi actor and post normalizer from fetch_vc_posts; the
difference is the author list (li_growth_authors.json), the freshness window
(minutes, not days) and no like threshold — we are racing the post's surge,
not measuring it.
"""
import json
import os
import logging
from datetime import datetime, timezone, timedelta

from apify_client import ApifyClient
from config import APIFY_API_TOKEN
from fetch_vc_posts import _normalize_post, ACTOR_LINKEDIN_POSTS

log = logging.getLogger(__name__)

AUTHORS_FILE = os.path.join(os.path.dirname(__file__), "li_growth_authors.json")
MAX_POST_AGE_MIN = 90
POSTS_PER_PROFILE = 3


def load_li_growth_authors() -> list:
    try:
        with open(AUTHORS_FILE) as f:
            return json.load(f)["authors"]
    except Exception as e:
        log.warning(f"li growth authors load failed: {e}")
        return []


def fetch_li_growth_posts() -> list:
    authors = load_li_growth_authors()
    if not authors:
        return []
    client = ApifyClient(APIFY_API_TOKEN)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=MAX_POST_AGE_MIN)
    posts = []
    try:
        run = client.actor(ACTOR_LINKEDIN_POSTS).call(run_input={
            "profileUrls": [a["linkedin_url"] for a in authors],
            "maxPosts": POSTS_PER_PROFILE,
        }, timeout_secs=900)
        by_key = {a["linkedin_url"].rstrip("/").split("/")[-1].lower(): a for a in authors}
        for item in client.dataset(run["defaultDatasetId"]).iterate_items():
            # match by author.publicIdentifier — same approach as the VC fetcher
            author = item.get("author") or {}
            key = (author.get("publicIdentifier") or "").lower()
            if not key:
                key = (author.get("linkedinUrl", "") or item.get("query", "")).rstrip("/").split("/")[-1].lower()
            entry = by_key.get(key, {"name": (author.get("name") or "?")})
            p = _normalize_post(item, entry)
            if not p:
                continue
            try:
                posted = datetime.fromisoformat((p.get("posted_at") or "").replace("Z", "+00:00"))
                if posted.tzinfo is None:
                    posted = posted.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if posted < cutoff:
                continue
            p["lane"] = "li_growth"
            posts.append(p)
    except Exception as e:
        log.warning(f"li growth fetch failed: {type(e).__name__}: {e}")
        return []
    log.info(f"li growth fetch: {len(posts)} fresh posts (<{MAX_POST_AGE_MIN}m)")
    return posts
