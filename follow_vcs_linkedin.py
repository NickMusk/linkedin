#!/usr/bin/env python3
"""Follow all LinkedIn profiles from the VC list Google Sheet using the real Chrome session.

Usage:
    python3 follow_vcs_linkedin.py            # follow everyone not yet followed
    python3 follow_vcs_linkedin.py --dry-run  # just show who would be followed

Clicks "Follow" only (never sends Connect invites). If Follow is hidden under
the "More" dropdown, the script opens it and clicks Follow there.

Requires the same one-time setup as follow_vcs.py:
    Chrome: View -> Developer -> Allow JavaScript from Apple Events
    macOS: allow the host app to control Google Chrome (Automation permission)

State is kept in followed_vcs_linkedin.json so the script is resumable and
never re-processes someone it already followed.
"""

import csv
import io
import json
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

from follow_vcs import chrome_open, chrome_js, check_js_allowed

SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "17lW6qSMOKPJK--Vr-ZSyCEzxHmsW_O4iQ8k9_3cFxCw/export?format=csv"
)
STATE_FILE = Path(__file__).parent / "followed_vcs_linkedin.json"
DELAY_RANGE = (12, 22)  # LinkedIn is stricter about automation than X — go slower
PAGE_LOAD_TIMEOUT = 25  # seconds to wait for a profile page to render

SLUG_RE = re.compile(r"linkedin\.com/in/([A-Za-z0-9\-_%]+)/?", re.IGNORECASE)

# Step 1: look for a visible Follow/Following button on the profile,
# otherwise open the "More" dropdown.
FOLLOW_JS = """
(() => {
  const bodyText = document.body.innerText;
  if (/Page not found|This page doesn.t exist|Эта страница не существует/i.test(bodyText)) return "missing";
  const txt = el => (el.innerText || el.getAttribute("aria-label") || "").trim();
  const isFollow = t => /^(Follow|Подписаться)( .*)?$/i.test(t) && !/unfollow|отписаться/i.test(t);
  const isFollowing = t => /^(Following|Отслеживание|Вы подписаны)( .*)?$/i.test(t);
  const main = document.querySelector("main") || document;
  const buttons = [...main.querySelectorAll("button")];
  if (buttons.some(b => isFollowing(txt(b)))) return "already";
  const fb = buttons.find(b => isFollow(txt(b)));
  if (fb) { fb.click(); return "followed"; }
  const more = buttons.find(b => /^(More|Ещё|Еще)( actions)?$/i.test(txt(b)));
  if (more) { more.click(); return "opened_more"; }
  return "loading";
})()
"""

# Step 2 (after opening More): click Follow inside the dropdown.
DROPDOWN_JS = """
(() => {
  const txt = el => (el.innerText || "").trim();
  const items = [...document.querySelectorAll('.artdeco-dropdown__content li, .artdeco-dropdown__content div[role="button"], div[role="menu"] *')];
  if (items.some(el => /^(Following|Отслеживание)$/i.test(txt(el)))) return "already";
  const follow = items.find(el => /^(Follow|Подписаться)$/i.test(txt(el)) && !/unfollow|отписаться/i.test(txt(el)));
  if (follow) { follow.click(); return "followed"; }
  document.body.click();
  return "no_follow_in_menu";
})()
"""


def load_sheet():
    with urllib.request.urlopen(SHEET_CSV_URL, timeout=30) as resp:
        data = resp.read().decode("utf-8")
    slugs, manual_rows = [], []
    for row in csv.DictReader(io.StringIO(data)):
        li = (row.get("LinkedIn") or "").strip()
        name = f'{row.get("Партнер", "?")} ({row.get("Фонд", "?")})'
        m = SLUG_RE.search(li)
        if m:
            slugs.append((m.group(1), name))
        else:
            manual_rows.append((name, li or "нет ссылки"))
    seen, unique = set(), []
    for slug, name in slugs:
        if slug.lower() not in seen:
            seen.add(slug.lower())
            unique.append((slug, name))
    return unique, manual_rows


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def follow(slug):
    chrome_open(f"https://www.linkedin.com/in/{slug}/")
    deadline = time.time() + PAGE_LOAD_TIMEOUT
    status = "loading"
    while time.time() < deadline:
        time.sleep(2.5)
        try:
            status = chrome_js(FOLLOW_JS)
        except RuntimeError:
            continue
        if status == "opened_more":
            time.sleep(1.5)
            try:
                status = chrome_js(DROPDOWN_JS)
            except RuntimeError:
                status = "loading"
                continue
        if status != "loading":
            break
    return status


def main():
    dry_run = "--dry-run" in sys.argv

    print("Загружаю список из Google Sheets...")
    profiles, manual_rows = load_sheet()
    state = load_state()
    todo = [(s, n) for s, n in profiles if state.get(s.lower()) not in ("followed", "already")]

    print(f"Всего профилей с прямой ссылкой: {len(profiles)}, к обработке: {len(todo)}")
    if manual_rows:
        print(f"Без ссылки (искать вручную): {len(manual_rows)}")

    if dry_run:
        for s, n in todo:
            print(f"  would follow linkedin.com/in/{s} — {n}")
        for n, li in manual_rows:
            print(f"  MANUAL: {n} — {li}")
        return

    if not todo:
        print("Все уже зафоллены.")
        return

    if not check_js_allowed():
        print(
            "\n⚠️  Chrome не разрешает JavaScript из Apple Events.\n"
            "Включи в Chrome: View → Developer → Allow JavaScript from Apple Events\n"
            "и запусти скрипт снова."
        )
        sys.exit(1)

    for i, (slug, name) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {slug} — {name} ... ", end="", flush=True)
        try:
            status = follow(slug)
        except Exception as e:
            status = f"error: {e}"
        print(status)
        state[slug.lower()] = status if status in ("followed", "already", "missing") else "retry"
        save_state(state)
        if i < len(todo):
            time.sleep(random.uniform(*DELAY_RANGE))

    print("\nГотово. Итоги:")
    counts = {}
    for v in state.values():
        counts[v] = counts.get(v, 0) + 1
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    if manual_rows:
        print("\nЭтих нужно найти вручную (в таблице нет ссылки на LinkedIn):")
        for n, li in manual_rows:
            print(f"  {n} — {li}")


if __name__ == "__main__":
    main()
