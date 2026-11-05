#!/usr/bin/env python3
"""LinkedIn post review session, same deal as the Reddit one.

The machine opens the LinkedIn composer in a visible tab with the draft
pre-filled (share URL + editor fill + clipboard, triple redundancy), NICK
reads it and clicks Post himself, the machine marks the item and closes the
tab. There is deliberately NO code path here that clicks LinkedIn's Post
button — publishing is the human's act, always.

    python3 linkedin_post_session.py
Started from the menu bar: in LINKEDIN -> «Пост: сессия ревью».
"""

import json
import os
import subprocess
import time
import urllib.parse
from pathlib import Path

import gx_browser

BASE = Path(__file__).parent
QUEUE_FILE = BASE / "linkedin_post_queue.json"
LOCK_FILE = BASE / "linkedin_post_session.lock"
STAMP_FILE = BASE / "linkedin_post_last.txt"
PAGE_LOAD_WAIT = 30
ITEM_TIMEOUT_MIN = 30

OVERLAY_JS = """
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
  s.textContent = 'MSG';
  d.appendChild(s);
  for (const [cmd, label] of [['posted','✓ Запостил'],['skip','Пропустить'],['stop','■ Стоп']]) {
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
  const ed = [...document.querySelectorAll('div.ql-editor[contenteditable="true"], div[role="textbox"][contenteditable="true"]')].find(vis);
  if (!ed) return 'no_editor';
  if ((ed.innerText || '').trim().length >= 10) return 'already_filled';
  ed.focus();
  document.execCommand('insertText', false, TEXT);
  return (ed.innerText || '').trim().length >= 10 ? 'filled' : 'insert_failed';
})()
"""


def _lock() -> bool:
    try:
        pid = int(LOCK_FILE.read_text())
        os.kill(pid, 0)
        return False
    except (OSError, ValueError):
        pass
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _save_status(item_id: str, status: str):
    try:
        q = json.loads(QUEUE_FILE.read_text())
    except (OSError, ValueError):
        return
    for it in q:
        if it.get("id") == item_id:
            it["status"] = status
            break
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, indent=1, ensure_ascii=False))
    os.replace(tmp, QUEUE_FILE)


def main():
    if not _lock():
        print("Сессия уже идёт (linkedin_post_session.lock) — вторая не нужна.")
        return
    try:
        q = json.loads(QUEUE_FILE.read_text())
    except (OSError, ValueError):
        q = []
    item = next((it for it in q if it.get("status") == "new"), None)
    if not item:
        print("Черновика нет — сначала: python3 linkedin_post.py gen")
        return
    if not gx_browser.available_patient(75):
        print("Расширение Tener Poster Bridge недоступно — обнови его в chrome://extensions")
        return

    print("Открываю композер. Публикация — только твоя кнопка Post.")
    subprocess.run("pbcopy", input=item["draft"].encode(), check=False)
    url = ("https://www.linkedin.com/feed/?shareActive=true&text="
           + urllib.parse.quote(item["draft"]))
    gx_browser.session_open(url)
    time.sleep(3)
    deadline = time.time() + PAGE_LOAD_WAIT
    while time.time() < deadline:
        try:
            if gx_browser.session_eval("document.readyState") == "complete":
                break
        except RuntimeError:
            pass
        time.sleep(1.5)

    fill_state = "?"
    fill_js = FILL_JS_TPL.replace("DRAFT_JSON", json.dumps(item["draft"]))
    for _ in range(8):
        try:
            fill_state = gx_browser.session_eval(fill_js)
        except RuntimeError as e:
            fill_state = f"err: {e}"
        if fill_state in ("filled", "already_filled"):
            break
        time.sleep(1.5)
    msg = ("Tener · черновик в композере. Проверь текст и нажми Post сам"
           if fill_state in ("filled", "already_filled") else
           "Tener · автозаполнение не вышло, черновик в буфере: Cmd+V и Post")
    try:
        gx_browser.session_eval(OVERLAY_JS.replace("MSG", msg))
    except RuntimeError:
        pass
    print(f"  композер открыт (fill: {fill_state})")

    verdict = "timeout"
    deadline = time.time() + ITEM_TIMEOUT_MIN * 60
    while time.time() < deadline:
        time.sleep(2)
        try:
            cmd = gx_browser.session_eval("window.__tener_cmd || ''")
        except RuntimeError:
            continue
        if cmd in ("posted", "skip", "stop"):
            verdict = cmd
            break

    if verdict == "posted":
        _save_status(item["id"], "posted")
        STAMP_FILE.write_text(time.strftime("%Y-%m-%d"))
        # refresh own-posts stats so the menubar sees today's post
        subprocess.Popen([os.sys.executable, str(BASE / "track_own_posts.py")],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("Запощено, отмечено.")
    elif verdict == "skip":
        _save_status(item["id"], "skipped")
        print("Пропущено.")
    else:
        print(f"Сессия закрыта без поста ({verdict}) — черновик остался в очереди.")
    try:
        gx_browser.session_close()
    except RuntimeError:
        pass


if __name__ == "__main__":
    main()
