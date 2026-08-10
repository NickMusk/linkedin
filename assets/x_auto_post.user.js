// ==UserScript==
// @name         X Agent Auto-Post (linkedin-commenter)
// @namespace    linkedin-commenter
// @version      1.2
// @description  Auto-clicks Post on the x.com composer opened by the dashboard Launch Agent. Esc cancels the current post. Reports the outcome back to the dashboard tab.
// @match        https://x.com/*
// @match        https://twitter.com/*
// @grant        none
// ==/UserScript==
(function () {
  // Arm only in agent context: the dashboard opens /intent/post URLs. X's SPA
  // often redirects that to another URL before we can click — the sessionStorage
  // flag survives the redirect (same tab), so the script re-arms there. On
  // normal X browsing the flag is absent and this script exits immediately.
  const isIntent = location.pathname.indexOf('/intent/') === 0;
  if (isIntent) sessionStorage.setItem('x-agent-armed', String(Date.now()));
  const armedTs = +(sessionStorage.getItem('x-agent-armed') || 0);
  if (!isIntent && (!armedTs || Date.now() - armedTs > 120000)) return;

  let cancelled = false;
  let clickedAt = 0;

  function report(status, reason) {
    try {
      if (window.opener) window.opener.postMessage({source: 'x-agent', status: status, reason: reason || ''}, '*');
    } catch (e) {}
  }

  const banner = document.createElement('div');
  banner.textContent = 'Agent: will post in 3s — press Esc to cancel';
  banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;' +
    'background:#0284c7;color:#fff;font:13px sans-serif;padding:6px;text-align:center';
  document.documentElement.appendChild(banner);

  function fail(reason) {
    sessionStorage.removeItem('x-agent-armed');
    banner.textContent = 'Agent: NOT posted — ' + reason;
    banner.style.background = '#b91c1c';
    report('failed', reason);
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !cancelled) {
      cancelled = true;
      sessionStorage.removeItem('x-agent-armed');
      banner.textContent = 'Agent: cancelled — post manually, agent will reuse this tab';
      banner.style.background = '#b45309';
      report('cancelled', 'Esc pressed');
    }
  });

  const text = new URLSearchParams(location.search).get('text') || '';
  let armedAt = 0;

  function isDisabled(btn) {
    return btn.disabled || btn.getAttribute('aria-disabled') === 'true';
  }

  const t = setInterval(function () {
    if (cancelled) { clearInterval(t); return; }
    const btn = document.querySelector('[data-testid="tweetButton"]');
    if (!btn || isDisabled(btn) || clickedAt) return;
    if (!armedAt) { armedAt = Date.now(); return; }          // composer ready — start the 3s grace window
    if (Date.now() - armedAt < 3000) return;
    clickedAt = Date.now();
    btn.click();
    clearInterval(t);
    // Give X a moment to actually create the post; an error toast (reply
    // restricted, rate limited...) means it did NOT go out.
    setTimeout(function () {
      const toast = document.querySelector('[data-testid="toast"]');
      const err = toast && /not able|can’t|cannot|error|restricted|try again|too fast|limit/i.test(toast.textContent || '');
      if (err) { fail('X rejected the reply: ' + toast.textContent.slice(0, 120)); return; }
      sessionStorage.removeItem('x-agent-armed');
      banner.textContent = 'Agent: posted ✓ — leave this tab open, the agent reuses it';
      banner.style.background = '#15803d';
      report('posted');
      // Do NOT window.close(): the dashboard reuses this named tab for the next
      // reply. Closing it forced a fresh popup (blockers + lost opener link).
    }, 2500);
  }, 400);

  setTimeout(function () {                                    // give up after 1 min
    clearInterval(t);
    if (cancelled || clickedAt) return;
    if (text.length > 280) fail('reply is ' + text.length + ' chars (over the 280 limit, Post stays disabled)');
    else fail('Post button never became clickable (login wall, reply restrictions, or X error)');
  }, 60000);
})();
