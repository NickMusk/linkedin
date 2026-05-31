import json
import os
import requests
from datetime import datetime, timezone, timedelta
from config import UNIPILE_API_KEY, UNIPILE_DSN, UNIPILE_ACCOUNT_ID, MIN_LIKES, DATA_DIR

MAX_POST_AGE_HOURS = 48  # skip posts older than this
SEEN_URLS_FILE = os.path.join(DATA_DIR, "seen_urls.json")
PUBLISHED_URLS_FILE = os.path.join(DATA_DIR, "published_urls.json")

FEED_URL = "https://www.linkedin.com/voyager/api/graphql?queryId=voyagerFeedDashMainFeed.7a50ef8ba5a7865c23ad5df46f735709"
FEED_BATCH = 10  # LinkedIn returns 10 per page max


def _load_seen_urls() -> set:
    return set()


def _load_published_urls() -> set:
    if not os.path.exists(PUBLISHED_URLS_FILE):
        return set()
    with open(PUBLISHED_URLS_FILE) as f:
        return set(json.load(f))


def mark_url_published(url: str):
    published = _load_published_urls()
    published.add(url)
    with open(PUBLISHED_URLS_FILE, "w") as f:
        json.dump(list(published), f)



def _unipile_feed_page(pagination_token: str = None) -> tuple:
    """Fetch one page of feed items. Returns (elements, next_pagination_token)."""
    url = FEED_URL + f"&count={FEED_BATCH}"
    if pagination_token:
        url += f"&paginationToken={pagination_token}"

    headers = {
        "X-API-KEY": UNIPILE_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{UNIPILE_DSN}/api/v1/linkedin",
        headers=headers,
        json={"account_id": UNIPILE_ACCOUNT_ID, "method": "GET", "request_url": url},
        timeout=30,
    )
    resp.raise_for_status()
    feed = resp.json()["data"]["data"]["feedDashMainFeedByMainFeed"]
    next_token = feed.get("metadata", {}).get("paginationToken")
    return feed.get("elements", []), next_token


def _extract_text(node) -> str:
    if isinstance(node, dict):
        return node.get("text", "")
    return ""


def _detect_content_type(el: dict) -> str:
    """Return content type: image, document, video, article, or text."""
    content = el.get("content") or {}
    if content.get("imageComponent"):
        return "image"
    if content.get("documentComponent"):
        return "document"
    if content.get("linkedInVideoComponent") or content.get("externalVideoComponent"):
        return "video"
    if content.get("articleComponent"):
        return "article"
    return "text"


def _fetch_attachment_url(activity_id: str) -> str:
    """Fetch image URL for a post via Unipile's native post endpoint."""
    try:
        resp = requests.get(
            f"{UNIPILE_DSN}/api/v1/posts/{activity_id}",
            headers={"X-API-KEY": UNIPILE_API_KEY},
            params={"account_id": UNIPILE_ACCOUNT_ID},
            timeout=10,
        )
        if resp.status_code != 200:
            return ""
        attachments = resp.json().get("attachments", [])
        for att in attachments:
            if att.get("type") == "img" and att.get("url") and not att.get("unavailable"):
                return att["url"]
    except Exception:
        pass
    return ""


def _normalize(el: dict):
    commentary = el.get("commentary") or {}
    text = _extract_text(commentary.get("text") or {})
    if not text.strip():
        return None

    url = (el.get("socialContent") or {}).get("shareUrl", "")
    if not url:
        return None

    actor = el.get("actor") or {}
    author_name = _extract_text(actor.get("name") or {})
    author_title = _extract_text(actor.get("description") or {})

    counts = ((el.get("socialDetail") or {}).get("totalSocialActivityCounts") or {})
    likes = counts.get("numLikes", 0) or 0
    comments = counts.get("numComments", 0) or 0

    # Extract post timestamp (LinkedIn returns Unix ms in created.time)
    created = el.get("created") or {}
    created_ms = created.get("time") or el.get("createdAt") or 0
    if created_ms:
        posted_at = datetime.fromtimestamp(int(created_ms) / 1000, tz=timezone.utc).isoformat()
    else:
        posted_at = ""

    # Engagement score: comments weighted 3x (they signal real interaction)
    engagement_score = likes + 3 * comments

    # Clean share URL to a canonical post URL
    clean_url = url.split("?")[0]

    return {
        "url": clean_url,
        "author": author_name or "Unknown",
        "author_title": author_title,
        "author_url": "",
        "text": text,
        "likes": likes,
        "comments": comments,
        "posted_at": posted_at,
        "engagement_score": engagement_score,
        "hashtags": [],
        "content_type": _detect_content_type(el),
        "image_url": "",
        "source": "feed",
    }


def fetch_feed_posts(target: int = 30) -> list[dict]:
    seen_urls = _load_seen_urls()
    published_urls = _load_published_urls()
    posts = []
    pagination_token = None
    pages_fetched = 0
    max_pages = 10  # safety cap

    while len(posts) < target and pages_fetched < max_pages:
        try:
            elements, pagination_token = _unipile_feed_page(pagination_token)
        except Exception as e:
            print(f"  Feed fetch error (page {pages_fetched + 1}): {e}")
            break

        pages_fetched += 1
        new_this_page = 0

        for el in elements:
            post = _normalize(el)
            if not post:
                continue
            url = post["url"]
            if url in seen_urls or url in published_urls:
                continue
            if post["likes"] < MIN_LIKES:
                continue
            # Skip posts older than MAX_POST_AGE_HOURS
            if post["posted_at"]:
                post_time = datetime.fromisoformat(post["posted_at"])
                age_hours = (datetime.now(timezone.utc) - post_time).total_seconds() / 3600
                if age_hours > MAX_POST_AGE_HOURS:
                    continue
            seen_urls.add(url)
            posts.append(post)
            new_this_page += 1

        print(f"  Page {pages_fetched}: +{new_this_page} posts (total {len(posts)})")

        if not pagination_token:
            break
    # Sort by engagement score descending so best posts get commented on first
    posts.sort(key=lambda p: p.get("engagement_score", 0), reverse=True)
    posts = posts[:target]

    # Enrich top image posts with accessible image URLs (top 10 only to limit API calls)
    import re as _re
    enriched = 0
    for post in posts[:10]:
        if post.get("content_type") != "image":
            continue
        m = _re.search(r"activity[:\-](\d+)", post["url"])
        if not m:
            continue
        img_url = _fetch_attachment_url(m.group(1))
        if img_url:
            post["image_url"] = img_url
            enriched += 1
    if enriched:
        print(f"  Enriched {enriched} posts with image URLs")

    return posts


def fetch_all_posts() -> list[dict]:
    print("Fetching LinkedIn feed posts via Unipile...")
    posts = fetch_feed_posts(target=30)
    print(f"Total posts fetched: {len(posts)}")
    return posts
