import time
import json
import os
from datetime import datetime, timezone, timedelta
from apify_client import ApifyClient
from config import APIFY_API_TOKEN, TWITTER_AUTH_TOKEN, TWITTER_CT0

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


def fetch_tweets() -> list[dict]:
    client = ApifyClient(APIFY_API_TOKEN)
    tweets = []
    seen = _load_seen_tweets()
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_TWEET_AGE_DAYS)

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
                url = item.get("url") or item.get("tweetUrl", "")
                likes = item.get("likeCount", 0) or item.get("favoriteCount", 0) or 0
                created = item.get("createdAt", "") or item.get("created_at", "")
                try:
                    tweet_date = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if tweet_date < cutoff:
                        continue
                except Exception:
                    pass
                if url and url not in seen and likes >= MIN_LIKES:
                    seen.add(url)
                    tweets.append(_normalize(item, keyword))
        except Exception as e:
            print(f"  Warning: failed for '{keyword}': {e}")
        time.sleep(2)

    _save_seen_tweets(seen)
    return tweets


def _normalize(item: dict, keyword: str) -> dict:
    author = item.get("author") or {}
    if isinstance(author, dict):
        author_name = author.get("name") or author.get("userName", "Unknown")
        author_username = author.get("userName", "")
    else:
        author_name = item.get("authorName", "Unknown")
        author_username = item.get("authorUsername", "")

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
        "source": f"keyword:{keyword}",
    }
