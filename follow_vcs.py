#!/usr/bin/env python3
"""Follow all X profiles from the VC list Google Sheet using the real Chrome session.

Usage:
    python3 follow_vcs.py            # follow everyone not yet followed
    python3 follow_vcs.py --dry-run  # just show who would be followed

Requires (one-time setup in Chrome):
    View -> Developer -> Allow JavaScript from Apple Events
On first run macOS will also ask to allow Terminal to control Google Chrome.

State is kept in followed_vcs.json so the script is resumable and never
re-follows someone it already processed.
"""

import csv
import io
import json
import random
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "17lW6qSMOKPJK--Vr-ZSyCEzxHmsW_O4iQ8k9_3cFxCw/export?format=csv"
)
STATE_FILE = Path(__file__).parent / "followed_vcs.json"
DELAY_RANGE = (5, 20)  # seconds between follows, randomized
PAGE_LOAD_TIMEOUT = 20  # seconds to wait for a profile page to render

PROFILE_RE = re.compile(r"^https?://x\.com/([A-Za-z0-9_]{1,15})/?$")


def load_sheet():
    with urllib.request.urlopen(SHEET_CSV_URL, timeout=30) as resp:
        data = resp.read().decode("utf-8")
    handles, search_rows = [], []
    for row in csv.DictReader(io.StringIO(data)):
        url = (row.get("X ссылка") or "").strip()
        name = f'{row.get("Партнер", "?")} ({row.get("Фонд", "?")})'
        m = PROFILE_RE.match(url)
        if m:
            handles.append((m.group(1), name))
        elif url:
            search_rows.append((name, url))
    # dedupe, keep order
    seen, unique = set(), []
    for handle, name in handles:
        if handle.lower() not in seen:
            seen.add(handle.lower())
            unique.append((handle, name))
    return unique, search_rows


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def jxa(script):
    """Run a JXA snippet against Chrome, return stdout or raise."""
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


_WIN_ID = None  # id of the dedicated Chrome window this script drives


def _ensure_window():
    """Return the id of the script's own Chrome window, creating it if needed.

    The window is never activated, so the user can keep using other Chrome
    windows while the script runs. If the user closes it mid-run, a new one
    is created automatically.
    """
    global _WIN_ID
    if _WIN_ID is not None:
        exists = jxa(f"""
            const chrome = Application('Google Chrome');
            chrome.windows().some(w => String(w.id()) === "{_WIN_ID}") ? "yes" : "no";
        """)
        if exists == "yes":
            return _WIN_ID
    _WIN_ID = jxa("""
        const chrome = Application('Google Chrome');
        const w = chrome.Window().make();
        w.activeTab.url = "about:blank";
        w.id();
    """).strip()
    return _WIN_ID


def chrome_open(url):
    win_id = _ensure_window()
    jxa(f"""
        const chrome = Application('Google Chrome');
        const w = chrome.windows().find(w => String(w.id()) === "{win_id}");
        w.activeTab.url = {json.dumps(url)};
    """)


def chrome_js(js_code):
    win_id = _ensure_window()
    return jxa(f"""
        const chrome = Application('Google Chrome');
        const w = chrome.windows().find(w => String(w.id()) === "{win_id}");
        chrome.execute(w.activeTab, {{ javascript: {json.dumps(js_code)} }});
    """)


def check_js_allowed():
    try:
        return chrome_js("1 + 1") == "2"
    except RuntimeError:
        return False


FOLLOW_JS_TEMPLATE = """
(() => {
  const col = document.querySelector('[data-testid="primaryColumn"]') || document;
  const text = document.body.innerText;
  if (text.includes("This account doesn") || text.includes("Account suspended")) return "missing";
  if (col.querySelector('button[data-testid$="-unfollow"]')) return "already";
  const buttons = [...col.querySelectorAll('button[data-testid$="-follow"]')];
  const target = buttons.find(b => (b.getAttribute("aria-label") || "").toLowerCase().includes("@HANDLE".toLowerCase())) || buttons[0];
  if (!target) return "loading";
  target.click();
  return "followed";
})()
"""


def follow(handle):
    chrome_open(f"https://x.com/{handle}")
    js = FOLLOW_JS_TEMPLATE.replace("HANDLE", handle)
    deadline = time.time() + PAGE_LOAD_TIMEOUT
    status = "loading"
    while time.time() < deadline:
        time.sleep(2)
        try:
            status = chrome_js(js)
        except RuntimeError:
            continue
        if status != "loading":
            break
    if status == "followed":
        # verify the click registered (button flipped to Following)
        time.sleep(2)
        try:
            verify = chrome_js(
                '(() => document.querySelector(\'[data-testid="primaryColumn"] button[data-testid$="-unfollow"]\') ? "ok" : "unconfirmed")()'
            )
            if verify != "ok":
                status = "clicked_unconfirmed"
        except RuntimeError:
            pass
    return status


def main():
    dry_run = "--dry-run" in sys.argv

    print("Загружаю список из Google Sheets...")
    profiles, search_rows = load_sheet()
    state = load_state()
    todo = [(h, n) for h, n in profiles if state.get(h.lower()) not in ("followed", "already")]

    print(f"Всего профилей с прямой ссылкой: {len(profiles)}, к обработке: {len(todo)}")
    if search_rows:
        print(f"Без хендла (только поиск, вручную): {len(search_rows)}")

    if dry_run:
        for h, n in todo:
            print(f"  would follow @{h} — {n}")
        for n, url in search_rows:
            print(f"  MANUAL: {n} — {url}")
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

    for i, (handle, name) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] @{handle} — {name} ... ", end="", flush=True)
        try:
            status = follow(handle)
        except Exception as e:
            status = f"error: {e}"
        print(status)
        state[handle.lower()] = status if status in ("followed", "already", "missing") else "retry"
        save_state(state)
        if i < len(todo):
            time.sleep(random.uniform(*DELAY_RANGE))

    print("\nГотово. Итоги:")
    counts = {}
    for v in state.values():
        counts[v] = counts.get(v, 0) + 1
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    if search_rows:
        print("\nЭтих нужно найти вручную (в таблице нет хендла):")
        for n, url in search_rows:
            print(f"  {n} — {url}")


if __name__ == "__main__":
    main()
