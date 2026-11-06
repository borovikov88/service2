/* Browser-local presentation only. No network, model writes or business actions. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root.document) api.mount(root.document, root);
})(typeof window === 'undefined' ? globalThis : window, function () {
  'use strict';
  const allowedSections = ['inbox', 'construction', 'service', 'retail', 'tender'];
  const normalize = text => String(text || '').toLocaleLowerCase('ru').replace(/ё/g, 'е').trim();
  function readState(search) {
    const p = new URLSearchParams(search);
    return { section: allowedSections.includes(p.get('section')) ? p.get('section') : 'inbox',
      view: p.get('view') === 'table' ? 'table' : 'kanban', q: (p.get('q') || '').slice(0, 200),
      owner: ['anna','max','none'].includes(p.get('owner')) ? p.get('owner') : '',
      source: ['site','phone','object','tender'].includes(p.get('source')) ? p.get('source') : '',
      stage: p.get('stage') || '', attention: ['overdue','missing'].includes(p.get('attention')) ? p.get('attention') : 'all',
      deal: p.get('deal') || '', theme: p.get('theme') === 'dark' ? 'dark' : 'light' };
  }
  function matches(record, state, includeAttention = true) {
    return record.pipeline === state.section && (!state.owner || record.owner === state.owner) &&
      (!state.stage || record.stage === state.stage) && (!state.source || record.source === state.source) &&
      (!includeAttention || state.attention === 'all' || record.status === state.attention) &&
      normalize(record.search).includes(normalize(state.q));
  }
  function stateSearch(state) {
    const p = new URLSearchParams();
    for (const key of ['section','view','q','owner','stage','source','attention','deal','theme']) {
      if (state[key] && !(key === 'attention' && state[key] === 'all')) p.set(key, state[key]);
    }
    return '?' + p.toString();
  }
  function mount(doc, win) {
    const $ = s => doc.querySelector(s), $$ = s => [...doc.querySelectorAll(s)];
    const cards = $$('.acrm-card');
    const records = [...new Map(cards.map(card => [card.dataset.deal, card.dataset])).values()];
    const dialog = $('#acrm-detail');
    const controls = {q: $('#acrm-search'), owner: $('#acrm-owner'), stage: $('#acrm-stage'), source: $('#acrm-source')};
    let state = readState(win.location.search), debounce, lastTrigger;
    function validateStage() {
      if (!$$('#acrm-stage option').some(o => o.value === state.stage && (!o.dataset.pipeline || o.dataset.pipeline === state.section))) state.stage = '';
    }
    function syncURL() { win.history.replaceState(null, '', win.location.pathname + stateSearch(state) + win.location.hash); }
    function render() {
      validateStage();
      doc.documentElement.dataset.theme = state.theme;
      $('#acrm-theme').setAttribute('aria-label', state.theme === 'dark' ? 'Включить светлую тему' : 'Включить тёмную тему');
      $('#acrm-theme').setAttribute('aria-pressed', String(state.theme === 'dark'));
      for (const [key, el] of Object.entries(controls)) if (el.value !== state[key]) el.value = state[key];
      $$('#acrm-stage option[data-pipeline]').forEach(o => { o.hidden = o.dataset.pipeline !== state.section; o.disabled = o.hidden; });
      $$('[data-section]').forEach(button => { if (button.dataset.section === state.section) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current'); });
      const name = $(`[data-section="${state.section}"]`).children[1].textContent;
      $('#acrm-title').textContent = name;
      $('#acrm-breadcrumb').textContent = state.section === 'inbox' ? 'Очередь обращений' : 'Сделки';
      $('#acrm-subtitle').textContent = state.section === 'inbox' ? 'Новые обращения и уточнение потребностей' : 'Сделки команды · следующий шаг и текущий этап';
      for (const key of ['all','overdue','missing']) {
        const count = records.filter(r => matches(r, state, false) && (key === 'all' || r.status === key)).length;
        $(`#acrm-${key}-count`).textContent = count;
      }
      $$('[data-attention]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.attention === state.attention)));
      const count = records.filter(r => matches(r,state)).length;
      $('#acrm-result-count').textContent = `Показано: ${count} из ${records.filter(r => r.pipeline === state.section).length}`;
      cards.forEach(c => { c.hidden = !matches(c.dataset, state); });
      $$('[data-board]').forEach(board => { board.hidden = board.dataset.board !== state.section; });
      $$('.acrm-lane').forEach(lane => {
        const visible = [...lane.querySelectorAll('.acrm-card')].filter(c => !c.hidden).length;
        lane.querySelector('.acrm-lane-count').textContent = visible;
        lane.querySelector('.acrm-lane-empty').hidden = visible > 0;
      });
      $('#acrm-kanban').hidden = state.view !== 'kanban' || count === 0;
      $('#acrm-table').hidden = state.view !== 'table' || count === 0;
      $('#acrm-empty').hidden = count > 0;
      $$('[data-view]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.view === state.view)));
    }
    function closeDetail() {
      state.deal = ''; if (dialog.open) dialog.close();
      syncURL();
      const fallback = $(`[data-section="${state.section}"]`);
      if (lastTrigger && lastTrigger.isConnected && !lastTrigger.closest('[hidden]')) lastTrigger.focus();
      else fallback.focus();
    }
    function showDetail(id, trigger) {
      const template = doc.getElementById('detail-' + id);
      if (!template || template.tagName !== 'TEMPLATE') { state.deal = ''; return; }
      lastTrigger = trigger || lastTrigger;
      $('#acrm-detail-content').replaceChildren(template.content.cloneNode(true));
      state.deal = id;
      if (!dialog.open) dialog.showModal();
      dialog.scrollTop = 0;
      $('#acrm-detail-close').focus();
    }
    $$('[data-section]').forEach(b => b.addEventListener('click', () => { clearTimeout(debounce); state.q = controls.q.value; state.section = b.dataset.section; state.stage = ''; state.deal = ''; render(); syncURL(); $('#acrm-kanban').scrollLeft = 0; }));
    $$('[data-view]').forEach(b => b.addEventListener('click', () => { state.view = b.dataset.view; render(); syncURL(); }));
    $$('[data-attention]').forEach(b => b.addEventListener('click', () => { state.attention = b.dataset.attention; render(); syncURL(); }));
    controls.q.addEventListener('input', () => { clearTimeout(debounce); state.q = controls.q.value; debounce = win.setTimeout(() => { render(); syncURL(); }, 300); });
    for (const key of ['owner','stage','source']) controls[key].addEventListener('change', () => { clearTimeout(debounce); state.q = controls.q.value; state[key] = controls[key].value; render(); syncURL(); });
    $('#acrm-reset').addEventListener('click', () => { clearTimeout(debounce); Object.assign(state, {q:'', owner:'', stage:'', source:'', attention:'all'}); render(); syncURL(); controls.q.focus(); });
    $('#acrm-theme').addEventListener('click', () => { state.theme = state.theme === 'dark' ? 'light' : 'dark'; render(); syncURL(); });
    $$('[data-open]').forEach(b => b.addEventListener('click', () => { showDetail(b.dataset.open, b); syncURL(); }));
    $('#acrm-detail-close').addEventListener('click', closeDetail);
    dialog.addEventListener('cancel', e => { e.preventDefault(); closeDetail(); });
    dialog.addEventListener('click', e => { if (e.target === dialog) { const r = dialog.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) closeDetail(); } });
    win.addEventListener('popstate', () => { clearTimeout(debounce); state = readState(win.location.search); render(); if (state.deal) showDetail(state.deal); else if (dialog.open) dialog.close(); });
    render(); if (state.deal) showDetail(state.deal); syncURL();
  }
  return { readState, matches, normalize, stateSearch, mount };
});
