"""Attention channel: anything that needs Nick's action lands here, and the
menu bar app turns it into a macOS notification + ❗ badge.

Producers: the poster (frozen batches, login walls), outreach agents, or
Claude directly. Consumer: poster_menubar.py.
"""
import json
import time
from pathlib import Path

FILE = Path(__file__).parent / "attention.json"


def _load() -> list:
    try:
        return json.loads(FILE.read_text())
    except Exception:
        return []


def add(msg: str, dedupe_hours: float = 12.0):
    """Append a notification unless the same message was added recently."""
    items = _load()
    now = time.time()
    for it in items:
        if it["msg"] == msg and now - it["ts"] < dedupe_hours * 3600:
            return
    items.append({"ts": now, "msg": msg})
    FILE.write_text(json.dumps(items[-20:], ensure_ascii=False))


def clear():
    FILE.write_text("[]")


def items() -> list:
    return _load()
