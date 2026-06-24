import time
import json
import os
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from apify_client import ApifyClient
from config import APIFY_API_TOKEN, TWITTER_AUTH_TOKEN, TWITTER_CT0


def _parse_tweet_date(created: str):
    """Parse a tweet timestamp robustly. The apidojo scraper returns Twitter's
    native format ('Tue Jun 23 19:05:56 +0000 2026'), which datetime.fromisoformat
    does NOT accept — relying on it silently broke the age filter (it parsed on
    newer Python in unexpected ways / failed on older). Try ISO first, then the
    Twitter/RFC-822 format. Returns an aware datetime or None if unparseable."""
    if not created:
        return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00"))
    except Exception:
        pass
    try:
        return parsedate_to_datetime(created)
    except Exception:
        return None

MAX_TWEET_AGE_DAYS = 2
SEEN_TWEETS_FILE = os.path.join(os.path.dirname(__file__), "seen_tweets.json")

TWITTER_KEYWORDS = [
    "venture capital founder",
    "VC startup advice",
    "product market fit startup",
    "fundraising pre-seed seed",
    "AI startup building",
    "founder lessons learned",
    "startup growth B2B SaaS",
    "angel investor startup",
]

TWEETS_PER_KEYWORD = 25
MIN_LIKES = 10

ACTOR_TWITTER = "apidojo/tweet-scraper"


def _load_seen_tweets() -> set:
    if not os.path.exists(SEEN_TWEETS_FILE):
        return set()
    with open(SEEN_TWEETS_FILE) as f:
        data = json.load(f)
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    if isinstance(data, list):
        return set(data)
    return {url for url, ts in data.items()
            if datetime.fromisoformat(ts) >= cutoff}


def _save_seen_tweets(seen: set):
    existing = {}
    if os.path.exists(SEEN_TWEETS_FILE):
        with open(SEEN_TWEETS_FILE) as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            existing = raw
    now = datetime.now(timezone.utc).isoformat()
    merged = {url: existing.get(url, now) for url in seen}
    with open(SEEN_TWEETS_FILE, "w") as f:
        json.dump(merged, f)


# Diagnostic snapshot of the most recent fetch_tweets() run. Lets callers tell
# "Twitter returned nothing because there was nothing new" apart from
# "the scrape failed entirely (expired cookies / Apify / network)".
LAST_FETCH = {"keywords": 0, "errors": 0, "raw_items": 0, "auth_suspect": False}


def fetch_tweets() -> list[dict]:
    client = ApifyClient(APIFY_API_TOKEN)
    tweets = []
    seen = _load_seen_tweets()
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_TWEET_AGE_DAYS)

    errors = 0
    raw_items = 0

    for keyword in TWITTER_KEYWORDS:
        print(f"  Fetching tweets: {keyword}")
        try:
            run = client.actor(ACTOR_TWITTER).call(run_input={
                "searchTerms": [keyword],
                "maxItems": TWEETS_PER_KEYWORD,
                "addUserInfo": True,
                "scrapeTweetReplies": False,
                "sort": "Top",
                "cookie": [
                    {"name": "auth_token", "value": TWITTER_AUTH_TOKEN},
                    {"name": "ct0", "value": TWITTER_CT0},
                ],
            })
            for item in client.dataset(run["defaultDatasetId"]).iterate_items():
                raw_items += 1
                url = item.get("url") or item.get("tweetUrl", "")
                likes = item.get("likeCount", 0) or item.get("favoriteCount", 0) or 0
                created = item.get("createdAt", "") or item.get("created_at", "")
                tweet_date = _parse_tweet_date(created)
                if tweet_date is not None and tweet_date < cutoff:
                    continue
                if url and url not in seen and likes >= MIN_LIKES:
                    seen.add(url)
                    tweets.append(_normalize(item, keyword))
        except Exception as e:
            errors += 1
            print(f"  Warning: failed for '{keyword}': {e}")
        time.sleep(2)

    # Every keyword failed, or the scraper returned zero raw items across all of
    # them: that is a systemic failure (most often dead Twitter cookies), not a
    # quiet feed. Surface it so the loop can flag auth health.
    auth_suspect = (errors == len(TWITTER_KEYWORDS)) or (raw_items == 0)
    LAST_FETCH.update(keywords=len(TWITTER_KEYWORDS), errors=errors,
                      raw_items=raw_items, auth_suspect=auth_suspect)
    if auth_suspect:
        print(f"  [fetch_tweets] SYSTEMIC FAILURE: {errors}/{len(TWITTER_KEYWORDS)} "
              f"keywords errored, {raw_items} raw items. Twitter cookies likely expired.")

    _save_seen_tweets(seen)
    return tweets


def _extract_media(item: dict) -> tuple[str, str]:
    """Return (image_url, content_type) from raw Apify tweet item."""
    media_list = item.get("media") or []
    ext = (item.get("extendedEntities") or {}).get("media") or []

    # Check extended entities for type info
    for m in ext:
        mtype = m.get("type", "")
        url = m.get("media_url_https", "")
        if mtype == "photo" and url:
            return url + "?format=jpg&name=medium", "image"
        if mtype in ("video", "animated_gif") and url:
            return url, "video"

    # Fallback to simple media array
    for url in media_list:
        if isinstance(url, str) and url:
            ctype = "video" if "video_thumb" in url else "image"
            return url, ctype

    # Check for article/card
    if item.get("card"):
        return "", "article"

    return "", "text"


def _normalize(item: dict, keyword: str) -> dict:
    author = item.get("author") or {}
    if isinstance(author, dict):
        author_name = author.get("name") or author.get("userName", "Unknown")
        author_username = author.get("userName", "")
    else:
        author_name = item.get("authorName", "Unknown")
        author_username = item.get("authorUsername", "")

    image_url, content_type = _extract_media(item)

    return {
        "url": item.get("url") or item.get("tweetUrl", ""),
        "tweet_id": item.get("id") or item.get("tweetId", ""),
        "author": author_name,
        "author_username": author_username,
        "text": item.get("text") or item.get("fullText", ""),
        "likes": item.get("likeCount") or item.get("favoriteCount") or 0,
        "replies": item.get("replyCount", 0),
        "retweets": item.get("retweetCount", 0),
        "posted_at": item.get("createdAt") or item.get("created_at", ""),
        "content_type": content_type,
        "image_url": image_url,
        "source": f"keyword:{keyword}",
    }
