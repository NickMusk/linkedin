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
# Step back up 2026-09-27 (one notch above the post-warning floor).
# Full restore (5,10)/(5,20)/(40,80) after a quiet week without X warnings.
BATCH_RANGE = (5, 8)             # replies per batch
TWEET_DELAY = (15, 40)           # seconds between tweets
BATCH_PAUSE_MIN = (70, 120)      # minutes between batches
EMPTY_QUEUE_WAIT_MIN = 20        # queue empty — check again sooner
COMPOSER_TIMEOUT = 25            # seconds for the composer to become clickable

FAILURES_FILE = Path(__file__).parent / "post_failures.json"
MAX_FAILURES = 3
# Replying to a weeks-old tweet reads as bot behavior — the queue has a long
# backlog, only post replies generated in the last few days.
MAX_ITEM_AGE_DAYS = 2  # tightened 2026-11-01
# One reply per author per this window: the VC pass puts many tweets of the
# same person into the queue, and replying to them batch after batch is spam.
AUTHOR_COOLDOWN_HOURS = 20
AUTHORS_FILE = Path(__file__).parent / "posted_authors.json"

STATUS_RE = re.compile(r"/status/(\d+)")

# Pre-send gate (same class of bug as the LinkedIn leak): never post a reply
# with emoji or leaked drafting narration. Blocked items are rejected on the
# dashboard so they leave the queue.
GATE_BAD_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF️]"
    r"|(^|\n)\s*wait[,.]|let me (redo|try|fix|rewrite)|no emoji|redo:|revised"
    r"|here['’]s (a|the) (version|comment|take)|as an ai|rewrit"
    r"|(?-i:(^|\n)\s*SKIP\s*($|\n))|nothing to (react|reply|respond)|reply to a reply",
    re.IGNORECASE,
)


def mark_rejected(item_id: str):
    req = urllib.request.Request(f"{DASHBOARD}/twitter/queue/{item_id}/reject", method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()

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
# composer with no error toast means the reply went out. "already said that"
# means this exact reply EXISTS on X (an earlier attempt succeeded) — that is
# a confirmation, not a failure.
VERIFY_JS = """
(() => {
  const toast = document.querySelector('[data-testid="toast"]');
  const tt = toast ? (toast.innerText || "") : "";
  if (/already said|already sent|duplicate/i.test(tt)) return "duplicate";
  if (toast && /not able|can.t|cannot|error|restricted|try again|too fast|limit/i.test(tt))
    return "rejected: " + tt.slice(0, 120);
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


def _load_recent_authors() -> set:
    """Authors replied to within the cooldown window (lowercased)."""
    if not AUTHORS_FILE.exists():
        return set()
    data = json.loads(AUTHORS_FILE.read_text())
    cutoff = time.time() - AUTHOR_COOLDOWN_HOURS * 3600
    return {a for a, ts in data.items() if ts >= cutoff}


def _record_author(author: str):
    data = {}
    if AUTHORS_FILE.exists():
        data = json.loads(AUTHORS_FILE.read_text())
    data[(author or "").lower()] = time.time()
    cutoff = time.time() - AUTHOR_COOLDOWN_HOURS * 3600
    AUTHORS_FILE.write_text(json.dumps({a: t for a, t in data.items() if t >= cutoff}))


def _wake_display():
    """Wake the display AND hold it awake for the whole batch (~6 min):
    with the screen off macOS freezes Chrome's rendering (occlusion) and the
    composer never mounts. A detached caffeinate keeps it lit long enough to
    post the batch, then the display sleeps again on its own."""
    import subprocess
    try:
        subprocess.Popen(["caffeinate", "-u", "-d", "-t", "360"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
    except Exception:
        pass


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


# Growth lane: replies to fresh tweets of big authors must land while the
# tweet is still surging. Checked every GROWTH_TICK_SEC, posted immediately.
GROWTH_TICK_SEC = 240
GROWTH_MAX_AGE_MIN = 45          # older than this = the window is gone, reject
GROWTH_PER_TICK = 3
GROWTH_DAILY_CAP = 30
_GROWTH_DAY = {"date": "", "count": 0}


def growth_items() -> list[dict]:
    failures = _load_failures()
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    fresh, stale = [], []
    for it in fetch_queue():
        if it.get("lane") != "growth" or it.get("status") != "pending":
            continue
        if not (it.get("reply") or "").strip() or failures.get(it["id"], 0) >= MAX_FAILURES:
            continue
        try:
            age_min = (now - _dt.datetime.fromisoformat(it["generated_at"])).total_seconds() / 60
        except Exception:
            continue
        (fresh if age_min <= GROWTH_MAX_AGE_MIN else stale).append(it)
    for it in stale:  # window gone — reject so the queue stays honest
        try:
            mark_rejected(it["id"])
        except Exception:
            pass
    fresh.sort(key=lambda x: x.get("generated_at", ""))
    return fresh


def run_growth_tick() -> int:
    import datetime as _dt
    today = _dt.date.today().isoformat()
    if _GROWTH_DAY["date"] != today:
        _GROWTH_DAY.update(date=today, count=0)
    if _GROWTH_DAY["count"] >= GROWTH_DAILY_CAP:
        return 0
    items = growth_items()
    if not items:
        return 0
    recent = _load_recent_authors()
    posted = 0
    for it in items:
        if posted >= GROWTH_PER_TICK or _GROWTH_DAY["count"] >= GROWTH_DAILY_CAP:
            break
        a = (it.get("author_username") or "").lower()
        if a in recent:
            continue
        if GATE_BAD_RE.search(it.get("reply") or ""):
            try:
                mark_rejected(it["id"])
            except Exception:
                pass
            continue
        print(f"  [growth] @{it['author_username']} ... ", end="", flush=True)
        try:
            result = post_one(it)
        except Exception as e:
            result = f"error: {e}"
        print(result)
        if result in ("posted", "duplicate", "unknown"):
            posted += 1
            _GROWTH_DAY["count"] += 1
            _SESSION["posted"] += 1
            _record_author(a)
            recent.add(a)
            try:
                mark_posted(it["id"])
            except Exception as e:
                print(f"    (не отмечен на дашборде: {e})")
            time.sleep(random.uniform(20, 40))
    return posted


def pending_items() -> list[dict]:
    failures = _load_failures()
    items = [
        it for it in fetch_queue()
        if it.get("lane") != "growth"
        and it.get("status") in ("pending", "approved")
        and (it.get("reply") or "").strip()
        and len(it["reply"]) <= 280
        and STATUS_RE.search(it.get("tweet_url") or "")
        and failures.get(it["id"], 0) < MAX_FAILURES
        and _fresh(it)
    ]
    # VC people first; inside each group — freshest first (reach decays fast)
    def _key(it):
        import datetime as _dt
        try:
            ts = _dt.datetime.fromisoformat(it["generated_at"]).timestamp()
        except Exception:
            ts = 0.0
        return (not _is_vc_item(it), -ts)
    items.sort(key=_key)
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
        result = chrome_js(VERIFY_JS)
        if result == "unknown":
            # give the SPA a few more seconds to close the composer before
            # concluding anything
            time.sleep(5)
            result = chrome_js(VERIFY_JS)
        return result
    except RuntimeError as e:
        return f"verify_error: {e}"


def run_batch(dry_run: bool = False) -> int:
    items = pending_items()
    _SESSION["queue"] = len(items)
    if not items:
        print(f"[{datetime.now():%H:%M}] Очередь пуста.")
        return 0
    # max one reply per author per batch AND per cooldown window — the same
    # person replied to in every batch (spotted with @ericbahn) reads as spam
    recent = _load_recent_authors()
    size = random.randint(*BATCH_RANGE)
    batch, seen_authors = [], set()
    for it in items:
        a = (it.get("author_username") or "").lower()
        if a in seen_authors or a in recent:
            continue
        seen_authors.add(a)
        batch.append(it)
        if len(batch) >= size:
            break
    vc_count = sum(1 for it in batch if _is_vc_item(it))
    print(f"[{datetime.now():%H:%M}] Батч: {len(batch)} реплаев ({vc_count} VC) из {len(items)} в очереди.")

    if dry_run:
        for it in batch:
            tag = "VC " if _is_vc_item(it) else "   "
            print(f"  {tag}@{it['author_username']}: {it['reply'][:80]}")
        return 0

    _wake_display()
    # Cookie hygiene for the headless transport: safety-net refresh every
    # REFRESH_EVERY_H hours, immediate refresh when X shows the login wall.
    try:
        import gb_browser
        from pathlib import Path as _P
        if (_P(__file__).parent / "use_headless.flag").exists() and gb_browser.available():
            gb_browser.refresh_cookies(force=False)
            gb_browser.navigate("https://x.com/home")
            time.sleep(4)
            if gb_browser.logged_out():
                print("  [cookies] X разлогинил headless-сессию — реимпорт из Chrome")
                gb_browser.refresh_cookies(force=True)
                time.sleep(2)
                gb_browser.navigate("https://x.com/home")
                time.sleep(4)
                if gb_browser.logged_out():
                    print("  [cookies] реимпорт не помог — батч пропущен, нужен вход в X в Chrome")
                    return 0
    except Exception as e:
        print(f"  [cookies] проверка не удалась ({e}) — продолжаю как есть")
    failures = _load_failures()
    posted = 0
    timeouts_in_a_row = 0
    for i, it in enumerate(batch, 1):
        print(f"  [{i}/{len(batch)}] @{it['author_username']} ... ", end="", flush=True)
        if GATE_BAD_RE.search(it.get("reply") or ""):
            print("blocked_by_gate (emoji/meta) — reject")
            try:
                mark_rejected(it["id"])
            except Exception as e:
                print(f"    (не смог отклонить на дашборде: {e})")
            continue
        try:
            result = post_one(it)
        except Exception as e:
            result = f"error: {e}"
        print(result)
        # "duplicate" = X says this exact text is already posted (an earlier
        # attempt worked); "unknown" = click went through, no error toast —
        # in both cases treat as posted: re-posting is spam, and a rare lost
        # reply is far cheaper than visible duplicates.
        if result in ("posted", "duplicate", "unknown"):
            posted += 1
            timeouts_in_a_row = 0
            failures.pop(it["id"], None)
            _record_author(it.get("author_username", ""))
            try:
                mark_posted(it["id"])
            except Exception as e:
                print(f"    (не смог отметить на дашборде: {e})")
        elif result == "composer_timeout":
            # Chrome isn't rendering (display off / locked screen) — this is
            # our infrastructure failing, not the tweet: don't count it
            # against the item, and abort the batch after two in a row.
            timeouts_in_a_row += 1
            if timeouts_in_a_row >= 2:
                print("  Chrome не рендерит (экран погашен?) — батч прерван, повтор позже.")
                break
        else:
            failures[it["id"]] = failures.get(it["id"], 0) + 1
        _save_failures(failures)
        if i < len(batch):
            time.sleep(random.uniform(*TWEET_DELAY))
    print(f"  Запостил {posted}/{len(batch)}.")
    return posted


PAUSE_FLAG = Path(__file__).parent / "poster_pause.flag"
STATUS_FILE = Path(__file__).parent / "poster_status.json"
_SESSION = {"started_at": None, "batches": 0, "posted": 0, "queue": 0, "last_batch_at": None}


def _write_status(state: str):
    import datetime as _dt
    try:
        STATUS_FILE.write_text(json.dumps({
            "state": state,
            "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
            **_SESSION,
        }))
    except Exception:
        pass


def _honor_pause():
    """Block while the pause flag exists (set from the menu bar app)."""
    was = False
    while PAUSE_FLAG.exists():
        if not was:
            print("[пауза] включена из менюбара — жду")
            was = True
        _write_status("paused")
        time.sleep(15)
    if was:
        print("[пауза] снята — продолжаю")


LOCK_FILE = Path(__file__).parent / "post_replies.lock"


def _acquire_lock() -> bool:
    """Refuse to run two posters at once — a second instance races the first
    over the same pending items and double-posts them."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text())
            import os
            os.kill(pid, 0)  # raises if that process is gone
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale lock
    import os
    import atexit
    LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: LOCK_FILE.unlink(missing_ok=True))
    return True


def main():
    dry_run = "--dry-run" in sys.argv
    once = "--once" in sys.argv

    if not dry_run and not _acquire_lock():
        print("⚠️  Постер уже запущен (post_replies.lock) — второй экземпляр запрещён.")
        sys.exit(1)

    if not dry_run and not follow_vcs.check_js_allowed():
        print("⚠️  Chrome: включи View → Developer → Allow JavaScript from Apple Events")
        sys.exit(1)

    import datetime as _dt
    _SESSION["started_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    _write_status("starting")
    while True:
        # Dual lane. Growth replies go out within minutes of the tweet;
        # VC/regular keep the old batch rhythm. A network blip must never
        # kill the loop — log it, wait, try again.
        try:
            _honor_pause()
            if not dry_run:
                g = run_growth_tick()
                if g:
                    print(f"[{datetime.now():%H:%M}] growth: +{g}")

            next_bulk = _SESSION.get("_next_bulk", 0)
            if time.time() >= next_bulk or once or dry_run:
                _write_status("posting")
                posted = run_batch(dry_run=dry_run)
                _SESSION["batches"] += 1
                _SESSION["posted"] += posted or 0
                _SESSION["last_batch_at"] = _dt.datetime.now().isoformat(timespec="seconds")

                if not dry_run and not daily_follow.done_today():
                    print(f"[{datetime.now():%H:%M}] Дневные подписки на VC...")
                    try:
                        daily_follow.run_daily()
                    except Exception as e:
                        print(f"  daily_follow error: {e}")

                pause = random.uniform(*BATCH_PAUSE_MIN) * 60
                if not pending_items():
                    pause = EMPTY_QUEUE_WAIT_MIN * 60
                _SESSION["_next_bulk"] = time.time() + pause
                print(f"[{datetime.now():%H:%M}] Следующий bulk-батч через {pause / 60:.0f} мин.")

            if once or dry_run:
                break
        except Exception as e:
            print(f"[{datetime.now():%H:%M}] Сбой цикла ({type(e).__name__}: {e}) — повтор через 5 мин.")
            time.sleep(300)
        _write_status("watching growth lane")
        slept = 0
        while slept < GROWTH_TICK_SEC:
            _honor_pause()
            time.sleep(30)
            slept += 30


if __name__ == "__main__":
    main()
