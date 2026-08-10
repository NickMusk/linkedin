// ==UserScript==
// @name         X Agent Auto-Post (linkedin-commenter)
// @namespace    linkedin-commenter
// @version      1.1
// @description  Auto-clicks Post on the x.com intent composer opened by the dashboard Launch Agent. Esc cancels the current post. Reports the outcome back to the dashboard tab.
// @match        https://x.com/intent/post*
// @match        https://twitter.com/intent/*
// @grant        none
// ==/UserScript==
(function () {
  let cancelled = false;
  let armedAt = 0;

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
    banner.textContent = 'Agent: NOT posted — ' + reason;
    banner.style.background = '#b91c1c';
    report('failed', reason);
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !cancelled) {
      cancelled = true;
      banner.textContent = 'Agent: cancelled — post manually or close the tab';
      banner.style.background = '#b45309';
      report('cancelled', 'Esc pressed');
    }
  });

  const text = new URLSearchParams(location.search).get('text') || '';

  function isDisabled(btn) {
    return btn.disabled || btn.getAttribute('aria-disabled') === 'true';
  }

  const t = setInterval(function () {
    if (cancelled) { clearInterval(t); return; }
    const btn = document.querySelector('[data-testid="tweetButton"]');
    if (btn && !isDisabled(btn)) {
      if (!armedAt) { armedAt = Date.now(); return; }        // composer ready — start the 3s grace window
      if (Date.now() - armedAt < 3000) return;
      btn.click();
      banner.textContent = 'Agent: posted ✓';
      banner.style.background = '#15803d';
      clearInterval(t);
      // Give X a moment to actually create the post; if an error toast pops up
      // (reply restricted etc.), the user still sees it before the tab closes.
      setTimeout(function () {
        const toast = document.querySelector('[data-testid="toast"]');
        const err = toast && /not able|can’t|cannot|error|restricted/i.test(toast.textContent || '');
        if (err) { fail('X rejected the reply: ' + toast.textContent.slice(0, 120)); return; }
        report('posted');
        window.close();                                       // works: tab was opened by the dashboard
      }, 2000);
    }
  }, 400);

  setTimeout(function () {                                    // give up after 1 min
    clearInterval(t);
    if (cancelled || banner.textContent.indexOf('✓') !== -1) return;
    if (text.length > 280) fail('reply is ' + text.length + ' chars (over the 280 limit, Post stays disabled)');
    else fail('Post button never became clickable (login wall, reply restrictions, or X error)');
  }, 60000);
})();
