#!/usr/bin/env python3
"""Local bridge between the poster and the Tener Poster Bridge extension.

- WebSocket server (127.0.0.1:8766): the Chrome extension connects here and
  executes commands on its dedicated tab via chrome.debugger.
- HTTP server (127.0.0.1:8765): the poster POSTs commands here.
    POST /cmd  {"cmd": "navigate"|"eval"|"status", ...} -> extension's reply
    GET  /health -> {"extension_connected": bool}

Both listeners are localhost-only. Runs as launchd agent ai.tener.bridge.
"""
import asyncio
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websockets

WS_PORT = 8766
HTTP_PORT = 8765

_loop = None
_ext_ws = None
_pending = {}  # id -> {"event": threading.Event, "resp": dict}


async def _ws_handler(ws):
    global _ext_ws
    print("[bridge] extension connected")
    _ext_ws = ws
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "hello" in msg:
                continue
            slot = _pending.get(msg.get("id"))
            if slot:
                slot["resp"] = msg
                slot["event"].set()
    except Exception:
        pass
    finally:
        if _ext_ws is ws:
            _ext_ws = None
        print("[bridge] extension disconnected")


def _send_to_ext(cmd: dict, timeout: float = 60.0) -> dict:
    if _ext_ws is None or _loop is None:
        return {"ok": False, "error": "extension not connected"}
    cmd_id = uuid.uuid4().hex[:12]
    cmd["id"] = cmd_id
    slot = {"event": threading.Event(), "resp": None}
    _pending[cmd_id] = slot
    try:
        fut = asyncio.run_coroutine_threadsafe(_ext_ws.send(json.dumps(cmd)), _loop)
        fut.result(timeout=10)
        if not slot["event"].wait(timeout):
            return {"ok": False, "error": "extension timeout"}
        return slot["resp"] or {"ok": False, "error": "empty response"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    finally:
        _pending.pop(cmd_id, None)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._reply({"extension_connected": _ext_ws is not None})
        else:
            self._reply({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/cmd":
            self._reply({"error": "not found"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            cmd = json.loads(self.rfile.read(n))
        except Exception:
            self._reply({"ok": False, "error": "bad json"}, 400)
            return
        self._reply(_send_to_ext(cmd))


async def _ws_main():
    global _loop
    _loop = asyncio.get_running_loop()
    async with websockets.serve(_ws_handler, "127.0.0.1", WS_PORT):
        print(f"[bridge] ws on {WS_PORT}, http on {HTTP_PORT}")
        await asyncio.Future()


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    asyncio.run(_ws_main())


if __name__ == "__main__":
    main()
