"""Growth lane: poll the curated author list for tweets younger than 30 minutes.

Reach economics: a tweet earns ~80% of its lifetime impressions in the first
two hours, so a reply only works when it lands in the first ~30 minutes.
This fetcher is built for that window — called every POLL_MINUTES from
growth_loop, it returns ONLY fresh tweets by listed authors, no like threshold
(a 10-minute-old banger has 12 likes).

Apify cost control: authors are batched 10-per-search-term (from:a OR from:b),
maxItems kept tight, and the loop itself only polls during US waking hours.
"""
import json
import os
import logging
from datetime import datetime, timezone, timedelta

from apify_client import ApifyClient
from config import APIFY_API_TOKEN
from fetch_tweets import _parse_tweet_date, _normalize, _load_seen_tweets, _save_seen_tweets

log = logging.getLogger(__name__)

AUTHORS_FILE = os.path.join(os.path.dirname(__file__), "growth_authors.json")
ACTOR = "apidojo/tweet-scraper"
MAX_TWEET_AGE_MIN = 30
BATCH = 10          # authors per search term
PER_TERM = 15       # maxItems per term


def load_authors() -> list:
    try:
        with open(AUTHORS_FILE) as f:
            return [a.strip().lstrip("@") for a in json.load(f)["authors"] if a.strip()]
    except Exception as e:
        log.warning(f"growth authors load failed: {e}")
        return []


def fetch_growth_tweets() -> list:
    authors = load_authors()
    if not authors:
        return []
    client = ApifyClient(APIFY_API_TOKEN)
    seen = _load_seen_tweets()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=MAX_TWEET_AGE_MIN)
    terms = [
        "(" + " OR ".join(f"from:{a}" for a in authors[i:i + BATCH]) + ") -filter:replies"
        for i in range(0, len(authors), BATCH)
    ]
    tweets = []
    try:
        run = client.actor(ACTOR).call(run_input={
            "searchTerms": terms,
            "maxItems": PER_TERM * len(terms),
            "addUserInfo": True,
            "scrapeTweetReplies": False,
            "sort": "Latest",
        })
        for item in client.dataset(run["defaultDatasetId"]).iterate_items():
            url = item.get("url") or item.get("tweetUrl", "")
            created = item.get("createdAt", "") or item.get("created_at", "")
            dt = _parse_tweet_date(created)
            if not url or url in seen or dt is None or dt < cutoff:
                continue
            seen.add(url)
            t = _normalize(item, "growth")
            t["lane"] = "growth"
            tweets.append(t)
    except Exception as e:
        log.warning(f"growth fetch failed: {type(e).__name__}: {e}")
        return []
    _save_seen_tweets(seen)
    log.info(f"growth fetch: {len(tweets)} fresh tweets (<{MAX_TWEET_AGE_MIN}m)")
    return tweets
