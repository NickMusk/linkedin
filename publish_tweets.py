import re
import time
import random
import logging
import requests
from pathlib import Path
from datetime import datetime, timezone

from config import TWITTER_AUTH_TOKEN, TWITTER_CT0

log = logging.getLogger(__name__)

BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
QUERY_ID = "c50A_puUoQGK_4SXseYz3A"
CREATE_TWEET_URL = f"https://x.com/i/api/graphql/{QUERY_ID}/CreateTweet"

SKIP_PHRASES = [
    "Could you share what the tweet says",
    "The tweet only contains a URL",
]


def _extract_tweet_id(url: str) -> str:
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else ""


def _post_reply(tweet_id: str, text: str) -> tuple:
    headers = {
        "authorization": f"Bearer {BEARER_TOKEN}",
        "x-csrf-token": TWITTER_CT0,
        "cookie": f"auth_token={TWITTER_AUTH_TOKEN}; ct0={TWITTER_CT0}",
        "content-type": "application/json",
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "en",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    body = {
        "variables": {
            "tweet_text": text,
            "reply": {
                "in_reply_to_tweet_id": tweet_id,
                "exclude_reply_user_ids": [],
            },
            "dark_request": False,
            "media": {"media_entities": [], "possibly_sensitive": False},
            "semantic_annotation_ids": [],
        },
        "features": {
            "tweetypie_unmention_optimization_enabled": True,
            "responsive_web_edit_tweet_api_enabled": True,
            "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
            "view_counts_everywhere_api_enabled": True,
            "longform_notetweets_consumption_enabled": True,
            "responsive_web_twitter_article_tweet_consumption_enabled": False,
            "tweet_awards_web_tipping_enabled": False,
            "freedom_of_speech_not_reach_label_enabled": False,
            "standardized_nudges_misinfo": True,
            "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
            "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": True,
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_enhance_cards_enabled": False,
        },
        "queryId": QUERY_ID,
    }
    try:
        resp = requests.post(CREATE_TWEET_URL, headers=headers, json=body, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            result = (data.get("data") or {}).get("create_tweet", {}).get("tweet_results", {}).get("result", {})
            new_id = result.get("rest_id", "")
            return True, f"https://x.com/i/status/{new_id}" if new_id else "posted"
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return False, str(e)


def run_twitter_session(settings: dict, log_fn=None, update_status_fn=None) -> int:
    """
    Full Twitter session: fetch → generate → auto-publish.
    Returns number of replies posted.
    """
    from fetch_tweets import fetch_tweets
    from generate_replies import generate_replies
    from knowledge_base import build_context, save_tweet_example

    if update_status_fn:
        update_status_fn(state="fetching", last_session=datetime.now(timezone.utc).isoformat())

    log.info("Twitter session: fetching tweets...")
    tweets = fetch_tweets()
    if not tweets:
        log.info("Twitter: no new tweets.")
        if update_status_fn:
            update_status_fn(state="sleeping")
        return 0

    log.info(f"Twitter: {len(tweets)} tweets. Generating replies...")
    if update_status_fn:
        update_status_fn(state="generating")

    kb = build_context()
    items = generate_replies(tweets, kb)

    budget = min(
        settings.get("tw_max_per_session", 8),
        settings.get("tw_max_per_day", 20) - settings.get("_tw_today_count", 0),
    )

    to_post = [
        it for it in items
        if not it.get("skip")
        and len(it.get("draft", "")) >= 20
        and not any(p in it.get("draft", "") for p in SKIP_PHRASES)
    ][:budget]

    if not to_post:
        log.info("Twitter: nothing to publish.")
        if update_status_fn:
            update_status_fn(state="sleeping")
        return 0

    log.info(f"Twitter: publishing {len(to_post)} replies...")
    if update_status_fn:
        update_status_fn(state="posting")

    delay_min = settings.get("tw_reply_delay_min", 180)
    delay_max = settings.get("tw_reply_delay_max", 240)
    posted = 0

    for i, item in enumerate(to_post):
        tweet_id = _extract_tweet_id(item["url"])
        if not tweet_id:
            continue

        log.info(f"  Twitter [{i+1}/{len(to_post)}] @{item.get('author_username', item.get('author', '?'))[:30]}")
        ok, detail = _post_reply(tweet_id, item["draft"])

        if ok:
            log.info(f"    OK: {detail}")
            save_tweet_example(item.get("text", ""), item["draft"])
            if log_fn:
                log_fn(
                    author=item.get("author", "?"),
                    tweet_url=item["url"],
                    tweet_text=item.get("text", ""),
                    reply_text=item["draft"],
                )
            posted += 1
        else:
            log.warning(f"    Failed: {detail}")

        if i < len(to_post) - 1:
            delay = random.randint(delay_min, delay_max)
            log.info(f"  Twitter: waiting {delay}s...")
            time.sleep(delay)

    log.info(f"Twitter session done: {posted}/{len(to_post)} posted.")
    if update_status_fn:
        update_status_fn(state="sleeping")
    return posted


def parse_replies_md(path: str) -> list:
    text = Path(path).read_text()
    blocks = re.split(r"\n---\n", text)
    items = []
    for block in blocks:
        url_m = re.search(r"\*\*URL:\*\* (https?://\S+)", block)
        draft_m = re.search(r"```\n([\s\S]+?)\n```", block)
        status_m = re.search(r"\*\*STATUS:\*\* (\w+)", block)
        num_m = re.search(r"^## (\d+)\.", block, re.MULTILINE)
        if not (url_m and draft_m and num_m):
            continue
        items.append({
            "num": num_m.group(1),
            "url": url_m.group(1),
            "draft": draft_m.group(1).strip(),
            "status": status_m.group(1) if status_m else "pending",
        })
    return items


def mark_published(path: str, num: str):
    lines = Path(path).read_text().split("\n")
    in_block = False
    for i, line in enumerate(lines):
        if re.match(rf"^## {num}\.", line):
            in_block = True
        elif line.startswith("## ") and in_block:
            break
        elif in_block and line.startswith("**STATUS:** pending"):
            lines[i] = "**STATUS:** published"
            break
    Path(path).write_text("\n".join(lines))


def publish_replies(path: str, delay_min=180, delay_max=240, skip_nums=None):
    skip_set = set(str(x) for x in (skip_nums or []))
    items = parse_replies_md(path)

    to_post = [
        it for it in items
        if it["status"] == "pending"
        and it["num"] not in skip_set
        and len(it["draft"]) >= 20
        and not any(p in it["draft"] for p in SKIP_PHRASES)
    ]

    est_mins = len(to_post) * (delay_min + delay_max) // 2 // 60
    print(f"Queued {len(to_post)} replies (~{est_mins}m total at {delay_min}-{delay_max}s gaps)")

    posted = 0
    for i, item in enumerate(to_post):
        tweet_id = _extract_tweet_id(item["url"])
        if not tweet_id:
            print(f"  [{item['num']}] No tweet_id — skip")
            continue

        print(f"\n[{i+1}/{len(to_post)}] #{item['num']} {item['url']}")
        print(f"  → {item['draft'][:120]}")

        ok, detail = _post_reply(tweet_id, item["draft"])
        if ok:
            print(f"  ✓ {detail}")
            mark_published(path, item["num"])
            posted += 1
        else:
            print(f"  ✗ {detail}")

        if i < len(to_post) - 1:
            delay = random.randint(delay_min, delay_max)
            mins, secs = divmod(delay, 60)
            print(f"  Sleeping {mins}m {secs}s...")
            time.sleep(delay)

    print(f"\nDone: {posted}/{len(to_post)} posted.")
    return posted


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        from report import find_latest_session
        session = find_latest_session()
        path = session + "/replies.md" if session else None
    if not path:
        print("No replies.md found")
        sys.exit(1)
    publish_replies(path, skip_nums=[35])
