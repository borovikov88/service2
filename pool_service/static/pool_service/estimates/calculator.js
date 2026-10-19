import {setExcluded, emptyRow} from './state.js';

const root = document.querySelector('#pool-estimator');
const $ = selector => root.querySelector(selector);
const form = $('#pe-parameters');
const org = $('#pe-org');
const titles = {economy: 'Эконом', standard: 'Стандарт', premium: 'Премиум'};
const money = value => value === null || value === undefined ? '—' : new Intl.NumberFormat('ru-RU', {minimumFractionDigits: 0, maximumFractionDigits: 2}).format(Number(value));
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
const stamp = value => new Date(value).toLocaleString('ru-RU', {dateStyle: 'short', timeStyle: 'short'});
let snapshot, selected = 'economy', projectId = null, revision = 0, loadedVersion = null, versions = [];
let dirty = false, parametersDirty = false, sequence = 0, busy = false, timer, searchTimer, searchAbort;
let rowBeingEdited = null, copyOnSave = false;
let currentOrg = org.value;
let catalogTarget, catalogTimer, catalogAbort, catalogSequence = 0;

function catalogOrigin(row) {
  if (!row.catalog) return '';
  const c = row.catalog;
  const override = Number(row.price) !== Number(c.price) || row.price === null;
  return `<span class="pe-row-detail" data-origin="${esc(row.id)}"><a href="${esc(c.url)}" target="_blank" rel="noopener noreferrer">Товар магазина${c.sku ? ' · ' + esc(c.sku) : ''}</a> · ${c.selection === 'auto' ? 'автоподбор' : 'выбран вручную'}<br>Цена сайта: ${money(c.price)} ₽ · ${esc(stamp(c.checked_at))}${override ? ' · цена сметы изменена вручную' : ''}${c.missing ? ' · отсутствует в выгрузке' : ''}</span>`;
}
function showCatalogStatus(data) {
  const s = data.catalog_status || data.status;
  $('#pe-catalog-status').textContent = data.catalog_error || (s
    ? `Товаров: ${s.count}. Получено ${stamp(s.checked_at)}; выгрузка магазина ${s.feed_date}. Обновление кэша — до 15 минут. ${Object.entries(s.brands).filter(([,count]) => !count).map(([brand]) => `${brand}: нет товаров в выгрузке.`).join(' ')}`
    : 'В расчёте зафиксированы модели и цены выбранной версии. Новые цены применяются отдельной кнопкой к текущей смете; ручные цены и исключённые строки сохраняются.');
}
function catalogInputsReady() {
  if (parametersDirty) { status('Сначала пересчитайте изменённые параметры бассейна.', true); return false; }
  return Boolean(snapshot);
}
function openCatalog(block, row) {
  if (!catalogInputsReady()) return;
  const item = snapshot.variants[selected].blocks.find(b => b.id === block).rows.find(r => r.id === row);
  catalogTarget = {block, row, variant: selected};
  $('#pe-catalog-target').textContent = `${titles[selected]} · заменить «${item.name}». Количество и исключения сохранятся.`;
  const q = new URL(location.href).searchParams;
  $('#pe-catalog-query').value = q.get('catalog_q') || '';
  $('#pe-catalog-kind').value = item.kind === 'other' ? '' : item.kind;
  $('#pe-catalog-brand').value = ['Aquaviva','Hayward','IML'].includes(item.brand) ? item.brand : '';
  $('#pe-catalog-dialog').showModal();
  searchCatalog();
}
async function searchCatalog() {
  catalogAbort?.abort(); catalogAbort = new AbortController();
  const token = ++catalogSequence;
  const query = $('#pe-catalog-query').value;
  const url = new URL(location.href);
  if (query) url.searchParams.set('catalog_q', query); else url.searchParams.delete('catalog_q');
  history.replaceState(null, '', url);
  $('#pe-catalog-results').textContent = 'Загружаем товары…';
  try {
    const data = await api({action: 'catalog_search', q: query, kind: $('#pe-catalog-kind').value, brand: $('#pe-catalog-brand').value, inputs: snapshot.inputs}, catalogAbort.signal);
    if (token !== catalogSequence || !$('#pe-catalog-dialog').open) return;
    showCatalogStatus(data);
    $('#pe-catalog-results').innerHTML = `<p class="pe-help">Найдено: ${data.total}${data.total > 40 ? '. Показаны первые 40 — уточните поиск.' : ''}</p>` + data.products.map(p => `<article class="pe-catalog-item"><div><strong>${esc(p.name)}</strong><span class="pe-row-detail">${esc(p.brand)}${p.sku ? ' · арт. ' + esc(p.sku) : ''} · ${p.available ? 'Доступен к заказу' : 'Доступность не подтверждена'}</span><span class="pe-row-detail">${p.nominal_flow ? money(p.nominal_flow) + ' м³/ч по каталогу · ' : ''}${p.head ? 'при ' + money(p.head) + ' м · ' : ''}${p.power !== 'unknown' ? esc(p.power) + ' В' : ''}</span><p class="pe-help">${esc(p.warning || (p.kind === 'other' ? 'Назначение, единицу измерения и количество проверьте в карточке.' : 'Предварительный кандидат. Проверьте рабочий режим, совместимость и комплектацию.'))}</p><a class="pe-link" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">Карточка товара ↗</a></div><div class="pe-catalog-price"><strong>${money(p.price)} ₽</strong><button class="pe-btn" type="button" data-catalog-product="${esc(p.id)}">Выбрать</button></div></article>`).join('') + (!data.total ? '<p>Товары не найдены. Измените бренд или запрос; строка сметы останется без замены.</p>' : '');
  } catch (error) { if (error.name !== 'AbortError' && token === catalogSequence) $('#pe-catalog-results').textContent = error.message; }
}

function status(message, error = false) {
  $('#pe-status').textContent = message;
  $('#pe-status').classList.toggle('pe-error', error);
}
function changed() {
  dirty = true;
  sequence++;
  status('Есть несохранённые изменения');
}
function inputs() {
  return Object.fromEntries(new FormData(form));
}
function rememberLocation() {
  const url = new URL(location.href);
  url.searchParams.set('organization', currentOrg);
  if (projectId) url.searchParams.set('id', projectId); else url.searchParams.delete('id');
  if (loadedVersion) url.searchParams.set('version', loadedVersion); else url.searchParams.delete('version');
  history.replaceState(null, '', url);
}
async function api(data, signal) {
  const response = await fetch(root.dataset.api, {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': $('[name=csrfmiddlewaretoken]').value},
    credentials: 'same-origin', body: JSON.stringify({organization: currentOrg, ...data}), signal,
  });
  let result;
  try { result = await response.json(); } catch { throw new Error('Сессия закончилась или сервер недоступен. Правки остаются на странице; сохраните после восстановления доступа.'); }
  if (!response.ok) throw new Error(result.error || 'Не удалось выполнить действие.');
  return result;
}
async function read(data, signal) {
  const response = await fetch(`${root.dataset.api}?${new URLSearchParams({organization: currentOrg, ...data})}`, {credentials: 'same-origin', signal});
  if (!response.ok) throw new Error('Расчёт недоступен или сессия закончилась.');
  return response.json();
}
async function run(action) {
  if (busy) return false;
  busy = true;
  // A request owns a stable UI snapshot. No edits can be lost to a late response.
  const disabled = [...root.querySelectorAll('button,input,select,textarea')].map(el => [el, el.disabled]);
  disabled.forEach(([el]) => { el.disabled = true; });
  try { await action(); return true; } catch (error) { status(error.message, true); return false; }
  finally { disabled.forEach(([el, was]) => { el.disabled = was; }); busy = false; }
}
function queueValidation() {
  clearTimeout(timer);
  const token = sequence;
  timer = setTimeout(async () => {
    try {
      const result = await api({action: 'validate', snapshot});
      if (token !== sequence || busy) return;
      snapshot = result.snapshot;
      renderTotals();
    } catch (error) { if (token === sequence) status(error.message, true); }
  }, 300);
}
function syncScope() {
  root.querySelectorAll('[data-scope]').forEach(input => {
    const values = Object.values(snapshot.variants).map(v => !v.blocks.find(b => b.id === input.dataset.scope).excluded);
    input.checked = values.every(Boolean);
    input.indeterminate = values.some(Boolean) && !values.every(Boolean);
  });
}
function renderTotals() {
  const g = snapshot.geometry;
  $('#pe-metrics').innerHTML = [['Объём воды', g.volume, 'м³'], ['Зеркало воды', g.surface, 'м²'], ['Площадь отделки', g.finish_area, 'м²'], ['Требуемый расход', g.required_flow, 'м³/ч']].map(([label, value, unit]) => `<div class="pe-metric"><span>${label}</span><strong>${money(value)}<small>${unit}</small></strong></div>`).join('');
  $('#pe-variants').innerHTML = Object.entries(snapshot.variants).map(([key, v]) => `<button type="button" class="pe-variant" role="tab" aria-selected="${key === selected}" data-variant="${key}"><strong>${titles[key]}</strong><span class="pe-brand">${esc(v.preferred_brand)} · предпочтение</span><span class="pe-total">${v.missing_prices && Number(v.total) === 0 ? 'Цена уточняется' : money(v.total) + ' ₽'}</span><span class="pe-price-note">${v.missing_prices ? `Без цены: ${v.missing_prices} · сумма неполная` : 'Сумма заполненных строк'}</span></button>`).join('');
  const variant = snapshot.variants[selected];
  $('#pe-editor-title').textContent = `${titles[selected]} · состав сметы`;
  $('#pe-version-label').textContent = projectId ? `Открыта версия ${loadedVersion} · последняя сохранённая ${revision}` : 'Несохранённый расчёт';
  $('#pe-warnings').innerHTML = `<details open><summary>Проверки и уточнения · ${variant.warnings.length}</summary><ul>${variant.warnings.map(text => `<li>${esc(text)}</li>`).join('')}${!variant.warnings.length ? '<li>Автоматические проверки пройдены. Полную проверку проекта выполняет инженер.</li>' : ''}</ul></details>`;
  for (const block of variant.blocks) {
    const subtotal = root.querySelector(`[data-subtotal="${block.id}"]`);
    if (subtotal) subtotal.textContent = `${money(block.subtotal)} ₽`;
    for (const row of block.rows) {
      const origin = root.querySelector(`[data-origin="${row.id}"]`);
      if (origin) origin.outerHTML = catalogOrigin(row);
      const amount = root.querySelector(`[data-amount="${row.id}"]`);
      if (amount) amount.textContent = row.existing ? 'Установлено' : row.amount === null ? '—' : money(row.amount);
    }
  }
  syncScope();
}
function renderEditor() {
  const variant = snapshot.variants[selected];
  $('#pe-blocks').innerHTML = variant.blocks.filter(b => !b.excluded).map(block => {
    const rows = block.rows.filter(r => !r.excluded);
    return `<section class="pe-block"><div class="pe-block-head"><h3>${esc(block.title)}</h3><span class="pe-subtotal" data-subtotal="${block.id}">${money(block.subtotal)} ₽</span><button type="button" class="pe-link" data-exclude-block="${block.id}">Исключить</button></div>${rows.length ? `<div class="pe-table-wrap"><table><thead><tr><th>Позиция</th><th>Кол-во</th><th>Цена, ₽</th><th>Сумма, ₽</th><th><span class="visually-hidden">Действия</span></th></tr></thead><tbody>${rows.map(row => `<tr><td><span class="pe-row-name">${esc(row.name)}</span><span class="pe-row-detail">${esc([row.brand, row.model].filter(Boolean).join(' · '))}${row.auto_quantity ? ' · по геометрии' : ''}${row.existing ? ' · уже установлено' : ''}</span>${catalogOrigin(row)}</td><td><input class="pe-qty" data-field="quantity" data-block="${block.id}" data-row="${row.id}" type="number" min="0.001" max="1000000" step="0.001" value="${esc(row.quantity)}" aria-label="Количество: ${esc(row.name)}"><span class="pe-row-detail">${esc(row.unit)}</span></td><td><input class="pe-price" data-field="price" data-block="${block.id}" data-row="${row.id}" type="number" min="0" max="1000000000" step="0.01" value="${esc(row.price)}" placeholder="Уточнить" aria-label="Цена: ${esc(row.name)}"></td><td class="pe-row-amount" data-amount="${row.id}">${money(row.amount)}</td><td class="pe-row-actions"><button type="button" class="pe-link" data-shop="${row.id}" data-block="${block.id}" aria-label="Выбрать товар: ${esc(row.name)}">Каталог</button><button type="button" class="pe-link" data-edit="${row.id}" data-block="${block.id}" aria-label="Изменить: ${esc(row.name)}">✎</button><button type="button" class="pe-link" data-exclude-row="${row.id}" data-block="${block.id}" aria-label="Исключить: ${esc(row.name)}">×</button></td></tr>`).join('')}</tbody></table></div>` : '<div class="pe-empty">В этом блоке пока нет активных позиций.</div>'}<button type="button" class="pe-link pe-add" data-add="${block.id}">+ Добавить позицию</button></section>`;
  }).join('');
  const excluded = variant.blocks.filter(b => b.excluded || b.rows.some(r => r.excluded));
  $('#pe-excluded-count').textContent = `(${excluded.reduce((sum, b) => sum + (b.excluded ? 1 : b.rows.filter(r => r.excluded).length), 0)})`;
  $('#pe-excluded-content').innerHTML = excluded.map(b => `${b.excluded ? `<div class="pe-excluded-item"><div><strong>${esc(b.title)}</strong><small>${esc(b.reason)} · ${b.rows.length} позиций</small></div><button type="button" class="pe-link" data-restore-block="${b.id}">Вернуть блок</button></div>` : ''}${b.rows.filter(r => b.excluded || r.excluded).map(r => `<div class="pe-excluded-item"><div>${esc(r.name)}<small>${esc(r.quantity)} ${esc(r.unit)} · цена ${money(r.price)} ₽ · ${esc(r.reason || b.reason)}${r.existing ? ' · уже установлено' : ''}</small></div><button type="button" class="pe-link" data-restore-row="${r.id}" data-block="${b.id}">Вернуть строку</button></div>`).join('')}`).join('') || '<p class="pe-help">Исключённых позиций нет. Их можно будет вернуть с прежними количеством и ценой.</p>';
  renderTotals();
}
function renderHistory() {
  $('#pe-history-list').innerHTML = versions.map(v => `<div class="pe-history-row"><div><strong>Версия ${v.number}</strong> · ${stamp(v.created_at)}<small>${esc(v.note || 'Без комментария')}</small></div><div><button type="button" class="pe-link" data-version="${v.number}">Открыть копию</button><a class="pe-link" href="${location.pathname}${encodeURIComponent(projectId)}/versions/${v.number}/print/" target="_blank" rel="noopener">Печать / PDF</a></div></div>`).join('') || 'После первого сохранения здесь появится версия 1.';
}
function fillParameters() {
  // Historical snapshots from before catalogue support have no heating_power.
  form.elements.heating_power.value = snapshot.inputs.heating_power ?? '';
  for (const [key, value] of Object.entries(snapshot.inputs)) {
    if (form.elements.namedItem(key)) form.elements.namedItem(key).value = value ?? '';
  }
  parametersDirty = false;
}
function discardAllowed() {
  return !dirty || window.confirm('Есть несохранённые изменения. Перейти без их сохранения?');
}
async function loadProject(id, version = '') {
  if (!discardAllowed()) return;
  await run(async () => {
    clearTimeout(timer); sequence++;
    const data = await read({id, version});
    snapshot = data.snapshot; showCatalogStatus({}); projectId = data.id; revision = data.revision; loadedVersion = data.loaded_version; versions = data.versions;
    dirty = false; fillParameters(); renderEditor(); renderHistory(); rememberLocation();
    $('#pe-projects-dialog').close();
    status(`Открыта версия ${loadedVersion}. Изменения сохранятся новой версией.`);
  });
}
async function generateNew() {
  // Collect form values before run disables the controls.
  if (!form.reportValidity()) return;
  const values = inputs();
  return await run(async () => {
    clearTimeout(timer); sequence++;
    const generated = await api({action: 'generate', inputs: values});
    snapshot = generated.snapshot; showCatalogStatus(generated);
    projectId = null; revision = 0; loadedVersion = null; versions = []; selected = 'economy';
    dirty = false; parametersDirty = false;
    renderEditor(); renderHistory(); rememberLocation(); status(generated.catalog_error || `Расчёт подготовлен. Подобрано товаров: ${generated.selected_count || 0}. Проверьте комплектацию и сохраните версию.`, Boolean(generated.catalog_error));
  });
}
form.addEventListener('input', event => {
  if (event.target === org) return;
  parametersDirty = true; changed();
  status('Параметры изменены. Нажмите «Пересчитать параметры», чтобы обновить сметы.');
});
form.addEventListener('submit', async event => {
  event.preventDefault();
  const values = inputs();
  await run(async () => {
    clearTimeout(timer); sequence++;
    const data = await api({action: snapshot ? 'recalculate' : 'generate', snapshot, inputs: values});
    snapshot = data.snapshot; showCatalogStatus(data);
    parametersDirty = false; changed(); renderEditor();
    status(data.catalog_error || `Параметры обновлены. Подобрано позиций: ${data.selected_count || 0}. Ручные замены, цены и исключения сохранены. Проверьте предупреждения.`, Boolean(data.catalog_error));
  });
});
root.addEventListener('input', event => {
  // Warn about loss even if the user closes the tab before the field blurs.
  if (event.target.dataset.field && snapshot) changed();
});
root.addEventListener('change', event => {
  const el = event.target;
  if (el.dataset.field && snapshot) {
    if (!el.reportValidity()) return;
    const row = snapshot.variants[selected].blocks.find(b => b.id === el.dataset.block).rows.find(r => r.id === el.dataset.row);
    row[el.dataset.field] = el.value === '' && el.dataset.field === 'price' ? null : el.value;
    if (el.dataset.field === 'quantity') { row.auto_quantity = false; row.selection_locked = true; if (row.catalog) row.catalog.selection = 'manual'; }
    changed(); queueValidation();
  }
  if (el.dataset.scope && snapshot) {
    setExcluded(snapshot, selected, el.dataset.scope, null, !el.checked, true);
    changed(); renderEditor(); queueValidation();
  }
});
root.addEventListener('click', event => {
  const el = event.target.closest('button');
  if (!el || busy) return;
  if (el.dataset.close) { $('#' + el.dataset.close).close(); return; }
  if (el.dataset.project) { loadProject(el.dataset.project); return; }
  if (!snapshot) return;
  if (el.dataset.variant) { selected = el.dataset.variant; renderEditor(); }
  if (el.dataset.version) loadProject(projectId, el.dataset.version);
  const blockId = el.dataset.excludeBlock || el.dataset.restoreBlock || el.dataset.block;
  if (el.dataset.excludeBlock || el.dataset.restoreBlock || el.dataset.excludeRow || el.dataset.restoreRow) {
    setExcluded(snapshot, selected, blockId, el.dataset.excludeRow || el.dataset.restoreRow || null, Boolean(el.dataset.excludeBlock || el.dataset.excludeRow), $('#pe-all').checked);
    changed(); renderEditor(); queueValidation();
  }
  if (el.dataset.shop) { openCatalog(el.dataset.block, el.dataset.shop); return; }
  if (el.dataset.edit || el.dataset.add) {
    const block = snapshot.variants[selected].blocks.find(b => b.id === (el.dataset.block || el.dataset.add));
    rowBeingEdited = el.dataset.edit ? structuredClone(block.rows.find(r => r.id === el.dataset.edit)) : emptyRow(crypto.randomUUID());
    const edit = $('#pe-row-form'); edit.reset();
    edit.elements.row_id.value = rowBeingEdited.id; edit.elements.block_id.value = block.id;
    for (const key of ['name', 'unit', 'brand', 'model', 'kind', 'quantity', 'price']) edit.elements.namedItem(key).value = rowBeingEdited[key] ?? '';
    for (const key of ['existing', 'auto_quantity']) edit.elements.namedItem(key).checked = rowBeingEdited[key];
    for (const key of ['flow', 'head', 'filter_flow', 'backwash_flow']) edit.elements.namedItem(key).value = rowBeingEdited.specs[key] ?? '';
    $('#pe-auto-label').hidden = !rowBeingEdited.basis;
    $('#pe-row-dialog').showModal();
  }
});
$('#pe-row-form').addEventListener('submit', event => {
  event.preventDefault();
  const f = event.currentTarget;
  const row = structuredClone(rowBeingEdited);
  row.selection_locked = true;
  for (const key of ['name', 'unit', 'brand', 'model', 'kind', 'quantity']) row[key] = f.elements.namedItem(key).value;
  row.price = f.elements.price.value || null;
  if (['name', 'brand', 'model', 'kind'].some(key => row[key] !== rowBeingEdited[key])) row.catalog = null;
  row.existing = f.elements.existing.checked;
  row.auto_quantity = Boolean(row.basis) && f.elements.auto_quantity.checked;
  if (row.auto_quantity) row.quantity = snapshot.geometry[row.basis];
  for (const key of ['flow', 'head', 'filter_flow', 'backwash_flow']) row.specs[key] = f.elements.namedItem(key).value || null;
  if (row.catalog && (row.quantity !== rowBeingEdited.quantity || ['flow','head','filter_flow','backwash_flow'].some(key => row.specs[key] !== rowBeingEdited.specs[key]))) row.catalog.selection = 'manual';
  const block = snapshot.variants[selected].blocks.find(b => b.id === f.elements.block_id.value);
  const index = block.rows.findIndex(r => r.id === row.id);
  if (index < 0 && block.rows.length >= 100) { status('В блоке допускается до 100 строк.', true); return; }
  if (index < 0) block.rows.push(row); else block.rows[index] = row;
  $('#pe-row-dialog').close(); changed(); renderEditor(); queueValidation();
});
function openSave(copy) {
  if (!snapshot || busy) return;
  if (parametersDirty) { status('Сначала пересчитайте изменённые параметры бассейна.', true); return; }
  if ([...$('#pe-blocks').querySelectorAll('input')].some(el => !el.reportValidity())) return;
  copyOnSave = copy;
  $('#pe-save-dialog').querySelector('h2').textContent = copy ? 'Сохранить отдельным расчётом' : 'Сохранить версию';
  $('#pe-save-dialog').showModal();
}
$('#pe-save').addEventListener('click', () => openSave(false));
$('#pe-copy').addEventListener('click', () => openSave(true));
$('#pe-save-form').addEventListener('submit', async event => {
  event.preventDefault();
  const note = event.currentTarget.elements.note.value;
  await run(async () => {
    clearTimeout(timer); sequence++;
    const data = await api({action: 'save', snapshot, id: copyOnSave ? null : projectId, expected_revision: copyOnSave ? 0 : revision, note});
    snapshot = data.snapshot; showCatalogStatus({}); projectId = data.id; revision = data.revision; loadedVersion = data.loaded_version; versions = data.versions;
    dirty = false; renderEditor(); renderHistory(); rememberLocation();
    $('#pe-save-dialog').close(); event.target.reset(); status(`Версия ${revision} сохранена. Предыдущие версии не изменены.`);
  });
});
async function searchProjects() {
  searchAbort?.abort(); searchAbort = new AbortController();
  const q = $('#pe-project-search').value;
  const url = new URL(location.href); if (q) url.searchParams.set('q', q); else url.searchParams.delete('q'); history.replaceState(null, '', url);
  try {
    const data = await read({q}, searchAbort.signal);
    $('#pe-project-list').innerHTML = data.projects.map(p => `<button type="button" class="pe-project" data-project="${esc(p.id)}"><strong>${esc(p.title)}</strong><small>Версия ${p.revision} · ${stamp(p.updated_at)}</small></button>`).join('') || '<p>Расчёты не найдены.</p>';
  } catch (error) { if (error.name !== 'AbortError') $('#pe-project-list').textContent = error.message; }
}
$('#pe-open').addEventListener('click', () => { $('#pe-projects-dialog').showModal(); searchProjects(); });
$('#pe-project-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(searchProjects, 300); });
$('#pe-new').addEventListener('click', async () => {
  if (!discardAllowed()) return;
  const previousInputs = inputs();
  form.reset(); org.value = currentOrg;
  $('#pe-projects-dialog').close();
  if (!await generateNew()) for (const [key, value] of Object.entries(previousInputs)) form.elements.namedItem(key).value = value;
});
org.addEventListener('change', async () => {
  if (!discardAllowed()) { org.value = currentOrg; return; }
  const previousOrg = currentOrg, previousInputs = inputs();
  currentOrg = org.value; form.reset(); org.value = currentOrg;
  if (!await generateNew()) {
    currentOrg = previousOrg;
    for (const [key, value] of Object.entries(previousInputs)) form.elements.namedItem(key).value = value;
    org.value = previousOrg;
  }
});
window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });

$('#pe-catalog-query').addEventListener('input', () => { clearTimeout(catalogTimer); catalogTimer = setTimeout(searchCatalog, 300); });
for (const id of ['#pe-catalog-kind', '#pe-catalog-brand']) $(id).addEventListener('change', searchCatalog);
$('#pe-catalog-dialog').addEventListener('close', () => { clearTimeout(catalogTimer); catalogAbort?.abort(); catalogSequence++; });
root.addEventListener('click', async event => {
  const button = event.target.closest('[data-catalog-product]');
  if (!button || busy || !catalogTarget) return;
  const target = {...catalogTarget};
  await run(async () => {
    clearTimeout(timer); sequence++;
    const data = await api({action: 'catalog_apply', snapshot, ...target, product_id: button.dataset.catalogProduct});
    snapshot = data.snapshot; changed(); renderEditor(); showCatalogStatus(data);
    $('#pe-catalog-dialog').close();
    status('Товар и цена перенесены в смету. Проверьте количество, единицу измерения и предупреждения.');
  });
});
$('#pe-catalog-auto').addEventListener('click', async () => {
  if (!catalogInputsReady()) return;
  await run(async () => {
    clearTimeout(timer); sequence++;
    const data = await api({action: 'catalog_auto', snapshot});
    snapshot = data.snapshot; changed(); renderEditor(); showCatalogStatus(data);
    status(`Подобрано позиций: ${data.selected_count}. Ручные замены, цены и исключения сохранены. Автоматические позиции обновлены под параметры бассейна. Если позиция не заполнена, проверьте наличие бренда и исходные параметры или выберите товар из каталога.`);
  });
});
$('#pe-catalog-prices').addEventListener('click', async () => {
  if (!catalogInputsReady()) return;
  await run(async () => {
    clearTimeout(timer); sequence++;
    const data = await api({action: 'catalog_prices', snapshot, variant: selected});
    snapshot = data.snapshot; changed(); renderEditor(); showCatalogStatus(data);
    status(`Обновлено цен: ${data.updated}. Сохранено ручных цен: ${data.skipped}. Предыдущие версии и исключённые позиции не изменены. Для фиксации сохраните новую версию.`);
  });
});

const initial = new URL(location.href).searchParams;
if ([...org.options].some(option => option.value === initial.get('organization'))) org.value = initial.get('organization');
currentOrg = org.value;
$('#pe-project-search').value = initial.get('q') || '';
if (initial.get('id')) await loadProject(initial.get('id'), initial.get('version') || '');
else await generateNew();
