// Tener Poster Bridge — service worker.
// Connects to the local bridge (ws://127.0.0.1:8766), owns ONE pinned
// background tab, and executes navigate/eval commands on it through
// chrome.debugger (CDP). Focus emulation keeps x.com rendering even though
// the tab is never focused.

const WS_URL = "ws://127.0.0.1:8766";
let ws = null;
let tabId = null;
let attached = false;

function log(...a) { console.log("[tener-bridge]", ...a); }

async function ensureTab() {
  if (tabId !== null) {
    try {
      await chrome.tabs.get(tabId);
      return tabId;
    } catch (e) { tabId = null; attached = false; }
  }
  const stored = await chrome.storage.local.get("tabId");
  if (stored.tabId !== undefined) {
    try {
      await chrome.tabs.get(stored.tabId);
      tabId = stored.tabId;
      return tabId;
    } catch (e) { /* tab gone */ }
  }
  const tab = await chrome.tabs.create({ url: "about:blank", pinned: true, active: false });
  tabId = tab.id;
  await chrome.storage.local.set({ tabId });
  attached = false;
  return tabId;
}

function sendCommand(target, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand(target, method, params || {}, (result) => {
      if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
      else resolve(result);
    });
  });
}

async function ensureAttached() {
  const id = await ensureTab();
  if (attached) return { tabId: id };
  const target = { tabId: id };
  await new Promise((resolve, reject) => {
    chrome.debugger.attach(target, "1.3", () => {
      if (chrome.runtime.lastError) {
        const msg = chrome.runtime.lastError.message || "";
        if (msg.includes("Another debugger")) resolve(); // уже привязан
        else reject(new Error(msg));
      } else resolve();
    });
  });
  attached = true;
  // страница должна считать себя сфокусированной, иначе x.com замораживает rAF
  await sendCommand(target, "Emulation.setFocusEmulationEnabled", { enabled: true }).catch(() => {});
  return target;
}

chrome.debugger.onDetach.addListener((source) => {
  if (source.tabId === tabId) { attached = false; log("detached"); }
  if (source.tabId === sessionTabId) { sessionAttached = false; }
});

// --- review session tab: VISIBLE, user-facing. The machine only opens pages
// and pre-fills text here; submitting is always the human's click. ---
let sessionTabId = null;
let sessionAttached = false;

chrome.tabs.onRemoved.addListener((closedId) => {
  if (closedId === sessionTabId) { sessionTabId = null; sessionAttached = false; }
});

async function ensureSessionAttached() {
  if (sessionTabId === null) throw new Error("no session tab");
  const target = { tabId: sessionTabId };
  if (sessionAttached) return target;
  await new Promise((resolve, reject) => {
    chrome.debugger.attach(target, "1.3", () => {
      const msg = (chrome.runtime.lastError && chrome.runtime.lastError.message) || "";
      if (!msg || msg.includes("Another debugger")) resolve();
      else reject(new Error(msg));
    });
  });
  sessionAttached = true;
  return target;
}

async function handle(cmd) {
  const target = await ensureAttached();
  if (cmd.cmd === "navigate") {
    await sendCommand(target, "Page.enable", {});
    await sendCommand(target, "Page.navigate", { url: cmd.url });
    return { ok: true, value: "navigating" };
  }
  if (cmd.cmd === "eval") {
    const r = await sendCommand(target, "Runtime.evaluate", {
      expression: cmd.js, returnByValue: true, awaitPromise: false,
    });
    if (r.exceptionDetails) return { ok: false, error: JSON.stringify(r.exceptionDetails).slice(0, 300) };
    const v = r.result && r.result.value;
    return { ok: true, value: v === undefined || v === null ? "" : String(v) };
  }
  if (cmd.cmd === "status") {
    return { ok: true, value: "ready tab=" + target.tabId };
  }
  return { ok: false, error: "unknown cmd " + cmd.cmd };
}

async function handleSession(cmd) {
  if (cmd.cmd === "session_open") {
    if (sessionTabId !== null) {
      try { await chrome.tabs.update(sessionTabId, { url: cmd.url, active: true }); }
      catch (e) { sessionTabId = null; sessionAttached = false; }
    }
    if (sessionTabId === null) {
      const t = await chrome.tabs.create({ url: cmd.url, active: true });
      sessionTabId = t.id;
    }
    try {
      const t = await chrome.tabs.get(sessionTabId);
      await chrome.windows.update(t.windowId, { focused: true });
    } catch (e) {}
    return { ok: true, value: "session tab " + sessionTabId };
  }
  if (cmd.cmd === "session_eval") {
    const target = await ensureSessionAttached();
    const r = await sendCommand(target, "Runtime.evaluate", {
      expression: cmd.js, returnByValue: true, awaitPromise: false,
    });
    if (r.exceptionDetails) return { ok: false, error: JSON.stringify(r.exceptionDetails).slice(0, 300) };
    const v = r.result && r.result.value;
    return { ok: true, value: v === undefined || v === null ? "" : String(v) };
  }
  if (cmd.cmd === "session_close") {
    if (sessionTabId !== null) {
      try { await chrome.tabs.remove(sessionTabId); } catch (e) {}
      sessionTabId = null; sessionAttached = false;
    }
    return { ok: true, value: "closed" };
  }
  return null;
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  try { ws = new WebSocket(WS_URL); } catch (e) { return; }
  ws.onopen = () => { log("bridge connected"); ws.send(JSON.stringify({ hello: "extension" })); };
  ws.onmessage = async (ev) => {
    let cmd;
    try { cmd = JSON.parse(ev.data); } catch (e) { return; }
    let resp;
    try {
      resp = cmd.cmd && cmd.cmd.startsWith("session_") ? await handleSession(cmd) : await handle(cmd);
      if (resp === null) resp = { ok: false, error: "unknown cmd " + cmd.cmd };
    }
    catch (e) { resp = { ok: false, error: String(e).slice(0, 300) }; }
    resp.id = cmd.id;
    try { ws.send(JSON.stringify(resp)); } catch (e) {}
  };
  ws.onclose = () => { log("bridge closed"); ws = null; setTimeout(connect, 5000); };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}

chrome.alarms.create("reconnect", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
chrome.runtime.onInstalled.addListener(() => connect());
connect();
