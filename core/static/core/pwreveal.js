/* Show / hide password: an eye button beside every password box (sign-in, my password, users, shop staff,
   Configuration). Works for boxes added later too. */
(function () {
  var EYE = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
  var EYE_OFF = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3l18 18"/><path d="M10.6 5.1A10.9 10.9 0 0 1 12 5c6.5 0 10 7 10 7a17.6 17.6 0 0 1-3.2 4.2M6.6 6.6C3.8 8.4 2 12 2 12s3.5 7 10 7c1.8 0 3.3-.5 4.6-1.2"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';
  var lang = (document.documentElement.lang || 'en').slice(0, 2);
  var T = lang === 'fr' ? { show: 'Afficher le mot de passe', hide: 'Masquer le mot de passe' }
                        : { show: 'Show password', hide: 'Hide password' };
  function add(input) {
    if (input.dataset.pwEye) return;
    input.dataset.pwEye = '1';
    var wrap = document.createElement('span');
    wrap.className = 'pw-wrap';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'pw-eye';
    b.innerHTML = EYE;
    b.title = T.show;
    b.setAttribute('aria-label', T.show);
    b.setAttribute('aria-pressed', 'false');
    b.addEventListener('click', function () {
      var show = input.type === 'password';
      var pos = input.selectionStart;
      input.type = show ? 'text' : 'password';
      b.innerHTML = show ? EYE_OFF : EYE;
      b.title = show ? T.hide : T.show;
      b.setAttribute('aria-label', b.title);
      b.setAttribute('aria-pressed', show ? 'true' : 'false');
      input.focus();
      try { if (pos != null) input.setSelectionRange(pos, pos); } catch (e) { /* ignore */ }
    });
    wrap.appendChild(b);
  }
  function scan(root) { (root || document).querySelectorAll('input[type=password]').forEach(add); }
  // hide again before the form is sent, so the browser saves it as a password
  document.addEventListener('submit', function (e) {
    e.target.querySelectorAll && e.target.querySelectorAll('input[data-pw-eye]').forEach(function (i) { i.type = 'password'; });
  }, true);
  var css = document.createElement('style');
  css.textContent = '.pw-wrap{position:relative;display:block;width:100%}.pw-wrap>input{width:100%;padding-right:2.9rem!important;box-sizing:border-box}' +
    '.pw-eye{position:absolute;right:.25rem;top:50%;transform:translateY(-50%);width:2.4rem;height:2.4rem;display:flex;align-items:center;justify-content:center;' +
    'background:none;border:0;border-radius:8px;color:inherit;opacity:.65;cursor:pointer;padding:0;margin:0}.pw-eye:hover,.pw-eye:focus-visible{opacity:1;background:rgba(127,127,127,.15)}' +
    '#content .pw-wrap{display:inline-block;width:auto}#content .pw-wrap>input{width:auto}';
  document.head.appendChild(css);
  function start() {
    scan(document);
    if (window.MutationObserver) new MutationObserver(function (ms) {
      ms.forEach(function (m) { m.addedNodes.forEach(function (n) { if (n.nodeType === 1) { if (n.matches && n.matches('input[type=password]')) add(n); else scan(n); } }); });
    }).observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
