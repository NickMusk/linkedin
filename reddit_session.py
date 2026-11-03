#!/usr/bin/env python3
"""Reddit review session: walk Nick through the draft queue, thread by thread.

The deal, exactly as agreed: Nick starts the session, the machine opens each
thread in a visible tab and pre-fills the comment box with the draft, NICK
reads and clicks Comment himself, the machine notices, moves to the next one
and closes the tab at the end. There is deliberately NO code path here that
clicks Reddit's submit button — submitting is the human's act, always.

A floating panel on the page gives: ✓ Дальше (after you posted or to move on),
Пропустить (drop this draft), ■ Стоп (end the session).

    python3 reddit_session.py        # run a session over all "new" items
Started from the menu bar: ◉ REDDIT -> «Начать сессию ревью».
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import gx_browser

QUEUE_FILE = Path(__file__).parent / "reddit_queue.json"
PAGE_LOAD_WAIT = 25          # seconds for the thread page to become ready
FILL_ATTEMPTS = 8            # editor may need a click to expand first
ITEM_TIMEOUT_MIN = 30        # user walked away — put the item back, stop

OVERLAY_JS_TPL = """
(() => {
  const old = document.getElementById('tener-ov');
  if (old) old.remove();
  window.__tener_cmd = "";
  const d = document.createElement('div');
  d.id = 'tener-ov';
  d.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;' +
    'background:#141414;color:#fff;padding:10px 14px;border-radius:12px;' +
    'font:13px -apple-system,sans-serif;display:flex;gap:10px;align-items:center;' +
    'box-shadow:0 6px 24px rgba(0,0,0,.45)';
  const s = document.createElement('span');
  s.textContent = 'Tener · тред IDX из TOTAL. Проверь текст и нажми Comment сам';
  d.appendChild(s);
  for (const [cmd, label] of [['next','✓ Дальше'],['skip','Пропустить'],['stop','■ Стоп']]) {
    const b = document.createElement('button');
    b.textContent = label;
    b.style.cssText = 'background:#2e2e2e;color:#fff;border:1px solid #555;' +
      'border-radius:7px;padding:4px 10px;cursor:pointer;font:12px -apple-system';
    b.onclick = () => { window.__tener_cmd = cmd; };
    d.appendChild(b);
  }
  document.body.appendChild(d);
  return 'ok';
})()
"""

FILL_JS_TPL = """
(() => {
  const TEXT = DRAFT_JSON;
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  let ed = [...document.querySelectorAll('div[contenteditable="true"], textarea[name="text"]')].find(vis);
  if (!ed) {
    const trig = [...document.querySelectorAll('button, [role="textbox"], [placeholder]')]
      .find(e => /join the conversation|add a comment|share your thoughts/i
        .test((e.innerText || '') + ' ' + ((e.getAttribute && e.getAttribute('placeholder')) || '')));
    if (trig && vis(trig)) { trig.click(); return 'expanding'; }
    return 'no_editor';
  }
  ed.focus();
  if (ed.tagName === 'TEXTAREA') {
    ed.value = TEXT;
    ed.dispatchEvent(new Event('input', { bubbles: true }));
  } else {
    if ((ed.innerText || '').trim().length > 0) return 'editor_not_empty';
    document.execCommand('insertText', false, TEXT);
  }
  const got = ed.tagName === 'TEXTAREA' ? ed.value : (ed.innerText || '');
  return got.trim().length >= 10 ? 'filled' : 'insert_failed';
})()
"""

POLL_JS_TPL = """
(() => {
  const SNIP = SNIP_JSON;
  const inEditor = [...document.querySelectorAll('div[contenteditable="true"], textarea[name="text"]')]
    .some(e => ((e.tagName === 'TEXTAREA' ? e.value : e.innerText) || '').includes(SNIP));
  const onPage = (document.body.innerText || '').includes(SNIP);
  return JSON.stringify({ cmd: window.__tener_cmd || '', posted: onPage && !inEditor });
})()
"""


def _load() -> list[dict]:
    try:
        return json.loads(QUEUE_FILE.read_text())
    except (OSError, ValueError):
        return []


def _save(q: list[dict]):
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, indent=1))
    os.replace(tmp, QUEUE_FILE)


def _wait_ready():
    deadline = time.time() + PAGE_LOAD_WAIT
    while time.time() < deadline:
        try:
            if gx_browser.session_eval("document.readyState") == "complete":
                return True
        except RuntimeError:
            pass
        time.sleep(1.5)
    return False


def _fill(draft: str) -> str:
    js = FILL_JS_TPL.replace("DRAFT_JSON", json.dumps(draft))
    state = "?"
    for _ in range(FILL_ATTEMPTS):
        try:
            state = gx_browser.session_eval(js)
        except RuntimeError as e:
            state = f"err: {e}"
        if state == "filled":
            return state
        time.sleep(1.5)
    return state


def run_item(item: dict, idx: int, total: int) -> str:
    """Returns 'next' | 'skip' | 'stop' | 'timeout'."""
    gx_browser.session_open(item["url"])
    time.sleep(2)
    _wait_ready()
    # clipboard fallback is always armed, even when auto-fill works
    subprocess.run("pbcopy", input=item["draft"].encode(), check=False)
    fill_state = _fill(item["draft"])
    overlay = (OVERLAY_JS_TPL
               .replace("IDX", str(idx)).replace("TOTAL", str(total)))
    if fill_state != "filled":
        overlay = overlay.replace(
            "Проверь текст и нажми Comment сам",
            "Автозаполнение не вышло, черновик в буфере: Cmd+V и Comment")
    try:
        gx_browser.session_eval(overlay)
    except RuntimeError:
        pass
    print(f"  [{idx}/{total}] r/{item['sub']} {item['title'][:60]} (fill: {fill_state})")

    snippet = item["draft"][:40]
    poll = POLL_JS_TPL.replace("SNIP_JSON", json.dumps(snippet))
    deadline = time.time() + ITEM_TIMEOUT_MIN * 60
    while time.time() < deadline:
        time.sleep(2)
        try:
            raw = gx_browser.session_eval(poll)
            st = json.loads(raw)
        except (RuntimeError, ValueError):
            continue
        if st.get("cmd") in ("next", "skip", "stop"):
            return st["cmd"]
        if st.get("posted"):
            return "next"
    return "timeout"


def main():
    q = _load()
    fresh = [it for it in q if it.get("status") == "new"]
    if not fresh:
        print("Очередь пуста — монитор ещё ничего не нашёл.")
        return
    if not gx_browser.available_patient(75):
        print("Расширение Tener Poster Bridge недоступно — обнови его в chrome://extensions")
        return

    print(f"Сессия ревью: {len(fresh)} тредов. Отправка — только твоя кнопка Comment.")
    done = skipped = 0
    for i, item in enumerate(fresh, 1):
        verdict = run_item(item, i, len(fresh))
        if verdict == "next":
            item["status"] = "posted"
            done += 1
        elif verdict == "skip":
            item["status"] = "skipped"
            skipped += 1
        else:   # stop / timeout: leave the rest for the next session
            break
        _save(q)
    _save(q)
    try:
        gx_browser.session_close()
    except RuntimeError:
        pass
    print(f"Сессия закончена: пройдено {done}, пропущено {skipped}, "
          f"осталось {sum(1 for it in q if it.get('status') == 'new')}.")


if __name__ == "__main__":
    main()
