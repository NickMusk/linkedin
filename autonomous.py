#!/usr/bin/env python3
"""
Autonomous LinkedIn comment poster — multi-account.
Runs as a Render Background Worker.

Schedule logic:
- Active hours: 09:00–18:00 San Francisco time (America/Los_Angeles, DST-aware)
- Each account gets 3-4 sessions per day, randomised intervals (~2.5-4 hours apart)
- Per session: fetch posts → generate comments → auto-publish
- Daily cap and per-session cap are configurable per account
- Accounts are pulled live from Unipile; no local list to maintain
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

SF_TZ          = ZoneInfo("America/Los_Angeles")
ACTIVE_START_H = 9
ACTIVE_END_H   = 18

MAX_PER_SESSION  = 6    # default per-session cap (overridden by account config)
SESSION_GAP_MIN  = 150  # min minutes between sessions for the same account
SESSION_GAP_MAX  = 270
CHECK_INTERVAL   = 10   # minutes between account-loop iterations
VC_DAILY_CAP     = 15   # max VC comments per Nick's account per day
VC_SESSION_GAP   = 240  # min minutes between VC sessions

from config import DATA_DIR


def _sf_now() -> datetime:
    return datetime.now(SF_TZ)


def _today_str() -> str:
    return _sf_now().strftime("%Y-%m-%d")


def _within_active_hours() -> bool:
    h = _sf_now().hour
    return ACTIVE_START_H <= h < ACTIVE_END_H


def _seconds_until_active() -> int:
    now = _sf_now()
    if now.hour < ACTIVE_START_H:
        target = now.replace(hour=ACTIVE_START_H, minute=random.randint(0, 30), second=0, microsecond=0)
    else:
        target = (now + timedelta(days=1)).replace(
            hour=ACTIVE_START_H, minute=random.randint(0, 30), second=0, microsecond=0
        )
    return max(0, int((target - now).total_seconds()))


def _comments_today(state: dict) -> int:
    if state.get("date") != _today_str():
        return 0
    return state.get("count", 0)


def _record_comments(account_id: str, n: int):
    from accounts import get_account_state, save_account_state
    state = get_account_state(account_id)
    today = _today_str()
    if state.get("date") != today:
        state = {"date": today, "count": 0, "last_session_ts": state.get("last_session_ts", 0)}
    state["count"] = state.get("count", 0) + n
    state["last_session_ts"] = time.time()
    save_account_state(account_id, state)


def _minutes_since_last_session(account_id: str) -> float:
    from accounts import get_account_state
    last = get_account_state(account_id).get("last_session_ts", 0)
    if not last:
        return 9999
    return (time.time() - last) / 60


def run_session(account_id: str, account_name: str, daily_cap: int, kb_path: str = None, system_prompt: str = None, min_likes: int = None):
    """One full session for a single account: fetch → generate → auto-publish."""
    log.info(f"=== Session starting [{account_name}] ===")

    from accounts import get_account_state
    state = get_account_state(account_id)
    done = _comments_today(state)
    budget = min(MAX_PER_SESSION, daily_cap - done)

    if budget <= 0:
        log.info(f"[{account_name}] Daily cap reached ({done}/{daily_cap}). Skipping.")
        return 0

    log.info(f"[{account_name}] Daily budget remaining: {daily_cap - done}. Will post up to {budget}.")

    from fetch_posts import fetch_all_posts
    from report import session_dir, save_posts, save_comments
    from knowledge_base import build_context
    from generate_comments import generate_comments
    from publish import _extract_activity_id, _get_social_id, _post_comment, _mark_published
    from knowledge_base import save_example
    from fetch_posts import mark_url_published
    from config import PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX
    import json as _json

    posts = fetch_all_posts(account_id=account_id, min_likes=min_likes)
    if not posts:
        log.info(f"[{account_name}] No new posts found.")
        return 0

    d = session_dir()
    save_posts(posts, d)
    with open(os.path.join(d, "posts.json"), "w") as f:
        _json.dump(posts, f, ensure_ascii=False, indent=2)

    log.info(f"[{account_name}] Building knowledge base...")
    kb = build_context(kb_path=kb_path)
    log.info(f"[{account_name}] KB: {len(kb):,} chars")

    log.info(f"[{account_name}] Generating comments...")
    items = generate_comments(posts, kb, system_prompt=system_prompt)

    publishable = [
        it for it in items
        if not it.get("skip")
        and it.get("draft", "").strip()
        and len(it.get("draft", "")) >= 40
        and not it.get("draft", "").startswith("[")
    ][:budget]

    if not publishable:
        log.info(f"[{account_name}] Nothing to publish after filtering.")
        save_comments(items, d)
        return 0

    _save_auto_comments(items, publishable, d)
    comments_path = os.path.join(d, "comments.md")

    published = 0
    for i, item in enumerate(publishable):
        text = item["draft"].strip()
        url = item["url"]
        author = item.get("author", "?")[:35]

        log.info(f"  [{account_name}] [{i+1}/{len(publishable)}] {author}")

        aid = _extract_activity_id(url)
        sid = _get_social_id(aid, account_id=account_id)
        if not sid:
            log.warning(f"    No social_id for {url[:60]}")
            continue

        ok, detail = _post_comment(sid, text, account_id=account_id)
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

    _record_comments(account_id, published)
    log.info(f"=== Session done [{account_name}]: {published}/{len(publishable)} posted ===")
    return published


def _log_comment(author: str, post_url: str, post_text: str, comment_text: str):
    """Append to comments_log.json so the dashboard picks it up."""
    import json as _json
    path = os.path.join(DATA_DIR, "comments_log.json")
    try:
        with open(path) as f:
            entries = _json.load(f)
    except Exception:
        entries = []
    entries.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "author": author,
        "post_url": post_url,
        "excerpt": post_text[:150],
        "comment": comment_text,
        "source": "vc_watchlist",
    })
    with open(path, "w") as f:
        _json.dump(entries[-500:], f, ensure_ascii=False, indent=2)


def run_vc_session(account_id: str, account_name: str, kb_path: str = None, system_prompt: str = None):
    """VC watchlist session: fetch VC posts → generate → publish. Up to VC_DAILY_CAP/day."""
    from fetch_vc_posts import fetch_vc_posts, record_vc_interaction, load_vc_state
    from report import session_dir, save_posts, save_comments
    from knowledge_base import build_context
    from generate_comments import generate_comments, VC_SYSTEM_PROMPT
    from publish import _extract_activity_id, _get_social_id, _post_comment, _mark_published
    from knowledge_base import save_example
    from fetch_posts import mark_url_published
    from accounts import get_account_state
    from config import PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX
    import json as _json

    state = get_account_state(account_id)
    vc_done = state.get("vc_count_today", 0) if state.get("date") == _today_str() else 0
    budget = VC_DAILY_CAP - vc_done
    if budget <= 0:
        log.info(f"[{account_name}] VC daily cap reached ({vc_done}/{VC_DAILY_CAP}). Skipping.")
        return 0

    last_vc_ts = state.get("last_vc_session_ts", 0)
    minutes_since = (time.time() - last_vc_ts) / 60 if last_vc_ts else 9999
    if minutes_since < VC_SESSION_GAP:
        log.info(f"[{account_name}] VC session gap not met ({int(minutes_since)}m < {VC_SESSION_GAP}m). Skipping.")
        return 0

    log.info(f"=== VC Session starting [{account_name}] (budget: {budget}) ===")

    posts = fetch_vc_posts(account_id=account_id)
    if not posts:
        log.info(f"[{account_name}] No new VC posts found.")
        return 0

    d = session_dir()
    save_posts(posts, d)
    with open(os.path.join(d, "vc_posts.json"), "w") as f:
        _json.dump(posts, f, ensure_ascii=False, indent=2)

    kb = build_context(kb_path=kb_path)
    # Always use VC-specific prompt regardless of per-account system_prompt override
    items = generate_comments(posts, kb, system_prompt=VC_SYSTEM_PROMPT)

    publishable = [
        it for it in items
        if not it.get("skip")
        and it.get("draft", "").strip()
        and len(it.get("draft", "")) >= 40
        and not it.get("draft", "").startswith("[")
    ][:budget]

    if not publishable:
        log.info(f"[{account_name}] No publishable VC comments after filtering.")
        save_comments(items, d)
        return 0

    _save_auto_comments(items, publishable, d)
    comments_path = os.path.join(d, "comments.md")

    published = 0
    for i, item in enumerate(publishable):
        text = item["draft"].strip()
        url = item["url"]
        author = item.get("author", "?")[:35]
        author_url = item.get("author_url", "")

        log.info(f"  [{account_name}] VC [{i+1}/{len(publishable)}] {author}")

        aid = _extract_activity_id(url)
        sid = _get_social_id(aid, account_id=account_id)
        if not sid:
            log.warning(f"    No social_id for {url[:60]}")
            continue

        ok, detail = _post_comment(sid, text, account_id=account_id)
        if ok:
            log.info(f"    VC Posted ✓ ({detail})")
            save_example(item.get("text", url), text)
            _mark_published(comments_path, url)
            mark_url_published(url)
            if author_url:
                record_vc_interaction(author_url, url)
            _log_comment(author, url, item.get("text", ""), text)
            published += 1
        else:
            log.warning(f"    VC Failed: {detail}")

        if i < len(publishable) - 1:
            delay = random.randint(PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX)
            log.info(f"  Waiting {delay}s...")
            time.sleep(delay)

    # Update VC-specific counters in account state
    from accounts import get_account_state as _gas, save_account_state as _sas
    st = _gas(account_id)
    today = _today_str()
    if st.get("date") != today:
        st = {"date": today, "count": st.get("count", 0), "last_session_ts": st.get("last_session_ts", 0)}
    st["vc_count_today"] = st.get("vc_count_today", 0) + published
    st["last_vc_session_ts"] = time.time()
    _sas(account_id, st)

    log.info(f"=== VC Session done [{account_name}]: {published}/{len(publishable)} posted ===")
    return published


def _save_auto_comments(items, publishable, directory):
    from report import save_comments
    import re
    publishable_urls = {it["url"] for it in publishable}
    for it in items:
        if it["url"] in publishable_urls:
            it["_override_status"] = "approved"
        elif it.get("skip"):
            it["_override_status"] = "rejected"
    save_comments(items, directory)
    path = os.path.join(directory, "comments.md")
    with open(path) as f:
        content = f.read()
    blocks = content.split("\n---\n")
    out = []
    for block in blocks:
        for it in publishable:
            if f"**URL:** {it['url']}" in block and "**STATUS:** pending" in block:
                block = block.replace("**STATUS:** pending", "**STATUS:** approved")
                break
        out.append(block)
    with open(path, "w") as f:
        f.write("\n---\n".join(out))


def _load_system_prompt(path: str) -> str | None:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def main():
    log.info("Autonomous LinkedIn commenter started (multi-account).")
    log.info(f"Active hours: {ACTIVE_START_H}:00–{ACTIVE_END_H}:00 San Francisco")
    log.info(f"Per session: {MAX_PER_SESSION} | Session gap: {SESSION_GAP_MIN}-{SESSION_GAP_MAX}m")

    while True:
        if not _within_active_hours():
            secs = _seconds_until_active()
            wake = _sf_now() + timedelta(seconds=secs)
            log.info(
                f"Outside active hours ({_sf_now().strftime('%H:%M')} SF). "
                f"Sleeping until ~{wake.strftime('%H:%M')} SF ({secs//60}m)."
            )
            time.sleep(secs + random.randint(0, 600))
            continue

        from accounts import list_linkedin_accounts, get_account_config

        try:
            accounts = list_linkedin_accounts()
        except Exception as e:
            log.error(f"Could not fetch accounts from Unipile: {e}")
            time.sleep(CHECK_INTERVAL * 60)
            continue

        if not accounts:
            log.warning("No LinkedIn accounts found in Unipile. Sleeping.")
            time.sleep(CHECK_INTERVAL * 60)
            continue

        log.info(f"Found {len(accounts)} LinkedIn account(s) in Unipile.")

        for account in accounts:
            account_id = account.get("id") or account.get("account_id", "")
            if not account_id:
                continue

            config = get_account_config(account_id)
            if not config.get("active", True):
                log.info(f"Account {account_id} is inactive, skipping.")
                continue

            name = config.get("name") or account.get("name") or account_id[:12]
            daily_cap = config.get("daily_cap", 10)
            since_last = _minutes_since_last_session(account_id)

            if since_last < SESSION_GAP_MIN:
                log.info(
                    f"[{name}] Last session {int(since_last)}m ago "
                    f"(need {SESSION_GAP_MIN}m gap). Skipping."
                )
                continue

            kb_path = config.get("kb_path")
            system_prompt = _load_system_prompt(config.get("system_prompt_path"))
            min_likes = config.get("min_likes")

            from config import UNIPILE_ACCOUNT_ID as _NICK_ACCOUNT_ID
            from generate_comments import GENERIC_SYSTEM_PROMPT
            if system_prompt is None and account_id != _NICK_ACCOUNT_ID:
                system_prompt = GENERIC_SYSTEM_PROMPT

            # VC watchlist is priority — runs first, Nick's account only
            if account_id == _NICK_ACCOUNT_ID:
                try:
                    run_vc_session(
                        account_id=account_id,
                        account_name=name,
                        kb_path=kb_path,
                        system_prompt=system_prompt,
                    )
                except Exception as e:
                    log.error(f"VC session error [{name}]: {e}", exc_info=True)

            try:
                run_session(
                    account_id=account_id,
                    account_name=name,
                    daily_cap=daily_cap,
                    kb_path=kb_path,
                    system_prompt=system_prompt,
                    min_likes=min_likes,
                )
            except Exception as e:
                log.error(f"Session error [{name}]: {e}", exc_info=True)

        log.info(f"All accounts checked. Next check in {CHECK_INTERVAL}m.")
        time.sleep(CHECK_INTERVAL * 60)


if __name__ == "__main__":
    main()
