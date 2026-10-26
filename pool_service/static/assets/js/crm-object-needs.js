/* Internal own-task classification. Keep uncertain POST payload/key frozen. */
(() => {
  'use strict';
  const root = document.getElementById('object-needs');
  if (!root) return;
  const $ = (id) => document.getElementById('needs-' + id);
  const text = (parent, tag, value) => { const el = document.createElement(tag); el.textContent = value; parent.append(el); return el; };
  const token = () => 'line_' + crypto.randomUUID().replaceAll('-', '');
  let page = 1, pages = 1, current = null, pending = null, controller = null, detailController = null, timer = null;
  const filters = {q: $('search'), scope: $('scope'), state: $('state'), mode: $('mode')};
  const initial = new URL(location.href).searchParams;
  for (const [key, field] of Object.entries(filters)) if (initial.has(key)) field.value = initial.get(key);
  page = Math.max(1, Number(initial.get('page')) || 1);
  const params = () => { const p = new URLSearchParams({org_id: root.dataset.orgId, page: String(page)}); for (const [key, field] of Object.entries(filters)) if (field.value) p.set(key, field.value); return p; };
  async function load() {
    controller?.abort(); controller = new AbortController();
    const p = params(); const url = new URL(location.href); url.search = p.toString(); history.replaceState(null, '', url);
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
  function addLine(line = {}) {
    if ($('lines').children.length >= 50) return;
    const el = document.querySelector('#needs-line-template').content.firstElementChild.cloneNode(true);
    el.dataset.token = line.line_token || token();
    for (const key of ['description', 'mode', 'reason']) el.querySelector(`[name="${key}"]`).value = line[key] || '';
    el.querySelector('[data-remove]').addEventListener('click', () => { if (!pending) el.remove(); });
    $('lines').append(el);
  }
  function frozen(value) { $('fields').disabled = value || !current?.can_review; $('close').disabled = value; $('refresh').hidden = !value; }
  async function openDetail(id) {
    if (pending) return;
    detailController?.abort(); detailController = new AbortController();
    try {
      const response = await fetch(root.dataset.detailBase + id + '/?org_id=' + root.dataset.orgId, {credentials: 'same-origin', signal: detailController.signal});
      if (!response.ok) throw Error('Исходная заявка недоступна.');
      current = (await response.json()).need; $('detail').hidden = false; $('lines').replaceChildren(); $('history').replaceChildren();
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
      $('detail').scrollIntoView({behavior: 'smooth', block: 'start'});
    } catch (error) { if (error.name !== 'AbortError') $('status').textContent = error.message; }
  }
  async function send() {
    if (!pending) return;
    $('retry').hidden = true; $('feedback').textContent = 'Сохранение…'; frozen(true);
    try {
      const csrf = document.cookie.split('; ').find((s) => s.startsWith('csrftoken='))?.split('=').slice(1).join('=') || '';
      const response = await fetch(root.dataset.commandUrl, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRFToken': decodeURIComponent(csrf)}, body: pending});
      const result = await response.json().catch(() => {
        if (!response.ok) return {error: 'Запрос отклонён. Проверьте текущий доступ и обновите данные.'};
        throw Error('Неопределённый ответ сервера.');
      });
      if (!response.ok) {
        // Explicit validation/conflict is a known failure. Preserve entered text
        // but require reload before a new operation on newer source versions.
        if (response.status < 500) { pending = null; $('fields').disabled = true; $('close').disabled = false; $('refresh').hidden = false; }
        else $('retry').hidden = false;
        $('feedback').textContent = result.error || 'Разбор не сохранён. Обновите данные.'; return;
      }
      pending = null; const id = current.id; await openDetail(id); $('feedback').textContent = 'Разбор сохранён. Задача и состояние исполнения не изменены.'; await load();
    } catch (error) { $('feedback').textContent = 'Ответ не получен. Повторите тот же запрос для проверки результата.'; $('retry').hidden = false; }
  }
  $('form').addEventListener('submit', (event) => {
    event.preventDefault(); if (pending || !current?.can_review || !$('form').reportValidity()) return;
    const positions = Array.from($('lines').children, (line) => ({line_token: line.dataset.token, description: line.querySelector('[name="description"]').value.trim(), mode: line.querySelector('[name="mode"]').value, reason: line.querySelector('[name="reason"]').value.trim()}));
    if (!positions.length) { $('feedback').textContent = 'Добавьте хотя бы одну позицию с основанием.'; return; }
    pending = JSON.stringify({task_id: current.id, source_revision: current.source_revision, source_hash: current.source_hash, review_revision: current.review_revision, task_version: current.task_version, operation_key: 'need_' + crypto.randomUUID().replaceAll('-', ''), positions});
    void send();
  });
  $('retry').addEventListener('click', () => { void send(); });
  $('refresh').addEventListener('click', () => { if (!pending && current) void openDetail(current.id); });
  $('add').addEventListener('click', () => { if (!pending) addLine(); });
  $('close').addEventListener('click', () => { if (!pending) $('detail').hidden = true; });
  for (const [key, field] of Object.entries(filters)) field.addEventListener(key === 'q' ? 'input' : 'change', () => { clearTimeout(timer); controller?.abort(); page = 1; timer = setTimeout(() => { void load(); }, 300); });
  $('prev').addEventListener('click', () => { if (page > 1) { page--; void load(); } });
  $('next').addEventListener('click', () => { if (page < pages) { page++; void load(); } });
  void load();
})();
