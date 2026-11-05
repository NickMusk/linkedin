#!/usr/bin/env python3
"""LinkedIn post pipeline: one reviewed draft a day, posting stays with Nick.

Idea source, in priority order:
1. linkedin_post_ideas.md — Nick's own ideas (first unchecked "- [ ]" line);
   used ideas get checked off with the date
2. the market: fresh high-engagement posts from his LinkedIn feed (Unipile),
   the same virality signal the comment pipeline hunts

The generator writes 3 drafts in Nick's voice (generate_posts.py prompt),
a haiku judge picks the strongest one, and that single draft lands in
linkedin_post_queue.json + raises the menu-bar flag. Nothing here publishes:
posting happens in the review session (linkedin_post_session.py) where NICK
clicks Post himself.

    python3 linkedin_post.py gen    # make today's draft (no-op if one waits)
    python3 linkedin_post.py list
    python3 linkedin_post.py skip
"""

import json
import os
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
IDEAS_FILE = BASE / "linkedin_post_ideas.md"
QUEUE_FILE = BASE / "linkedin_post_queue.json"
JUDGE_MODEL = "claude-haiku-4-5"

PICK_SYSTEM = """You pick the strongest LinkedIn post draft for Nick Nagatkin's account.
What performs on his account: personal, contrarian, SHORT, strong first line that
stops the scroll. Hard reject anything whiny or resentful toward investors/VCs
(he is fundraising, it reads as cope), then penalize generic founder content,
long posts, weak hooks. Reply with JSON only: {"pick": 1|2|3, "why": "one short line"}"""


def _load_queue() -> list[dict]:
    try:
        return json.loads(QUEUE_FILE.read_text())
    except (OSError, ValueError):
        return []


def _save_queue(q: list[dict]):
    cut = time.time() - 7 * 86400
    q = [it for it in q if it.get("added", 0) >= cut or it.get("status") == "new"]
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, indent=1, ensure_ascii=False))
    os.replace(tmp, QUEUE_FILE)


def _next_idea() -> str:
    """First unchecked "- [ ]" line from the ideas inbox."""
    try:
        for line in IDEAS_FILE.read_text().splitlines():
            m = re.match(r"\s*- \[ \] (.+)", line)
            if m:
                return m.group(1).strip()
    except OSError:
        pass
    return ""


def _mark_idea_used(idea: str):
    text = IDEAS_FILE.read_text()
    stamp = time.strftime("%Y-%m-%d")
    text = text.replace(f"- [ ] {idea}", f"- [x] {idea} (черновик {stamp})", 1)
    IDEAS_FILE.write_text(text)


def _strip_label(draft: str) -> str:
    """Drop the "DRAFT 1 (Format A)" header line the prompt asks for."""
    lines = draft.splitlines()
    if lines and re.match(r"\s*DRAFT \d", lines[0], re.IGNORECASE):
        lines = lines[1:]
    out = "\n".join(lines).strip()
    out = re.sub(r"[ \t]*[—–][ \t]*", ", ", out)
    out = re.sub(r"[ \t]+-[ \t]+", ", ", out)
    return out


def gen():
    q = _load_queue()
    if any(it.get("status") == "new" for it in q):
        print("Черновик уже ждёт в очереди — новый не нужен.")
        return

    from knowledge_base import build_context
    from generate_posts import generate_post_drafts, extract_trending_topics

    idea = _next_idea()
    topics = []
    try:
        from fetch_posts import fetch_feed_posts
        posts = fetch_feed_posts(target=30)
        topics = extract_trending_topics(posts)
    except Exception as e:
        print(f"лента недоступна ({type(e).__name__}: {e}), работаю без контекста рынка")
    if not idea and not topics:
        print("Ни идеи от Ника, ни рыночного контекста — черновик не из чего делать.")
        return

    source = "идея Ника" if idea else "рынок"
    print(f"Источник: {source}" + (f" — {idea[:80]}" if idea else ""))
    drafts = generate_post_drafts(topics, build_context(), idea=idea or None)
    drafts = [d for d in drafts if len(d.strip()) > 40]
    if not drafts:
        print("Генерация не дала черновиков.")
        return

    pick = 0
    if len(drafts) > 1:
        import anthropic
        from config import ANTHROPIC_API_KEY
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        numbered = "\n\n".join(f"=== {i+1} ===\n{d}" for i, d in enumerate(drafts))
        try:
            msg = client.messages.create(
                model=JUDGE_MODEL, max_tokens=150, system=PICK_SYSTEM,
                messages=[{"role": "user", "content": numbered}])
            raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            v = json.loads(m.group(0)) if m else {}
            pick = max(0, min(len(drafts) - 1, int(v.get("pick", 1)) - 1))
            print(f"Выбран вариант {pick + 1}: {v.get('why', '')}")
        except Exception as e:
            print(f"судья не сработал ({type(e).__name__}), беру первый вариант")

    draft = _strip_label(drafts[pick])
    q.append({"id": f"lipost_{int(time.time())}", "source": source,
              "idea": idea, "draft": draft, "status": "new", "added": time.time()})
    _save_queue(q)
    try:
        import attention
        attention.add("LinkedIn: черновик поста готов — менюбар → Пост: сессия ревью")
    except Exception:
        pass
    print(f"\nЧерновик в очереди:\n\n{draft}")


def list_queue():
    for it in _load_queue():
        print(f"{it['status']} | {it.get('source', '?')} | {it['draft'][:90]}")


def skip():
    q = _load_queue()
    for it in q:
        if it.get("status") == "new":
            it["status"] = "skipped"
            _save_queue(q)
            print(f"Пропущен: {it['draft'][:60]}")
            return
    print("Нечего пропускать.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "gen"
    {"gen": gen, "list": list_queue, "skip": skip}.get(cmd, gen)()
