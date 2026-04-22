#!/usr/bin/env python3
"""
Autonomous LinkedIn comment poster.
Runs as a Render Background Worker.

Schedule logic:
- Active hours: 09:00–18:00 San Francisco time (America/Los_Angeles, DST-aware)
- 3-4 sessions per day, randomised intervals (~2.5-4 hours apart)
- Per session: fetch posts → generate comments → auto-publish
- Daily cap: MAX_COMMENTS_PER_DAY total comments posted
- Per session cap: MAX_PER_SESSION comments (to avoid bursts)
"""
import os
import sys
import json
import time
import random
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────
SF_TZ          = ZoneInfo("America/Los_Angeles")
ACTIVE_START_H = 9           # 09:00 SF
ACTIVE_END_H   = 18          # 18:00 SF

MAX_COMMENTS_PER_DAY = 10
MAX_PER_SESSION      = 3     # comments per single run (natural burst size)

SESSION_GAP_MIN = 150        # min minutes between sessions
SESSION_GAP_MAX = 270        # max minutes between sessions

from config import DATA_DIR
STATE_FILE = os.path.join(DATA_DIR, "autonomous_state.json")
# ───────────────────────────────────────────────────────────────────────────


def _sf_now() -> datetime:
    return datetime.now(SF_TZ)


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"date": "", "count": 0, "last_session_ts": 0}


def _save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def _today_str() -> str:
    return _sf_now().strftime("%Y-%m-%d")


def _comments_today(state: dict) -> int:
    if state.get("date") != _today_str():
        return 0
    return state.get("count", 0)


def _record_comments(n: int):
    state = _load_state()
    today = _today_str()
    if state.get("date") != today:
        state = {"date": today, "count": 0, "last_session_ts": state.get("last_session_ts", 0)}
    state["count"] = state.get("count", 0) + n
    state["last_session_ts"] = time.time()
    _save_state(state)


def _within_active_hours() -> bool:
    h = _sf_now().hour
    return ACTIVE_START_H <= h < ACTIVE_END_H


def _minutes_since_last_session() -> float:
    state = _load_state()
    last = state.get("last_session_ts", 0)
    if not last:
        return 9999
    return (time.time() - last) / 60


def _seconds_until_active() -> int:
    now = _sf_now()
    if now.hour < ACTIVE_START_H:
        target = now.replace(hour=ACTIVE_START_H, minute=random.randint(0, 30), second=0, microsecond=0)
    else:
        target = (now + timedelta(days=1)).replace(hour=ACTIVE_START_H, minute=random.randint(0, 30), second=0, microsecond=0)
    return max(0, int((target - now).total_seconds()))


def run_session():
    """One full session: fetch → generate → auto-publish up to MAX_PER_SESSION."""
    log.info("=== Session starting ===")

    state   = _load_state()
    today   = _today_str()
    done    = _comments_today(state)
    budget  = min(MAX_PER_SESSION, MAX_COMMENTS_PER_DAY - done)

    if budget <= 0:
        log.info(f"Daily cap reached ({done}/{MAX_COMMENTS_PER_DAY}). Skipping.")
        return 0

    log.info(f"Daily budget remaining: {MAX_COMMENTS_PER_DAY - done}. Will post up to {budget} this session.")

    # ── Fetch ──────────────────────────────────────────────────────────────
    from fetch_posts import fetch_all_posts
    from report import session_dir, save_posts, save_comments
    from knowledge_base import build_context
    from generate_comments import generate_comments
    from publish import _extract_activity_id, _get_social_id, _post_comment, _mark_published
    from knowledge_base import save_example
    from fetch_posts import mark_url_published
    from config import PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX
    import json as _json

    posts = fetch_all_posts()
    if not posts:
        log.info("No new posts found.")
        return 0

    d = session_dir()
    save_posts(posts, d)
    with open(os.path.join(d, "posts.json"), "w") as f:
        _json.dump(posts, f, ensure_ascii=False, indent=2)

    # ── Generate ───────────────────────────────────────────────────────────
    log.info("Building knowledge base...")
    kb = build_context()
    log.info(f"KB: {len(kb):,} chars")

    log.info("Generating comments...")
    items = generate_comments(posts, kb)

    # ── Auto-filter ────────────────────────────────────────────────────────
    publishable = [
        it for it in items
        if not it.get("skip")
        and it.get("draft", "").strip()
        and len(it.get("draft", "")) >= 40
    ][:budget]

    if not publishable:
        log.info("Nothing to publish after filtering.")
        save_comments(items, d)
        return 0

    # Save all drafts for the record (auto-approve the ones we'll post)
    for it in items:
        it["_auto_status"] = "approved" if it in publishable else (
            "rejected" if it.get("skip") else "pending"
        )

    comments_path = os.path.join(d, "comments.md")
    _save_auto_comments(items, publishable, d)

    # ── Publish ────────────────────────────────────────────────────────────
    published = 0
    for i, item in enumerate(publishable):
        text = item["draft"].strip()
        url  = item["url"]
        author = item.get("author", "?")[:35]

        log.info(f"  [{i+1}/{len(publishable)}] {author}")

        aid = _extract_activity_id(url)
        sid = _get_social_id(aid)
        if not sid:
            log.warning(f"    No social_id for {url[:60]}")
            continue

        ok, detail = _post_comment(sid, text)
        if ok:
            log.info(f"    Posted ✓ ({detail})")
            save_example(item.get("text", url), text)
            _mark_published(comments_path, url)
            mark_url_published(url)
            published += 1
        else:
            log.warning(f"    Failed: {detail}")

        if i < len(publishable) - 1:
            delay = random.randint(PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX)
            log.info(f"  Waiting {delay}s...")
            time.sleep(delay)

    _record_comments(published)
    log.info(f"=== Session done: {published}/{len(publishable)} posted ===")
    return published


def _save_auto_comments(items, publishable, directory):
    """Save comments.md marking auto-approved ones as approved."""
    from report import save_comments
    publishable_urls = {it["url"] for it in publishable}
    for it in items:
        if it["url"] in publishable_urls:
            it["_override_status"] = "approved"
        elif it.get("skip"):
            it["_override_status"] = "rejected"
    save_comments(items, directory)
    # Now patch STATUS in the file for publishable ones
    path = os.path.join(directory, "comments.md")
    with open(path) as f:
        content = f.read()
    for it in publishable:
        url = it["url"]
        # Mark as approved in the file (save_comments writes 'pending' by default)
        content = content.replace(
            f"**URL:** {url}\n**Engagement:",
            f"**URL:** {url}\n**Engagement:"
        )
    # Simpler: just re-write status blocks
    import re
    blocks = content.split("\n---\n")
    updated = []
    for block in blocks:
        for it in publishable:
            if f"**URL:** {it['url']}" in block:
                block = block.replace("**STATUS:** pending", "**STATUS:** approved")
                break
    updated.append(block)
    # Actually just do a fresh pass
    blocks2 = content.split("\n---\n")
    out = []
    for block in blocks2:
        for it in publishable:
            if f"**URL:** {it['url']}" in block and "**STATUS:** pending" in block:
                block = block.replace("**STATUS:** pending", "**STATUS:** approved")
                break
        out.append(block)
    with open(path, "w") as f:
        f.write("\n---\n".join(out))


def main():
    log.info("Autonomous LinkedIn commenter started.")
    log.info(f"Active hours: {ACTIVE_START_H}:00–{ACTIVE_END_H}:00 San Francisco (America/Los_Angeles)")
    log.info(f"Daily cap: {MAX_COMMENTS_PER_DAY} | Per session: {MAX_PER_SESSION}")

    while True:
        now_sf = _sf_now()

        if not _within_active_hours():
            secs = _seconds_until_active()
            wake = now_sf + timedelta(seconds=secs)
            log.info(f"Outside active hours ({now_sf.strftime('%H:%M')} SF). "
                     f"Sleeping until ~{wake.strftime('%H:%M')} SF ({secs//60}m).")
            time.sleep(secs + random.randint(0, 600))
            continue

        since_last = _minutes_since_last_session()

        if since_last < SESSION_GAP_MIN:
            wait_min = SESSION_GAP_MIN - int(since_last)
            log.info(f"Last session was {int(since_last)}m ago. Waiting {wait_min}m more.")
            time.sleep(wait_min * 60)
            continue

        # Run session
        try:
            run_session()
        except Exception as e:
            log.error(f"Session error: {e}", exc_info=True)

        # Sleep until next session
        next_gap = random.randint(SESSION_GAP_MIN, SESSION_GAP_MAX)
        next_time = _sf_now() + timedelta(minutes=next_gap)
        log.info(f"Next session in ~{next_gap}m (~{next_time.strftime('%H:%M')} SF).")
        time.sleep(next_gap * 60)


if __name__ == "__main__":
    main()
