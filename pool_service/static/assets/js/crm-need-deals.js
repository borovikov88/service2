/* Selected saved approval lines only. Unknown POST results retain exact bytes/key. */
(() => {
  'use strict';
  const root = document.getElementById('object-needs');
  if (!root) return;
  const $ = (name) => document.getElementById('needs-' + name);
  const append = (parent, tag, value) => { const el = document.createElement(tag); el.textContent = value; parent.append(el); return el; };
  let current = null, pending = null, blocked = false, retainedController = null, linePage = 1, linePages = 1, timer = null, notice = '', retainedGeneration = 0, sending = false, reconciledSelection = null;
  const initial = new URL(location.href).searchParams;
  $('line-search').value = initial.get('line_q') || '';
  linePage = Math.max(1, Number(initial.get('line_page')) || 1);
  function frozen() {
    const busy = !!pending;
    root.dataset.dealPending = String(busy);
    $('deal-fields').disabled = busy || blocked || !current?.need_deals?.can_convert || root.dataset.reviewDirty === 'true' || root.dataset.reviewPending === 'true';
    if (busy) $('fields').disabled = true;
    $('close').disabled = busy || root.dataset.reviewPending === 'true';
    $('deal-refresh').hidden = !blocked;
  }
  function link(parent, deal) {
    if (!deal) return;
    const a = append(parent, 'a', deal.title); a.href = deal.url;
  }
  function invalidateHistory() { retainedGeneration++; retainedController?.abort(); }
  async function historyLines() {
    if (!current || current.need_deals?.restricted) return;
    invalidateHistory(); retainedController = new AbortController();
    const generation = retainedGeneration;
    const id = current.id;
    const url = new URL(location.href); url.searchParams.set('need_task', id);
    url.searchParams.set('line_q', $('line-search').value); url.searchParams.set('line_page', linePage);
    history.replaceState(null, '', url);
    $('line-status').textContent = 'Загрузка прежних позиций…';
    try {
      const response = await fetch(root.dataset.detailBase + id + '/lines/?' + new URLSearchParams({org_id:root.dataset.orgId, q:$('line-search').value, page:linePage}), {signal:retainedController.signal, credentials:'same-origin'});
      if (!response.ok) throw Error('История позиций сейчас недоступна.');
      const data = await response.json(); if (generation !== retainedGeneration || current?.id !== id) return;
      linePage = data.page; linePages = data.pages; $('line-history').replaceChildren();
      for (const row of data.rows) {
        const p = append($('line-history'), 'p', '#' + row.id + ' · ' + row.description + ' · состав #' + row.source_revision + (row.consumed ? ' · Уже в сделке: ' : ' · Ещё без сделки'));
        if (row.consumed) { if (row.deal) link(p, row.deal); else append(p, 'span', 'карточка недоступна'); }
      }
      $('line-status').textContent = 'Прежних позиций: ' + data.total;
      $('line-page').textContent = 'Страница ' + linePage + ' из ' + linePages;
      $('line-prev').disabled = linePage <= 1; $('line-next').disabled = linePage >= linePages;
    } catch (error) { if (generation === retainedGeneration && error.name !== 'AbortError') { $('line-history').replaceChildren(); $('line-status').textContent = error.message; } }
  }
  root.addEventListener('need:detail', (event) => {
    if (pending) return;
    invalidateHistory();
    const oldId = current?.id; current = event.detail; blocked = false;
    if (oldId !== current.id) { notice = ''; $('deal-result').replaceChildren(); }
    $('deals').hidden = !!current.need_deals?.restricted;
    $('deal-lines').replaceChildren(); $('retained').replaceChildren();
    const restoreSelection = reconciledSelection
      && reconciledSelection.task_id === current.id
      && reconciledSelection.review_id === current.review_id
      && reconciledSelection.review_revision === current.review_revision
      && reconciledSelection.source_revision === current.source_revision
      && reconciledSelection.source_hash === current.source_hash;
    const selectedTokens = new Set(restoreSelection ? reconciledSelection.tokens : []);
    reconciledSelection = null;
    const bindings = current.need_deals?.bindings || [];
    for (const position of current.positions.filter((row) => row.mode === 'approval_required')) {
      const binding = bindings.find((row) => row.line_token === position.line_token);
      const wrap = append($('deal-lines'), 'div', ''); wrap.className = 'border rounded p-2 mb-2'; wrap.dataset.token = position.line_token;
      const label = append(wrap, 'label', ''); label.className = 'd-flex gap-2 align-items-start';
      const check = document.createElement('input'); check.type = 'checkbox'; check.name = 'selected'; check.className = 'form-check-input';
      check.disabled = !!binding?.consumed;
      check.checked = !check.disabled && selectedTokens.has(position.line_token); label.append(check); append(label, 'span', position.description);
      if (binding) {
        wrap.dataset.lineId = binding.line_id;
        const p = append(wrap, 'p', 'Устойчивая позиция #' + binding.line_id + (binding.consumed ? ' · Уже в сделке: ' : ' · Сопоставление подтверждено'));
        if (binding.consumed) { if (binding.deal) link(p, binding.deal); else append(p, 'span', 'карточка недоступна'); }
      } else {
        const modeLabel = append(wrap, 'label', 'Идентичность выбранной позиции'); modeLabel.className = 'd-block';
        const select = append(modeLabel, 'select', ''); select.name = 'identity'; select.className = 'form-select';
        for (const [value, title] of [['','Выберите явно'], ['existing','Та же прежняя позиция по ID'], ['new','Новая самостоятельная потребность (подтверждаю)']]) { const opt = append(select, 'option', title); opt.value = value; }
        const idLabel = append(wrap, 'label', 'Прежний устойчивый ID'); idLabel.className = 'd-block';
        const input = append(idLabel, 'input', ''); input.name = 'line_id'; input.type = 'number'; input.min = '1'; input.step = '1'; input.className = 'form-control';
      }
    }
    if (!$('deal-lines').children.length) append($('deal-lines'), 'p', 'В текущем сохранённом разборе нет согласуемых позиций.');
    $('deal-feedback').textContent = notice; frozen();
    if (oldId != null && oldId !== current.id) linePage = 1;
    void historyLines();
  });
  root.addEventListener('need:busy', frozen);
  root.addEventListener('need:dirty', () => { frozen(); $('deal-feedback').textContent = 'Сначала сохраните изменённый разбор или обновите данные.'; });
  function base(kind, selections) {
    return {command_type:kind, task_id:current.id, source_revision:current.source_revision, source_hash:current.source_hash,
      review_id:current.review_id, review_revision:current.review_revision, task_version:current.task_version,
      operation_key:'needdeal_' + crypto.randomUUID().replaceAll('-', ''), selections};
  }
  function selected(reconcile) {
    const rows = Array.from($('deal-lines').children).filter((row) => row.querySelector('[name="selected"]:checked'));
    if (!rows.length) throw Error('Выберите хотя бы одну согласуемую позицию.');
    return rows.map((row) => {
      if (row.dataset.lineId) return reconcile ? {line_token:row.dataset.token, kind:'existing', line_id:Number(row.dataset.lineId)} : {line_token:row.dataset.token, line_id:Number(row.dataset.lineId)};
      if (!reconcile) throw Error('Сначала подтвердите идентичность выбранных позиций.');
      const kind = row.querySelector('[name="identity"]').value;
      if (!kind) throw Error('Явно выберите прежнюю или новую самостоятельную потребность для каждой выбранной строки.');
      const id = Number(row.querySelector('[name="line_id"]').value);
      if (kind === 'existing' && (!Number.isSafeInteger(id) || id <= 0)) throw Error('Укажите прежний устойчивый ID из истории.');
      return {line_token:row.dataset.token, kind, line_id:kind === 'new' ? null : id};
    });
  }
  async function send() {
    if (!pending || sending) return;
    sending = true; const request = pending; const payload = JSON.parse(request);
    frozen(); $('deal-retry').hidden = true; $('deal-feedback').textContent = 'Сохранение…';
    try {
      const csrf = document.cookie.split('; ').find((value) => value.startsWith('csrftoken='))?.split('=').slice(1).join('=') || '';
      const response = await fetch(root.dataset.dealCommandUrl, {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json', 'X-CSRFToken':decodeURIComponent(csrf)}, body:request});
      const data = await response.json().catch(() => { throw Error('Ответ не определён.'); });
      if (!response.ok) {
        if (response.status < 500) { pending = null; blocked = true; frozen(); }
        else $('deal-retry').hidden = false;
        $('deal-feedback').textContent = response.status === 409
          ? 'Операция могла быть выполнена ранее. Обновите данные и проверьте историю устойчивых позиций и связанных сделок перед новой операцией. ' + (data.error || '')
          : data.error || 'Ответ не определён. Повторите тот же запрос.';
        return;
      }
      const kind = payload.command_type;
      const resultId = kind === 'object_need.convert' ? data?.result?.deal_id : data?.result?.review_id;
      if (!Number.isSafeInteger(resultId) || resultId <= 0) throw Error('Ответ не определён.');
      reconciledSelection = kind === 'object_need.reconcile' ? {
        task_id: payload.task_id, review_id: payload.review_id, review_revision: payload.review_revision,
        source_revision: payload.source_revision, source_hash: payload.source_hash,
        tokens: payload.selections.map(row => row.line_token),
      } : null;
      pending = null; root.dataset.dealPending = 'false';
      notice = kind === 'object_need.reconcile' ? 'Идентичность подтверждена. Выберите группу сопоставленных позиций для сделки.' : 'Сделка и реальное следующее действие созданы. Исходная заявка не изменена.';
      if (data.result.deal_id) { $('deal-result').replaceChildren(); link($('deal-result'), {title:'Открыть созданную сделку', url:'/crm/workspace/?org_id=' + root.dataset.orgId + '&pipeline=' + payload.pipeline_id + '&deal=' + data.result.deal_id}); }
      // Do not reuse old bindings if the post-success refresh fails.
      blocked = true; frozen();
      root.dispatchEvent(new CustomEvent('need:refresh', {detail: {task_id: payload.task_id}}));
    } catch (error) { $('deal-feedback').textContent = 'Ответ не получен. Повторите тот же запрос для проверки результата.'; $('deal-retry').hidden = false; }
    finally { sending = false; }
  }
  function start(kind) {
    if (pending || blocked || !current?.need_deals?.can_convert || root.dataset.reviewDirty === 'true' || root.dataset.reviewPending === 'true') return;
    try {
      const payload = base(kind, selected(kind === 'object_need.reconcile'));
      if (kind === 'object_need.convert') {
        if (!$('deal-form').reportValidity()) return;
        const due = $('deal-due').value;
        payload.client_id = current.need_deals.client_id; payload.pipeline_id = Number($('deal-pipeline').value);
        payload.title = $('deal-title').value.trim(); payload.next_action = {title:$('deal-next').value.trim(), due_at:due + ':00' + root.dataset.timezoneOffset};
      }
      pending = JSON.stringify(payload); frozen(); root.dispatchEvent(new Event('need:busy')); void send();
    } catch (error) { $('deal-feedback').textContent = error.message; }
  }
  $('reconcile').addEventListener('click', () => start('object_need.reconcile'));
  $('deal-form').addEventListener('submit', (event) => { event.preventDefault(); start('object_need.convert'); });
  $('deal-retry').addEventListener('click', () => void send());
  $('deal-refresh').addEventListener('click', () => { if (!pending) root.dispatchEvent(new Event('need:refresh')); });
  $('line-search').addEventListener('input', () => { clearTimeout(timer); invalidateHistory(); linePage = 1; timer = setTimeout(() => void historyLines(), 300); });
  $('line-prev').addEventListener('click', () => { if (linePage > 1) { linePage--; void historyLines(); } });
  $('line-next').addEventListener('click', () => { if (linePage < linePages) { linePage++; void historyLines(); } });
})();
