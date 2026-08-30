#!/usr/bin/env python3
"""Follow up to 30 VC people (SF partners/analysts/etc.) on X per day.

Sources, in priority order:
  1. Unfinished handles from the VC sheet (entries marked "retry" in
     followed_vcs.json — the ones X's daily follow limit blocked earlier).
  2. Discovery: X people-search for SF venture capital bios; candidates are
     filtered by bio keywords before following.

Runs in the script's own Chrome window (via follow_vcs helpers) and never
touches the user's windows. Safe to run multiple times a day — the daily
counter in daily_follow_state.json caps total follows at DAILY_TARGET.

Usage:
    python3 daily_follow.py            # run today's batch
Also called from post_replies.py once per day automatically.
"""

import json
import random
import re
import sys
import time
from datetime import date
from pathlib import Path

import follow_vcs
from follow_vcs import chrome_open, chrome_js, load_state, save_state, follow

DAILY_TARGET = 30
DELAY_RANGE = (5, 20)
STATE_FILE = Path(__file__).parent / "daily_follow_state.json"

DISCOVERY_QUERIES = [
    "VC partner San Francisco",
    "venture capital partner SF",
    "venture capital analyst San Francisco",
    "seed investor San Francisco",
    "VC principal San Francisco",
    "general partner venture fund SF",
    "pre-seed fund San Francisco",
    "venture capital associate San Francisco",
]

VC_BIO_RE = re.compile(
    r"(\bvc\b|venture|invest(or|ing|ment)|general partner|\bgp\b|angel|"
    r"principal|analyst|associate.*(fund|capital)|seed fund|pre.?seed)",
    re.IGNORECASE,
)
SF_BIO_RE = re.compile(
    r"(\bsf\b|san francisco|bay area|silicon valley|menlo park|palo alto|south park)",
    re.IGNORECASE,
)

SCRAPE_CELLS_JS = """
(() => {
  const cells = [...document.querySelectorAll('[data-testid="UserCell"]')];
  const out = [];
  for (const c of cells) {
    const link = c.querySelector('a[href^="/"]');
    if (!link) continue;
    const handle = link.getAttribute("href").split("/")[1];
    if (!handle || handle.includes("?")) continue;
    const following = !!c.querySelector('button[data-testid$="-unfollow"]');
    out.push({h: handle, t: (c.innerText || "").slice(0, 400), f: following});
  }
  return JSON.stringify(out);
})()
"""


def _today_state():
    state = {"date": "", "count": 0}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    today = date.today().isoformat()
    if state.get("date") != today:
        state = {"date": today, "count": 0}
    return state


def _save_today(state):
    STATE_FILE.write_text(json.dumps(state))


def done_today() -> bool:
    return _today_state()["count"] >= DAILY_TARGET


def _discover_candidates(query: str, followed: set) -> list[tuple[str, str]]:
    """Scrape X people-search results for VC-looking bios not yet followed."""
    from urllib.parse import quote
    chrome_open(f"https://x.com/search?q={quote(query)}&f=user")
    time.sleep(6)
    cells = []
    for _ in range(4):  # scroll to load more results
        try:
            cells = json.loads(chrome_js(SCRAPE_CELLS_JS))
        except (RuntimeError, ValueError):
            cells = cells or []
        try:
            chrome_js("window.scrollBy(0, 1500)")
        except RuntimeError:
            pass
        time.sleep(3)
    candidates = []
    for c in cells:
        handle, bio = c.get("h", ""), c.get("t", "")
        if not handle or c.get("f") or handle.lower() in followed:
            continue
        if not VC_BIO_RE.search(bio):
            continue
        candidates.append((handle, bio))
    # SF-mentioning bios first — the query targets SF but bios are the proof
    candidates.sort(key=lambda x: not SF_BIO_RE.search(x[1]))
    return candidates


def run_daily(target: int = DAILY_TARGET) -> int:
    """Follow up to `target` VC people today. Returns how many were followed."""
    today = _today_state()
    remaining = target - today["count"]
    if remaining <= 0:
        print(f"daily_follow: дневная цель {target} уже выполнена.")
        return 0

    followed_state = load_state()  # shared with follow_vcs.py
    followed = {h for h, s in followed_state.items() if s in ("followed", "already")}
    done = 0

    def try_follow(handle, label):
        nonlocal done, remaining
        status = follow(handle)
        print(f"  @{handle} ({label}) ... {status}")
        followed_state[handle.lower()] = (
            status if status in ("followed", "already", "missing") else "retry"
        )
        save_state(followed_state)
        if status in ("followed", "already"):
            followed.add(handle.lower())
        if status == "followed":
            done += 1
            remaining -= 1
            today["count"] += 1
            _save_today(today)
        if status == "clicked_unconfirmed":
            # X's follow limit kicked in — stop for today, retrying just
            # aggravates the anti-spam.
            print("daily_follow: X перестал подтверждать подписки (дневной лимит) — стоп.")
            return False
        time.sleep(random.uniform(*DELAY_RANGE))
        return True

    # 1. finish the sheet backlog first
    backlog = [h for h, s in followed_state.items() if s == "retry"]
    for handle in backlog:
        if remaining <= 0:
            break
        if not try_follow(handle, "sheet"):
            return done

    # 2. discovery via X people-search
    queries = random.sample(DISCOVERY_QUERIES, len(DISCOVERY_QUERIES))
    for query in queries:
        if remaining <= 0:
            break
        print(f"daily_follow: ищу кандидатов: {query}")
        for handle, bio in _discover_candidates(query, followed):
            if remaining <= 0:
                break
            if not try_follow(handle, "discovery"):
                return done

    print(f"daily_follow: за сегодня подписано {done}, всего {today['count']}/{target}.")
    return done


if __name__ == "__main__":
    if not follow_vcs.check_js_allowed():
        print("⚠️  Chrome: включи View → Developer → Allow JavaScript from Apple Events")
        sys.exit(1)
    run_daily()
