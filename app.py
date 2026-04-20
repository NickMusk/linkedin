#!/usr/bin/env python3
"""
LinkedIn + Twitter Commenter — Web Dashboard + Autonomous Loops
"""
import os
import json
import threading
import time
import random
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, redirect, render_template_string

SF_TZ = ZoneInfo("America/Los_Angeles")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

from config import DATA_DIR

STATUS_FILE        = os.path.join(DATA_DIR, "status.json")
SETTINGS_FILE      = os.path.join(DATA_DIR, "settings.json")
COMMENTS_LOG       = os.path.join(DATA_DIR, "comments_log.json")
TW_STATUS_FILE     = os.path.join(DATA_DIR, "twitter_status.json")
TW_LOG             = os.path.join(DATA_DIR, "twitter_log.json")

DEFAULT_SETTINGS = {
    # LinkedIn
    "max_per_day":     18,
    "max_per_session":  6,
    "active_start":     8,
    "active_end":      21,
    "gap_min":        150,
    "gap_max":        240,
    # Twitter
    "tw_max_per_day":       20,
    "tw_max_per_session":    8,
    "tw_gap_min":          240,
    "tw_gap_max":          480,
    "tw_reply_delay_min":  180,
    "tw_reply_delay_max":  240,
}

app = Flask(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default.copy() if isinstance(default, dict) else default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_settings():
    s = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    for k, v in DEFAULT_SETTINGS.items():
        s.setdefault(k, v)
    return s


def get_status():
    return load_json(STATUS_FILE, {
        "today_count": 0, "date": "", "last_session": None,
        "next_session": None, "state": "idle", "last_error": None,
    })


def get_tw_status():
    return load_json(TW_STATUS_FILE, {
        "today_count": 0, "date": "", "last_session": None,
        "next_session": None, "state": "idle", "last_error": None,
    })


def get_recent_comments(limit=30):
    return load_json(COMMENTS_LOG, [])[-limit:]


def get_recent_tw_replies(limit=20):
    return load_json(TW_LOG, [])[-limit:]


def log_comment(author, post_url, post_excerpt, comment_text):
    entries = load_json(COMMENTS_LOG, [])
    entries.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "author": author, "post_url": post_url,
        "excerpt": post_excerpt[:150], "comment": comment_text,
    })
    save_json(COMMENTS_LOG, entries[-500:])


def log_tw_reply(author, tweet_url, tweet_text, reply_text):
    entries = load_json(TW_LOG, [])
    entries.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "author": author, "tweet_url": tweet_url,
        "excerpt": tweet_text[:150], "reply": reply_text,
    })
    save_json(TW_LOG, entries[-500:])


def local_now():
    return datetime.now(SF_TZ)


def today_str():
    return local_now().strftime("%Y-%m-%d")


def fmt_time(iso):
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        local = dt.astimezone(SF_TZ) if dt.tzinfo else dt
        return local.strftime("%d %b %H:%M")
    except Exception:
        return iso


# ── Flask routes ───────────────────────────────────────────────────────────

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Commenter Dashboard</title>
<script src="https://cdn.tailwindcss.com"></script>
<meta http-equiv="refresh" content="60">
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen p-6 font-sans">
<div class="max-w-5xl mx-auto">

  <div class="flex items-center justify-between mb-8">
    <h1 class="text-2xl font-bold">Commenter Dashboard</h1>
    <span class="text-xs text-gray-500">auto-refreshes every 60s · SF time</span>
  </div>

  <!-- ── LinkedIn ── -->
  <h2 class="text-xs font-semibold text-blue-400 uppercase tracking-widest mb-3">LinkedIn</h2>

  <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Today's comments</div>
      <div class="text-3xl font-bold {% if li.today_count >= settings.max_per_day %}text-red-400{% else %}text-green-400{% endif %}">
        {{ li.today_count }}<span class="text-lg text-gray-500">/{{ settings.max_per_day }}</span>
      </div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Status</div>
      <div class="text-lg font-semibold mt-1
        {% if li.state == 'posting' %}text-yellow-400
        {% elif li.state == 'sleeping' %}text-blue-400
        {% elif li.state == 'idle' %}text-gray-400
        {% else %}text-gray-300{% endif %}">{{ li.state | title }}</div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Last session</div>
      <div class="text-lg font-semibold mt-1">{{ fmt(li.last_session) }}</div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Next session</div>
      <div class="text-lg font-semibold mt-1 text-blue-300">{{ fmt(li.next_session) }}</div>
    </div>
  </div>

  <!-- LinkedIn Settings -->
  <div class="bg-gray-900 rounded-xl p-6 mb-6">
    <h3 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-4">LinkedIn Settings</h3>
    <form method="POST" action="/settings" class="grid grid-cols-2 md:grid-cols-3 gap-4">
      <label class="flex flex-col gap-1">
        <span class="text-xs text-gray-500">Max comments/day</span>
        <input type="number" name="max_per_day" value="{{ settings.max_per_day }}" min="1" max="50"
          class="bg-gray-800 rounded-lg px-3 py-2 text-white border border-gray-700 focus:border-blue-500 outline-none">
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-xs text-gray-500">Max per session</span>
        <input type="number" name="max_per_session" value="{{ settings.max_per_session }}" min="1" max="20"
          class="bg-gray-800 rounded-lg px-3 py-2 text-white border border-gray-700 focus:border-blue-500 outline-none">
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-xs text-gray-500">Active hours (SF)</span>
        <div class="flex gap-2 items-center">
          <input type="number" name="active_start" value="{{ settings.active_start }}" min="0" max="23"
            class="bg-gray-800 rounded-lg px-3 py-2 text-white border border-gray-700 focus:border-blue-500 outline-none w-20">
          <span class="text-gray-500">–</span>
          <input type="number" name="active_end" value="{{ settings.active_end }}" min="0" max="23"
            class="bg-gray-800 rounded-lg px-3 py-2 text-white border border-gray-700 focus:border-blue-500 outline-none w-20">
        </div>
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-xs text-gray-500">Session gap min (min)</span>
        <input type="number" name="gap_min" value="{{ settings.gap_min }}" min="30" max="480"
          class="bg-gray-800 rounded-lg px-3 py-2 text-white border border-gray-700 focus:border-blue-500 outline-none">
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-xs text-gray-500">Session gap max (min)</span>
        <input type="number" name="gap_max" value="{{ settings.gap_max }}" min="30" max="480"
          class="bg-gray-800 rounded-lg px-3 py-2 text-white border border-gray-700 focus:border-blue-500 outline-none">
      </label>
      <div class="flex items-end">
        <button type="submit"
          class="bg-blue-600 hover:bg-blue-500 text-white rounded-lg px-6 py-2 font-semibold transition-colors w-full">Save LinkedIn</button>
      </div>
    </form>
    {% if li.last_error %}
    <div class="mt-3 text-red-400 text-sm">Last error: {{ li.last_error }}</div>
    {% endif %}
  </div>

  <!-- Recent LinkedIn comments -->
  <div class="bg-gray-900 rounded-xl p-6 mb-10">
    <h3 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-4">
      Recent LinkedIn Comments <span class="text-gray-600 font-normal">({{ li_comments|length }})</span>
    </h3>
    {% if not li_comments %}
    <div class="text-gray-600 text-sm">No comments posted yet.</div>
    {% else %}
    <div class="space-y-4">
      {% for c in li_comments|reverse %}
      <div class="border-l-2 border-blue-800 pl-4">
        <div class="flex items-center gap-3 mb-1">
          <span class="text-xs text-gray-500">{{ fmt(c.ts) }}</span>
          <a href="{{ c.post_url }}" target="_blank"
            class="text-xs text-blue-400 hover:text-blue-300 truncate max-w-xs">{{ c.author }}</a>
        </div>
        <p class="text-xs text-gray-500 mb-1 italic truncate">{{ c.excerpt }}</p>
        <p class="text-sm text-gray-200">{{ c.comment }}</p>
      </div>
      {% endfor %}
    </div>
    {% endif %}
  </div>

  <!-- ── Twitter ── -->
  <h2 class="text-xs font-semibold text-sky-400 uppercase tracking-widest mb-3">Twitter / X</h2>

  <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Today's replies</div>
      <div class="text-3xl font-bold {% if tw.today_count >= settings.tw_max_per_day %}text-red-400{% else %}text-green-400{% endif %}">
        {{ tw.today_count }}<span class="text-lg text-gray-500">/{{ settings.tw_max_per_day }}</span>
      </div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Status</div>
      <div class="text-lg font-semibold mt-1
        {% if tw.state == 'posting' %}text-yellow-400
        {% elif tw.state == 'sleeping' %}text-sky-400
        {% elif tw.state == 'idle' %}text-gray-400
        {% else %}text-gray-300{% endif %}">{{ tw.state | title }}</div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Last session</div>
      <div class="text-lg font-semibold mt-1">{{ fmt(tw.last_session) }}</div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Next session</div>
      <div class="text-lg font-semibold mt-1 text-sky-300">{{ fmt(tw.next_session) }}</div>
    </div>
  </div>

  <!-- Twitter Settings -->
  <div class="bg-gray-900 rounded-xl p-6 mb-6">
    <h3 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-4">Twitter Settings</h3>
    <form method="POST" action="/settings/twitter" class="grid grid-cols-2 md:grid-cols-3 gap-4">
      <label class="flex flex-col gap-1">
        <span class="text-xs text-gray-500">Max replies/day</span>
        <input type="number" name="tw_max_per_day" value="{{ settings.tw_max_per_day }}" min="1" max="50"
          class="bg-gray-800 rounded-lg px-3 py-2 text-white border border-gray-700 focus:border-sky-500 outline-none">
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-xs text-gray-500">Max per session</span>
        <input type="number" name="tw_max_per_session" value="{{ settings.tw_max_per_session }}" min="1" max="20"
          class="bg-gray-800 rounded-lg px-3 py-2 text-white border border-gray-700 focus:border-sky-500 outline-none">
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-xs text-gray-500">Session gap min (min)</span>
        <input type="number" name="tw_gap_min" value="{{ settings.tw_gap_min }}" min="60" max="720"
          class="bg-gray-800 rounded-lg px-3 py-2 text-white border border-gray-700 focus:border-sky-500 outline-none">
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-xs text-gray-500">Session gap max (min)</span>
        <input type="number" name="tw_gap_max" value="{{ settings.tw_gap_max }}" min="60" max="720"
          class="bg-gray-800 rounded-lg px-3 py-2 text-white border border-gray-700 focus:border-sky-500 outline-none">
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-xs text-gray-500">Delay between replies (min s)</span>
        <input type="number" name="tw_reply_delay_min" value="{{ settings.tw_reply_delay_min }}" min="60" max="600"
          class="bg-gray-800 rounded-lg px-3 py-2 text-white border border-gray-700 focus:border-sky-500 outline-none">
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-xs text-gray-500">Delay between replies (max s)</span>
        <input type="number" name="tw_reply_delay_max" value="{{ settings.tw_reply_delay_max }}" min="60" max="600"
          class="bg-gray-800 rounded-lg px-3 py-2 text-white border border-gray-700 focus:border-sky-500 outline-none">
      </label>
      <div class="flex items-end col-span-2 md:col-span-3">
        <button type="submit"
          class="bg-sky-600 hover:bg-sky-500 text-white rounded-lg px-6 py-2 font-semibold transition-colors">Save Twitter</button>
      </div>
    </form>
    {% if tw.last_error %}
    <div class="mt-3 text-red-400 text-sm">Last error: {{ tw.last_error }}</div>
    {% endif %}
  </div>

  <!-- Recent Twitter replies -->
  <div class="bg-gray-900 rounded-xl p-6">
    <h3 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-4">
      Recent Twitter Replies <span class="text-gray-600 font-normal">({{ tw_replies|length }})</span>
    </h3>
    {% if not tw_replies %}
    <div class="text-gray-600 text-sm">No replies posted yet.</div>
    {% else %}
    <div class="space-y-4">
      {% for r in tw_replies|reverse %}
      <div class="border-l-2 border-sky-800 pl-4">
        <div class="flex items-center gap-3 mb-1">
          <span class="text-xs text-gray-500">{{ fmt(r.ts) }}</span>
          <a href="{{ r.tweet_url }}" target="_blank"
            class="text-xs text-sky-400 hover:text-sky-300 truncate max-w-xs">{{ r.author }}</a>
        </div>
        <p class="text-xs text-gray-500 mb-1 italic truncate">{{ r.excerpt }}</p>
        <p class="text-sm text-gray-200">{{ r.reply }}</p>
      </div>
      {% endfor %}
    </div>
    {% endif %}
  </div>

</div>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        TEMPLATE,
        li=get_status(),
        tw=get_tw_status(),
        settings=get_settings(),
        li_comments=get_recent_comments(),
        tw_replies=get_recent_tw_replies(),
        fmt=fmt_time,
    )


@app.route("/settings", methods=["POST"])
def save_settings():
    s = get_settings()
    for key in ("max_per_day", "max_per_session", "active_start", "active_end", "gap_min", "gap_max"):
        try:
            s[key] = int(request.form[key])
        except Exception:
            pass
    save_json(SETTINGS_FILE, s)
    return redirect("/")


@app.route("/settings/twitter", methods=["POST"])
def save_tw_settings():
    s = get_settings()
    for key in ("tw_max_per_day", "tw_max_per_session", "tw_gap_min", "tw_gap_max",
                "tw_reply_delay_min", "tw_reply_delay_max"):
        try:
            s[key] = int(request.form[key])
        except Exception:
            pass
    save_json(SETTINGS_FILE, s)
    return redirect("/")


# ── LinkedIn autonomous loop ───────────────────────────────────────────────

def update_status(**kwargs):
    st = get_status()
    today = today_str()
    if st.get("date") != today:
        st = {"date": today, "today_count": 0, "last_session": None,
              "next_session": None, "state": "idle", "last_error": None}
    st.update(kwargs)
    st["date"] = today
    save_json(STATUS_FILE, st)


def within_active_hours(s):
    h = local_now().hour
    return s["active_start"] <= h < s["active_end"]


def seconds_until_active(s):
    now = local_now()
    if now.hour < s["active_start"]:
        target = now.replace(hour=s["active_start"], minute=random.randint(0, 20), second=0, microsecond=0)
    else:
        target = (now + timedelta(days=1)).replace(
            hour=s["active_start"], minute=random.randint(0, 20), second=0, microsecond=0)
    return max(60, int((target - now).total_seconds()))


def run_linkedin_session(s):
    from fetch_posts import fetch_all_posts
    from report import session_dir, save_posts
    from knowledge_base import build_context, save_example
    from generate_comments import generate_comments
    from publish import _extract_activity_id, _get_social_id, _post_comment
    from config import PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX
    import json as _json

    st = get_status()
    today = today_str()
    done = st.get("today_count", 0) if st.get("date") == today else 0
    budget = min(s["max_per_session"], s["max_per_day"] - done)

    if budget <= 0:
        log.info(f"LinkedIn: daily cap reached ({done}/{s['max_per_day']}). Skipping.")
        return 0

    log.info(f"LinkedIn session start — budget: {budget}")
    update_status(state="fetching", last_session=datetime.now(timezone.utc).isoformat())

    posts = fetch_all_posts()
    if not posts:
        log.info("LinkedIn: no new posts.")
        update_status(state="sleeping")
        return 0

    d = session_dir()
    save_posts(posts, d)
    with open(os.path.join(d, "posts.json"), "w") as f:
        _json.dump(posts, f, ensure_ascii=False, indent=2)

    update_status(state="generating")
    kb = build_context()
    items = generate_comments(posts, kb)

    publishable = [
        it for it in items
        if not it.get("skip") and len(it.get("draft", "")) >= 40
    ][:budget]

    if not publishable:
        log.info("LinkedIn: nothing to publish.")
        update_status(state="sleeping")
        return 0

    published = 0
    update_status(state="posting")

    for i, item in enumerate(publishable):
        text   = item["draft"].strip()
        url    = item["url"]
        author = item.get("author", "?")

        log.info(f"  LinkedIn [{i+1}/{len(publishable)}] {author[:35]}")
        aid = _extract_activity_id(url)
        sid = _get_social_id(aid)
        if not sid:
            log.warning("    No social_id")
            continue

        ok, detail = _post_comment(sid, text)
        if ok:
            log.info(f"    OK: {detail}")
            save_example(item.get("text", url), text)
            log_comment(author, url, item.get("text", ""), text)
            published += 1
        else:
            log.warning(f"    Failed: {detail}")

        if i < len(publishable) - 1:
            delay = random.randint(PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX)
            log.info(f"  LinkedIn: waiting {delay}s...")
            time.sleep(delay)

    st2 = get_status()
    today2 = today_str()
    prev = st2.get("today_count", 0) if st2.get("date") == today2 else 0
    update_status(today_count=prev + published, state="sleeping")
    log.info(f"LinkedIn session done: {published}/{len(publishable)} posted.")
    return published


def linkedin_loop():
    log.info("LinkedIn loop started.")
    last_session_ts = 0

    while True:
        try:
            s = get_settings()

            if not within_active_hours(s):
                secs = seconds_until_active(s)
                wake = datetime.now(timezone.utc) + timedelta(seconds=secs)
                wake_sf = wake.astimezone(SF_TZ)
                update_status(state="sleeping (off hours)", next_session=wake.isoformat())
                log.info(f"LinkedIn: off hours. Sleeping {secs//60}m until {wake_sf.strftime('%H:%M')} SF.")
                time.sleep(secs)
                continue

            since_last = (time.time() - last_session_ts) / 60
            if last_session_ts and since_last < s["gap_min"]:
                wait_sec = int((s["gap_min"] - since_last) * 60)
                wake = datetime.now(timezone.utc) + timedelta(seconds=wait_sec)
                update_status(state="sleeping", next_session=wake.isoformat())
                log.info(f"LinkedIn: too soon ({int(since_last)}m ago). Waiting {wait_sec//60}m.")
                time.sleep(wait_sec)
                continue

            last_session_ts = time.time()
            try:
                run_linkedin_session(s)
            except Exception as e:
                log.error(f"LinkedIn session error: {e}", exc_info=True)
                update_status(state="error", last_error=str(e)[:200])

            s = get_settings()
            gap = random.randint(s["gap_min"], s["gap_max"])
            wake = datetime.now(timezone.utc) + timedelta(minutes=gap)
            wake_sf = wake.astimezone(SF_TZ)
            update_status(next_session=wake.isoformat())
            log.info(f"LinkedIn: next session in {gap}m (~{wake_sf.strftime('%H:%M')} SF).")
            last_session_ts = time.time()
            time.sleep(gap * 60)

        except Exception as e:
            log.error(f"LinkedIn loop error: {e}", exc_info=True)
            time.sleep(300)


# ── Twitter autonomous loop ────────────────────────────────────────────────

def update_tw_status(**kwargs):
    st = get_tw_status()
    today = today_str()
    if st.get("date") != today:
        st = {"date": today, "today_count": 0, "last_session": None,
              "next_session": None, "state": "idle", "last_error": None}
    st.update(kwargs)
    st["date"] = today
    save_json(TW_STATUS_FILE, st)


def twitter_loop():
    log.info("Twitter loop started.")
    last_session_ts = 0

    while True:
        try:
            s = get_settings()

            since_last = (time.time() - last_session_ts) / 60
            if last_session_ts and since_last < s["tw_gap_min"]:
                wait_sec = int((s["tw_gap_min"] - since_last) * 60)
                wake = datetime.now(timezone.utc) + timedelta(seconds=wait_sec)
                update_tw_status(state="sleeping", next_session=wake.isoformat())
                log.info(f"Twitter: too soon ({int(since_last)}m ago). Waiting {wait_sec//60}m.")
                time.sleep(wait_sec)
                continue

            tw_st = get_tw_status()
            today = today_str()
            done = tw_st.get("today_count", 0) if tw_st.get("date") == today else 0

            if done >= s["tw_max_per_day"]:
                secs = seconds_until_active({"active_start": 0, "active_end": 24})
                wake = datetime.now(timezone.utc) + timedelta(seconds=3600)
                update_tw_status(state="sleeping (daily cap)", next_session=wake.isoformat())
                log.info(f"Twitter: daily cap reached ({done}/{s['tw_max_per_day']}). Sleeping 1h.")
                time.sleep(3600)
                continue

            last_session_ts = time.time()
            update_tw_status(state="starting", last_session=datetime.now(timezone.utc).isoformat())

            s["_tw_today_count"] = done

            from publish_tweets import run_twitter_session
            try:
                posted = run_twitter_session(
                    settings=s,
                    log_fn=log_tw_reply,
                    update_status_fn=update_tw_status,
                )
            except Exception as e:
                log.error(f"Twitter session error: {e}", exc_info=True)
                update_tw_status(state="error", last_error=str(e)[:200])
                posted = 0

            tw_st2 = get_tw_status()
            today2 = today_str()
            prev = tw_st2.get("today_count", 0) if tw_st2.get("date") == today2 else 0
            new_count = prev + posted

            s2 = get_settings()
            gap = random.randint(s2["tw_gap_min"], s2["tw_gap_max"])
            wake = datetime.now(timezone.utc) + timedelta(minutes=gap)
            update_tw_status(today_count=new_count, state="sleeping", next_session=wake.isoformat())
            log.info(f"Twitter: next session in {gap}m (~{wake.astimezone(SF_TZ).strftime('%H:%M')} SF).")
            last_session_ts = time.time()
            time.sleep(gap * 60)

        except Exception as e:
            log.error(f"Twitter loop error: {e}", exc_info=True)
            time.sleep(300)


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=linkedin_loop, daemon=True, name="linkedin").start()
    threading.Thread(target=twitter_loop, daemon=True, name="twitter").start()

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
