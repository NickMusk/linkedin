// ==UserScript==
// @name         X Agent Auto-Post (linkedin-commenter)
// @namespace    linkedin-commenter
// @version      1.0
// @description  Auto-clicks Post on the x.com intent composer opened by the dashboard Launch Agent. Esc cancels the current post.
// @match        https://x.com/intent/post*
// @match        https://twitter.com/intent/*
// @grant        none
// ==/UserScript==
(function () {
  let cancelled = false;
  let armedAt = 0;

  const banner = document.createElement('div');
  banner.textContent = 'Agent: will post in 3s — press Esc to cancel';
  banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;' +
    'background:#0284c7;color:#fff;font:13px sans-serif;padding:6px;text-align:center';
  document.documentElement.appendChild(banner);

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      cancelled = true;
      banner.textContent = 'Agent: cancelled — post manually or close the tab';
      banner.style.background = '#b45309';
    }
  });

  const t = setInterval(function () {
    if (cancelled) { clearInterval(t); return; }
    const btn = document.querySelector('[data-testid="tweetButton"]');
    if (btn && !btn.disabled) {
      if (!armedAt) { armedAt = Date.now(); return; }        // composer ready — start the 3s grace window
      if (Date.now() - armedAt < 3000) return;
      btn.click();
      banner.textContent = 'Agent: posted ✓';
      banner.style.background = '#15803d';
      clearInterval(t);
      setTimeout(function () { window.close(); }, 2000);      // works: tab was opened by the dashboard
    }
  }, 400);

  setTimeout(function () { clearInterval(t); }, 60000);       // give up after 1 min (login wall, error, etc.)
})();
