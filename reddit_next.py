#!/usr/bin/env python3
"""Open the next Reddit thread from the monitor's queue, draft in clipboard.

Nick's half of the Reddit flow: the monitor finds threads and writes drafts,
this command serves them one at a time. It copies the draft to the clipboard
and opens the thread in the default browser — paste, edit if needed, submit.

    python3 reddit_next.py          # open next thread, copy its draft
    python3 reddit_next.py list     # show what's waiting
    python3 reddit_next.py skip     # drop the next item without opening
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

QUEUE_FILE = Path(__file__).parent / "reddit_queue.json"


def _load() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    try:
        return json.loads(QUEUE_FILE.read_text())
    except ValueError:
        return []


def _save(q: list[dict]):
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, indent=1))
    os.replace(tmp, QUEUE_FILE)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "next"
    q = _load()
    fresh = [it for it in q if it.get("status") == "new"]

    if cmd == "list":
        print(f"в очереди: {len(fresh)}")
        for it in fresh:
            age_h = (time.time() - it.get("added", 0)) / 3600
            print(f"  r/{it['sub']} ({age_h:.0f}ч): {it['title'][:70]}")
        return

    if not fresh:
        print("Очередь пуста — монитор ещё ничего не нашёл.")
        return

    item = fresh[0]
    if cmd == "skip":
        item["status"] = "skipped"
        _save(q)
        print(f"Пропущен: r/{item['sub']} {item['title'][:60]}")
        return

    subprocess.run("pbcopy", input=item["draft"].encode(), check=False)
    subprocess.run(["open", item["url"]], check=False)
    item["status"] = "opened"
    _save(q)
    rest = len(fresh) - 1
    print(f"r/{item['sub']}: {item['title'][:70]}")
    print(f"  {item['url']}")
    print(f"Черновик в буфере — Cmd+V в поле комментария. Осталось в очереди: {rest}")


if __name__ == "__main__":
    main()
