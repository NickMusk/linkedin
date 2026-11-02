#!/usr/bin/env python3
"""F4F lane: join fresh follow-for-follow threads on X and follow back followers.

Early-account bootstrap — f4f threads are where small accounts trade follows.
Three moves, all through the real Chrome session:
  1. find fresh f4f threads (Apify search, same actor as the growth lane),
  2. reply with a short rotating template + follow the thread author,
  3. once a day follow back everyone who followed @becamehokage.

Follows draw from the SAME daily budget as the VC follows
(daily_follow_state.json, X soft-limits ~22-30/day) and run earlier in the
poster loop, so f4f gets the budget first — first followers beat list
building right now.

    python3 f4f.py --once      # one tick by hand
    python3 f4f.py --dry-run   # show the threads it would join
"""

import json
import os
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import daily_follow
import follow_vcs
from follow_vcs import follow

BASE = Path(__file__).parent
STATE_FILE = BASE / "f4f_state.json"
ACTOR = "apidojo/tweet-scraper"

EVERY_MIN = 150            # minutes between Apify sweeps
PER_RUN = 2                # thread replies per sweep
REPLY_DAILY_CAP = 8        # f4f replies per day
MAX_THREAD_AGE_MIN = 480   # f4f threads stay alive for hours, unlike news
AUTHOR_COOLDOWN_DAYS = 7   # one reply per thread author per week
TEMPLATE_COOLDOWN_H = 48   # X blocks exact-duplicate reply text
FOLLOWBACK_SCROLLS = 6

OWN_HANDLE = "becamehokage"

SEARCH_TERMS = [
    '"follow for follow" lang:en -filter:replies',
    '"follow train" lang:en -filter:replies',
    '"follow back" "drop" lang:en -filter:replies',
    '"follow everyone who" lang:en -filter:replies',
    '"f4f" lang:en -filter:replies',
]

# the tweet must actually be an invitation to trade follows
F4F_RE = re.compile(
    r"follow\s*(for|4|2)\s*follow|\bf4f\b|follow\s*train|follow\s*loop"
    r"|follow(ing)?\s*(back|everyone|all)|drop your (handle|@|username)"
    r"|let'?s (all )?(connect|grow)|gain (followers|mutuals)|\bmutuals\b",
    re.IGNORECASE,
)
# f4f search results drag in adult/crypto spam — stay out of those threads
SKIP_RE = re.compile(
    r"onlyfans|nsfw|porn|18\s*\+|adult|crypto\s*(pump|signal)|airdrop|forex",
    re.IGNORECASE,
)

TEMPLATES = [
    "In. Following back everyone from this thread.",
    "Count me in, I follow back.",
    "Done, following people here. Follow back and let's grow.",
    "Building an AI recruiter, posting about AI agents and hiring. I follow back.",
    "Let's go. Following everyone who follows.",
    "In for this one. I post about AI and startups, always follow back.",
    "Good thread. Following everyone, follow me back.",
    "Here for it. AI founder, I follow back.",
    "Following all from here, do the same.",
    "Easy. Follow me and I follow back same day.",
    "Alright, in. Follow back 100%.",
    "Joining. Building in AI recruiting, follow back guaranteed.",
]

_NEXT_SWEEP = 0.0  # module-level timer; run_tick is called every poster pass


def _env_token() -> str:
    tok = os.environ.get("APIFY_API_TOKEN", "")
    if tok:
        return tok
    try:
        for line in (BASE / ".env").read_text().splitlines():
            if line.startswith("APIFY_API_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _load_state() -> dict:
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except ValueError:
            state = {}
    today = datetime.now().date().isoformat()
    if state.get("date") != today:
        state["date"] = today
        state["replies_today"] = 0
    state.setdefault("seen", {})
    state.setdefault("authors", {})        # author -> ts of our reply
    state.setdefault("templates", {})      # template idx -> ts last used
    state.setdefault("followed_back", {})  # handle -> ts
    return state


def _save_state(state: dict):
    # keep the maps from growing forever
    cut = time.time() - 30 * 86400
    for key in ("seen", "authors", "followed_back"):
        state[key] = {k: v for k, v in state[key].items() if v >= cut}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE_FILE)


def _parse_date(raw: str):
    for parser in (parsedate_to_datetime, datetime.fromisoformat):
        try:
            dt = parser(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def fetch_threads(state: dict) -> list[dict]:
    """Fresh, alive f4f threads we haven't touched, best engagement first."""
    from apify_client import ApifyClient
    token = _env_token()
    if not token:
        print("  f4f: нет APIFY_API_TOKEN")
        return []
    client = ApifyClient(token)
    now = datetime.now(timezone.utc)
    threads = []
    run = client.actor(ACTOR).call(run_input={
        "searchTerms": SEARCH_TERMS,
        "maxItems": 15 * len(SEARCH_TERMS),
        "addUserInfo": True,
        "scrapeTweetReplies": False,
        "sort": "Latest",
    })
    author_cut = time.time() - AUTHOR_COOLDOWN_DAYS * 86400
    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        url = item.get("url") or item.get("tweetUrl") or ""
        text = item.get("text") or item.get("fullText") or ""
        author = ((item.get("author") or {}).get("userName")
                  or item.get("authorUsername") or "").lstrip("@")
        dt = _parse_date(item.get("createdAt") or item.get("created_at") or "")
        likes = int(item.get("likeCount") or 0)
        replies = int(item.get("replyCount") or 0)
        if not url or not author or dt is None:
            continue
        if url in state["seen"] or author.lower() == OWN_HANDLE:
            continue
        if state["authors"].get(author.lower(), 0) >= author_cut:
            continue
        if (now - dt).total_seconds() / 60 > MAX_THREAD_AGE_MIN:
            continue
        if not F4F_RE.search(text) or SKIP_RE.search(text):
            continue
        if likes < 2 and replies < 3:   # dead thread, nobody will see the reply
            continue
        threads.append({"tweet_url": url, "author_username": author,
                        "text": text[:200], "score": likes + 2 * replies})
    threads.sort(key=lambda t: -t["score"])
    return threads


def _pick_template(state: dict) -> str | None:
    cut = time.time() - TEMPLATE_COOLDOWN_H * 3600
    fresh = [i for i in range(len(TEMPLATES))
             if state["templates"].get(str(i), 0) < cut]
    if not fresh:
        return None
    i = random.choice(fresh)
    state["templates"][str(i)] = time.time()
    return TEMPLATES[i]


def _follow_budget_left() -> int:
    today = daily_follow._today_state()
    return daily_follow.DAILY_TARGET - today["count"]


def _count_follow():
    today = daily_follow._today_state()
    today["count"] += 1
    daily_follow._save_today(today)


def follow_back_pass(state: dict) -> int:
    """Follow back everyone who followed us — the other half of the f4f deal."""
    today = datetime.now().date().isoformat()
    if state.get("followback_date") == today:
        return 0
    cells = daily_follow._scrape_user_cells(
        f"https://x.com/{OWN_HANDLE}/followers", scrolls=FOLLOWBACK_SCROLLS)
    done = 0
    for c in cells:
        handle = c.get("h", "")
        if not handle or c.get("f") or handle.lower() in state["followed_back"]:
            continue
        if _follow_budget_left() <= 0:
            print("  f4f: дневной бюджет подписок исчерпан — follow-back завтра")
            break
        status = follow(handle)
        print(f"  [f4f followback] @{handle} ... {status}")
        state["followed_back"][handle.lower()] = time.time()
        if status == "followed":
            done += 1
            _count_follow()
        _save_state(state)
        if status == "clicked_unconfirmed":
            print("  f4f: X перестал подтверждать подписки — стоп на сегодня.")
            return done
        time.sleep(random.uniform(5, 15))
    state["followback_date"] = today
    _save_state(state)
    return done


def run_tick(post_fn, dry_run: bool = False) -> int:
    """Called from the poster loop every pass; sweeps at most every EVERY_MIN."""
    global _NEXT_SWEEP
    if time.time() < _NEXT_SWEEP:
        return 0
    _NEXT_SWEEP = time.time() + EVERY_MIN * 60
    state = _load_state()
    posted = 0

    if state["replies_today"] < REPLY_DAILY_CAP:
        threads = fetch_threads(state)
        print(f"  f4f: найдено живых тредов: {len(threads)}")
        for t in threads[:PER_RUN * 3]:
            if posted >= PER_RUN or state["replies_today"] >= REPLY_DAILY_CAP:
                break
            text = _pick_template(state)
            if text is None:
                print("  f4f: все шаблоны на кулдауне — пропуск")
                break
            if dry_run:
                print(f"  [f4f dry] @{t['author_username']}: {text}")
                posted += 1
                continue
            print(f"  [f4f] @{t['author_username']} ... ", end="", flush=True)
            try:
                result = post_fn({**t, "reply": text})
            except Exception as e:
                result = f"error: {e}"
            print(result)
            state["seen"][t["tweet_url"]] = time.time()
            if result in ("posted", "duplicate", "unknown"):
                posted += 1
                state["replies_today"] += 1
                state["authors"][t["author_username"].lower()] = time.time()
                # the author gets our follow — that's the trade
                if _follow_budget_left() > 0:
                    st = follow(t["author_username"])
                    print(f"    follow @{t['author_username']} ... {st}")
                    if st == "followed":
                        _count_follow()
            _save_state(state)
            time.sleep(random.uniform(20, 40))

    if not dry_run:
        fb = follow_back_pass(state)
        if fb:
            print(f"  f4f: follow-back +{fb}")
    return posted


def main():
    import sys
    dry = "--dry-run" in sys.argv
    if not dry and not follow_vcs.check_js_allowed():
        print("Chrome: включи View → Developer → Allow JavaScript from Apple Events")
        sys.exit(1)
    import post_replies
    n = run_tick(post_replies.post_one, dry_run=dry)
    print(f"f4f: реплаев за тик: {n}")


if __name__ == "__main__":
    main()
