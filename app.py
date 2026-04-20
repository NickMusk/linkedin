#!/usr/bin/env python3
"""
LinkedIn Commenter — Web Dashboard + Autonomous Loop
Serves a simple UI and runs the posting loop in a background thread.
"""
import os
import json
import threading
import time
import random
import logging
from datetime import datetime, timezone, timedelta
from flask import Flask, request, redirect, url_for, render_template_string

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

from config import DATA_DIR

STATUS_FILE   = os.path.join(DATA_DIR, "status.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
COMMENTS_LOG  = os.path.join(DATA_DIR, "comments_log.json")

DEFAULT_SETTINGS = {
    "max_per_day": 18,
    "max_per_session": 6,
    "active_start": 8,
    "active_end": 21,
    "gap_min": 150,
    "gap_max": 240,
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
        "today_count": 0,
        "date": "",
        "last_session": None,
        "next_session": None,
        "state": "idle",
        "last_error": None,
    })


def get_recent_comments(limit=30):
    return load_json(COMMENTS_LOG, [])[-limit:]


def log_comment(author, post_url, post_excerpt, comment_text):
    entries = load_json(COMMENTS_LOG, [])
    entries.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "author": author,
        "post_url": post_url,
        "excerpt": post_excerpt[:150],
        "comment": comment_text,
    })
    save_json(COMMENTS_LOG, entries[-500:])  # keep last 500


def dubai_now():
    return datetime.now(timezone.utc) + timedelta(hours=4)


def today_str():
    return dubai_now().strftime("%Y-%m-%d")


def fmt_time(iso):
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        dubai = dt + timedelta(hours=4) if dt.tzinfo else dt
        return dubai.strftime("%d %b %H:%M")
    except Exception:
        return iso


# ── Flask routes ───────────────────────────────────────────────────────────

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LinkedIn Commenter</title>
<script src="https://cdn.tailwindcss.com"></script>
<meta http-equiv="refresh" content="60">
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen p-6 font-sans">
<div class="max-w-4xl mx-auto">

  <div class="flex items-center justify-between mb-8">
    <h1 class="text-2xl font-bold">LinkedIn Commenter</h1>
    <span class="text-xs text-gray-500">auto-refreshes every 60s · Dubai time</span>
  </div>

  <!-- Status cards -->
  <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Today's comments</div>
      <div class="text-3xl font-bold {% if status.today_count >= settings.max_per_day %}text-red-400{% else %}text-green-400{% endif %}">
        {{ status.today_count }}<span class="text-lg text-gray-500">/{{ settings.max_per_day }}</span>
      </div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Status</div>
      <div class="text-lg font-semibold mt-1
        {% if status.state == 'posting' %}text-yellow-400
        {% elif status.state == 'sleeping' %}text-blue-400
        {% elif status.state == 'idle' %}text-gray-400
        {% else %}text-gray-300{% endif %}">
        {{ status.state | title }}
      </div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Last session</div>
      <div class="text-lg font-semibold mt-1">{{ fmt(status.last_session) }}</div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Next session</div>
      <div class="text-lg font-semibold mt-1 text-blue-300">{{ fmt(status.next_session) }}</div>
    </div>
  </div>

  <!-- Settings -->
  <div class="bg-gray-900 rounded-xl p-6 mb-8">
    <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-4">Settings</h2>
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
        <span class="text-xs text-gray-500">Active hours (Dubai)</span>
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
          class="bg-blue-600 hover:bg-blue-500 text-white rounded-lg px-6 py-2 font-semibold transition-colors w-full">
          Save
        </button>
      </div>
    </form>
    {% if saved %}
    <div class="mt-3 text-green-400 text-sm">✓ Settings saved</div>
    {% endif %}
    {% if status.last_error %}
    <div class="mt-3 text-red-400 text-sm">Last error: {{ status.last_error }}</div>
    {% endif %}
  </div>

  <!-- Recent comments -->
  <div class="bg-gray-900 rounded-xl p-6">
    <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-4">
      Recent Comments <span class="text-gray-600 font-normal">({{ comments|length }})</span>
    </h2>
    {% if not comments %}
    <div class="text-gray-600 text-sm">No comments posted yet.</div>
    {% else %}
    <div class="space-y-4">
      {% for c in comments|reverse %}
      <div class="border-l-2 border-gray-700 pl-4">
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

</div>
</body>
</html>
"""


@app.route("/")
def index():
    saved = request.args.get("saved")
    return render_template_string(
        TEMPLATE,
        status=get_status(),
        settings=get_settings(),
        comments=get_recent_comments(),
        fmt=fmt_time,
        saved=saved,
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
    return redirect("/?saved=1")


# ── Autonomous loop ────────────────────────────────────────────────────────

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
    h = dubai_now().hour
    return s["active_start"] <= h < s["active_end"]


def seconds_until_active(s):
    now = dubai_now()
    if now.hour < s["active_start"]:
        target = now.replace(hour=s["active_start"], minute=random.randint(0, 20), second=0)
    else:
        target = (now + timedelta(days=1)).replace(
            hour=s["active_start"], minute=random.randint(0, 20), second=0)
    return max(60, int((target - now).total_seconds()))


def run_session(s):
    from fetch_posts import fetch_all_posts
    from report import session_dir, save_posts
    from knowledge_base import build_context, save_example
    from generate_comments import generate_comments
    from publish import _extract_activity_id, _get_social_id, _post_comment, _mark_published
    from config import PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX
    import json as _json

    st = get_status()
    today = today_str()
    done = st.get("today_count", 0) if st.get("date") == today else 0
    budget = min(s["max_per_session"], s["max_per_day"] - done)

    if budget <= 0:
        log.info(f"Daily cap reached ({done}/{s['max_per_day']}). Skipping.")
        return 0

    log.info(f"Session start — budget: {budget}")
    update_status(state="fetching", last_session=datetime.now(timezone.utc).isoformat())

    posts = fetch_all_posts()
    if not posts:
        log.info("No new posts.")
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
        if not it.get("skip")
        and len(it.get("draft", "")) >= 40
    ][:budget]

    if not publishable:
        log.info("Nothing to publish.")
        update_status(state="sleeping")
        return 0

    published = 0
    update_status(state="posting")

    for i, item in enumerate(publishable):
        text = item["draft"].strip()
        url  = item["url"]
        author = item.get("author", "?")

        log.info(f"  [{i+1}/{len(publishable)}] {author[:35]}")

        aid = _extract_activity_id(url)
        sid = _get_social_id(aid)
        if not sid:
            log.warning(f"    No social_id")
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
            log.info(f"  Waiting {delay}s...")
            time.sleep(delay)

    # Update today count
    st2 = get_status()
    today2 = today_str()
    prev = st2.get("today_count", 0) if st2.get("date") == today2 else 0
    update_status(today_count=prev + published, state="sleeping")

    log.info(f"Session done: {published}/{len(publishable)} posted.")
    return published


def autonomous_loop():
    log.info("Autonomous loop started.")
    last_session_ts = 0

    while True:
        try:
            s = get_settings()

            if not within_active_hours(s):
                secs = seconds_until_active(s)
                wake = datetime.now(timezone.utc) + timedelta(seconds=secs)
                wake_dubai = wake + timedelta(hours=4)
                update_status(state="sleeping (off hours)",
                              next_session=wake.isoformat())
                log.info(f"Off hours. Sleeping {secs//60}m until {wake_dubai.strftime('%H:%M')} Dubai.")
                time.sleep(secs)
                continue

            since_last = (time.time() - last_session_ts) / 60
            if last_session_ts and since_last < s["gap_min"]:
                wait_sec = int((s["gap_min"] - since_last) * 60)
                wake = datetime.now(timezone.utc) + timedelta(seconds=wait_sec)
                update_status(state="sleeping", next_session=wake.isoformat())
                log.info(f"Too soon ({int(since_last)}m ago). Waiting {wait_sec//60}m.")
                time.sleep(wait_sec)
                continue

            # Run session
            last_session_ts = time.time()
            try:
                run_session(s)
            except Exception as e:
                log.error(f"Session error: {e}", exc_info=True)
                update_status(state="error", last_error=str(e)[:200])

            # Schedule next
            s = get_settings()
            gap = random.randint(s["gap_min"], s["gap_max"])
            wake = datetime.now(timezone.utc) + timedelta(minutes=gap)
            wake_dubai = wake + timedelta(hours=4)
            update_status(next_session=wake.isoformat())
            log.info(f"Next session in {gap}m (~{wake_dubai.strftime('%H:%M')} Dubai).")
            last_session_ts = time.time()
            time.sleep(gap * 60)

        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)
            time.sleep(300)


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=autonomous_loop, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
