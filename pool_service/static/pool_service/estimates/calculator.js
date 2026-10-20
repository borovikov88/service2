import {setExcluded, emptyRow, isWork, samePrice, workPriceTargets} from './state.js?v=3';

const root = document.querySelector('#pool-estimator');
const $ = selector => root.querySelector(selector);
const form = $('#pe-parameters');

const titles = {economy: 'Эконом', standard: 'Стандарт', premium: 'Премиум'};
const money = value => value === null || value === undefined ? '—' : new Intl.NumberFormat('ru-RU', {minimumFractionDigits: 0, maximumFractionDigits: 2}).format(Number(value));
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
function safeImage(value) {
  try { const u = new URL(value); return u.protocol === 'https:' && u.hostname === 'shop.aqualine22.ru' && !u.username && !u.password && (!u.port || u.port === '443') && u.pathname.startsWith('/image/') ? u.href : ''; }
  catch { return ''; }
}
function thumbnail(url, name, catalog = false) {
  const src = safeImage(url);
  return src ? `<button type="button" class="pe-thumbnail${catalog ? ' pe-thumbnail-catalog' : ''}" data-image="${esc(src)}" data-image-name="${esc(name)}" aria-label="Увеличить фото: ${esc(name)}"><img src="${esc(src)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer"></button>` : '';
}
const stamp = value => new Date(value).toLocaleString('ru-RU', {dateStyle: 'short', timeStyle: 'short'});
let snapshot, selected = 'economy', projectId = null, revision = 0, loadedVersion = null, versions = [];
let dirty = false, parametersDirty = false, sequence = 0, busy = false, timer, searchTimer, searchAbort;
let rowBeingEdited = null, copyOnSave = false;
const currentOrg = root.dataset.organization;
let scopeTarget, priceTarget;
let productImages = {};
const embeddedDefaults = {economy: "plastic_budget", standard: "plastic_europe", premium: "steel316"};
let catalogTarget, catalogTimer, catalogAbort, catalogSequence = 0;

function heatingFields() {
  $('#pe-heating-fields').hidden = !form.querySelector('[name="heating_types"]:checked');
  const heating = snapshot?.heating;
  $('#pe-heating-result').textContent = parametersDirty ? 'Пересчитайте параметры для подбора нагревателя.' : heating?.power
    ? `${heating.mode === 'manual' ? 'Заданная' : 'Предварительная'} мощность: ${money(heating.power)} кВт.${heating.mode === 'estimated' && snapshot.inputs.location === 'outdoor' ? ' Улица: учтён коэффициент ×1,5.' : ''}`
    : 'Заполните условия прогрева или задайте мощность вручную.';
}
function showCatalogStatus(data) {
  const s = data.catalog_status || data.status;
  $('#pe-catalog-status').textContent = data.catalog_error || (s
    ? `Товаров: ${s.count}. ${Object.entries(s.brands).filter(([,count]) => !count).map(([brand]) => `${brand}: нет товаров в каталоге.`).join(' ')}`
    : 'В расчёте зафиксированы модели и цены выбранной версии. Новые цены применяются отдельной кнопкой к текущей смете; ручные цены и исключённые строки сохраняются.');
  if (s?.heater_brands) {
    const brands = s.heater_brands;
    const catalogBrand = $('#pe-catalog-brand'), previous = catalogBrand.value;
    catalogBrand.innerHTML = '<option value="">Все бренды</option>' + [...new Set(['Aquaviva', 'Aquant', 'Hayward', 'Kripsol', 'AstralPool', 'IML', ...brands])].map(b => `<option>${esc(b)}</option>`).join('');
    catalogBrand.value = previous;
  }
}
function catalogInputsReady() {
  if (priceTarget) return false;
  if (parametersDirty) { status('Сначала пересчитайте изменённые параметры бассейна.', true); return false; }
  return Boolean(snapshot);
}
function openCatalog(block, row) {
  if (!catalogInputsReady()) return;
  const item = snapshot.variants[selected].blocks.find(b => b.id === block).rows.find(r => r.id === row);
  catalogTarget = {block, row, variant: selected};
  $('#pe-catalog-target').textContent = `${titles[selected]} · заменить «${item.name}». Количество и исключения сохранятся.`;
  const q = new URL(location.href).searchParams;
  const finishQuery = item.id.startsWith('finish_material') ? {film: 'пленка', tile: 'плитка', mosaic: 'мозаика'}[item.finish_type] : '';
  $('#pe-catalog-query').value = finishQuery || q.get('catalog_q') || '';
  $('#pe-catalog-kind').value = item.kind === 'other' ? '' : item.kind;
  $('#pe-catalog-brand').value = [...$('#pe-catalog-brand').options].some(o => o.value === item.brand) ? item.brand : '';
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
    const data = await api({action: 'catalog_search', variant: selected, q: query, kind: $('#pe-catalog-kind').value, brand: $('#pe-catalog-brand').value, inputs: snapshot.inputs}, catalogAbort.signal);
    if (token !== catalogSequence || !$('#pe-catalog-dialog').open) return;
    showCatalogStatus(data);
    $('#pe-catalog-results').innerHTML = `<p class="pe-help">Найдено: ${data.total}${data.total > 40 ? '. Показаны первые 40 — уточните поиск.' : ''}</p>` + data.products.map(p => `<article class="pe-catalog-item"><div class="pe-product-description">${thumbnail(p.image, p.name, true)}<strong>${esc(p.name)}</strong><span class="pe-row-detail">${esc(p.brand)}${p.sku ? ' · арт. ' + esc(p.sku) : ''} · ${p.available ? 'Доступен к заказу' : 'Доступность не подтверждена'}</span><span class="pe-row-detail">${p.kind === 'circulation_pump' ? (p.frequency_control === true ? 'Частотное регулирование указано в названии · ' : p.frequency_control === false ? 'Без частотного регулирования · ' : 'Регулирование не подтверждено · ') : ''}${p.nominal_flow ? money(p.nominal_flow) + ' м³/ч по каталогу · ' : ''}${p.head ? 'при ' + money(p.head) + ' м · ' : ''}${p.heating_power ? money(p.heating_power) + ' кВт · ' : ''}${p.power !== 'unknown' ? esc(p.power) + ' В' : ''}</span><p class="pe-help">${esc(p.warning || (p.kind === 'other' ? 'Назначение, единицу измерения и количество проверьте в карточке.' : 'Предварительный кандидат. Проверьте рабочий режим, совместимость и комплектацию.'))}</p><a class="pe-link" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">Карточка товара ↗</a></div><div class="pe-catalog-price"><strong>${money(p.price)} ₽</strong><button class="pe-btn" type="button" data-catalog-product="${esc(p.id)}">Выбрать</button></div></article>`).join('') + (!data.total ? '<p>Товары не найдены. Измените бренд или запрос; строка сметы останется без замены.</p>' : '');
  } catch (error) { if (error.name !== 'AbortError' && token === catalogSequence) $('#pe-catalog-results').textContent = error.message; }
}

function status(message, error = false) {
  $('#pe-status').textContent = message;
  $('#pe-status').classList.toggle('pe-error', error);
  $('#pe-new-error').textContent = error && $('#pe-new-dialog').open ? message : '';
  $('#pe-price-error').textContent = error && $('#pe-price-dialog').open ? message : '';
}
function changed() {
  dirty = true;
  sequence++;
  status('Есть несохранённые изменения');
}
function inputs() {
  const data = new FormData(form);
  const heating_types = data.getAll('heating_types');
  return {...Object.fromEntries(data), heating_types, heating: heating_types[0] || 'none'};
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
function rowLayout(variant) {
  return JSON.stringify(variant.blocks.map(b => [b.id, b.excluded, b.rows.map(r => [r.id, r.excluded])]));
}
function queueValidation() {
  clearTimeout(timer);
  const token = sequence;
  timer = setTimeout(async () => {
    try {
      const result = await api({action: 'validate', snapshot});
      if (token !== sequence || busy) return;
      const layoutChanged = rowLayout(snapshot.variants[selected]) !== rowLayout(result.snapshot.variants[selected]);
      snapshot = result.snapshot;
      if (layoutChanged) renderEditor(); else renderTotals();
    } catch (error) { if (token === sequence) status(error.message, true); }
  }, 300);
}
function syncScope() {
  root.querySelectorAll('[data-scope]').forEach(input => {
    input.checked = !snapshot.variants[selected].blocks.find(b => b.id === input.dataset.scope).excluded;
    input.indeterminate = false;
  });
}
function quantityMode(row) {
  return row.auto_quantity ? 'авто' : row.quantity === null ? 'не задано' : 'вручную';
}
function renderRowCheck(row, blockId) {
  const check = row.compatibility;
  return check ? `<span>${esc(check.message)}</span>${check.can_auto ? `<button type="button" class="pe-link" data-auto-row="${row.id}" data-block="${blockId}">Подобрать автоматически</button>` : ''}` : '';
}
function renderTotals() {
  const g = snapshot.geometry;
  const prices = snapshot.prices;
  $('#pe-price-date').textContent = `Дата цен: ${prices?.checked_at ? new Date(prices.checked_at).toLocaleDateString('ru-RU') : 'не указана'}${prices?.mixed ? ' · наиболее ранняя из использованных' : ''}${prices?.manual ? ' · ручные цены проверяются отдельно' : ''}`;
  heatingFields();
  $('#pe-metrics').innerHTML = [['Объём воды', g.volume, 'м³'], ['Зеркало воды', g.surface, 'м²'], ['Площадь отделки', g.finish_area, 'м²'], ['Требуемый расход', g.required_flow, 'м³/ч'], ...(g.concrete_total ? [['Бетон чаши (точно)', g.concrete_total, 'м³']] : [])].map(([label, value, unit]) => `<div class="pe-metric"><span>${label}</span><strong>${money(value)}<small>${unit}</small></strong></div>`).join('');
  $('#pe-variants').innerHTML = Object.entries(snapshot.variants).map(([key, v]) => `<button type="button" class="pe-variant" role="tab" aria-selected="${key === selected}" data-variant="${key}"><strong>${titles[key]}</strong><span class="pe-brand">${{economy:'Дешёвые подходящие модели', standard:'Средний ценовой уровень', premium:'Дорогие подходящие модели'}[key]}</span><span class="pe-total">${(v.missing_prices || v.missing_quantities) && Number(v.total) === 0 ? 'Цена уточняется' : money(v.total) + ' ₽'}</span><span class="pe-price-note">${v.missing_prices || v.missing_quantities ? `Без цены: ${v.missing_prices} · без объёма: ${v.missing_quantities || 0} · сумма неполная` : 'Сумма заполненных строк'}</span></button>`).join('');
  const variant = snapshot.variants[selected];
  $('#pe-embedded-profile').value = variant.embedded_profile || embeddedDefaults[selected];
  const q = variant.embedded_counts;
  $('#pe-embedded-summary').textContent = q ? `Скиммеры: ${q.skimmer}, возвратные форсунки: ${q.inlet}, донные сливы: ${q.drain}, пылесос: ${q.vacuum}. Количества предварительные; их можно изменить в строках. При смене материала прежние позиции переносятся в исключённые.` : 'Выберите материал и пересчитайте закладные. Сохранённая версия не меняется.';
  $('#pe-editor-title').textContent = `${titles[selected]} · состав сметы`;
  $('#pe-version-label').textContent = projectId ? `Открыта версия ${loadedVersion} · последняя сохранённая ${revision}` : 'Несохранённый расчёт';
  $('#pe-warnings').innerHTML = `<details open><summary>Проверки и уточнения · ${variant.warnings.length}</summary><ul>${variant.warnings.map(text => `<li>${esc(text)}</li>`).join('')}${!variant.warnings.length ? '<li>Автоматические проверки пройдены. Полную проверку проекта выполняет инженер.</li>' : ''}</ul></details>`;
  for (const block of variant.blocks) {
    const subtotal = root.querySelector(`[data-subtotal="${block.id}"]`);
    if (subtotal) subtotal.textContent = `${money(block.subtotal)} ₽`;
    for (const row of block.rows) {
      const quantity = root.querySelector(`[data-row="${row.id}"][data-field="quantity"]`);
      if (row.auto_quantity && quantity && document.activeElement !== quantity) quantity.value = row.quantity;
      const mode = root.querySelector(`[data-quantity-mode="${row.id}"]`);
      if (mode) mode.textContent = quantityMode(row);
      const check = root.querySelector(`[data-row-check="${row.id}"]`);
      if (check) { check.innerHTML = renderRowCheck(row, block.id); check.hidden = !row.compatibility; }
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
    return `<section class="pe-block"><div class="pe-block-head"><h3>${esc(block.title)}</h3><span class="pe-subtotal" data-subtotal="${block.id}">${money(block.subtotal)} ₽</span><button type="button" class="pe-link" data-exclude-block="${block.id}">Исключить</button></div>${rows.length ? `<div class="pe-table-wrap"><table><thead><tr><th>Позиция</th><th>Кол-во</th><th>Цена, ₽</th><th>Сумма, ₽</th><th><span class="visually-hidden">Действия</span></th></tr></thead><tbody>${rows.map(row => `<tr><td>${thumbnail(row.catalog?.image || productImages[row.catalog?.id], row.name)}<span class="pe-row-name">${esc(row.name)}</span><span class="pe-row-detail">${esc([({electric:'Электронагреватель', exchanger:'Теплообменник', heatpump:'Тепловой насос'})[row.heating_type], row.brand, row.model].filter(Boolean).join(' · '))}${row.existing ? ' · уже установлено' : ''}</span><div class="pe-row-check" data-row-check="${row.id}" aria-live="polite"${row.compatibility ? '' : ' hidden'}>${renderRowCheck(row, block.id)}</div></td><td><input class="pe-qty" data-field="quantity" data-block="${block.id}" data-row="${row.id}" type="number" min="0" max="1000000" step="0.001" value="${esc(row.quantity)}" placeholder="Уточнить" aria-label="Количество: ${esc(row.name)}"><span class="pe-row-detail">${esc(row.unit)} · <span data-quantity-mode="${row.id}">${quantityMode(row)}</span></span></td><td><input class="pe-price" data-field="price" data-block="${block.id}" data-row="${row.id}" type="number" min="0" max="1000000000" step="0.01" value="${esc(row.price)}" placeholder="Уточнить" aria-label="Цена: ${esc(row.name)}"></td><td class="pe-row-amount" data-amount="${row.id}">${money(row.amount)}</td><td class="pe-row-actions">${!isWork(row) ? `<button type="button" class="pe-link" data-shop="${row.id}" data-block="${block.id}" aria-label="Выбрать товар: ${esc(row.name)}">Каталог</button>` : ''}<button type="button" class="pe-link" data-edit="${row.id}" data-block="${block.id}" aria-label="Изменить: ${esc(row.name)}">✎</button><button type="button" class="pe-link" data-exclude-row="${row.id}" data-block="${block.id}" aria-label="Исключить: ${esc(row.name)}">×</button></td></tr>`).join('')}</tbody></table></div>` : '<div class="pe-empty">В этом блоке пока нет активных позиций.</div>'}<button type="button" class="pe-link pe-add" data-add="${block.id}">+ Добавить позицию</button></section>`;
  }).join('');
  const excluded = variant.blocks.filter(b => b.excluded || b.rows.some(r => r.excluded));
  $('#pe-excluded-count').textContent = `(${excluded.reduce((sum, b) => sum + (b.excluded ? 1 : b.rows.filter(r => r.excluded).length), 0)})`;
  $('#pe-excluded-content').innerHTML = excluded.map(b => `${b.excluded ? `<div class="pe-excluded-item"><div><strong>${esc(b.title)}</strong><small>${esc(b.reason)} · ${b.rows.length} позиций</small></div><button type="button" class="pe-link" data-restore-block="${b.id}">Вернуть блок</button></div>` : ''}${b.rows.filter(r => b.excluded || r.excluded).map(r => `<div class="pe-excluded-item"><div>${esc(r.name)}<small>${esc(r.quantity)} ${esc(r.unit)} · цена ${money(r.price)} ₽ · ${esc(r.reason || b.reason)}${r.existing ? ' · уже установлено' : ''}</small></div><button type="button" class="pe-link" data-restore-row="${r.id}" data-block="${b.id}">Вернуть строку</button></div>`).join('')}`).join('') || '<p class="pe-help">Исключённых позиций нет. Их можно будет вернуть с прежними количеством и ценой.</p>';
  renderTotals();
}
function renderHistory() {
  $('#pe-history-list').innerHTML = versions.map(v => `<div class="pe-history-row"><div><strong>Версия ${v.number}</strong> · ${stamp(v.created_at)}<small>${esc(v.note || 'Без комментария')}</small></div><div><button type="button" class="pe-link" data-version="${v.number}">Открыть копию</button><a class="pe-link" href="${location.pathname}${encodeURIComponent(projectId)}/versions/${v.number}/print/" target="_blank" rel="noopener">Печать / PDF</a></div></div>`).join('') || 'После первого сохранения здесь появится версия 1.';
}
function setHeating(values) {
  const selectedTypes = values.heating_types ?? (values.heating === 'none' ? [] : [values.heating]);
  for (const checkbox of form.querySelectorAll('[name="heating_types"]')) checkbox.checked = selectedTypes.includes(checkbox.value);
}
function fillParameters() {
  // Historical snapshots must not inherit the inputs of a more recent estimate.
  for (const [key, value] of Object.entries({bowl_height: '', wall_thickness: '', slab_thickness: '', heating_power: '', water_start: '', water_target: '', heating_hours: '', heating_reserve: '', finish: '', skimmer_area: '25', skimmer_flow: '5', inlet_flow: '3'})) {
    form.elements.namedItem(key).value = snapshot.inputs[key] ?? value;
  }
  for (const [key, value] of Object.entries(snapshot.inputs)) {
    if (key !== 'heating_types' && form.elements.namedItem(key)) form.elements.namedItem(key).value = value ?? '';
  }
  setHeating(snapshot.inputs);
  parametersDirty = false;
  heatingFields();
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
    loadMissingImages(snapshot);
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
  parametersDirty = true; changed();
  heatingFields();
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
// A blur may open the price dialog during the same click on another action.
root.addEventListener('click', event => {
  if (priceTarget && !event.target.closest('#pe-price-dialog')) {
    event.preventDefault(); event.stopImmediatePropagation();
  }
}, true);
root.addEventListener('input', event => {
  // Warn about loss even if the user closes the tab before the field blurs.
  if (event.target.dataset.field && snapshot) changed();
});
root.addEventListener('change', event => {
  const el = event.target;
  if (el.dataset.field && snapshot) {
    if (!el.reportValidity()) return;
    const row = snapshot.variants[selected].blocks.find(b => b.id === el.dataset.block).rows.find(r => r.id === el.dataset.row);
    const value = el.value === '' && (el.dataset.field === 'price' || isWork(row)) ? null : el.value;
    if (el.dataset.field === 'price' && isWork(row) && !samePrice(row.price, value)) {
      askWorkPrice(el.dataset.block, row, value); return;
    }
    row[el.dataset.field] = value;
    if (el.dataset.field === 'quantity') { row.auto_quantity = false; row.quantity_stale = false; row.selection_locked = true; if (row.catalog) row.catalog.selection = 'manual'; }
    const mode = root.querySelector(`[data-quantity-mode="${row.id}"]`);
    if (mode) mode.textContent = quantityMode(row);
    changed(); queueValidation();
  }
  if (el.dataset.scope && snapshot) {
    askScope(el.dataset.scope, null, !el.checked);
    syncScope();
  }
});
root.addEventListener('click', event => {
  const el = event.target.closest('button');
  if (!el || busy || (priceTarget && !el.closest('#pe-price-dialog'))) return;
  if (el.dataset.close) { $('#' + el.dataset.close).close(); return; }
  if (el.dataset.image) { openImage(el.dataset.image, el.dataset.imageName); return; }
  if (el.dataset.project) { loadProject(el.dataset.project); return; }
  if (!snapshot) return;
  if (el.dataset.variant) { selected = el.dataset.variant; renderEditor(); }
  if (el.dataset.version) loadProject(projectId, el.dataset.version);
  const blockId = el.dataset.excludeBlock || el.dataset.restoreBlock || el.dataset.block;
  if (el.dataset.excludeBlock || el.dataset.restoreBlock || el.dataset.excludeRow || el.dataset.restoreRow) {
    askScope(blockId, el.dataset.excludeRow || el.dataset.restoreRow || null, Boolean(el.dataset.excludeBlock || el.dataset.excludeRow));
  }
  if (el.dataset.autoRow) { repairRow(el.dataset.block, el.dataset.autoRow); return; }
  if (el.dataset.shop) { openCatalog(el.dataset.block, el.dataset.shop); return; }
  if (el.dataset.edit || el.dataset.add) {
    const block = snapshot.variants[selected].blocks.find(b => b.id === (el.dataset.block || el.dataset.add));
    rowBeingEdited = el.dataset.edit ? structuredClone(block.rows.find(r => r.id === el.dataset.edit)) : emptyRow(crypto.randomUUID());
    const edit = $('#pe-row-form'); edit.reset();
    edit.elements.row_id.value = rowBeingEdited.id; edit.elements.block_id.value = block.id;
    for (const key of ['name', 'unit', 'brand', 'model', 'kind', 'quantity', 'price']) edit.elements.namedItem(key).value = rowBeingEdited[key] ?? '';
    for (const key of ['existing', 'auto_quantity']) edit.elements.namedItem(key).checked = rowBeingEdited[key];
    for (const key of ['flow', 'head', 'filter_flow', 'backwash_flow']) edit.elements.namedItem(key).value = rowBeingEdited.specs[key] ?? '';
    edit.elements.quantity.required = !isWork(rowBeingEdited);
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
  if (row.kind === 'work' && !row.quantity) row.quantity = null;
  if (row.kind === 'work' && !row.work_code) row.work_code = row.id.startsWith('finish_work') ? 'finish_' + (row.finish_type || 'unknown') : row.id;
  if (['name', 'brand', 'model', 'kind'].some(key => row[key] !== rowBeingEdited[key])) row.catalog = null;
  if (row.quantity !== rowBeingEdited.quantity) row.quantity_stale = false;
  row.existing = f.elements.existing.checked;
  row.auto_quantity = Boolean(row.basis) && f.elements.auto_quantity.checked;
  if (row.auto_quantity) row.quantity = String(Math.ceil(Number((row.basis === 'exchanger_quantity' ? snapshot.variants[selected].blocks.flatMap(b => b.rows).find(r => r.id === row.heating_parent)?.quantity : snapshot.variants[selected].embedded_counts?.[row.basis] ?? snapshot.geometry[row.basis]))));
  for (const key of ['flow', 'head', 'filter_flow', 'backwash_flow']) row.specs[key] = f.elements.namedItem(key).value || null;
  if (row.catalog && (row.quantity !== rowBeingEdited.quantity || ['flow','head','filter_flow','backwash_flow'].some(key => row.specs[key] !== rowBeingEdited.specs[key]))) row.catalog.selection = 'manual';
  const block = snapshot.variants[selected].blocks.find(b => b.id === f.elements.block_id.value);
  const index = block.rows.findIndex(r => r.id === row.id);
  if (index < 0 && block.rows.length >= 100) { status('В блоке допускается до 100 строк.', true); return; }
  const commit = () => { if (index < 0) block.rows.push(row); else block.rows[index] = row; $('#pe-row-dialog').close(); };
  if (isWork(row) && !samePrice(rowBeingEdited.price, row.price)) {
    askWorkPrice(block.id, row, row.price, row); return;
  }
  commit(); changed(); renderEditor(); queueValidation();
});
function askWorkPrice(blockId, row, price, draftRow = null) {
  clearTimeout(timer); sequence++;
  priceTarget = {variant: selected, blockId, source: structuredClone(row), price, draftRow};
  const matches = workPriceTargets(snapshot, selected, blockId, row);
  const other = matches.filter(t => t.variant !== selected).map(t => titles[t.variant]);
  $('#pe-price-target').textContent = `${row.name}: ${money(price)} ₽ / ${row.unit}. Где изменить расценку?`;
  $('#pe-price-matches').textContent = other.length
    ? `В других КП: ${other.join(', ')}. Меняется только цена такой же работы с той же единицей измерения. Объёмы и исключения сохраняются.`
    : 'В других КП нет однозначно соответствующей работы с такой же единицей измерения. Будет изменено только текущее КП.';
  $('#pe-price-error').textContent = '';
  $('#pe-price-dialog').showModal();
}
$('#pe-row-form').elements.kind.addEventListener('change', event => {
  $('#pe-row-form').elements.quantity.required = event.target.value !== 'work';
});
root.addEventListener('click', async event => {
  const button = event.target.closest('[data-price-choice]');
  if (!button || busy || !priceTarget) return;
  const target = priceTarget;
  await run(async () => {
    clearTimeout(timer); sequence++;
    const data = await api({action: 'work_price', snapshot, variant: target.variant, block: target.blockId,
      row: target.source.id, price: target.price, all: button.dataset.priceChoice === 'all', draft_row: target.draftRow});
    snapshot = data.snapshot;
    if (target.draftRow) $('#pe-row-dialog').close();
    priceTarget = null; $('#pe-price-dialog').close();
    changed(); renderEditor();
    status(data.remembered ? 'Расценка применена и сохранена для следующих расчётов.' : 'Цена изменена в выбранных КП. Сохранённые расценки для новых расчётов не снижены.');
  });
});
$('#pe-price-dialog').addEventListener('cancel', event => { if (busy) event.preventDefault(); });
$('#pe-price-dialog').addEventListener('close', () => {
  if ($('#pe-price-dialog').open) return; // Ignore a late close event after reopening.
  if (priceTarget) { priceTarget = null; renderEditor(); queueValidation(); }
});
function openSave(copy) {
  if (!snapshot || busy || priceTarget) return;
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
function askScope(blockId, rowId, excluded) {
  const block = snapshot.variants[selected].blocks.find(b => b.id === blockId);
  scopeTarget = {variant: selected, blockId, rowId, excluded};
  $('#pe-scope-title').textContent = excluded ? 'Исключить из сметы?' : 'Восстановить в смете?';
  $('#pe-scope-target').textContent = `${rowId ? block.rows.find(r => r.id === rowId).name : block.title} · ${titles[selected]}`;
  $('#pe-scope-dialog').showModal();
}
root.addEventListener('click', event => {
  const button = event.target.closest('[data-scope-choice]');
  if (!button || busy || !scopeTarget) return;
  const t = scopeTarget;
  setExcluded(snapshot, t.variant, t.blockId, t.rowId, t.excluded, button.dataset.scopeChoice === 'all');
  $('#pe-scope-dialog').close(); scopeTarget = null;
  changed(); renderEditor(); queueValidation();
});
$('#pe-embedded-profile').addEventListener('change', async event => {
  const profile = event.target.value;
  if (!catalogInputsReady()) { renderTotals(); return; }
  await run(async () => {
    clearTimeout(timer); sequence++;
    const data = await api({action: 'embedded_profile', snapshot, variant: selected, profile});
    snapshot = data.snapshot; changed(); renderEditor(); showCatalogStatus(data);
    status(data.catalog_error || 'Комплект закладных обновлён только в выбранном КП. Прежние позиции сохранены в исключённых.', Boolean(data.catalog_error));
  });
  renderTotals();
});
function askNew() {
  if (busy || priceTarget) return;
  $('#pe-projects-dialog').close();
  $('#pe-new-error').textContent = '';
  $('#pe-new-dialog').showModal();
}
for (const id of ['#pe-new', '#pe-new-from-list']) $(id).addEventListener('click', askNew);
async function startNew() {
  const previousInputs = inputs();
  form.reset();
  if (!await generateNew()) {
    for (const [key, value] of Object.entries(previousInputs)) if (key !== 'heating_types' && form.elements.namedItem(key)) form.elements.namedItem(key).value = value;
    setHeating(previousInputs);
    heatingFields();
  }
}
root.addEventListener('click', async event => {
  const button = event.target.closest('[data-new-action]');
  if (!button || busy) return;
  const choice = button.dataset.newAction;
  if (choice === 'save') {
    if (!form.reportValidity() || [...$('#pe-blocks').querySelectorAll('input')].some(el => !el.reportValidity())) return;
    const values = inputs();
    const saved = await run(async () => {
      clearTimeout(timer); sequence++;
      let current = snapshot;
      if (parametersDirty || !current) current = (await api({action: current ? 'recalculate' : 'generate', snapshot: current, inputs: values})).snapshot;
      const data = await api({action: 'save', snapshot: current, id: projectId, expected_revision: revision, note: 'Сохранено перед новым расчётом'});
      snapshot = data.snapshot; projectId = data.id; revision = data.revision; loadedVersion = data.loaded_version; versions = data.versions;
      dirty = false; parametersDirty = false; renderEditor(); renderHistory(); rememberLocation();
    });
    if (!saved) return;
  }
  $('#pe-new-dialog').close();
  await startNew();
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
async function repairRow(block, row) {
  if (!catalogInputsReady()) return;
  const variant = selected;
  await run(async () => {
    clearTimeout(timer); sequence++;
    const data = await api({action: 'compatibility_auto', snapshot, variant, block, row});
    snapshot = data.snapshot; changed(); renderEditor();
    if (data.catalog_status) showCatalogStatus(data);
    status('Позиция подобрана только в выбранном КП. Проверьте результат и сохраните новую версию.');
  });
}
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

$('#pe-add-works').addEventListener('click', async () => {
  if (!snapshot || priceTarget) return;
  await run(async () => {
    clearTimeout(timer); sequence++;
    const data = await api({action: 'add_works', snapshot});
    snapshot = data.snapshot; changed(); renderEditor();
    status('Недостающие работы и известные цены из Excel добавлены во все три КП. Уже заполненные цены сохранены. Пустыми остаются расценки, которых нет в файле, и суммы, зависящие от состава комплекта. Для фиксации сохраните новую версию.');
  });
});

async function loadMissingImages(loaded) {
  const ids = [...new Set(Object.values(loaded.variants).flatMap(v => v.blocks.flatMap(b => b.rows)).filter(r => r.catalog && !r.catalog.image && !productImages[r.catalog.id]).map(r => r.catalog.id))];
  if (!ids.length) return;
  const organization = currentOrg;
  try {
    const data = await api({action: 'catalog_images', ids});
    if (!snapshot || currentOrg !== organization) return;
    Object.assign(productImages, data.images);
    // Attach thumbnails only: no re-render that could erase a typed price.
    for (const row of snapshot.variants[selected].blocks.flatMap(b => b.rows)) {
      const input = root.querySelector(`[data-field="price"][data-row="${row.id}"]`);
      const cell = input?.closest('tr')?.querySelector('td');
      if (cell && !cell.querySelector('.pe-thumbnail')) cell.insertAdjacentHTML('afterbegin', thumbnail(productImages[row.catalog?.id], row.name));
    }
  } catch { /* Historical totals and editing remain available without the shop. */ }
}
function openImage(url, name) {
  const src = safeImage(url);
  if (!src) return;
  $('#pe-image-title').textContent = name || 'Фото товара';
  $('#pe-image-error').hidden = true;
  $('#pe-image-large').hidden = false;
  $('#pe-image-large').alt = name || 'Фото товара';
  $('#pe-image-large').src = src;
  $('#pe-image-dialog').showModal();
}
root.addEventListener('error', event => {
  if (event.target.matches('.pe-thumbnail img')) event.target.closest('button').hidden = true;
  if (event.target.id === 'pe-image-large') { event.target.hidden = true; $('#pe-image-error').hidden = false; }
}, true);
$('#pe-image-dialog').addEventListener('close', () => { $('#pe-image-large').removeAttribute('src'); });
for (const id of ['#pe-image-dialog', '#pe-catalog-dialog']) $(id).addEventListener('click', event => {
  if (busy || event.target !== event.currentTarget) return;
  const r = event.currentTarget.getBoundingClientRect();
  if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) event.currentTarget.close();
});

const initial = new URL(location.href).searchParams;
$('#pe-project-search').value = initial.get('q') || '';
if (initial.get('id')) await loadProject(initial.get('id'), initial.get('version') || '');
else await generateNew();
