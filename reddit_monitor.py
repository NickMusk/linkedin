#!/usr/bin/env python3
"""Reddit monitor: find fresh threads worth answering, draft a reply, notify.

Collection goes through Apify (trudax/reddit-scraper-lite, no Reddit keys
needed); the official OAuth API stays as a fallback when REDDIT_CLIENT_ID /
REDDIT_CLIENT_SECRET are in .env.

The script never posts to Reddit — undisclosed bot participation is
astroturfing, and Reddit shadowbans young automated accounts anyway. It
scores fresh threads against Tener's pitch, drafts a comment for each hit,
queues them, and raises the menu-bar attention flag. Nick posts himself:
`python3 reddit_next.py` opens the next thread with the draft already in
the clipboard.

Runs as launchd agent ai.tener.reddit (one sweep per run, then exits).
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
QUEUE_FILE = BASE / "reddit_queue.json"
UA = "macos:tener.monitor:v1.0 (read-only research tool)"
ACTOR = "trudax/reddit-scraper-lite"

SUBREDDITS = [
    # startup industry — the main hunting ground (virality first)
    "startups", "Entrepreneur", "SaaS", "smallbusiness", "ycombinator",
    "indiehackers", "venturecapital", "EntrepreneurRideAlong",
    # AI crowd
    "artificial", "ArtificialInteligence", "AI_Agents", "OpenAI",
    # hiring world — Tener's home turf, still worth watching
    "recruiting", "recruitinghell", "humanresources",
]
MAX_THREAD_AGE_H = 26
PER_SUB = 10            # posts pulled per subreddit per sweep
HAIKU_CAP = 12          # Claude calls per sweep
RELEVANT_AT = 7         # score threshold for a draft
MODEL = "claude-haiku-4-5"
MIN_UPS = 15            # virality floor: below this the thread has no reach
MIN_COMMENTS = 10       # ...unless the conversation itself is busy

SYSTEM = """You help Nick Nagatkin decide which Reddit threads to answer personally.
Nick: built an IT staffing company from 5 to 150 people and sold it, recruiter for
10+ years, now founder of Tener.ai — an autonomous AI recruiter. He wants to be a
sharp founder voice in big startup/AI threads, not an advertiser.

Given a thread (its upvote/comment counts are included), reply with JSON only:
{"score": 0-10, "why": "one short line", "draft": "comment text or empty"}

score = reach x what Nick can add. High score needs BOTH: the thread already has
traction (lots of upvotes/comments) AND Nick can say something concrete from his
own experience. His ground: starting/scaling/selling a company, fundraising as a
founder, hiring and teams, agency and SaaS economics, AI tools and agents in real
workflows. Recruiting expertise is a bonus, not a requirement.
If the thread needs domain knowledge he does not have (telecom, infra, legal,
medicine, deep engineering), score <= 4 even when AI is involved. Never invent
experience for him. Memes, rants with no question, politics: score <= 2.
If score >= 7 write a draft comment in Nick's voice: direct, concrete, from practice,
2-6 sentences, English. Plain sentences, no emoji, no em dashes, no bullet lists.
Never complain or write from a victim position — observations and insights, not grievances.
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


def _counts_from_html(html: str) -> tuple[int, int]:
    """The lite actor has no count fields, but the page html carries them as
    shreddit-post attributes."""
    ups = comments = 0
    m = re.search(r'\bscore="(\d+)"', html)
    if m:
        ups = int(m.group(1))
    m = re.search(r'\bcomment-count="(\d+)"', html)
    if m:
        comments = int(m.group(1))
    return ups, comments


def _fetch_apify() -> list[dict]:
    """Primary source: Apify crawl of each subreddit's top-of-the-day — the
    viral threads, not just the fresh ones."""
    from apify_client import ApifyClient
    client = ApifyClient(_env("APIFY_API_TOKEN"))
    run = client.actor(ACTOR).call(run_input={
        "startUrls": [{"url": f"https://www.reddit.com/r/{s}/top/?t=day"}
                      for s in SUBREDDITS],
        "skipComments": True,
        "maxItems": PER_SUB * len(SUBREDDITS),
    }, timeout_secs=1800)
    posts = []
    for it in client.dataset(run["defaultDatasetId"]).iterate_items():
        if it.get("dataType") != "post":
            continue
        created = 0.0
        try:
            created = datetime.fromisoformat(
                (it.get("createdAt") or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        ups, comments = _counts_from_html(it.get("html") or "")
        posts.append({
            "id": it.get("id") or it.get("url", ""),
            "sub": it.get("parsedCommunityName")
                   or (it.get("communityName") or "").lstrip("r/"),
            "title": it.get("title", ""),
            "text": (it.get("body") or "")[:2500],
            "url": it.get("url", ""),
            "ups": ups, "comments": comments,
            "created": created,
            "stickied": False,
        })
    return posts


# --- fallback: official OAuth API (used when REDDIT_* keys are in .env) ---

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


def _fetch_oauth() -> list[dict]:
    token = _token(_env("REDDIT_CLIENT_ID"), _env("REDDIT_CLIENT_SECRET"))
    posts = []
    for sub in SUBREDDITS:
        req = urllib.request.Request(
            f"https://oauth.reddit.com/r/{sub}/new?limit={PER_SUB}&raw_json=1",
            headers={"User-Agent": UA, "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
        except Exception as e:
            print(f"  r/{sub}: fetch failed ({type(e).__name__}: {e})")
            continue
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            posts.append({
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
        time.sleep(1.2)
    return posts


def _candidates(posts: list[dict], state: dict) -> list[dict]:
    now = time.time()
    cands = []
    for p in posts:
        if not p["id"] or p["id"] in state["seen"] or p["stickied"]:
            continue
        if not p["created"] or (now - p["created"]) / 3600 > MAX_THREAD_AGE_H:
            continue
        if p["ups"] < MIN_UPS and p["comments"] < MIN_COMMENTS:
            continue    # no reach — a perfect comment lands in an empty room
        cands.append(p)
    # most viral first; haiku judges the top of this list
    cands.sort(key=lambda p: -(p["ups"] + 3 * p["comments"]))
    return cands


def _judge(client, p: dict) -> dict:
    msg = client.messages.create(
        model=MODEL, max_tokens=500, system=SYSTEM,
        messages=[{"role": "user", "content":
                   f"Subreddit: r/{p['sub']}\nTitle: {p['title']}\n"
                   f"Upvotes: {p['ups']}, comments: {p['comments']}\n\n{p['text']}"}])
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        v = json.loads(m.group(0)) if m else {}
    except ValueError:
        v = {}
    draft = str(v.get("draft") or "").strip()
    draft = re.sub(r"\s*[—–]\s*", ", ", draft)   # the no-dash rule, enforced
    return {"score": int(v.get("score") or 0),
            "why": str(v.get("why") or "")[:200],
            "draft": draft}


def _queue_add(hits: list[dict]):
    q = []
    if QUEUE_FILE.exists():
        try:
            q = json.loads(QUEUE_FILE.read_text())
        except ValueError:
            q = []
    known = {it.get("id") for it in q}
    for p in hits:
        if p["id"] in known:
            continue
        q.append({"id": p["id"], "sub": p["sub"], "title": p["title"],
                  "url": p["url"], "why": p["why"], "draft": p["draft"],
                  "status": "new", "added": time.time()})
    cut = time.time() - 7 * 86400   # a week-old thread is not worth answering
    q = [it for it in q if it.get("added", 0) >= cut]
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, indent=1))
    os.replace(tmp, QUEUE_FILE)


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
    state = _load_state()
    if _env("APIFY_API_TOKEN"):
        posts = _fetch_apify()
    elif _env("REDDIT_CLIENT_ID"):
        posts = _fetch_oauth()
    else:
        print("reddit: нет ни APIFY_API_TOKEN, ни REDDIT_CLIENT_ID в .env")
        return 0
    cands = _candidates(posts, state)
    print(f"reddit: собрано {len(posts)}, кандидатов после префильтра: {len(cands)}")

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
        print(f"  {verdict['score']}/10 r/{p['sub']} {p['title'][:60]} — {verdict['why']}")
        if verdict["score"] >= RELEVANT_AT and verdict["draft"]:
            hits.append({**p, **verdict})
    # everything judged is seen; the rest of the prefilter pool waits for the
    # next sweep so a burst doesn't blow the Claude budget
    if not dry_run:
        _save_state(state)
        if hits:
            _write_leads(hits)
            _queue_add(hits)
            attention.add(f"Reddit: {len(hits)} тред(а) ждут ответа — команда: reddit_next")
    print(f"reddit: релевантных: {len(hits)}")
    return len(hits)


if __name__ == "__main__":
    sweep(dry_run="--dry-run" in sys.argv)
