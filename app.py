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
TW_QUEUE_FILE      = os.path.join(DATA_DIR, "twitter_queue.json")

_PREV_DEFAULTS = {
    "max_per_day": 18, "max_per_session": 6, "active_start": 0, "active_end": 23,
    "gap_min": 45, "gap_max": 120,
    "tw_max_per_day": 20, "tw_max_per_session": 8, "tw_gap_min": 240, "tw_gap_max": 480,
    "tw_max_per_day": 50, "tw_max_per_session": 15, "tw_gap_min": 45, "tw_gap_max": 120,
    "tw_reply_delay_min": 180, "tw_reply_delay_max": 240,
    "tw_reply_delay_min": 120, "tw_reply_delay_max": 360,
}

DEFAULT_SETTINGS = {
    # LinkedIn — daytime SF hours (8–21), randomised gaps 150–240 min
    "max_per_day":     50,
    "max_per_session": 12,
    "active_start":     8,
    "active_end":      21,
    "gap_min":        150,
    "gap_max":        240,
    # Twitter — 3/day, 1/session, gaps 3–4h
    "tw_max_per_day":      3,
    "tw_max_per_session":  1,
    "tw_gap_min":        180,
    "tw_gap_max":        240,
    "tw_reply_delay_min":  60,
    "tw_reply_delay_max": 120,
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
    changed = False
    for k, new_v in DEFAULT_SETTINGS.items():
        if k not in s:
            s[k] = new_v
            changed = True
        elif s[k] == _PREV_DEFAULTS.get(k):
            # Value was never customised — update to new default
            s[k] = new_v
            changed = True
    if changed:
        save_json(SETTINGS_FILE, s)
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
        "auth_ok": True, "last_post_at": None, "last_auth_error": None,
    })


# A Twitter session can legitimately post nothing, so "hours since last reply"
# alone is noisy. We only call it unhealthy after this long with zero posts.
TW_STALE_HOURS = 24


def twitter_health() -> dict:
    """Health snapshot for the Twitter loop. healthy=False means it needs a human
    (almost always: refresh TWITTER_AUTH_TOKEN / TWITTER_CT0)."""
    st = get_tw_status()
    reasons = []

    if st.get("auth_ok") is False:
        reasons.append("auth_failed: " + (st.get("last_auth_error") or "Twitter cookies rejected"))

    last_post_at = st.get("last_post_at")
    hours_since = None
    if last_post_at:
        try:
            dt = datetime.fromisoformat(last_post_at)
            hours_since = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            if hours_since > TW_STALE_HOURS:
                reasons.append(f"stale: no reply posted in {int(hours_since)}h (>{TW_STALE_HOURS}h)")
        except Exception:
            pass

    return {
        "healthy": not reasons,
        "reasons": reasons,
        "auth_ok": st.get("auth_ok", True),
        "last_post_at": last_post_at,
        "hours_since_last_post": round(hours_since, 1) if hours_since is not None else None,
        "state": st.get("state"),
        "today_count": st.get("today_count", 0),
    }


def get_recent_comments(limit=30):
    return load_json(COMMENTS_LOG, [])[-limit:]


def get_recent_tw_replies(limit=20):
    return load_json(TW_LOG, [])[-limit:]


_api_health_cache = {"ok": None, "ts": 0}

def check_api_health() -> bool:
    now = time.time()
    if now - _api_health_cache["ts"] < 300:
        return _api_health_cache["ok"]
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        client.messages.create(model="claude-haiku-4-5-20251001",
                               max_tokens=1, messages=[{"role": "user", "content": "hi"}])
        _api_health_cache.update(ok=True, ts=now)
        return True
    except Exception as e:
        _api_health_cache.update(ok=False, ts=now)
        return False


def log_comment(author, post_url, post_excerpt, comment_text):
    entries = load_json(COMMENTS_LOG, [])
    entries.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "author": author, "post_url": post_url,
        "excerpt": post_excerpt[:150], "comment": comment_text,
    })
    save_json(COMMENTS_LOG, entries[-500:])


def get_tw_queue() -> list:
    return load_json(TW_QUEUE_FILE, [])

def save_tw_queue(q: list):
    save_json(TW_QUEUE_FILE, q)

def add_to_tw_queue(items: list):
    import uuid
    q = get_tw_queue()
    existing_urls = {it["tweet_url"] for it in q}
    now = datetime.now(timezone.utc).isoformat()
    for it in items:
        if it["tweet_url"] in existing_urls:
            continue
        q.append({
            "id": str(uuid.uuid4())[:8],
            "author": it.get("author", ""),
            "author_username": it.get("author_username", it.get("author", "")),
            "tweet_url": it["tweet_url"],
            "tweet_text": it.get("text", "")[:280],
            "reply": it.get("draft", ""),
            "likes": it.get("likes", 0),
            "tweet_posted_at": it.get("posted_at", ""),
            "status": "pending",
            "generated_at": now,
            "posted_at": None,
        })
    save_tw_queue(q)
    return len(q)

def tw_queue_today_posted() -> int:
    today = today_str()
    return sum(1 for it in get_tw_queue()
               if it.get("status") == "posted" and (it.get("posted_at") or "")[:10] == today)


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
    <h1 class="text-2xl font-bold">Commenter Dashboard <a href="/viral-posts" class="text-sm font-normal text-gray-500 hover:text-blue-400 ml-3">Viral Posts DB →</a> <a href="/stats" class="text-sm font-normal text-gray-500 hover:text-blue-400 ml-3">Stats →</a></h1>
    <div class="flex items-center gap-4">
      <div class="flex items-center gap-2">
        <span class="w-2.5 h-2.5 rounded-full {% if api_ok %}bg-green-400{% else %}bg-red-500 animate-pulse{% endif %}"></span>
        <span class="text-xs {% if api_ok %}text-gray-500{% else %}text-red-400 font-semibold{% endif %}">
          {% if api_ok %}Anthropic API OK{% else %}Anthropic API — no credits!{% endif %}
        </span>
      </div>
      <span class="text-xs text-gray-500">auto-refreshes every 60s · SF time</span>
    </div>
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

  <!-- Auto-loop status -->
  <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Posted today (auto)</div>
      <div class="text-3xl font-bold {% if tw.today_count >= settings.tw_max_per_day %}text-red-400{% else %}text-green-400{% endif %}">
        {{ tw.today_count }}<span class="text-lg text-gray-500">/{{ settings.tw_max_per_day }}</span>
      </div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Loop status</div>
      <div class="text-lg font-semibold mt-1
        {% if tw.state == 'posting' %}text-yellow-400
        {% elif tw.state and 'error' in tw.state %}text-red-400
        {% elif tw.state == 'sleeping' %}text-blue-400
        {% else %}text-gray-400{% endif %}">{{ (tw.state or 'idle') | title }}</div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Last session</div>
      <div class="text-lg font-semibold mt-1">{{ fmt(tw.last_session) }}</div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="text-xs text-gray-500 mb-1">Next session</div>
      <div class="text-lg font-semibold mt-1 text-blue-300">{{ fmt(tw.next_session) }}</div>
    </div>
  </div>
  {% if tw.last_error %}
  <div class="bg-red-950 border border-red-800 rounded-xl px-4 py-3 mb-4 text-red-300 text-sm">
    Last error: {{ tw.last_error }}
  </div>
  {% endif %}

  {% set pending = tw_queue | selectattr('status','eq','pending') | list %}
  {% set approved = tw_queue | selectattr('status','eq','approved') | list %}
  {% set posted = tw_queue | selectattr('status','eq','posted') | list %}

  {% if pending or approved %}
  <div id="tw-queue" class="space-y-3 mb-6">
    {% for it in (approved + pending)|sort(attribute='generated_at', reverse=True) %}
    <div id="card-{{ it.id }}" class="bg-gray-900 rounded-xl p-4 {% if it.status == 'approved' %}border border-sky-800{% endif %}">
      <div class="flex items-center gap-3 mb-3">
        <a href="{{ it.tweet_url }}" target="_blank" class="text-sky-400 hover:text-sky-300 text-xs font-semibold">@{{ it.author_username }}</a>
        <span class="text-xs text-gray-600">{{ (it.tweet_posted_at or it.generated_at or '')[:10] }}</span>
        <span class="text-xs text-gray-600">{{ it.likes or '' }}{% if it.likes %} likes{% endif %}</span>
        <span data-badge class="ml-auto text-xs px-2 py-0.5 rounded-full {% if it.status == 'approved' %}bg-sky-900 text-sky-300{% else %}bg-gray-800 text-yellow-400{% endif %}">
          {{ it.status }}
        </span>
      </div>

      <!-- Tweet text with expand -->
      <div class="mb-3">
        <p class="text-xs text-gray-500 leading-relaxed">
          <span class="tweet-short-{{ it.id }}">{{ it.tweet_text[:160] }}{% if it.tweet_text|length > 160 %}<span>… <button onclick="document.querySelector('.tweet-short-{{ it.id }}').style.display='none'; document.querySelector('.tweet-full-{{ it.id }}').style.display='block'" class="text-gray-600 hover:text-gray-400 underline">show more</button></span>{% endif %}</span>
          {% if it.tweet_text|length > 160 %}<span class="tweet-full-{{ it.id }}" style="display:none">{{ it.tweet_text }} <button onclick="document.querySelector('.tweet-full-{{ it.id }}').style.display='none'; document.querySelector('.tweet-short-{{ it.id }}').style.display='block'" class="text-gray-600 hover:text-gray-400 underline">show less</button></span>{% endif %}
        </p>
      </div>

      <!-- Our reply — full text -->
      <div class="bg-gray-800 rounded-lg p-3 mb-3">
        <p class="text-sm text-gray-100 leading-relaxed whitespace-pre-wrap">{{ it.reply }}</p>
      </div>

      <!-- Actions -->
      <div class="flex gap-2 justify-end" data-actions="{{ it.id }}">
        {% if it.status == 'pending' %}
        <button data-action="approve" data-id="{{ it.id }}" class="bg-green-800 hover:bg-green-700 text-green-200 text-xs rounded px-3 py-1.5 font-medium">Approve</button>
        <button data-action="reject"  data-id="{{ it.id }}" class="bg-gray-800 hover:bg-gray-700 text-gray-400 text-xs rounded px-3 py-1.5">Reject</button>
        {% elif it.status == 'approved' %}
        <button data-action="copy"   data-reply="{{ it.reply }}" class="bg-sky-800 hover:bg-sky-700 text-sky-200 text-xs rounded px-3 py-1.5 font-medium">Copy</button>
        <button data-action="posted" data-id="{{ it.id }}" class="bg-gray-800 hover:bg-green-800 text-gray-300 hover:text-green-200 text-xs rounded px-3 py-1.5">Mark Posted</button>
        <button data-action="reject" data-id="{{ it.id }}" class="bg-gray-800 hover:bg-gray-700 text-gray-500 text-xs rounded px-3 py-1.5">Reject</button>
        {% endif %}
      </div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="bg-gray-900 rounded-xl p-6 mb-6 text-gray-600 text-sm">No replies in queue.</div>
  {% endif %}

  <!-- Posted history -->
  {% if posted %}
  <div class="bg-gray-900 rounded-xl overflow-hidden">
    <div class="px-4 py-3 border-b border-gray-800">
      <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider">Posted ({{ posted|length }})</h3>
    </div>
    <div class="divide-y divide-gray-800">
      {% for it in posted|reverse %}
      <div class="px-4 py-3">
        <div class="flex items-center gap-3 mb-1">
          <span class="text-xs text-gray-600">{{ (it.posted_at or '')[:10] }}</span>
          <a href="{{ it.tweet_url }}" target="_blank" class="text-xs text-gray-500 hover:text-gray-400">@{{ it.author_username }}</a>
          <span class="text-xs text-gray-600">{{ it.likes or '' }}{% if it.likes %} likes{% endif %}</span>
        </div>
        <p class="text-xs text-gray-600 mb-1">{{ it.tweet_text }}</p>
        <p class="text-xs text-gray-400">{{ it.reply }}</p>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

<script>
document.addEventListener('click', function(e) {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;

  if (action === 'copy') {
    const text = btn.dataset.reply;
    navigator.clipboard.writeText(text).then(() => {
      btn.textContent = 'Copied!';
      setTimeout(() => btn.textContent = 'Copy', 1500);
    });
    return;
  }

  const id = btn.dataset.id;
  const card = document.getElementById('card-' + id);
  btn.disabled = true;
  btn.style.opacity = '0.5';

  fetch('/twitter/queue/' + id + '/' + action, {method: 'POST'})
    .then(r => r.json())
    .then(data => {
      if (action === 'reject') {
        card.style.transition = 'opacity 0.3s';
        card.style.opacity = '0';
        setTimeout(() => card.remove(), 300);
      } else if (action === 'approve') {
        card.classList.add('border', 'border-sky-800');
        const badge = card.querySelector('[data-badge]');
        if (badge) { badge.textContent = 'approved'; badge.className = 'text-xs px-2 py-0.5 rounded-full bg-sky-900 text-sky-300'; }
        const actionsDiv = card.querySelector('[data-actions]');
        const copyBtn = document.createElement('button');
        copyBtn.dataset.action = 'copy';
        copyBtn.dataset.reply = data.reply;
        copyBtn.className = 'bg-sky-800 hover:bg-sky-700 text-sky-200 text-xs rounded px-3 py-1.5 font-medium';
        copyBtn.textContent = 'Copy';
        const postedBtn = document.createElement('button');
        postedBtn.dataset.action = 'posted';
        postedBtn.dataset.id = id;
        postedBtn.className = 'bg-gray-800 hover:bg-green-800 text-gray-300 hover:text-green-200 text-xs rounded px-3 py-1.5';
        postedBtn.textContent = 'Mark Posted';
        const rejectBtn = document.createElement('button');
        rejectBtn.dataset.action = 'reject';
        rejectBtn.dataset.id = id;
        rejectBtn.className = 'bg-gray-800 hover:bg-gray-700 text-gray-500 text-xs rounded px-3 py-1.5';
        rejectBtn.textContent = 'Reject';
        actionsDiv.innerHTML = '';
        actionsDiv.append(copyBtn, postedBtn, rejectBtn);
      } else if (action === 'posted') {
        card.style.opacity = '0.4';
        const actionsDiv = card.querySelector('[data-actions]');
        actionsDiv.innerHTML = '<span class="text-xs text-green-400">Posted ✓</span>';
      }
    })
    .catch(() => { btn.disabled = false; btn.style.opacity = '1'; });
});
</script>

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
        tw_queue=get_tw_queue(),
        tw_posted_today=tw_queue_today_posted(),
        fmt=fmt_time,
        api_ok=check_api_health(),
    )


def _run_twitter_generate():
    try:
        from fetch_tweets import fetch_tweets
        from generate_replies import generate_replies
        from knowledge_base import build_context
        log.info("Twitter: fetching tweets for queue...")
        tweets = fetch_tweets()
        if not tweets:
            log.info("Twitter: no new tweets found.")
            return
        kb = build_context()
        items = generate_replies(tweets, kb)
        publishable = [it for it in items if not it.get("skip") and it.get("draft", "").strip()]
        for it in publishable:
            it["tweet_url"] = it.get("url", "")
        added = add_to_tw_queue(publishable)
        log.info(f"Twitter: added {len(publishable)} replies to queue (total {added}).")
    except Exception as e:
        log.error(f"Twitter generate error: {e}", exc_info=True)


def twitter_generate_loop():
    log.info("Twitter generate loop started (every 6h, queue only — no auto-posting).")
    while True:
        try:
            _run_twitter_generate()
        except Exception as e:
            log.error(f"Twitter generate loop error: {e}", exc_info=True)
        time.sleep(6 * 3600)


@app.route("/twitter/queue/<item_id>/approve", methods=["POST"])
def twitter_queue_approve(item_id):
    q = get_tw_queue()
    reply = ""
    for it in q:
        if it["id"] == item_id:
            it["status"] = "approved"
            reply = it.get("reply", "")
            break
    save_tw_queue(q)
    from flask import jsonify
    return jsonify({"ok": True, "reply": reply})


@app.route("/twitter/queue/<item_id>/reject", methods=["POST"])
def twitter_queue_reject(item_id):
    q = get_tw_queue()
    for it in q:
        if it["id"] == item_id:
            it["status"] = "rejected"
            break
    save_tw_queue(q)
    from flask import jsonify
    return jsonify({"ok": True})


@app.route("/twitter/queue/<item_id>/posted", methods=["POST"])
def twitter_queue_posted(item_id):
    q = get_tw_queue()
    for it in q:
        if it["id"] == item_id:
            it["status"] = "posted"
            it["posted_at"] = datetime.now(timezone.utc).isoformat()
            log_tw_reply(it["author"], it["tweet_url"], it["tweet_text"], it["reply"])
            break
    save_tw_queue(q)
    from flask import jsonify
    return jsonify({"ok": True})


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


@app.route("/viral-posts")
def viral_posts():
    import json as _json
    path = os.path.join(DATA_DIR, "viral_posts_db.json")
    db = load_json(path, [])
    return render_template_string(VIRAL_TEMPLATE, posts=db, fmt=fmt_time)


VIRAL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Viral Posts DB</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen p-6 font-sans">
<div class="max-w-4xl mx-auto">
  <div class="flex items-center justify-between mb-8">
    <h1 class="text-2xl font-bold">Viral Posts Database</h1>
    <div class="flex items-center gap-4">
      <span class="text-xs text-gray-500">{{ posts|length }} posts · sorted by engagement</span>
      <a href="/" class="text-xs text-blue-400 hover:text-blue-300">← Dashboard</a>
    </div>
  </div>

  {% if not posts %}
  <div class="text-gray-600">No posts yet. They appear here after the first successful comment session.</div>
  {% else %}
  <div class="space-y-6">
    {% for p in posts %}
    <div class="bg-gray-900 rounded-xl p-5">
      <div class="flex items-center gap-3 mb-3">
        <a href="{{ p.url }}" target="_blank" class="text-blue-400 hover:text-blue-300 font-semibold text-sm">{{ p.author }}</a>
        {% if p.author_title %}
        <span class="text-xs text-gray-500 truncate max-w-sm">{{ p.author_title }}</span>
        {% endif %}
        <span class="ml-auto flex items-center gap-3 text-xs text-gray-500 whitespace-nowrap">
          <span>{{ p.likes }} likes</span>
          <span>{{ p.comments }} comments</span>
          <span class="text-gray-600">{{ fmt(p.saved_at) }}</span>
        </span>
      </div>
      <p class="text-sm text-gray-300 leading-relaxed mb-4 whitespace-pre-wrap">{{ p.text }}</p>
      <div class="border-l-2 border-blue-800 pl-3">
        <div class="text-xs text-gray-500 mb-1">Our comment</div>
        <p class="text-sm text-gray-400">{{ p.our_comment }}</p>
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>
</body>
</html>
"""


@app.route("/twitter/health")
def twitter_health_endpoint():
    """Health probe for the Twitter loop. Returns HTTP 503 when it needs a human
    (almost always: refresh TWITTER_AUTH_TOKEN / TWITTER_CT0). Point an uptime
    monitor here to get alerted instead of noticing weeks later."""
    h = twitter_health()
    return h, (200 if h["healthy"] else 503)


@app.route("/multi-status")
def multi_status():
    """Diagnostic: show per-account state for multi-account loop."""
    import json as _json
    from accounts import list_linkedin_accounts, get_account_config, get_account_state
    from config import UNIPILE_ACCOUNT_ID as NICK_ID
    try:
        accounts = list_linkedin_accounts()
    except Exception as e:
        return {"error": f"list_linkedin_accounts: {e}"}, 500
    out = []
    for a in accounts:
        aid = a.get("id") or ""
        if aid == NICK_ID:
            continue
        cfg = get_account_config(aid)
        st = get_account_state(aid)
        last_ts = st.get("last_session_ts", 0)
        out.append({
            "name": a.get("name"),
            "id": aid,
            "active": cfg.get("active", True),
            "daily_cap": cfg.get("daily_cap"),
            "min_likes": cfg.get("min_likes"),
            "today_count": st.get("count", 0) if st.get("date") == today_str() else 0,
            "last_session_ago_min": int((time.time() - last_ts) / 60) if last_ts else None,
        })
    return {"count": len(out), "accounts": out}


@app.route("/multi-trigger", methods=["GET", "POST"])
def multi_trigger():
    """Manually trigger one multi-account session (for debugging)."""
    try:
        posted = run_multi_account_sessions()
        return {"ok": True, "posted": posted}
    except Exception as e:
        import traceback
        return {"ok": False, "error": str(e), "trace": traceback.format_exc()[:2000]}, 500


@app.route("/stats")
def stats():
    import json as _json
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)

    def _week_counts(log_path, date_fields, text_fields):
        entries = load_json(log_path, [])
        buckets = {}
        total_7d = 0
        for e in entries:
            raw = ""
            for f in (date_fields if isinstance(date_fields, list) else [date_fields]):
                raw = e.get(f, "")
                if raw:
                    break
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                continue
            age = (now - dt).days
            if age > 28:
                continue
            week_label = f"Week -{age // 7}" if age >= 7 else "This week"
            text = ""
            for f in (text_fields if isinstance(text_fields, list) else [text_fields]):
                text = e.get(f, "")
                if text:
                    break
            buckets.setdefault(week_label, []).append(text[:80])
            if age < 7:
                total_7d += 1
        return total_7d, buckets

    tw_7d, tw_buckets = _week_counts(TW_LOG, ["posted_at", "ts"], "reply")
    li_7d, li_buckets = _week_counts(COMMENTS_LOG, ["posted_at", "ts"], "comment")

    tw_log = load_json(TW_LOG, [])
    li_log = load_json(COMMENTS_LOG, [])

    return render_template_string(STATS_TEMPLATE,
        tw_log=tw_log, li_log=li_log,
        tw_7d=tw_7d, li_7d=li_7d,
        tw_buckets=tw_buckets, li_buckets=li_buckets,
        now=now, fmt=fmt_time)


STATS_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Activity Stats</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen p-6 font-sans">
<div class="max-w-3xl mx-auto">
  <div class="flex items-center justify-between mb-8">
    <h1 class="text-2xl font-bold">Activity Stats</h1>
    <a href="/" class="text-xs text-blue-400 hover:text-blue-300">← Dashboard</a>
  </div>

  <!-- Summary cards -->
  <div class="grid grid-cols-2 gap-4 mb-10">
    <div class="bg-gray-900 rounded-xl p-5">
      <div class="text-xs text-gray-500 mb-1">Twitter replies · last 7 days</div>
      <div class="text-4xl font-bold text-blue-400">{{ tw_7d }}</div>
      <div class="text-xs text-gray-600 mt-1">{{ tw_log|length }} total all time</div>
    </div>
    <div class="bg-gray-900 rounded-xl p-5">
      <div class="text-xs text-gray-500 mb-1">LinkedIn comments · last 7 days</div>
      <div class="text-4xl font-bold text-blue-400">{{ li_7d }}</div>
      <div class="text-xs text-gray-600 mt-1">{{ li_log|length }} total all time</div>
    </div>
  </div>

  <!-- Twitter by week -->
  <div class="mb-8">
    <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">Twitter · by week</h2>
    {% for week, entries in tw_buckets.items()|sort %}
    <div class="mb-4">
      <div class="text-xs text-gray-500 mb-2">{{ week }} — {{ entries|length }} replies</div>
      <div class="space-y-1">
        {% for e in entries %}
        <div class="text-xs text-gray-400 bg-gray-900 rounded px-3 py-1 truncate">{{ e }}</div>
        {% endfor %}
      </div>
    </div>
    {% endfor %}
    {% if not tw_buckets %}<div class="text-gray-600 text-sm">No data yet.</div>{% endif %}
  </div>

  <!-- LinkedIn by week -->
  <div class="mb-8">
    <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">LinkedIn · by week</h2>
    {% for week, entries in li_buckets.items()|sort %}
    <div class="mb-4">
      <div class="text-xs text-gray-500 mb-2">{{ week }} — {{ entries|length }} comments</div>
      <div class="space-y-1">
        {% for e in entries %}
        <div class="text-xs text-gray-400 bg-gray-900 rounded px-3 py-1 truncate">{{ e }}</div>
        {% endfor %}
      </div>
    </div>
    {% endfor %}
    {% if not li_buckets %}<div class="text-gray-600 text-sm">No data yet.</div>{% endif %}
  </div>

  <!-- Recent Twitter replies -->
  <div>
    <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">Last 10 Twitter replies</h2>
    <div class="space-y-3">
    {% for e in tw_log[-10:]|reverse %}
    <div class="bg-gray-900 rounded-xl p-4">
      <div class="flex items-center justify-between mb-2">
        <span class="text-xs font-semibold text-blue-400">{{ e.get('author','?') }}</span>
        <span class="text-xs text-gray-600">{{ fmt(e.get('posted_at') or e.get('ts','')) }}</span>
      </div>
      <p class="text-xs text-gray-500 mb-2 truncate">{{ (e.get('tweet_text') or e.get('excerpt',''))[:120] }}</p>
      <p class="text-sm text-gray-200">{{ e.get('reply','') }}</p>
    </div>
    {% endfor %}
    {% if not tw_log %}<div class="text-gray-600 text-sm">No replies logged yet.</div>{% endif %}
    </div>
  </div>

</div>
</body>
</html>
"""


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


VC_DAILY_CAP = 15
VC_SESSION_GAP_MIN = 240  # minutes between VC sessions
_vc_last_session_ts = 0


def run_vc_session():
    global _vc_last_session_ts
    from fetch_vc_posts import fetch_vc_posts, record_vc_interaction
    from report import session_dir, save_posts
    from knowledge_base import build_context, save_example
    from generate_comments import generate_comments, VC_SYSTEM_PROMPT
    from publish import _extract_activity_id, _get_social_id, _post_comment
    from fetch_posts import mark_url_published
    from config import UNIPILE_ACCOUNT_ID, PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX
    import json as _json

    minutes_since = (time.time() - _vc_last_session_ts) / 60 if _vc_last_session_ts else 9999
    if minutes_since < VC_SESSION_GAP_MIN:
        log.info(f"VC: gap not met ({int(minutes_since)}m < {VC_SESSION_GAP_MIN}m). Skipping.")
        return 0

    st = get_status()
    today = today_str()
    vc_done = st.get("vc_today_count", 0) if st.get("date") == today else 0
    budget = VC_DAILY_CAP - vc_done
    if budget <= 0:
        log.info(f"VC: daily cap reached ({vc_done}/{VC_DAILY_CAP}). Skipping.")
        return 0

    log.info(f"=== VC session start — budget: {budget} ===")
    _vc_last_session_ts = time.time()

    posts = fetch_vc_posts(account_id=UNIPILE_ACCOUNT_ID)
    if not posts:
        log.info("VC: no new posts found.")
        return 0

    d = session_dir()
    save_posts(posts, d)
    with open(os.path.join(d, "vc_posts.json"), "w") as f:
        _json.dump(posts, f, ensure_ascii=False, indent=2)

    kb = build_context()
    items = generate_comments(posts, kb, system_prompt=VC_SYSTEM_PROMPT)

    publishable = [
        it for it in items
        if not it.get("skip") and len(it.get("draft", "")) >= 40
    ][:budget]

    if not publishable:
        log.info("VC: nothing to publish after filtering.")
        return 0

    published = 0
    for i, item in enumerate(publishable):
        text = item["draft"].strip()
        url = item["url"]
        author = item.get("author", "?")
        author_url = item.get("author_url", "")

        log.info(f"  VC [{i+1}/{len(publishable)}] {author[:35]}")
        aid = _extract_activity_id(url)
        sid = _get_social_id(aid, account_id=UNIPILE_ACCOUNT_ID)
        if not sid:
            log.warning("    No social_id")
            continue

        ok, detail = _post_comment(sid, text, account_id=UNIPILE_ACCOUNT_ID)
        if ok:
            log.info(f"    VC OK: {detail}")
            save_example(item.get("text", url), text)
            log_comment(author, url, item.get("text", ""), text)
            mark_url_published(url)
            if author_url:
                record_vc_interaction(author_url, url)
            published += 1
        else:
            log.warning(f"    VC Failed: {detail}")

        if i < len(publishable) - 1:
            delay = random.randint(PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX)
            log.info(f"  VC: waiting {delay}s...")
            time.sleep(delay)

    st2 = get_status()
    today2 = today_str()
    prev_vc = st2.get("vc_today_count", 0) if st2.get("date") == today2 else 0
    prev_total = st2.get("today_count", 0) if st2.get("date") == today2 else 0
    update_status(vc_today_count=prev_vc + published, today_count=prev_total + published)
    log.info(f"=== VC session done: {published}/{len(publishable)} posted ===")
    return published


def run_linkedin_session(s):
    from fetch_posts import fetch_all_posts
    from report import session_dir, save_posts
    from knowledge_base import build_context, save_example
    from generate_comments import generate_comments
    from publish import _extract_activity_id, _get_social_id, _post_comment
    from fetch_posts import mark_url_published
    from knowledge_base import save_viral_post
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
            mark_url_published(url)
            save_viral_post(item, text)
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

    if published > 0:
        try:
            from analyze_viral_posts import run_analysis
            run_analysis()
        except Exception as e:
            log.warning(f"  Viral pattern analysis failed: {e}")

        try:
            from track_own_posts import run as track_own
            track_own(silent=True)
        except Exception as e:
            log.warning(f"  Own post tracking failed: {e}")

    return published


def run_multi_account_sessions():
    """Iterate non-Nick LinkedIn accounts from Unipile and run a session for each."""
    from accounts import list_linkedin_accounts, get_account_config, get_account_state, save_account_state
    from fetch_posts import fetch_all_posts, mark_url_published
    from report import session_dir, save_posts
    from knowledge_base import build_context, save_example
    from generate_comments import generate_comments, GENERIC_SYSTEM_PROMPT, SYSTEM_PROMPT
    from publish import _extract_activity_id, _get_social_id, _post_comment
    from config import UNIPILE_ACCOUNT_ID as NICK_ID, PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX
    import json as _json

    try:
        accounts = list_linkedin_accounts()
    except Exception as e:
        log.error(f"Multi-account: could not list accounts: {e}")
        return 0

    other_accounts = [a for a in accounts if (a.get("id") or "") != NICK_ID]
    log.info(f"Multi-account: found {len(other_accounts)} non-Nick LinkedIn accounts.")

    total_posted = 0
    for account in other_accounts:
        account_id = account.get("id") or ""
        if not account_id:
            continue

        config = get_account_config(account_id)
        if not config.get("active", True):
            continue

        name = config.get("name") or account.get("name") or account_id[:12]
        daily_cap = config.get("daily_cap", 10)
        min_likes = config.get("min_likes", 0)

        state = get_account_state(account_id)
        today = today_str()
        done = state.get("count", 0) if state.get("date") == today else 0
        if done >= daily_cap:
            log.info(f"[{name}] Daily cap reached ({done}/{daily_cap}). Skipping.")
            continue

        last_ts = state.get("last_session_ts", 0)
        minutes_since = (time.time() - last_ts) / 60 if last_ts else 9999
        if minutes_since < 150:
            log.info(f"[{name}] Last session {int(minutes_since)}m ago. Skipping.")
            continue

        budget = min(6, daily_cap - done)
        log.info(f"=== Multi-acc session [{name}] — budget: {budget} ===")

        try:
            posts = fetch_all_posts(account_id=account_id, min_likes=min_likes)
        except Exception as e:
            log.error(f"[{name}] fetch error: {e}")
            continue

        if not posts:
            log.info(f"[{name}] No posts.")
            continue

        kb = build_context()  # default Nick's KB (user explicitly chose this)
        items = generate_comments(posts, kb, system_prompt=GENERIC_SYSTEM_PROMPT)

        publishable = [
            it for it in items
            if not it.get("skip") and len(it.get("draft", "")) >= 40
        ][:budget]

        if not publishable:
            log.info(f"[{name}] Nothing publishable.")
            continue

        published = 0
        for i, item in enumerate(publishable):
            text = item["draft"].strip()
            url = item["url"]
            author = item.get("author", "?")[:35]
            log.info(f"  [{name}] [{i+1}/{len(publishable)}] {author}")

            aid = _extract_activity_id(url)
            sid = _get_social_id(aid, account_id=account_id)
            if not sid:
                log.warning(f"    [{name}] No social_id")
                continue

            ok, detail = _post_comment(sid, text, account_id=account_id)
            if ok:
                log.info(f"    [{name}] OK: {detail}")
                log_comment(f"{name} → {author}", url, item.get("text", ""), text)
                mark_url_published(url)
                published += 1
            else:
                log.warning(f"    [{name}] Failed: {detail}")

            if i < len(publishable) - 1:
                delay = random.randint(PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX)
                time.sleep(delay)

        # Update account state
        new_state = state if state.get("date") == today else {"date": today, "count": 0, "last_session_ts": 0}
        new_state["count"] = new_state.get("count", 0) + published
        new_state["last_session_ts"] = time.time()
        new_state["date"] = today
        save_account_state(account_id, new_state)

        total_posted += published
        log.info(f"=== Multi-acc [{name}] done: {published}/{len(publishable)} posted ===")

    return total_posted


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
                run_vc_session()
            except Exception as e:
                log.error(f"VC session error: {e}", exc_info=True)

            try:
                run_linkedin_session(s)
            except Exception as e:
                log.error(f"LinkedIn session error: {e}", exc_info=True)
                update_status(state="error", last_error=str(e)[:200])

            try:
                run_multi_account_sessions()
            except Exception as e:
                log.error(f"Multi-account sessions error: {e}", exc_info=True)

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
        # New day resets counters, but auth/post health must persist across days
        # (a cookie that died yesterday is still dead today).
        st = {"date": today, "today_count": 0, "last_session": None,
              "next_session": None, "state": "idle", "last_error": None,
              "auth_ok": st.get("auth_ok", True),
              "last_post_at": st.get("last_post_at"),
              "last_auth_error": st.get("last_auth_error")}
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

            health = twitter_health()
            if not health["healthy"]:
                log.error(
                    "TWITTER UNHEALTHY — needs attention (likely expired cookies, "
                    "refresh TWITTER_AUTH_TOKEN / TWITTER_CT0): "
                    + "; ".join(health["reasons"])
                    + " | check /twitter/health"
                )

            s2 = get_settings()
            gap = random.randint(s2["tw_gap_min"], s2["tw_gap_max"])
            wake = datetime.now(timezone.utc) + timedelta(minutes=gap)
            tw_state = "sleeping" if health["healthy"] else "auth_error"
            update_tw_status(today_count=new_count, state=tw_state, next_session=wake.isoformat())
            log.info(f"Twitter: next session in {gap}m (~{wake.astimezone(SF_TZ).strftime('%H:%M')} SF).")
            last_session_ts = time.time()
            time.sleep(gap * 60)

        except Exception as e:
            log.error(f"Twitter loop error: {e}", exc_info=True)
            time.sleep(300)


# ── Own X posts (autonomous, 1/day) ─────────────────────────────────────────

OWN_X_LOG = os.path.join(DATA_DIR, "own_x_posts.json")  # persists on /data disk
OWN_X_GAP_HOURS = 24


def get_own_x_posts(limit=30):
    return load_json(OWN_X_LOG, [])[-limit:]


def _log_own_x_post(text, url):
    posts = load_json(OWN_X_LOG, [])
    posts.append({
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "text": text, "url": url,
    })
    save_json(OWN_X_LOG, posts)


def _hours_since_last_own_x():
    posts = load_json(OWN_X_LOG, [])
    if not posts:
        return None
    try:
        last = datetime.fromisoformat(posts[-1]["posted_at"])
        return (datetime.now(timezone.utc) - last).total_seconds() / 3600
    except Exception:
        return None


def own_x_post_loop():
    """Publish one original tweet per day in Nick's voice. The posted-log lives on
    the persistent /data disk, so redeploys don't cause double-posting."""
    log.info("Own X-post loop started (1/day).")
    time.sleep(120)  # let the app settle after boot / avoid deploy-storm posts
    while True:
        try:
            hrs = _hours_since_last_own_x()
            if hrs is not None and hrs < OWN_X_GAP_HOURS:
                wait_h = OWN_X_GAP_HOURS - hrs
                log.info(f"Own X post: last was {hrs:.1f}h ago, sleeping {wait_h:.1f}h.")
                time.sleep(wait_h * 3600)
                continue

            from generate_posts import generate_tweet
            from knowledge_base import build_context
            recent = [p.get("text", "") for p in load_json(OWN_X_LOG, [])]
            try:
                kb = build_context()
            except Exception:
                kb = ""
            text = generate_tweet(recent, kb)
            log.info(f"Own X post draft ({len(text)} chars): {text[:120]}")

            import x_api
            ok, res = x_api.post_tweet(text)
            if ok:
                _log_own_x_post(text, res)
                log.info(f"Own X post PUBLISHED: {res}")
            else:
                log.warning(f"Own X post failed: {res}")

            jitter = random.randint(-7200, 7200)  # +/- 2h so it isn't the same time daily
            time.sleep(max(3600, OWN_X_GAP_HOURS * 3600 + jitter))
        except Exception as e:
            log.error(f"Own X-post loop error: {e}", exc_info=True)
            time.sleep(3600)


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=linkedin_loop, daemon=True, name="linkedin").start()
    threading.Thread(target=twitter_loop, daemon=True, name="twitter").start()
    threading.Thread(target=own_x_post_loop, daemon=True, name="own_x_post").start()

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
