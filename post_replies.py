#!/usr/bin/env python3
"""Auto-poster: pulls generated replies from the dashboard and posts them on X
through the real Chrome session, in batches, forever.

Cycle:
  - fetch the queue from the dashboard (pending + approved, VC people first)
  - post a batch of 5–10 replies, random 5–20s pause between tweets
  - once per day: follow up to 30 VC people (daily_follow.py)
  - sleep 40–80 minutes, repeat

Posting goes through X's pre-filled composer (https://x.com/intent/post) in
the script's own Chrome window — the user keeps browsing undisturbed. The
dashboard is notified via POST /twitter/queue/<id>/posted, so the UI stays in
sync. Rejected items are never posted. Items that fail 3 times are skipped
permanently (recorded in post_failures.json).

Usage:
    python3 post_replies.py             # run the loop
    python3 post_replies.py --once      # single batch, then exit
    python3 post_replies.py --dry-run   # show what would be posted

Requires the same Chrome/macOS permissions as follow_vcs.py.
"""

import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import follow_vcs
from follow_vcs import chrome_open, chrome_js
import daily_follow

DASHBOARD = "https://linkedin-commenter-kwha.onrender.com"
BATCH_RANGE = (5, 10)            # replies per batch
TWEET_DELAY = (5, 20)            # seconds between tweets
BATCH_PAUSE_MIN = (40, 80)       # minutes between batches
EMPTY_QUEUE_WAIT_MIN = 20        # queue empty — check again sooner
COMPOSER_TIMEOUT = 25            # seconds for the composer to become clickable

FAILURES_FILE = Path(__file__).parent / "post_failures.json"
MAX_FAILURES = 3
# Replying to a weeks-old tweet reads as bot behavior — the queue has a long
# backlog, only post replies generated in the last few days.
MAX_ITEM_AGE_DAYS = 5

STATUS_RE = re.compile(r"/status/(\d+)")

# Click Post once the composer is ready; report exactly what happened.
CLICK_JS = """
(() => {
  const btn = document.querySelector('[data-testid="tweetButton"]');
  if (!btn) return "waiting";
  if (btn.disabled || btn.getAttribute("aria-disabled") === "true") return "disabled";
  btn.click();
  return "clicked";
})()
"""

# After the click: an error toast means X rejected it; a gone/disabled
# composer with no error toast means the reply went out.
VERIFY_JS = """
(() => {
  const toast = document.querySelector('[data-testid="toast"]');
  if (toast && /not able|can.t|cannot|error|restricted|try again|too fast|limit/i.test(toast.innerText || ""))
    return "rejected: " + (toast.innerText || "").slice(0, 120);
  const btn = document.querySelector('[data-testid="tweetButton"]');
  if (!btn) return "posted";
  const empty = !document.querySelector('[data-testid="tweetTextarea_0"]')
    || (document.querySelector('[data-testid="tweetTextarea_0"]').innerText || "").trim() === "";
  return empty ? "posted" : "unknown";
})()
"""


def fetch_queue() -> list[dict]:
    with urllib.request.urlopen(f"{DASHBOARD}/twitter/queue.json", timeout=30) as r:
        return json.load(r)


def mark_posted(item_id: str):
    req = urllib.request.Request(f"{DASHBOARD}/twitter/queue/{item_id}/posted", method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def _load_failures() -> dict:
    if FAILURES_FILE.exists():
        return json.loads(FAILURES_FILE.read_text())
    return {}


def _save_failures(f: dict):
    FAILURES_FILE.write_text(json.dumps(f, indent=1))


def _is_vc_item(item: dict) -> bool:
    if item.get("vc"):
        return True
    try:
        from vc_priority import is_vc
        return is_vc(item.get("author_username", ""))
    except Exception:
        return False


def _fresh(item: dict) -> bool:
    try:
        gen = datetime.fromisoformat(item.get("generated_at", ""))
        return (datetime.now(gen.tzinfo) - gen).days < MAX_ITEM_AGE_DAYS
    except ValueError:
        return False


def pending_items() -> list[dict]:
    failures = _load_failures()
    items = [
        it for it in fetch_queue()
        if it.get("status") in ("pending", "approved")
        and (it.get("reply") or "").strip()
        and len(it["reply"]) <= 280
        and STATUS_RE.search(it.get("tweet_url") or "")
        and failures.get(it["id"], 0) < MAX_FAILURES
        and _fresh(it)
    ]
    # VC people first; inside each group keep dashboard order
    items.sort(key=lambda it: not _is_vc_item(it))
    return items


def post_one(item: dict) -> str:
    tweet_id = STATUS_RE.search(item["tweet_url"]).group(1)
    text = urllib.parse.quote(item["reply"])
    # no #xagent marker: the Tampermonkey userscript must stay inert here,
    # this script does its own clicking
    chrome_open(f"https://x.com/intent/post?in_reply_to={tweet_id}&text={text}")

    deadline = time.time() + COMPOSER_TIMEOUT
    clicked = False
    while time.time() < deadline:
        time.sleep(2)
        try:
            state = chrome_js(CLICK_JS)
        except RuntimeError:
            continue
        if state == "clicked":
            clicked = True
            break
    if not clicked:
        return "composer_timeout"

    time.sleep(4)
    try:
        return chrome_js(VERIFY_JS)
    except RuntimeError as e:
        return f"verify_error: {e}"


def run_batch(dry_run: bool = False) -> int:
    items = pending_items()
    if not items:
        print(f"[{datetime.now():%H:%M}] Очередь пуста.")
        return 0
    batch = items[:random.randint(*BATCH_RANGE)]
    vc_count = sum(1 for it in batch if _is_vc_item(it))
    print(f"[{datetime.now():%H:%M}] Батч: {len(batch)} реплаев ({vc_count} VC) из {len(items)} в очереди.")

    if dry_run:
        for it in batch:
            tag = "VC " if _is_vc_item(it) else "   "
            print(f"  {tag}@{it['author_username']}: {it['reply'][:80]}")
        return 0

    failures = _load_failures()
    posted = 0
    for i, it in enumerate(batch, 1):
        print(f"  [{i}/{len(batch)}] @{it['author_username']} ... ", end="", flush=True)
        try:
            result = post_one(it)
        except Exception as e:
            result = f"error: {e}"
        print(result)
        if result == "posted":
            posted += 1
            failures.pop(it["id"], None)
            try:
                mark_posted(it["id"])
            except Exception as e:
                print(f"    (не смог отметить на дашборде: {e})")
        else:
            failures[it["id"]] = failures.get(it["id"], 0) + 1
        _save_failures(failures)
        if i < len(batch):
            time.sleep(random.uniform(*TWEET_DELAY))
    print(f"  Запостил {posted}/{len(batch)}.")
    return posted


def main():
    dry_run = "--dry-run" in sys.argv
    once = "--once" in sys.argv

    if not dry_run and not follow_vcs.check_js_allowed():
        print("⚠️  Chrome: включи View → Developer → Allow JavaScript from Apple Events")
        sys.exit(1)

    while True:
        run_batch(dry_run=dry_run)

        if not dry_run and not daily_follow.done_today():
            print(f"[{datetime.now():%H:%M}] Дневные подписки на VC...")
            try:
                daily_follow.run_daily()
            except Exception as e:
                print(f"  daily_follow error: {e}")

        if once or dry_run:
            break
        pause = random.uniform(*BATCH_PAUSE_MIN) * 60
        if not pending_items():
            pause = EMPTY_QUEUE_WAIT_MIN * 60
        print(f"[{datetime.now():%H:%M}] Пауза {pause / 60:.0f} мин.\n")
        time.sleep(pause)


if __name__ == "__main__":
    main()
