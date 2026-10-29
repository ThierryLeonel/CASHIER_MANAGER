/* Cashier Manager UI helpers: modern pickers for <select>, live customer search,
   money formatting while typing. No external libraries. The native fields stay
   in the form (hidden) so the server receives exactly the same values. */
(function () {
  'use strict';
  var I18N = window.CM_I18N || {};
  function t(k, d) { return I18N[k] || d; }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  var KIND_LABEL = { CASH_DRAWER: t('cash', 'Cash'), UVE: 'UVE', ER: 'ER', MTN: 'MTN', BANK: t('bank', 'Bank') };
  var openPicker = null;
  function closeOpen() { if (openPicker) { openPicker.close(); } }
  document.addEventListener('click', function (e) { if (openPicker && !openPicker.root.contains(e.target)) closeOpen(); });

  /* ---------- select picker ---------- */
  function splitLabel(text) {
    // "UVE · [SAMPLE] Fako master (690 00 00 01)" -> main + sub
    var m = text.match(/^(.*?)\s*\(([^()]*)\)\s*$/);
    var main = text, sub = '';
    if (m) { main = m[1]; sub = m[2]; }
    var parts = main.split(' · ');
    if (parts.length > 1 && parts[0].length <= 4) { main = parts.slice(1).join(' · '); }
    return { main: main, sub: sub };
  }

  function enhanceSelect(sel) {
    if (sel.dataset.enhanced || sel.multiple || sel.closest('.no-enhance')) return;
    sel.dataset.enhanced = '1';
    var root = el('div', 'pk');
    sel.parentNode.insertBefore(root, sel);
    root.appendChild(sel);
    sel.classList.add('pk-native');
    sel.tabIndex = -1;
    var btn = el('button', 'pk-btn'); btn.type = 'button';
    btn.setAttribute('aria-haspopup', 'listbox');
    var lab = sel.id ? document.querySelector('label[for="' + sel.id + '"]') : null;
    if (lab) { lab.addEventListener('click', function (e) { e.preventDefault(); btn.focus(); open(); }); }
    var face = el('span', 'pk-face'), chev = el('span', 'pk-chev');
    chev.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>';
    btn.appendChild(face); btn.appendChild(chev);
    root.appendChild(btn);
    var pop = el('div', 'pk-pop'); pop.hidden = true;
    var search = el('input', 'pk-search'); search.type = 'text'; search.placeholder = t('search', 'Search…'); search.autocomplete = 'off';
    var list = el('div', 'pk-list'); list.setAttribute('role', 'listbox');
    pop.appendChild(search); pop.appendChild(list); root.appendChild(pop);
    var items = [], active = -1;

    function optView(o, target) {
      target.innerHTML = '';
      if (!o || !o.value) { target.appendChild(el('span', 'pk-ph', o ? (o.text.replace(/^-+$/, '') || t('choose', 'Choose…')) : t('choose', 'Choose…'))); return; }
      var kind = o.dataset.kind;
      if (kind) target.appendChild(el('span', 'pk-kind k-' + kind, KIND_LABEL[kind] || kind));
      var lbl = kind ? splitLabel(o.text) : { main: o.text, sub: '' };
      var txt = el('span', 'pk-txt');
      txt.appendChild(el('span', 'pk-main', lbl.main));
      if (lbl.sub) txt.appendChild(el('span', 'pk-sub', lbl.sub));
      target.appendChild(txt);
    }
    function render() { optView(sel.options[sel.selectedIndex], face); btn.classList.toggle('empty', !sel.value); }
    function build(filter) {
      list.innerHTML = ''; items = [];
      var f = (filter || '').toLowerCase();
      Array.prototype.forEach.call(sel.options, function (o) {
        if (o.hidden || o.disabled) return;
        if (!o.value && sel.required) return;
        if (f && o.text.toLowerCase().indexOf(f) < 0) return;
        var it = el('div', 'pk-opt'); it.setAttribute('role', 'option');
        if (o.selected && o.value) it.classList.add('sel');
        optView(o, it);
        it.addEventListener('mousedown', function (e) { e.preventDefault(); choose(o); });
        list.appendChild(it); items.push({ el: it, opt: o });
      });
      if (!items.length) list.appendChild(el('div', 'pk-none', t('noresult', 'No match')));
      active = items.findIndex(function (x) { return x.opt.selected; });
      highlight();
    }
    function highlight() {
      items.forEach(function (x, i) { x.el.classList.toggle('act', i === active); });
      if (items[active]) items[active].el.scrollIntoView({ block: 'nearest' });
    }
    function choose(o) {
      sel.value = o.value;
      sel.dispatchEvent(new Event('change', { bubbles: true }));
      render(); close(); btn.focus();
    }
    function open() {
      if (!pop.hidden) return;
      closeOpen();
      var count = Array.prototype.filter.call(sel.options, function (o) { return !o.hidden && !o.disabled && o.value; }).length;
      search.hidden = count < 7; search.value = '';
      build(''); pop.hidden = false; root.classList.add('open');
      openPicker = api;
      if (!search.hidden) search.focus();
    }
    function close() { pop.hidden = true; root.classList.remove('open'); if (openPicker === api) openPicker = null; }
    var api = { root: root, close: close };
    btn.addEventListener('click', function () { pop.hidden ? open() : close(); });
    function keys(e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (pop.hidden) { open(); return; }
        active = Math.max(0, Math.min(items.length - 1, active + (e.key === 'ArrowDown' ? 1 : -1)));
        highlight();
      } else if (e.key === 'Enter') {
        if (!pop.hidden && items[active]) { e.preventDefault(); choose(items[active].opt); }
      } else if (e.key === 'Escape') { close(); btn.focus(); }
    }
    btn.addEventListener('keydown', keys); search.addEventListener('keydown', keys);
    search.addEventListener('input', function () { build(search.value); });
    sel.addEventListener('change', render);
    // re-render when other scripts change hidden/disabled options
    new MutationObserver(render).observe(sel, { subtree: true, attributes: true, attributeFilter: ['hidden', 'disabled', 'selected'] });
    render();
  }

  /* ---------- live customer search ---------- */
  function enhanceCustomer(input) {
    if (input.dataset.enhanced) return;
    input.dataset.enhanced = '1';
    input.removeAttribute('list');
    input.autocomplete = 'off';
    var kind = input.dataset.customerSearch || '';
    var root = el('div', 'cs');
    input.parentNode.insertBefore(root, input); root.appendChild(input);
    var pop = el('div', 'pk-pop cs-pop'); pop.hidden = true;
    var list = el('div', 'pk-list'); pop.appendChild(list); root.appendChild(pop);
    var card = el('div', 'cs-card'); card.hidden = true; root.appendChild(card);
    var items = [], active = -1, timer = null, seq = 0;
    var api = { root: root, close: close };

    function subline(c, withKind) {
      var parts = [c.sim_display];
      if (c.subzone) parts.push(c.subzone);
      if (c.agent_in_charge) parts.push(t('agent', 'AG') + ': ' + c.agent_in_charge);
      if (withKind) parts.push(c.kind_label);
      return parts.join(' · ');
    }
    function showCard(c) {
      card.innerHTML = '';
      if (!c) { card.hidden = true; return; }
      var av = el('span', 'cs-av k-' + c.kind, c.kind === 'POS' ? t('pos', 'POS') : t('agent', 'AG'));
      var info = el('span', 'cs-info');
      info.appendChild(el('strong', null, c.microzone ? c.microzone + ' — ' + c.name : c.name));
      info.appendChild(el('span', 'pk-sub', subline(c, true)));
      var bal = el('span', 'cs-bal ' + (c.balance > 0 ? 'owes' : c.balance < 0 ? 'due' : ''));
      bal.appendChild(el('strong', null, c.balance_display + ' FCFA'));
      bal.appendChild(el('span', null, c.balance_label));
      card.appendChild(av); card.appendChild(info); card.appendChild(bal);
      card.hidden = false;
    }
    function row(c) {
      var it = el('div', 'pk-opt cs-opt');
      it.appendChild(el('span', 'cs-av k-' + c.kind, c.kind === 'POS' ? t('pos', 'POS') : t('agent', 'AG')));
      var info = el('span', 'pk-txt');
      if (c.microzone) {
        var main = el('span', 'pk-main');
        main.appendChild(el('span', 'cs-mz', c.microzone));
        main.appendChild(document.createTextNode(' ' + c.name));
        info.appendChild(main);
      } else {
        info.appendChild(el('span', 'pk-main', c.name));
      }
      info.appendChild(el('span', 'pk-sub', subline(c, false)));
      it.appendChild(info);
      var b = el('span', 'cs-mini ' + (c.balance > 0 ? 'owes' : c.balance < 0 ? 'due' : ''), c.balance_display);
      it.appendChild(b);
      it.addEventListener('mousedown', function (e) { e.preventDefault(); pick(c); });
      return it;
    }
    function pick(c) { input.value = c.sim; showCard(c); close(); input.dispatchEvent(new Event('change', { bubbles: true })); }
    function close() { pop.hidden = true; root.classList.remove('open'); if (openPicker === api) openPicker = null; }
    function highlight() { items.forEach(function (x, i) { x.el.classList.toggle('act', i === active); }); }
    function query(q, exactPick) {
      var my = ++seq;
      fetch('/api/customers/?kind=' + encodeURIComponent(kind) + '&q=' + encodeURIComponent(q), { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (my !== seq) return;
          var res = data.results || [];
          if (exactPick) {
            var digits = q.replace(/\D/g, '').slice(-9);
            var hit = res.find(function (c) { return c.sim === digits; });
            showCard(hit || null);
            return;
          }
          list.innerHTML = ''; items = [];
          res.forEach(function (c) { var it = row(c); list.appendChild(it); items.push({ el: it, c: c }); });
          if (!res.length) list.appendChild(el('div', 'pk-none', t('nocustomer', 'No agent or POS found')));
          active = res.length ? 0 : -1; highlight();
          closeOpen(); pop.hidden = false; root.classList.add('open'); openPicker = api;
        }).catch(function () {});
    }
    input.addEventListener('input', function () {
      showCard(null);
      clearTimeout(timer);
      var q = input.value.trim();
      timer = setTimeout(function () { query(q, false); }, 180);
    });
    input.addEventListener('focus', function () { if (!input.value.trim()) query('', false); });
    input.addEventListener('keydown', function (e) {
      if (pop.hidden) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault(); active = Math.max(0, Math.min(items.length - 1, active + (e.key === 'ArrowDown' ? 1 : -1))); highlight();
      } else if (e.key === 'Enter' && items[active]) { e.preventDefault(); pick(items[active].c); }
      else if (e.key === 'Escape') close();
    });
    if (input.value.trim()) query(input.value.trim(), true);
  }

  /* ---------- money input: group thousands while typing ---------- */
  function enhanceMoney(input) {
    if (input.dataset.enhanced) return;
    input.dataset.enhanced = '1';
    function fmt() {
      var neg = /^\s*-/.test(input.value);
      var d = input.value.replace(/[^\d]/g, '');
      if (!d) { if (!neg) input.value = ''; return; }
      input.value = (neg ? '-' : '') + d.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    }
    input.addEventListener('input', function () {
      var end = input.value.length - input.selectionStart;
      fmt();
      var p = Math.max(0, input.value.length - end); input.setSelectionRange(p, p);
    });
    fmt();
  }

  function init(scope) {
    scope.querySelectorAll('main select').forEach(enhanceSelect);
    scope.querySelectorAll('input[data-customer-search]').forEach(enhanceCustomer);
    scope.querySelectorAll('input.money').forEach(enhanceMoney);
  }
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeOpen(); });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { init(document); });
  else init(document);
})();
