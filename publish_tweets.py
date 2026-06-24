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

# Markers that mean the Twitter cookies (auth_token/ct0) are dead/invalid, as
# opposed to a transient per-tweet failure. Used to flag auth health so an
# expired cookie surfaces explicitly instead of looking like "no new tweets".
_AUTH_ERROR_MARKERS = (
    "HTTP 401", "HTTP 403",
    "API error 32",   # Could not authenticate you
    "API error 89",   # Invalid or expired token
    "API error 64",   # account suspended
    "API error 215",  # Bad authentication data
    "API error 326",  # account locked
    "Could not authenticate",
    "invalid or expired token",
    "bad authentication",
)


def _is_auth_failure(detail: str) -> bool:
    d = (detail or "").lower()
    if _is_reply_restriction(detail):
        return False  # per-tweet restriction, not an auth problem
    return any(m.lower() in d for m in _AUTH_ERROR_MARKERS)


# X blocks replying when the post's author limited who can reply (conversation
# controls), common when replying to strangers from a young account. This is a
# per-tweet condition — skip the tweet and try the next, do NOT abort the session.
def _is_reply_restriction(detail: str) -> bool:
    d = (detail or "").lower()
    return ("reply to this conversation is not allowed" in d
            or "not been mentioned or otherwise engaged" in d)


# Pay-per-use credits exhausted — fatal for the whole session (top up needed).
def _is_credit_failure(detail: str) -> bool:
    d = (detail or "").lower()
    return "creditsdepleted" in d or "does not have any credits" in d or "http 402" in d


def _extract_tweet_id(url: str) -> str:
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else ""


def _post_reply(tweet_id: str, text: str) -> tuple:
    """Reply to a tweet via the official X API v2 (OAuth 1.0a).

    Replaces the old scrape-based CreateTweet, which hit X's automation block
    (error 226) from the server IP. Returns (ok, url_or_error) — same contract
    as before, so run_twitter_session / publish_replies are unaffected.
    """
    import x_api
    return x_api.reply(text, tweet_id)


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
        import fetch_tweets as _ft
        if _ft.LAST_FETCH.get("auth_suspect"):
            detail = (f"fetch returned no raw items "
                      f"({_ft.LAST_FETCH['errors']}/{_ft.LAST_FETCH['keywords']} keywords errored)")
            log.error(f"Twitter: SYSTEMIC fetch failure, cookies likely expired: {detail}")
            if update_status_fn:
                update_status_fn(state="auth_error", auth_ok=False, last_auth_error=detail)
        else:
            log.info("Twitter: no new tweets.")
            if update_status_fn:
                update_status_fn(state="sleeping")
        return 0

    log.info(f"Twitter: {len(tweets)} tweets. Generating replies...")
    if update_status_fn:
        # Fetch clearly worked — clear any stale systemic-failure flag.
        update_status_fn(state="generating", auth_ok=True, last_auth_error=None)

    kb = build_context()
    items = generate_replies(tweets, kb)

    budget = min(
        settings.get("tw_max_per_session", 8),
        settings.get("tw_max_per_day", 20) - settings.get("_tw_today_count", 0),
    )

    candidates = [
        it for it in items
        if not it.get("skip")
        and len(it.get("draft", "")) >= 20
        and not any(p in it.get("draft", "") for p in SKIP_PHRASES)
    ]
    # Many target tweets restrict replies (403). Try a larger pool than `budget`
    # so we keep going past restricted tweets until `budget` replies actually
    # land. Restricted attempts fail before a tweet is created, so they cost no
    # credits. Cap attempts to avoid an endless loop / runaway cost.
    max_attempts = min(len(candidates), max(budget * 8, 12))
    candidates = candidates[:max_attempts]

    if not candidates:
        log.info("Twitter: nothing to publish.")
        if update_status_fn:
            update_status_fn(state="sleeping")
        return 0

    log.info(f"Twitter: up to {len(candidates)} candidates, target {budget} replies...")
    if update_status_fn:
        update_status_fn(state="posting")

    delay_min = settings.get("tw_reply_delay_min", 180)
    delay_max = settings.get("tw_reply_delay_max", 240)
    posted = 0
    skipped_restricted = 0

    for item in candidates:
        if posted >= budget:
            break
        tweet_id = _extract_tweet_id(item["url"])
        if not tweet_id:
            continue

        author = item.get("author_username", item.get("author", "?"))[:30]
        ok, detail = _post_reply(tweet_id, item["draft"])

        if ok:
            log.info(f"    OK @{author}: {detail}")
            save_tweet_example(item.get("text", ""), item["draft"])
            try:
                from analyze_viral_tweets import save_viral_tweet
                save_viral_tweet(item, item["draft"])
            except Exception:
                pass
            if log_fn:
                log_fn(
                    author=item.get("author", "?"),
                    tweet_url=item["url"],
                    tweet_text=item.get("text", ""),
                    reply_text=item["draft"],
                )
            posted += 1
            if update_status_fn:
                update_status_fn(
                    auth_ok=True,
                    last_auth_error=None,
                    last_post_at=datetime.now(timezone.utc).isoformat(),
                )
            if posted < budget:
                delay = random.randint(delay_min, delay_max)
                log.info(f"  Twitter: waiting {delay}s...")
                time.sleep(delay)
        elif _is_credit_failure(detail):
            log.error(f"    CREDITS DEPLETED — top up X API credits: {detail}")
            if update_status_fn:
                update_status_fn(state="credits_depleted", auth_ok=False,
                                 last_auth_error="credits depleted: " + detail[:250])
            return posted
        elif _is_auth_failure(detail):
            log.error(f"    AUTH FAILURE: {detail}")
            if update_status_fn:
                update_status_fn(state="auth_error", auth_ok=False,
                                 last_auth_error=detail[:300])
            return posted
        elif _is_reply_restriction(detail):
            skipped_restricted += 1
            log.info(f"    skip @{author}: replies restricted by author")
            continue  # try next candidate, no delay, no credit spent
        else:
            log.warning(f"    Failed @{author}: {detail}")
            continue

    log.info(f"Twitter session done: {posted} posted, {skipped_restricted} skipped (restricted).")

    if posted > 0:
        try:
            from analyze_viral_tweets import run as run_tweet_analysis
            run_tweet_analysis()
        except Exception as e:
            log.warning(f"  Tweet pattern analysis failed: {e}")

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
