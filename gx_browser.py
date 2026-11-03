"""Extension transport (the real Codex way): the poster talks to the local
bridge, the bridge talks to the Tener Poster Bridge Chrome extension, the
extension drives its pinned background tab via chrome.debugger in Nick's real
Chrome. No windows, no focus changes, real session."""

import json
import urllib.request

BRIDGE = "http://127.0.0.1:8765"


def _post(cmd: dict, timeout: int = 70) -> dict:
    req = urllib.request.Request(
        f"{BRIDGE}/cmd", data=json.dumps(cmd).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def available() -> bool:
    try:
        with urllib.request.urlopen(f"{BRIDGE}/health", timeout=3) as r:
            if not json.loads(r.read()).get("extension_connected"):
                return False
        return _post({"cmd": "status"}, timeout=15).get("ok", False)
    except Exception:
        return False


def navigate(url: str):
    resp = _post({"cmd": "navigate", "url": url})
    if not resp.get("ok"):
        raise RuntimeError(f"extension navigate: {resp.get('error', '?')}")


def evaluate(js_code: str) -> str:
    resp = _post({"cmd": "eval", "js": js_code})
    if not resp.get("ok"):
        raise RuntimeError(f"extension eval: {resp.get('error', '?')}")
    return resp.get("value", "")


# --- review session tab: visible, user-facing. Open pages and pre-fill text
# only; the submit click is always the human's. ---

def session_open(url: str):
    resp = _post({"cmd": "session_open", "url": url})
    if not resp.get("ok"):
        raise RuntimeError(f"session_open: {resp.get('error', '?')}")


def session_eval(js_code: str) -> str:
    resp = _post({"cmd": "session_eval", "js": js_code})
    if not resp.get("ok"):
        raise RuntimeError(f"session_eval: {resp.get('error', '?')}")
    return resp.get("value", "")


def session_close():
    resp = _post({"cmd": "session_close"})
    if not resp.get("ok"):
        raise RuntimeError(f"session_close: {resp.get('error', '?')}")


def available_patient(max_wait: int = 75) -> bool:
    """Service worker расширения спит между делами и просыпается будильником
    раз в минуту. Если мост жив, а расширение не подключено — ждём пробуждения,
    вместо того чтобы падать в оконный фоллбек и плодить окна."""
    import time
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if available():
            return True
        try:
            with urllib.request.urlopen(f"{BRIDGE}/health", timeout=3):
                pass
        except Exception:
            return False  # моста нет — ждать нечего
        time.sleep(5)
    return False
