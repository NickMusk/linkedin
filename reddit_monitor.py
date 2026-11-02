#!/usr/bin/env python3
"""Reddit monitor: find fresh threads worth answering, draft a reply, notify.

READ-ONLY by design. The script never posts to Reddit — undisclosed bot
participation is astroturfing, and Reddit shadowbans young automated accounts
anyway. It watches the subreddits below through the official OAuth API,
scores fresh threads against Tener's pitch, drafts a comment for each hit,
and raises the menu-bar attention flag. Nick reads reddit_leads.md and posts
himself.

One-time setup (~2 min): https://www.reddit.com/prefs/apps -> create app ->
type "script" (name/redirect are anything). Add to .env:
    REDDIT_CLIENT_ID=<under the app name>
    REDDIT_CLIENT_SECRET=<the "secret" field>

Runs hourly as launchd agent ai.tener.reddit (one sweep per run, then exits).
    python3 reddit_monitor.py            # one sweep
    python3 reddit_monitor.py --dry-run  # score but write no drafts/files
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import attention

BASE = Path(__file__).parent
STATE_FILE = BASE / "reddit_state.json"
LEADS_FILE = BASE / "reddit_leads.md"
UA = "macos:tener.monitor:v1.0 (read-only research tool)"

SUBREDDITS = [
    # hiring world — Tener's home turf
    "recruiting", "recruitinghell", "humanresources", "askhr", "jobs",
    # founders and buyers
    "startups", "Entrepreneur", "SaaS", "smallbusiness", "ycombinator",
    # AI crowd
    "artificial", "ArtificialInteligence", "AI_Agents",
]
MAX_THREAD_AGE_H = 24
PER_SUB = 40
HAIKU_CAP = 12          # Claude calls per sweep
RELEVANT_AT = 7         # score threshold for a draft
MODEL = "claude-haiku-4-5"

# cheap prefilter before Claude sees anything
TOPIC_RE = re.compile(
    r"recruit|hiring|hire\b|talent|sourc(?:e|ing)|candidate|\bats\b|applicant"
    r"|interview|headhunt|resume|cv screen|job (?:post|ad|description)"
    r"|(?=.*\bai\b|.*agent|.*automat)(?=.*(hir|recruit|hr\b|people ops|staffing))",
    re.IGNORECASE | re.DOTALL,
)

SYSTEM = """You help Nick Nagatkin decide which Reddit threads to answer personally.
Nick: recruiter for 10+ years, then founder of Tener.ai — an autonomous AI recruiter
(sources, screens, interviews candidates end to end). He wants to be a useful voice
in hiring/AI threads, not an advertiser.

Given a thread, reply with JSON only:
{"score": 0-10, "why": "one short line", "draft": "comment text or empty"}

score = how much Nick specifically can add (10 = question he's uniquely placed to
answer; 0 = off-topic, meme, rant with no question, or politics).
If score >= 7 write a draft comment in Nick's voice: direct, concrete, from practice,
2-6 sentences, English. Plain sentences, no emoji, no em dashes, no bullet lists.
Mention Tener only when the thread explicitly asks for tools like it; otherwise don't.
Never write anything that pretends to be a random bystander ad."""


def _env(key: str) -> str:
    val = os.environ.get(key, "")
    if val:
        return val
    try:
        for line in (BASE / ".env").read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _load_state() -> dict:
    state = {"seen": {}}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except ValueError:
            pass
    state.setdefault("seen", {})
    cut = time.time() - 14 * 86400
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cut}
    return state


def _save_state(state: dict):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE_FILE)


def _token(cid: str, secret: str) -> str:
    req = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={"User-Agent": UA}, method="POST")
    import base64
    req.add_header("Authorization",
                   "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def _fetch_sub(token: str, sub: str) -> list[dict]:
    req = urllib.request.Request(
        f"https://oauth.reddit.com/r/{sub}/new?limit={PER_SUB}&raw_json=1",
        headers={"User-Agent": UA, "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    out = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        out.append({
            "id": d.get("name", ""),
            "sub": sub,
            "title": d.get("title", ""),
            "text": (d.get("selftext") or "")[:2500],
            "url": "https://www.reddit.com" + d.get("permalink", ""),
            "ups": int(d.get("ups") or 0),
            "comments": int(d.get("num_comments") or 0),
            "created": float(d.get("created_utc") or 0),
            "stickied": bool(d.get("stickied")),
        })
    return out


def _candidates(token: str, state: dict) -> list[dict]:
    now = time.time()
    cands = []
    for sub in SUBREDDITS:
        try:
            posts = _fetch_sub(token, sub)
        except Exception as e:
            print(f"  r/{sub}: fetch failed ({type(e).__name__}: {e})")
            continue
        for p in posts:
            if not p["id"] or p["id"] in state["seen"] or p["stickied"]:
                continue
            if (now - p["created"]) / 3600 > MAX_THREAD_AGE_H:
                continue
            if not TOPIC_RE.search(p["title"] + "\n" + p["text"]):
                continue
            cands.append(p)
        time.sleep(1.2)  # stay well under the API rate limit
    # busiest threads first — that's where an answer gets read
    cands.sort(key=lambda p: -(p["ups"] + 2 * p["comments"]))
    return cands


def _judge(client, p: dict) -> dict:
    msg = client.messages.create(
        model=MODEL, max_tokens=500, system=SYSTEM,
        messages=[{"role": "user", "content":
                   f"Subreddit: r/{p['sub']}\nTitle: {p['title']}\n\n{p['text']}"}])
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        v = json.loads(m.group(0)) if m else {}
    except ValueError:
        v = {}
    return {"score": int(v.get("score") or 0),
            "why": str(v.get("why") or "")[:200],
            "draft": str(v.get("draft") or "").strip()}


def _write_leads(hits: list[dict]):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = [f"\n## Свежее — {stamp}\n"]
    for p in hits:
        age_h = (time.time() - p["created"]) / 3600
        block.append(
            f"### r/{p['sub']}: {p['title']}\n"
            f"{p['url']}\n"
            f"- {p['ups']} up, {p['comments']} комментов, {age_h:.0f}ч назад — {p['why']}\n\n"
            f"Черновик:\n\n> {p['draft']}\n")
    old = LEADS_FILE.read_text() if LEADS_FILE.exists() else "# Reddit: треды для ответа\n"
    header, _, rest = old.partition("\n")
    LEADS_FILE.write_text(header + "\n" + "\n".join(block) + rest)


def sweep(dry_run: bool = False) -> int:
    cid, secret = _env("REDDIT_CLIENT_ID"), _env("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        print("reddit: нет REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET в .env — "
              "создай script-app на https://www.reddit.com/prefs/apps")
        attention.add("Reddit-монитор ждёт ключи: создай app на reddit.com/prefs/apps "
                      "и добавь REDDIT_CLIENT_ID/SECRET в .env")
        return 0
    token = _token(cid, secret)
    state = _load_state()
    cands = _candidates(token, state)
    print(f"reddit: кандидатов после префильтра: {len(cands)}")

    from anthropic import Anthropic
    client = Anthropic(api_key=_env("ANTHROPIC_API_KEY"))
    hits = []
    for p in cands[:HAIKU_CAP]:
        try:
            verdict = _judge(client, p)
        except Exception as e:
            print(f"  judge failed ({type(e).__name__}: {e})")
            continue
        state["seen"][p["id"]] = time.time()
        tag = f"{verdict['score']}/10 r/{p['sub']} {p['title'][:60]}"
        print(f"  {tag} — {verdict['why']}")
        if verdict["score"] >= RELEVANT_AT and verdict["draft"]:
            hits.append({**p, **verdict})
    # everything judged is seen; the rest of the prefilter pool waits for the
    # next sweep so a burst doesn't blow the Claude budget
    if not dry_run:
        _save_state(state)
        if hits:
            _write_leads(hits)
            attention.add(f"Reddit: {len(hits)} тред(а) ждут твоего ответа — reddit_leads.md")
    print(f"reddit: релевантных: {len(hits)}")
    return len(hits)


if __name__ == "__main__":
    sweep(dry_run="--dry-run" in sys.argv)
