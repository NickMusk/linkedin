#!/usr/bin/env python3
"""Menu bar companion for the Twitter poster.

Shows session state in the macOS menu bar, lets Nick pause/resume the poster
(via poster_pause.flag, which post_replies.py honors) and see session stats
(from poster_status.json, which the poster keeps fresh).

Runs under its own LaunchAgent (ai.tener.menubar) with the dedicated venv:
~/.tener-menubar-venv/bin/python poster_menubar.py
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path

import rumps

BASE = Path(__file__).parent
STATUS_FILE = BASE / "poster_status.json"
PAUSE_FLAG = BASE / "poster_pause.flag"
DASHBOARD = "https://linkedin-commenter-kwha.onrender.com/"

STATE_RU = {
    "starting": "запускается",
    "posting": "постит батч",
    "paused": "на паузе",
}


def poster_alive() -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", "post_replies.py"],
                             capture_output=True, timeout=5)
        return out.returncode == 0
    except Exception:
        return False


class PosterBar(rumps.App):
    def __init__(self):
        super().__init__("𝕏", quit_button=None)
        self.m_state = rumps.MenuItem("Статус: …")
        self.m_posted = rumps.MenuItem("За сессию: …")
        self.m_queue = rumps.MenuItem("Очередь: …")
        self.m_tw_today = rumps.MenuItem("Twitter сегодня: …")
        self.m_li_today = rumps.MenuItem("LinkedIn сегодня: …")
        self.m_toggle = rumps.MenuItem("⏸ Пауза", callback=self.toggle_pause)
        self.m_dash = rumps.MenuItem("Открыть дашборд", callback=self.open_dash)
        self.menu = [self.m_state, self.m_posted, self.m_queue, None,
                     self.m_tw_today, self.m_li_today, None,
                     self.m_toggle, self.m_dash, None,
                     rumps.MenuItem("Выйти (иконка, не постер)", callback=rumps.quit_application)]
        self.refresh(None)

    @rumps.timer(10)
    def refresh(self, _):
        alive = poster_alive()
        paused = PAUSE_FLAG.exists()
        st = {}
        try:
            st = json.loads(STATUS_FILE.read_text())
        except Exception:
            pass

        if not alive:
            self.title = "𝕏 ✕"
            self.m_state.title = "Статус: постер не запущен"
        elif paused:
            self.title = "𝕏 ⏸"
            self.m_state.title = "Статус: на паузе"
        else:
            raw = st.get("state", "?")
            if raw.startswith("sleeping"):
                nice = "ждёт следующий батч"
            else:
                nice = STATE_RU.get(raw, raw)
            self.title = "𝕏 ▸"
            self.m_state.title = f"Статус: {nice}"

        self.title = self.title + getattr(self, "_badge", "")
        started = (st.get("started_at") or "")[11:16]
        self.m_posted.title = (f"За сессию: {st.get('posted', 0)} реплаев, "
                               f"{st.get('batches', 0)} батчей (с {started or '—'})")
        self.m_queue.title = f"Очередь: {st.get('queue', '—')} свежих"
        self.m_toggle.title = "▶︎ Продолжить" if paused else "⏸ Пауза"

    @rumps.timer(30)
    def refresh_attention(self, _):
        """Needs-action channel: ❗ badge + macOS notification per new item."""
        try:
            import attention
            items = attention.items()
        except Exception:
            items = []
        if not hasattr(self, "_seen_ts"):
            self._seen_ts = 0.0
            self._att_menu = []
        for it in items:
            if it["ts"] > self._seen_ts:
                rumps.notification("Tener автоматика", "нужно действие", it["msg"][:200])
        self._seen_ts = max([it["ts"] for it in items], default=self._seen_ts)
        # перестроить пункты меню
        for mi in self._att_menu:
            try:
                del self.menu[mi.title]
            except Exception:
                pass
        self._att_menu = []
        if items:
            self._badge = "❗"
            for it in items[-3:]:
                mi = rumps.MenuItem("❗ " + it["msg"][:70])
                self.menu.insert_before(self.m_state.title, mi)
                self._att_menu.append(mi)
            clear_mi = rumps.MenuItem("Очистить уведомления", callback=self.clear_attention)
            self.menu.insert_before(self.m_state.title, clear_mi)
            self._att_menu.append(clear_mi)
        else:
            self._badge = ""

    def clear_attention(self, _):
        try:
            import attention
            attention.clear()
        except Exception:
            pass
        self.refresh_attention(None)
        self.refresh(None)

    @rumps.timer(300)
    def refresh_daily(self, _):
        """Daily totals from the dashboard (every 5 min, network — keep rare)."""
        import urllib.request
        from datetime import date
        today = date.today().isoformat()
        try:
            q = json.loads(urllib.request.urlopen(
                DASHBOARD + "twitter/queue.json", timeout=20).read())
            tw = sum(1 for x in q if (x.get("posted_at") or "").startswith(today))
            self.m_tw_today.title = f"Twitter сегодня: {tw} реплаев"
        except Exception:
            self.m_tw_today.title = "Twitter сегодня: — (дашборд молчит)"
        try:
            log = json.loads(urllib.request.urlopen(
                DASHBOARD + "comments/log.json", timeout=20).read())
            li = sum(1 for c in log
                     if ((c.get("ts") or c.get("timestamp") or "")[:10] == today))
            self.m_li_today.title = f"LinkedIn сегодня: {li} комм."
        except Exception:
            self.m_li_today.title = "LinkedIn сегодня: — (дашборд молчит)"

    def toggle_pause(self, _):
        if PAUSE_FLAG.exists():
            PAUSE_FLAG.unlink(missing_ok=True)
        else:
            PAUSE_FLAG.write_text(datetime.now().isoformat())
        self.refresh(None)

    def open_dash(self, _):
        subprocess.run(["open", DASHBOARD])


if __name__ == "__main__":
    PosterBar().run()
