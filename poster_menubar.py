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
        self.m_att = [rumps.MenuItem("•"), rumps.MenuItem("•"), rumps.MenuItem("•")]
        self.m_att_clear = rumps.MenuItem("Очистить уведомления", callback=self.clear_attention)
        self.m_toggle = rumps.MenuItem("⏸ Пауза", callback=self.toggle_pause)
        self.m_dash = rumps.MenuItem("Открыть дашборд", callback=self.open_dash)
        self.m_hdr_tw = rumps.MenuItem("𝕏  TWITTER")
        self.m_hdr_li = rumps.MenuItem("in  LINKEDIN")
        self.m_li_post = rumps.MenuItem("Пост: черновика нет", callback=self.start_li_post_session)
        self.m_hdr_rd = rumps.MenuItem("◉  REDDIT")
        self.m_rd_session = rumps.MenuItem("▶ Начать сессию ревью", callback=self.start_reddit_session)
        self.m_rd_next = rumps.MenuItem("Очередь пуста", callback=self.open_reddit)
        self.m_rd_skip = rumps.MenuItem("Пропустить следующий", callback=self.skip_reddit)
        self.menu = [self.m_att[0], self.m_att[1], self.m_att[2], self.m_att_clear, None,
                     self.m_hdr_tw, self.m_state, self.m_posted, self.m_queue, self.m_tw_today, None,
                     self.m_hdr_li, self.m_li_today, self.m_li_post, None,
                     self.m_hdr_rd, self.m_rd_session, self.m_rd_next, self.m_rd_skip, None,
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
        for it in items:
            if it["ts"] > self._seen_ts:
                rumps.notification("Tener автоматика", "нужно действие", it["msg"][:200])
        self._seen_ts = max([it["ts"] for it in items], default=self._seen_ts)
        self._badge = "❗" if items else ""
        last = items[-3:]
        for i, slot in enumerate(self.m_att):
            slot.title = ("❗ " + last[i]["msg"][:70]) if i < len(last) else "•"
        self.m_att_clear.title = "Очистить уведомления" if items else "Уведомлений нет"

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
            self.m_tw_today.title = f"Реплаев сегодня: {tw}"
        except Exception:
            self.m_tw_today.title = "Реплаев сегодня: — (дашборд молчит)"
        try:
            log = json.loads(urllib.request.urlopen(
                DASHBOARD + "comments/log.json", timeout=20).read())
            li = sum(1 for c in log
                     if ((c.get("ts") or c.get("timestamp") or "")[:10] == today))
            self.m_li_today.title = f"Комментов сегодня: {li}"
        except Exception:
            self.m_li_today.title = "Комментов сегодня: — (дашборд молчит)"

    def _reddit_pending(self) -> int:
        try:
            q = json.loads((BASE / "reddit_queue.json").read_text())
            return sum(1 for it in q if it.get("status") == "new")
        except Exception:
            return 0

    @rumps.timer(60)
    def refresh_reddit(self, _):
        n = self._reddit_pending()
        self.m_rd_next.title = (f"Открыть следующий тред ({n} в очереди)"
                                if n else "Очередь пуста")
        # afternoon nudge: queue is waiting but no session ran today
        # (attention dedupes by message for 12h, so this fires once a day)
        if n and datetime.now().hour >= 13:
            try:
                last = (BASE / "reddit_session_last.txt").read_text().strip()
            except OSError:
                last = ""
            if last != datetime.now().strftime("%Y-%m-%d"):
                try:
                    import attention
                    attention.add("Reddit-сессия сегодня не запускалась, треды ждут: менюбар → Начать сессию ревью")
                except Exception:
                    pass

    def _own_posted_today(self) -> bool:
        """True when Nick's own-posts DB shows a post dated today, or the
        review session stamped one today."""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            if (BASE / "linkedin_post_last.txt").read_text().strip() == today:
                return True
        except OSError:
            pass
        try:
            d = json.loads((BASE / "own_posts_db.json").read_text())
            posts = d if isinstance(d, list) else d.get("posts", [])
            return any((p.get("posted_at") or "").startswith(today) for p in posts)
        except Exception:
            return False

    def _li_draft_pending(self) -> bool:
        try:
            q = json.loads((BASE / "linkedin_post_queue.json").read_text())
            return any(it.get("status") == "new" for it in q)
        except Exception:
            return False

    @rumps.timer(120)
    def refresh_li_post(self, _):
        draft = self._li_draft_pending()
        self.m_li_post.title = ("▶ Пост: сессия ревью (черновик ждёт)"
                                if draft else "Пост: черновика нет")
        # afternoon nudge: no own post today (12h dedupe = once a day)
        if datetime.now().hour >= 13 and not self._own_posted_today():
            try:
                import attention
                attention.add("Сегодня ещё не постили в LinkedIn"
                              + (" — черновик готов: менюбар → Пост: сессия ревью"
                                 if draft else
                                 " и черновика нет — запусти: python3 linkedin_post.py gen"))
            except Exception:
                pass

    def start_li_post_session(self, _):
        """Guided post review: composer opens pre-filled, Nick clicks Post."""
        log = open(BASE / "logs" / "linkedin_post_session.log", "a")
        subprocess.Popen([str(Path.home() / ".tener-menubar-venv/bin/python"),
                          str(BASE / "linkedin_post_session.py")],
                         stdout=log, stderr=log, start_new_session=True)

    def start_reddit_session(self, _):
        """Guided pass over the queue: tab opens, draft pre-filled, Nick clicks
        Comment himself, next thread follows. Runs detached, log in logs/."""
        log = open(BASE / "logs" / "reddit_session.log", "a")
        subprocess.Popen([str(Path.home() / ".tener-menubar-venv/bin/python"),
                          str(BASE / "reddit_session.py")],
                         stdout=log, stderr=log, start_new_session=True)

    def open_reddit(self, _):
        """One click: thread opens in the browser, draft lands in the clipboard."""
        subprocess.run([str(Path.home() / ".tener-menubar-venv/bin/python"),
                        str(BASE / "reddit_next.py")], timeout=30)
        self.refresh_reddit(None)

    def skip_reddit(self, _):
        subprocess.run([str(Path.home() / ".tener-menubar-venv/bin/python"),
                        str(BASE / "reddit_next.py"), "skip"], timeout=30)
        self.refresh_reddit(None)

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
