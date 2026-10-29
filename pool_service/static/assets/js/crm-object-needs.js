/* Internal own-task classification. Keep uncertain POST payload/key frozen. */
(() => {
  'use strict';
  const root = document.getElementById('object-needs');
  if (!root) return;
  const $ = (id) => document.getElementById('needs-' + id);
  const text = (parent, tag, value) => { const el = document.createElement(tag); el.textContent = value; parent.append(el); return el; };
  const token = () => 'line_' + crypto.randomUUID().replaceAll('-', '');
  let page = 1, pages = 1, current = null, pending = null, controller = null, detailController = null, timer = null, detailGeneration = 0, sending = false;
  const filters = {q: $('search'), scope: $('scope'), state: $('state'), mode: $('mode')};
  const initial = new URL(location.href).searchParams;
  for (const [key, field] of Object.entries(filters)) if (initial.has(key)) field.value = initial.get(key);
  page = Math.max(1, Number(initial.get('page')) || 1);
  const params = () => { const p = new URLSearchParams({org_id: root.dataset.orgId, page: String(page)}); for (const [key, field] of Object.entries(filters)) if (field.value) p.set(key, field.value); return p; };
  async function load() {
    controller?.abort(); controller = new AbortController();
    const p = params(); const url = new URL(location.href);
    // Own only list filters: retained-history URL state belongs to the sibling controller.
    for (const key of ['org_id', 'page', ...Object.keys(filters)]) {
      if (p.has(key)) url.searchParams.set(key, p.get(key)); else url.searchParams.delete(key);
    }
    history.replaceState(null, '', url);
    $('status').textContent = 'Загрузка…';
    try {
      const response = await fetch(root.dataset.dataUrl.split('?')[0] + '?' + p, {signal: controller.signal, credentials: 'same-origin'});
      if (!response.ok) throw Error('Не удалось загрузить доступные потребности.');
      const data = await response.json(); $('rows').replaceChildren(); page = data.page; pages = data.pages;
      for (const row of data.rows) {
        const tr = document.createElement('tr');
        text(tr, 'td', row.client + '\n' + row.object); text(tr, 'td', row.author + '\n' + new Date(row.recorded_at).toLocaleString('ru-RU'));
        text(tr, 'td', row.materials + (row.materials_truncated ? '…' : ''));
        text(tr, 'td', row.state_label + ' · редакция ' + (row.source_revision || 'источник без редакции'));
        const cell = text(tr, 'td', ''); const open = text(cell, 'button', 'Открыть разбор'); open.type = 'button'; open.className = 'btn btn-sm btn-outline-primary'; open.addEventListener('click', () => openDetail(row.id));
        $('rows').append(tr);
      }
      $('status').textContent = 'Доступно: ' + data.total;
      $('page').textContent = 'Страница ' + page + ' из ' + pages;
      $('prev').disabled = page <= 1; $('next').disabled = page >= pages;
    } catch (error) { if (error.name !== 'AbortError') $('status').textContent = error.message; }
  }
  // Abort alone cannot stop a response whose body is already being parsed.
  function invalidateDetail() { detailGeneration++; detailController?.abort(); }
  function markDirty() {
    invalidateDetail();
    root.dataset.reviewDirty = 'true'; root.dispatchEvent(new Event('need:dirty'));
  }
  function editable() { return !pending && root.dataset.dealPending !== 'true' && current?.can_review; }
  function addLine(line = {}) {
    if ($('lines').children.length >= 50) return;
    const el = document.querySelector('#needs-line-template').content.firstElementChild.cloneNode(true);
    el.dataset.token = line.line_token || token();
    for (const key of ['description', 'mode', 'reason']) el.querySelector(`[name="${key}"]`).value = line[key] || '';
    el.querySelector('[data-remove]').addEventListener('click', () => { if (editable()) { el.remove(); markDirty(); } });
    $('lines').append(el);
    return true;
  }
  function frozen(value) { $('fields').disabled = value || !current?.can_review; $('close').disabled = value; $('refresh').hidden = !value; }
  async function openDetail(id) {
    if (pending || root.dataset.dealPending === 'true') return;
    invalidateDetail(); detailController = new AbortController();
    const generation = detailGeneration;
    try {
      const response = await fetch(root.dataset.detailBase + id + '/?org_id=' + root.dataset.orgId, {credentials: 'same-origin', signal: detailController.signal});
      if (!response.ok) throw Error('Исходная заявка недоступна.');
      const data = await response.json();
      if (generation !== detailGeneration || pending || root.dataset.dealPending === 'true') return false;
      current = data.need; $('detail').hidden = false; $('lines').replaceChildren(); $('history').replaceChildren();
      $('title').textContent = current.client + ' · ' + current.object;
      $('meta').textContent = current.author + ' · ' + new Date(current.recorded_at).toLocaleString('ru-RU') + ' · ' + current.state_label + ' · редакция ' + current.source_revision;
      $('materials').textContent = current.materials || '(Состав очищен в исходной записи)';
      $('task').href = current.task_url; $('readonly').textContent = current.readonly_reason;
      if (current.materials_truncated) $('readonly').textContent += ' Состав превышает предел экрана; полный текст доступен в исходной задаче.';
      for (const line of current.positions) addLine(line);
      if (!current.positions.length && current.can_review) addLine();
      for (const source of current.sources) text($('history'), 'p', 'Состав #' + source.sequence + ' · ' + source.actor + ' · ' + new Date(source.recorded_at).toLocaleString('ru-RU') + ': ' + (source.materials || '(очищен)') + (source.materials_truncated ? '…' : ''));
      for (const review of current.history) {
        text($('history'), 'h3', 'Разбор #' + review.sequence + ' состава #' + review.source_revision + ' · ' + review.actor);
        for (const line of review.positions) text($('history'), 'p', line.description + ' · ' + (document.querySelector(`#needs-mode option[value="${line.mode}"]`)?.textContent || line.mode) + ' · ' + line.reason);
      }
      $('feedback').textContent = ''; $('retry').hidden = true; frozen(false);
      root.dataset.reviewDirty = 'false';
      root.dispatchEvent(new CustomEvent('need:detail', {detail: current}));
      $('detail').scrollIntoView({behavior: 'smooth', block: 'start'});
      return true;
    } catch (error) { if (generation === detailGeneration && error.name !== 'AbortError') $('status').textContent = error.message; }
    return false;
  }
  async function send() {
    if (!pending || sending) return;
    sending = true; const request = pending; const taskId = JSON.parse(request).task_id;
    $('retry').hidden = true; $('feedback').textContent = 'Сохранение…'; frozen(true);
    try {
      const csrf = document.cookie.split('; ').find((s) => s.startsWith('csrftoken='))?.split('=').slice(1).join('=') || '';
      const response = await fetch(root.dataset.commandUrl, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRFToken': decodeURIComponent(csrf)}, body: request});
      const result = await response.json().catch(() => {
        if (!response.ok) return {error: 'Запрос отклонён. Проверьте текущий доступ и обновите данные.'};
        throw Error('Неопределённый ответ сервера.');
      });
      if (!response.ok) {
        // Explicit validation/conflict is a known failure. Preserve entered text
        // but require reload before a new operation on newer source versions.
        if (response.status < 500) { pending = null; root.dataset.reviewPending = 'false'; markDirty(); $('fields').disabled = true; $('close').disabled = false; $('refresh').hidden = false; }
        else $('retry').hidden = false;
        $('feedback').textContent = response.status === 409
          ? 'Операция могла быть выполнена ранее. Обновите данные и проверьте историю разбора, устойчивых позиций и связанных сделок перед новой операцией. ' + (result.error || '')
          : result.error || 'Разбор не сохранён. Обновите данные.';
        return;
      }
      if (!Number.isSafeInteger(result?.result?.review_id) || result.result.review_id <= 0) throw Error('Ответ не определён.');
      pending = null; root.dataset.reviewPending = 'false';
      if (await openDetail(taskId)) $('feedback').textContent = 'Разбор сохранён. Задача и состояние исполнения не изменены.';
      // List freshness must not retain POST ownership after detail is editable.
      void load();
    } catch (error) { $('feedback').textContent = 'Ответ не получен. Повторите тот же запрос для проверки результата.'; $('retry').hidden = false; }
    finally { sending = false; }
  }
  $('form').addEventListener('submit', (event) => {
    event.preventDefault(); if (pending || sending || root.dataset.dealPending === 'true' || !current?.can_review || !$('form').reportValidity()) return;
    const positions = Array.from($('lines').children, (line) => ({line_token: line.dataset.token, description: line.querySelector('[name="description"]').value.trim(), mode: line.querySelector('[name="mode"]').value, reason: line.querySelector('[name="reason"]').value.trim()}));
    if (!positions.length) { $('feedback').textContent = 'Добавьте хотя бы одну позицию с основанием.'; return; }
    pending = JSON.stringify({task_id: current.id, source_revision: current.source_revision, source_hash: current.source_hash, review_revision: current.review_revision, task_version: current.task_version, operation_key: 'need_' + crypto.randomUUID().replaceAll('-', ''), positions});
    root.dataset.reviewPending = 'true'; root.dispatchEvent(new Event('need:busy'));
    void send();
  });
  $('retry').addEventListener('click', () => { void send(); });
  $('refresh').addEventListener('click', () => { if (!pending && current) void openDetail(current.id); });
  $('add').addEventListener('click', () => { if (editable() && addLine()) markDirty(); });
  $('close').addEventListener('click', () => { if (!pending && root.dataset.dealPending !== 'true') { invalidateDetail(); $('detail').hidden = true; } });
  $('form').addEventListener('input', markDirty);
  $('form').addEventListener('change', markDirty);
  root.addEventListener('need:busy', invalidateDetail);
  root.addEventListener('need:refresh', (event) => {
    const id = event.detail?.task_id || current?.id;
    if (!pending && id) void openDetail(id);
  });
  for (const [key, field] of Object.entries(filters)) field.addEventListener(key === 'q' ? 'input' : 'change', () => { clearTimeout(timer); controller?.abort(); page = 1; timer = setTimeout(() => { void load(); }, 300); });
  $('prev').addEventListener('click', () => { if (page > 1) { page--; void load(); } });
  $('next').addEventListener('click', () => { if (page < pages) { page++; void load(); } });
  void load();
  if (Number(initial.get('need_task')) > 0) void openDetail(Number(initial.get('need_task')));
})();
