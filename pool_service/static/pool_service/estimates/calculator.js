import {productDetails,productTitle} from './product-details.js?v=1';
import {sections as viewSections, groupFor, moveViewRow} from './layout.js?v=5';
import {installHelp} from './help.js?v=5';
import {displayNumber, money, indivisible, exactNumber, cleanNote, bindNumber, rawValue, installNumbers} from './numbers.js?v=3';
import {constructionScope, setConstructionExcluded, setExcluded, addRow, emptyRow, isWork, samePrice, workPriceTargets, automaticQuantity, quantityRequired, moveRow} from './state.js?v=14';

const root = document.querySelector('#pool-estimator');
const $ = selector => root.querySelector(selector);
const form = $('#pe-parameters');

const titles = {economy: 'Эконом', standard: 'Стандарт', premium: 'Премиум'};
installNumbers(root);
const help = installHelp(root);
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
let scopeTarget, priceTarget, addRowTarget;
let productImages = {};
const embeddedDefaults = {economy: "plastic_budget", standard: "plastic_europe", premium: "steel316"};
let catalogTarget, catalogTimer, catalogAbort, catalogSequence = 0;

function heatingFields() {
  $('#pe-heating-fields').hidden = !form.querySelector('[name="heating_types"]:checked');
  const heating = snapshot?.heating;
  $('#pe-heating-result').textContent = parametersDirty ? 'Пересчитайте параметры для подбора нагревателя.' : heating?.power
    ? `${heating.mode === 'manual' ? 'Заданная' : 'Предварительная'} мощность: ${displayNumber(heating.power)} кВт.${heating.mode === 'estimated' && snapshot.inputs.location === 'outdoor' ? ' Улица: учтён коэффициент ×1,5.' : ''}`
    : 'Заполните условия прогрева или задайте мощность вручную.';
}
function showCatalogStatus(data) {
  const s = data.catalog_status || data.status;
  $('#pe-catalog-status').toggleAttribute('data-help-warning', Boolean(data.catalog_error));
  $('#pe-catalog-status').textContent = data.catalog_error || (s
    ? `Товаров: ${s.count}. ${Object.entries(s.brands).filter(([,count]) => !count).map(([brand]) => `${brand}: нет товаров в каталоге.`).join(' ')}`
    : 'В расчёте зафиксированы модели и цены выбранной версии. Новые цены применяются отдельной кнопкой к текущей смете; ручные цены и исключённые строки сохраняются.');
  if (s?.heater_brands) {
    const brands = s.heater_brands;
    const catalogBrand = $('#pe-catalog-brand'), previous = catalogBrand.value;
    catalogBrand.innerHTML = '<option value="">Все бренды</option>' + [...new Set(['Aquaviva', 'Aquant', 'Hayward', 'Kripsol', 'AstralPool', 'IML', ...brands])].map(b => `<option>${esc(b)}</option>`).join('');
    catalogBrand.value = previous;
  }
  help.refresh();
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
  $('#pe-catalog-query').value = (item.id==='liner_corner_2m'?'0050032':'') || finishQuery || (item.kind==='filter_media'?'':q.get('catalog_q')) || '';
  $('#pe-catalog-kind').value = item.kind === 'other' ? '' : item.kind;
  $('#pe-catalog-brand').value = item.kind!=='filter_media' && [...$('#pe-catalog-brand').options].some(o => o.value === item.brand) ? item.brand : '';
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
  help.refresh();
  try {
    const data = await api({action: 'catalog_search', variant: selected, embedded_profile: snapshot.variants[selected].embedded_profile, q: query, kind: $('#pe-catalog-kind').value, brand: $('#pe-catalog-brand').value, inputs: snapshot.inputs}, catalogAbort.signal);
    if (token !== catalogSequence || !$('#pe-catalog-dialog').open) return;
    showCatalogStatus(data);
    $('#pe-catalog-results').innerHTML = `<p class="pe-catalog-count">Найдено: ${data.total}${data.total > 40 ? '. Показаны первые 40 — уточните поиск.' : ''}</p>` + data.products.map(p => `<article class="pe-catalog-item"><div class="pe-product-description">${thumbnail(p.image, p.name, true)}<strong>${esc(p.name)}</strong><span class="pe-row-detail">${esc(p.brand)}${p.sku ? ' · арт. ' + esc(p.sku) : ''} · ${p.available ? 'Доступен к заказу' : 'Доступность не подтверждена'}</span><span class="pe-row-detail">${p.kind === 'circulation_pump' ? (p.frequency_control === true ? 'Частотное регулирование указано в названии · ' : p.frequency_control === false ? 'Без частотного регулирования · ' : 'Регулирование не подтверждено · ') : ''}${p.nominal_flow ? money(p.nominal_flow) + ' м³/ч по каталогу · ' : ''}${p.head ? 'при ' + money(p.head) + ' м · ' : ''}${p.heating_power ? money(p.heating_power) + ' кВт · ' : ''}${p.power !== 'unknown' ? esc(p.power) + ' В' : ''}</span><p class="pe-help" ${p.warning ? 'data-help-warning' : ''}>${esc(p.warning || (p.kind === 'other' ? 'Назначение, единицу измерения и количество проверьте в карточке.' : 'Предварительный кандидат. Проверьте рабочий режим, совместимость и комплектацию.'))}</p><a class="pe-link" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">Карточка товара ↗</a></div><div class="pe-catalog-price"><strong>${money(p.price)} ₽${p.roll_area ? " / рулон" : ""}</strong>${p.roll_area ? `<small>${esc(p.roll_area)} м² в рулоне; в смете пересчёт на м²</small>` : ""}<button class="pe-btn" type="button" data-catalog-product="${esc(p.id)}">Выбрать</button></div></article>`).join('') + (!data.total ? '<p>Товары не найдены. Измените бренд или запрос; строка сметы останется без замены.</p>' : '');
    help.refresh();
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
  if (form.elements.height_mode.value === 'freeboard') {
    const depth = form.elements.depth_start.value, board = form.elements.freeboard.value;
    form.elements.bowl_height.value = depth !== '' && board !== '' ? String(Number(depth)+Number(board)) : '';
  } else if (form.elements.bowl_height.value && form.elements.depth_start?.value) {
    form.elements.freeboard.value = String(Math.round((Number(form.elements.bowl_height.value)-Number(form.elements.depth_start.value))*1000)/1000);
  }
  if (form.elements.tech_insulation_wall_height_mode.value === 'auto') {
    const h = Number(form.elements.bowl_height.value);
    form.elements.tech_insulation_wall_height.value = h > .1 ? String(Math.round((h-.1)*1000)/1000) : '';
  }
  const data = new FormData(form);
  if (form.elements.height_mode.value !== 'freeboard') data.delete('freeboard');
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
  return JSON.stringify(variant.blocks.map(b => [b.id, b.excluded, b.rows.map(r => [r.id, r.excluded, r.kind])]));
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
    const blocks=input.dataset.scope==='construction'?constructionScope(snapshot.variants[selected]):snapshot.variants[selected].blocks.filter(b=>b.id===input.dataset.scope);
    input.checked=blocks.every(b=>!b.excluded);
    input.indeterminate=blocks.some(b=>b.excluded)&&blocks.some(b=>!b.excluded);
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
  $('#pe-metrics').innerHTML = [['Объём воды', g.volume, 'м³'], ['Зеркало воды', g.surface, 'м²'], ['Площадь отделки', g.finish_area, 'м²'], ['Требуемый расход', g.required_flow, 'м³/ч'], ...(g.concrete_total ? [['Бетон чаши', g.concrete_total, 'м³']] : [])].map(([label, value, unit]) => `<div class="pe-metric"><span>${label}</span><strong>${displayNumber(value)}<small>${unit}</small></strong></div>`).join('');
  $('#pe-variants').innerHTML = Object.keys(titles).map(key => [key,snapshot.variants[key]]).map(([key, v]) => `<button type="button" class="pe-variant" role="tab" aria-selected="${key === selected}" data-variant="${key}"><strong>${titles[key]}</strong><span class="pe-brand">${{economy:'Дешёвые подходящие модели', standard:'Средний ценовой уровень', premium:'Дорогие подходящие модели'}[key]}</span><span class="pe-total">${(v.missing_prices || v.missing_quantities) && Number(v.total) === 0 ? 'Цена уточняется' : money(v.total) + ' ₽'}</span><span class="pe-price-note">${v.missing_prices || v.missing_quantities ? `Без цены: ${v.missing_prices} · без объёма: ${v.missing_quantities || 0} · сумма неполная` : 'Сумма заполненных строк'}</span></button>`).join('');
  const variant = snapshot.variants[selected];
  $('#pe-embedded-profile').value = variant.embedded_profile || embeddedDefaults[selected];
  const q = variant.embedded_counts;
  $('#pe-embedded-summary').textContent = q ? `Скиммеры: ${q.skimmer}, возвратные форсунки: ${q.inlet}, донные сливы: ${q.drain}, пылесос: ${q.vacuum}. Количества предварительные; их можно изменить в строках. При смене материала прежние позиции переносятся в исключённые.` : 'Выберите материал и пересчитайте закладные. Сохранённая версия не меняется.';
  $('#pe-editor-title').textContent = `${titles[selected]} · состав сметы`;
  $('#pe-version-label').textContent = projectId ? `Открыта версия ${loadedVersion} · последняя сохранённая ${revision}` : 'Несохранённый расчёт';
  $('#pe-warnings').innerHTML = `<div data-help-source data-help-label="! Проверки · ${variant.warnings.length}"><strong>Проверки и уточнения · ${variant.warnings.length}</strong><ul>${[...(variant.warnings || []), ...(variant.construction_notes || [])].map(text => `<li>${esc(cleanNote(text))}</li>`).join('')}${!variant.warnings.length ? '<li>Автоматические проверки пройдены. Полную проверку проекта выполняет инженер.</li>' : ''}</ul></div>`;
  for (const block of variant.blocks) {
    for (const row of block.rows) {
      const quantity = root.querySelector(`[data-row="${row.id}"][data-field="quantity"]`);
      if (quantity && document.activeElement !== quantity) bindNumber(quantity, row.quantity, {whole:indivisible(row.unit)});
      const price = root.querySelector(`[data-row="${row.id}"][data-field="price"]`);
      if (price && document.activeElement !== price) bindNumber(price, row.price, {money:true});
      const mode = root.querySelector(`[data-quantity-mode="${row.id}"]`);
      if (mode) mode.textContent = quantityMode(row);
      const check = root.querySelector(`[data-row-check="${row.id}"]`);
      if (check) { check.innerHTML = renderRowCheck(row, block.id); check.hidden = !row.compatibility; }
      const quantityNote = root.querySelector(`[data-construction-quantity="${row.id}"]`);
      if (quantityNote) quantityNote.textContent = cleanNote([row.construction_quantity_note, row.level_quantity_note].filter(Boolean).join(' '));
      const selectionNote=root.querySelector(`[data-selection-note="${row.id}"]`);
      if(selectionNote)selectionNote.textContent=cleanNote(row.selection_note||'');
      const constructionPrice = root.querySelector(`[data-construction-price="${row.id}"]`);
      if (constructionPrice) constructionPrice.textContent = cleanNote(row.construction_price_note);
      const conversion = root.querySelector(`[data-price-conversion="${row.id}"]`);
      if (conversion) conversion.textContent = cleanNote(row.price_conversion_note);
      const amount = root.querySelector(`[data-amount="${row.id}"]`);
      if (amount) amount.textContent = row.existing ? 'Установлено' : row.amount === null ? '—' : money(row.amount);
    }
  }
  for(const block of viewSections(variant).flatMap(s=>s.blocks)) {
    const el=root.querySelector(`[data-subtotal="${block.id}"]`);if(el)el.textContent=`${money(block.subtotal)} ₽`;
  }
  syncScope();
  help.refresh();
}
function renderEditor() {
  const variant = snapshot.variants[selected];
  const allSections=viewSections(variant);
  const sections=allSections.map(s=>({...s,blocks:s.blocks.filter(b=>b.rows.some(r=>!r.excluded&&!r.source_excluded))})).filter(s=>s.blocks.length);
  $('#pe-blocks').innerHTML = sections.map(section=>`<section class="pe-stage" data-estimate-stage="${section.id}">${section.show_title?`<h2>${section.title}</h2>`:''}`+section.blocks.map(block => {
    const rows = block.rows.filter(r => !r.excluded && !r.source_excluded);
    return `<section class="pe-block"><div class="pe-block-head"><h3>${esc(block.title)}</h3><span class="pe-subtotal" data-subtotal="${block.id}">${money(block.subtotal)} ₽</span><button type="button" class="pe-link" data-exclude-view="${block.id}">Исключить</button></div><div class="pe-table-wrap"><table><thead><tr><th>Позиция</th><th>Кол-во</th><th>Цена, ₽</th><th>Сумма, ₽</th><th><span class="visually-hidden">Действия</span></th></tr></thead><tbody>${rows.map(row => `<tr data-order-row="${row.id}"><td class="pe-position-cell"><button type="button" class="pe-drag-handle" data-drag-row="${row.id}" data-view="${block.id}" data-block="${row.source_block}" aria-label="Переместить: ${esc(row.name)}" aria-description="Перетащите строку или используйте стрелки вверх и вниз" title="Перетащите строку; с клавиатуры — стрелки вверх/вниз">⠿</button>${thumbnail(row.catalog?.image || productImages[row.catalog?.id], row.name)}<span class="pe-row-name">${esc(productTitle(row))}</span><span class="pe-row-detail">${productDetails(row)}${row.existing ? ' · уже установлено' : ''}${row.selection_status === 'preliminary_primary' ? ' · предварительный подбор — проверить первичный контур' : ''}</span>${row.catalog?.specification_note ? `<small class="pe-help">${esc(row.catalog.specification_note)}${row.catalog.specification_source === 'https://aquaviva.nt-rt.ru/images/manuals/2-zakladnye-dlya-basseinov.pdf' ? ' <a href="https://aquaviva.nt-rt.ru/images/manuals/2-zakladnye-dlya-basseinov.pdf" target="_blank" rel="noopener">Источник</a>' : ''}</small>` : ''}${row.id==='liner_corner_2m' ? `<small class="pe-help"><a href="https://shop.aqualine22.ru/basseini/297-otdelochnye-materialy/307-lajner/krepegnyj-ugol-vneshnij-pvh-aquaviva-0050032-m.html" target="_blank" rel="noopener">Карточка уголка. Проверьте цену за отрезок 2 м.</a></small>` : ''}${row.selection_note || ['thermostat','filter_media'].includes(row.kind) ? `<small class="pe-help" data-selection-note="${row.id}">${esc(row.selection_note)}</small>` : ''}<div class="pe-row-check" data-row-check="${row.id}" aria-live="polite"${row.compatibility ? '' : ' hidden'}>${renderRowCheck(row, row.source_block)}</div></td><td><input class="pe-qty" data-field="quantity" data-block="${row.source_block}" data-row="${row.id}" type="number" min="0" max="1000000" step="any" value="${esc(row.quantity)}" placeholder="Уточнить" aria-label="Количество: ${esc(row.name)}"><span class="pe-row-detail">${esc(row.unit)} · <span data-quantity-mode="${row.id}">${quantityMode(row)}</span></span></td><td><input class="pe-price" data-field="price" data-block="${row.source_block}" data-row="${row.id}" type="number" min="0" max="1000000000" step="0.01" value="${esc(row.price)}" placeholder="Уточнить" aria-label="Цена: ${esc(row.name)}"><small>${esc(row.rate_source || '')}</small><small data-construction-quantity="${row.id}">${esc(cleanNote([row.construction_quantity_note, row.level_quantity_note].filter(Boolean).join(' ')))}</small><small data-construction-price="${row.id}">${esc(cleanNote(row.construction_price_note))}</small><small data-price-conversion="${row.id}">${esc(cleanNote(row.price_conversion_note))}</small></td><td class="pe-row-amount" data-amount="${row.id}">${money(row.amount)}</td><td class="pe-row-actions"><div class="pe-actions-inline">${!isWork(row) ? `<button type="button" class="pe-link" data-shop="${row.id}" data-block="${row.source_block}" aria-label="Выбрать товар: ${esc(row.name)}" title="Каталог"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"/></svg></button>` : ''}<button type="button" class="pe-link" data-edit="${row.id}" data-block="${row.source_block}" aria-label="Изменить: ${esc(row.name)}">✎</button><button type="button" class="pe-link" data-exclude-row="${row.id}" data-block="${row.source_block}" aria-label="Исключить: ${esc(row.name)}">×</button></div></td></tr>`).join('')}</tbody></table></div><button type="button" class="pe-link pe-add" data-add-view="${block.id}">+ Добавить позицию</button></section>`;
  }).join('')+'</section>').join('') + `<div class="pe-add-destination"><label>Добавить позицию в блок<select id="pe-add-view">${allSections.flatMap(s=>s.blocks).map(b=>`<option value="${b.id}">${esc(b.title)}</option>`).join('')}</select></label><button type="button" class="pe-btn" data-add-selected>Добавить позицию</button></div>`;
  const excluded = variant.blocks.filter(b => b.excluded || b.rows.some(r => r.excluded));
  $('#pe-excluded-count').textContent = `(${excluded.reduce((sum, b) => sum + (b.excluded ? 1 : b.rows.filter(r => r.excluded).length), 0)})`;
  $('#pe-excluded-content').innerHTML = excluded.map(b => `${b.excluded ? `<div class="pe-excluded-item"><div><strong>${esc(b.title)}</strong><small>${esc(b.reason)} · ${b.rows.length} позиций</small></div><button type="button" class="pe-link" data-restore-block="${b.id}">Вернуть блок</button></div>` : ''}${b.rows.filter(r => b.excluded || r.excluded).map(r => `<div class="pe-excluded-item"><div>${esc(r.name)}<small>${esc(r.quantity)} ${esc(r.unit)} · цена ${money(r.price)} ₽ · ${esc(r.reason || b.reason)}${r.existing ? ' · уже установлено' : ''}</small></div><button type="button" class="pe-link" data-restore-row="${r.id}" data-block="${b.id}">Вернуть строку</button></div>`).join('')}`).join('') || '<p class="pe-help">Исключённых позиций нет. Их можно будет вернуть с прежними количеством и ценой.</p>';
  renderTotals();
}
function renderHistory() {
  $('#pe-history-list').innerHTML = versions.map(v => `<div class="pe-history-row"><div><strong>Версия ${v.number}</strong> · ${stamp(v.created_at)} · ${esc(v.created_by?.name || 'Автор не указан')}<small>${esc(v.note || 'Без комментария')}</small></div><div><button type="button" class="pe-link" data-version="${v.number}">Открыть копию</button><a class="pe-link" href="${location.pathname}${encodeURIComponent(projectId)}/versions/${v.number}/print/" target="_blank" rel="noopener">Печать / PDF</a></div></div>`).join('') || 'После первого сохранения здесь появится версия 1.';
}
function setHeating(values) {
  const selectedTypes = values.heating_types ?? (values.heating === 'none' ? [] : [values.heating]);
  for (const checkbox of form.querySelectorAll('[name="heating_types"]')) checkbox.checked = selectedTypes.includes(checkbox.value);
}
function fillParameters() {
  form.querySelectorAll('[name^="tech_"], [name^="construction_"]').forEach(el => { el.value = ''; });
  form.elements.light_color.value=snapshot.inputs.light_color || '';
  form.elements.drain_scheme.value=snapshot.inputs.drain_scheme || 'unknown';
  form.elements.lighting.value=snapshot.inputs.lighting || 'no';
  form.elements.light_count.value=snapshot.inputs.light_count || '';
  form.elements.construction_scheme.value = snapshot.construction_version ? 'package' : 'legacy';
  form.elements.height_mode.value = Object.hasOwn(snapshot.inputs,'freeboard') ? 'freeboard' : 'legacy';
  form.elements.freeboard.value = snapshot.inputs.freeboard ?? (snapshot.inputs.bowl_height ? String(Math.max(0,Number(snapshot.inputs.bowl_height)-Number(snapshot.inputs.depth_start))) : '');
  // Historical snapshots must not inherit the inputs of a more recent estimate.
  for (const [key, value] of Object.entries({primary_pump_connection:'', primary_valve_connection:'', primary_medium:'', primary_temperature:'', primary_delta:'', primary_flow:'', primary_head:'', bowl_height: '', wall_thickness: '', slab_thickness: '', heating_power: '', water_start: '', water_target: '', heating_hours: '', heating_reserve: '', finish: '', skimmer_area: '25', skimmer_flow: '5', inlet_flow: '3'})) {
    form.elements.namedItem(key).value = snapshot.inputs[key] ?? value;
  }
  for (const [key, value] of Object.entries(snapshot.inputs)) {
    if (key !== 'heating_types' && form.elements.namedItem(key)) form.elements.namedItem(key).value = form.elements.namedItem(key).type === 'number' ? exactNumber(value) : value ?? '';
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
  if (event.target.name === 'freeboard') form.elements.height_mode.value='freeboard';
  if (['freeboard','depth_start'].includes(event.target.name) && form.elements.tech_insulation_wall_height_mode.value === 'auto') inputs();
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
    status(data.catalog_error || `Параметры обновлены. Подобрано позиций: ${data.selected_count || 0}. Недостающие работы и известные базовые ставки добавлены автоматически. Ручные замены, цены и исключения сохранены. Проверьте предупреждения.`, Boolean(data.catalog_error));
  });
});
// A blur may open the price dialog during the same click on another action.
// One handle supports mouse/touch dragging and keyboard arrows; no cross-table moves.
let rowDrag = null;
function orderReady() {
  if (!snapshot || busy || priceTarget) return false;
  const invalid = root.querySelector('[data-field]:invalid');
  if (invalid) { invalid.reportValidity(); invalid.focus(); return false; }
  return true;
}
function finishOrder(viewId, rowId, targetIndex) {
  const view=viewSections(snapshot.variants[selected]).flatMap(s=>s.blocks).find(b=>b.id===viewId);
  const rows=view?.rows.filter(r=>!r.excluded&&!r.source_excluded)||[];
  let from=rows.findIndex(r=>r.id===rowId);
  if(from<0||targetIndex<0||targetIndex>=rows.length||targetIndex===from)return;
  const direction=targetIndex>from?1:-1;
  while(from!==targetIndex && moveViewRow(snapshot.variants[selected],viewId,rowId,direction))from+=direction;
  clearTimeout(timer);sequence++;changed();renderEditor();queueValidation();
  [...root.querySelectorAll('[data-drag-row]')].find(b=>b.dataset.dragRow===rowId)?.focus({preventScroll:true});
  status('Строка перемещена в текущей таблице этого КП. Есть несохранённые изменения.');
}
function endRowDrag(commit) {
  const drag = rowDrag;
  if (!drag) return;
  rowDrag = null;
  root.classList.remove('pe-is-dragging');
  root.querySelectorAll('.pe-dragging,.pe-drop-before,.pe-drop-after').forEach(el => el.classList.remove('pe-dragging','pe-drop-before','pe-drop-after'));
  if (root.hasPointerCapture(drag.pointerId)) root.releasePointerCapture(drag.pointerId);
  if (commit && drag.moved && drag.target !== null && drag.variant === selected) finishOrder(drag.view, drag.id, drag.target);
  queueValidation();
}
root.addEventListener('keydown', event => {
  if (event.key === 'Escape' && rowDrag) { endRowDrag(false); event.preventDefault(); return; }
  const handle = event.target.closest('[data-drag-row]');
  if (!handle || !['ArrowUp','ArrowDown'].includes(event.key)) return;
  event.preventDefault();
  if (!orderReady()) return;
  const rows = [...handle.closest('table').querySelectorAll('[data-order-row]')].map(el=>({id:el.dataset.orderRow}));
  finishOrder(handle.dataset.view, handle.dataset.dragRow, rows.findIndex(r => r.id === handle.dataset.dragRow) + (event.key === 'ArrowUp' ? -1 : 1));
});
root.addEventListener('pointerdown', event => {
  const handle = event.target.closest('[data-drag-row]');
  if (!handle || event.button !== 0 || rowDrag) return;
  if (document.activeElement?.matches('[data-field]')) document.activeElement.blur();
  if (!orderReady()) return;
  event.preventDefault(); handle.focus({preventScroll:true});
  clearTimeout(timer); sequence++;
  rowDrag = {id:handle.dataset.dragRow, block:handle.dataset.block, view:handle.dataset.view, variant:selected, table:handle.closest('table'), pointerId:event.pointerId, x:event.clientX, y:event.clientY, moved:false, target:null};
  root.setPointerCapture(event.pointerId);
});
root.addEventListener('pointermove', event => {
  if (!rowDrag || event.pointerId !== rowDrag.pointerId) return;
  const drag = rowDrag;
  if (!drag.moved && Math.hypot(event.clientX-drag.x,event.clientY-drag.y) < 5) return;
  drag.moved = true; event.preventDefault(); root.classList.add('pe-is-dragging');
  const rows = [...drag.table.querySelectorAll('tbody tr[data-order-row]')];
  rows.forEach(tr => {tr.classList.remove('pe-drop-before','pe-drop-after');tr.classList.toggle('pe-dragging',tr.dataset.orderRow === drag.id);});
  const target = document.elementFromPoint(event.clientX,event.clientY)?.closest('tr[data-order-row]');
  drag.target = null;
  if (!target || target.closest('table') !== drag.table) return;
  const bounds = target.getBoundingClientRect(), after = event.clientY > bounds.top + bounds.height/2;
  const from = rows.findIndex(tr => tr.dataset.orderRow === drag.id);
  let to = rows.indexOf(target) + (after ? 1 : 0);
  if (to > from) to--;
  drag.target = to;
  target.classList.add(after ? 'pe-drop-after' : 'pe-drop-before');
  if (event.clientY < 65) window.scrollBy(0,-18);
  else if (event.clientY > window.innerHeight-65) window.scrollBy(0,18);
});
root.addEventListener('pointerup', event => {if (event.pointerId === rowDrag?.pointerId) endRowDrag(true);});
root.addEventListener('pointercancel', () => endRowDrag(false));
root.addEventListener('lostpointercapture', () => endRowDrag(false));

root.addEventListener('click', event => {
  if (priceTarget && !event.target.closest('#pe-price-dialog')) {
    event.preventDefault(); event.stopImmediatePropagation();
  }
}, true);
root.addEventListener('input', event => {
  // Warn about loss even if the user closes the tab before the field blurs.
  if (event.target.dataset.field && snapshot) {
    // An older validation response must not replace an edit before it blurs.
    clearTimeout(timer); changed();
  }
});
root.addEventListener('change', event => {
  const el = event.target;
  if (el.dataset.field && snapshot) {
    if (!el.reportValidity()) return;
    const row = snapshot.variants[selected].blocks.find(b => b.id === el.dataset.block).rows.find(r => r.id === el.dataset.row);
    const value = rawValue(el) === '' && (el.dataset.field === 'price' || isWork(row)) ? null : rawValue(el);
    if (String(row[el.dataset.field] ?? '') === String(value ?? '')) return;
    if (el.dataset.field === 'price' && isWork(row) && !samePrice(row.price, value)) {
      askWorkPrice(el.dataset.block, row, value); return;
    }
    if (el.dataset.field === 'price' && (row.catalog?.roll_area || row.construction_price || row.id==='material_supplies' || row.pvc_computed_price != null || row.consumables_computed_price != null)) row.unit_price_manual = true;
    row[el.dataset.field] = value;
    if (el.dataset.field === 'quantity') { row.quantity_edited = true; row.auto_quantity = false; row.quantity_stale = false; row.selection_locked = true; if (row.catalog) row.catalog.selection = 'manual'; }
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
  if(el.dataset.excludeView){askScope(null,null,true,el.dataset.excludeView);return;}
  const destinations={bowl_work:'construction',bowl_materials:snapshot.construction_version?'construction_materials':'construction',embedded:'embedded',equipment:'equipment',materials:'finish',installation_work:'finish',extra_materials:'options',extra_work:'options'};
  if(el.hasAttribute('data-add-selected'))el.dataset.addView=$('#pe-add-view').value;
  if(el.dataset.addView){el.dataset.add=destinations[el.dataset.addView];el.dataset.addWork=String(el.dataset.addView.endsWith('_work'));}
  const blockId = el.dataset.excludeBlock || el.dataset.restoreBlock || el.dataset.block;
  if (el.dataset.excludeBlock || el.dataset.restoreBlock || el.dataset.excludeRow || el.dataset.restoreRow) {
    askScope(blockId, el.dataset.excludeRow || el.dataset.restoreRow || null, Boolean(el.dataset.excludeBlock || el.dataset.excludeRow), el.dataset.stage);
  }
  if (el.dataset.autoRow) { repairRow(el.dataset.block, el.dataset.autoRow); return; }
  if (el.dataset.shop) { openCatalog(el.dataset.block, el.dataset.shop); return; }
  if (el.dataset.edit || el.dataset.add) {
    const block = snapshot.variants[selected].blocks.find(b => b.id === (el.dataset.block || el.dataset.add));
    rowBeingEdited = el.dataset.edit ? structuredClone(block.rows.find(r => r.id === el.dataset.edit)) : emptyRow(crypto.randomUUID());
    if(el.dataset.addWork==='true')rowBeingEdited.kind='work';
    const edit = $('#pe-row-form'); edit.reset();
    edit.querySelector('[type=submit]').textContent=el.dataset.edit?'Применить к текущей смете':'Продолжить';
    edit.elements.row_id.value = rowBeingEdited.id; edit.elements.block_id.value = block.id;
    for (const key of ['name', 'unit', 'brand', 'model', 'kind', 'quantity', 'price']) edit.elements.namedItem(key).value = rowBeingEdited[key] ?? '';
    for (const key of ['existing', 'auto_quantity']) edit.elements.namedItem(key).checked = rowBeingEdited[key];
    for (const key of ['flow', 'head', 'filter_flow', 'backwash_flow']) edit.elements.namedItem(key).value = rowBeingEdited.specs[key] ?? '';
    edit.elements.quantity.required = quantityRequired(rowBeingEdited);
    $('#pe-handling-fields').hidden = block.id !== 'construction_materials';
    edit.elements.handling_mode.value = rowBeingEdited.handling_mode || 'unknown';
    edit.elements.handling_mass.value = rowBeingEdited.handling_mass ?? '';
    $('#pe-level-fields').hidden = rowBeingEdited.kind !== 'skimmer';
    edit.elements.level_status.value = rowBeingEdited.level_status || 'unknown';
    edit.elements.level_evidence.value = rowBeingEdited.level_evidence || '';
    for (const el of edit.querySelectorAll('input[type=number], input[name=price]')) bindNumber(el, el.value, {money:el.name==='price', whole:el.name==='quantity' && indivisible(rowBeingEdited.unit)});
    $('#pe-filter-parent-label').hidden = rowBeingEdited.kind!=='filter_media';
    edit.elements.filter_parent.innerHTML='<option value="">Не привязан</option>'+snapshot.variants[selected].blocks.flatMap(b=>b.rows).filter(r=>r.kind==='filter').map(r=>`<option value="${esc(r.id)}">${esc(r.name)} · ${esc(r.model||r.id)}</option>`).join('');
    edit.elements.filter_parent.value=rowBeingEdited.filter_parent||'';
    const accessory = ['circulation_pump','solenoid_valve','thermostat'].includes(rowBeingEdited.kind);
    $('#pe-heating-parent-label').hidden = !accessory;
    const parentSelect = edit.elements.heating_parent;
    parentSelect.innerHTML = '<option value="">Не привязан</option>' + snapshot.variants[selected].blocks.flatMap(b => b.rows).filter(r => r.kind === 'heater' && (r.heating_type || r.catalog?.heater_type || snapshot.inputs.heating) === 'exchanger').map(r => `<option value="${esc(r.id)}">${esc(r.name)} · ${esc(r.model || r.id)}</option>`).join('');
    parentSelect.value = rowBeingEdited.heating_parent || '';
    $('#pe-auto-label').hidden = !rowBeingEdited.basis;
    $('#pe-row-dialog').showModal();
  }
});
$('#pe-row-form').addEventListener('change', event => {
  if (!['auto_quantity', 'kind', 'unit'].includes(event.target.name)) return;
  const f = event.currentTarget;
  bindNumber(f.elements.quantity, rawValue(f.elements.quantity), {whole:indivisible(f.elements.unit.value)});
  f.elements.quantity.required = quantityRequired({...rowBeingEdited, kind:f.elements.kind.value,
    auto_quantity:f.elements.auto_quantity.checked});
});
$('#pe-row-form').addEventListener('submit', event => {
  event.preventDefault();
  const f = event.currentTarget;
  const row = structuredClone(rowBeingEdited);
  row.selection_locked = true;
  for (const key of ['name', 'unit', 'brand', 'model', 'kind', 'quantity']) row[key] = rawValue(f.elements.namedItem(key));
  row.price = rawValue(f.elements.price) || null;
  if ((row.quantity !== rowBeingEdited.quantity || row.unit !== rowBeingEdited.unit) && indivisible(row.unit) && row.quantity !== '' && !Number.isInteger(Number(row.quantity))) {
    f.elements.quantity.setCustomValidity('Укажите целое количество для штучной позиции.'); f.elements.quantity.reportValidity(); return;
  }
  if (!samePrice(row.price, rowBeingEdited.price) && (row.catalog?.roll_area || row.construction_price || row.id==='material_supplies' || row.pvc_computed_price != null || row.consumables_computed_price != null)) row.unit_price_manual = true;
  if (row.kind === 'work' && !row.quantity) row.quantity = null;
  if (row.kind === 'work' && !row.work_code) row.work_code = row.id.startsWith('finish_work') ? 'finish_' + (row.finish_type || 'unknown') : row.id;
  if (['name', 'brand', 'model', 'kind'].some(key => row[key] !== rowBeingEdited[key])) row.catalog = null;
  if (row.quantity !== rowBeingEdited.quantity || row.unit !== rowBeingEdited.unit) { row.quantity_stale = false; row.quantity_edited = true; }
  if(row.kind==='filter_media'){row.filter_parent=f.elements.filter_parent.value;row.basis='filter_load';}
  if (['circulation_pump','solenoid_valve','thermostat'].includes(row.kind)) {
    row.heating_parent = f.elements.heating_parent.value;
    row.heating_unbound = !row.heating_parent;
  }
  if (row.kind === 'skimmer') {
    const identityChanged = ['name','brand','model','kind'].some(key => row[key] !== rowBeingEdited[key]);
    row.level_status = identityChanged ? 'unknown' : f.elements.level_status.value;
    row.level_evidence = identityChanged ? '' : f.elements.level_evidence.value.trim();
  }
  row.handling_mode = f.elements.handling_mode.value;
  row.handling_mass = rawValue(f.elements.handling_mass) || null;
  row.existing = f.elements.existing.checked;
  row.auto_quantity = Boolean(row.basis) && f.elements.auto_quantity.checked;
  if (row.auto_quantity) row.quantity = automaticQuantity(snapshot, selected, row);
  for (const key of ['flow', 'head', 'filter_flow', 'backwash_flow']) row.specs[key] = rawValue(f.elements.namedItem(key)) || null;
  if (row.catalog && (row.quantity !== rowBeingEdited.quantity || ['flow','head','filter_flow','backwash_flow'].some(key => row.specs[key] !== rowBeingEdited.specs[key]))) row.catalog.selection = 'manual';
  const block = snapshot.variants[selected].blocks.find(b => b.id === f.elements.block_id.value);
  const index = block.rows.findIndex(r => r.id === row.id);
  if (index < 0 && block.rows.length >= 100) { status('В блоке допускается до 100 строк.', true); return; }
  const commit = () => { if (index < 0) block.rows.push(row); else block.rows[index] = row; $('#pe-row-dialog').close(); };
  if (index < 0) {
    clearTimeout(timer); sequence++;
    addRowTarget={variant:selected,blockId:block.id,row};
    $('#pe-add-row-target').textContent=row.name;
    $('#pe-add-row-error').textContent='';
    $('#pe-add-row-dialog').showModal(); return;
  }
  if (isWork(row) && !samePrice(rowBeingEdited.price, row.price)) {
    askWorkPrice(block.id, row, row.price, row); return;
  }
  commit(); changed(); renderEditor(); queueValidation();
});
root.addEventListener('click',async event=>{
 const button=event.target.closest('[data-add-row-choice]');
 if(!button||busy||!addRowTarget)return;
 const target=addRowTarget,all=button.dataset.addRowChoice==='all';
 await run(async()=>{
  clearTimeout(timer);sequence++;
  try{
   const candidate=addRow(snapshot,target.variant,target.blockId,target.row,all);
   let data;
   if(isWork(target.row)&&target.row.price!==null){
    for(const [key,v] of Object.entries(candidate.variants))if(all||key===target.variant){
     v.blocks.find(b=>b.id===target.blockId).rows.find(r=>r.id===target.row.id).price=null;
    }
    data=await api({action:'work_price',snapshot:candidate,variant:target.variant,block:target.blockId,row:target.row.id,price:target.row.price,all});
   }else data=await api({action:'validate',snapshot:candidate});
   snapshot=data.snapshot;addRowTarget=null;
   $('#pe-add-row-dialog').close();$('#pe-row-dialog').close();
   changed();renderEditor();status(all?'Позиция добавлена во все три КП.':'Позиция добавлена в текущее КП.');
  }catch(error){$('#pe-add-row-error').textContent=error.message;}
 });
});
$('#pe-add-row-dialog').addEventListener('cancel',event=>{if(busy)event.preventDefault();});
$('#pe-add-row-dialog').addEventListener('close',()=>{if(!$('#pe-add-row-dialog').open)addRowTarget=null;});
function askWorkPrice(blockId, row, price, draftRow = null) {
  clearTimeout(timer); sequence++;
  priceTarget = {variant: selected, blockId, source: structuredClone(row), price, draftRow};
  const matches = workPriceTargets(snapshot, selected, blockId, row);
  const other = matches.filter(t => t.variant !== selected).map(t => titles[t.variant]);
  $('#pe-price-dialog [data-price-choice=one]').hidden = Boolean(snapshot.construction_version && blockId==='construction');
  $('#pe-price-target').textContent = `${row.name}: ${money(price)} ₽ / ${row.unit}. Где изменить расценку?`;
  $('#pe-price-matches').textContent = other.length
    ? `В других КП: ${other.join(', ')}. Меняется только цена такой же работы с той же единицей измерения. Объёмы и исключения сохраняются.`
    : 'В других КП нет однозначно соответствующей работы с такой же единицей измерения. Будет изменено только текущее КП.';
  $('#pe-price-error').textContent = '';
  $('#pe-price-dialog').showModal();
}
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
    $('#pe-project-list').innerHTML = data.projects.map(p => `<button type="button" class="pe-project" data-project="${esc(p.id)}"><strong>${esc(p.title)}</strong><small>Версия ${p.revision} · ${stamp(p.updated_at)} · Создал: ${esc(p.created_by?.name || 'Автор не указан')}</small></button>`).join('') || '<p>Расчёты не найдены.</p>';
  } catch (error) { if (error.name !== 'AbortError') $('#pe-project-list').textContent = error.message; }
}
$('#pe-open').addEventListener('click', () => { $('#pe-projects-dialog').showModal(); searchProjects(); });
$('#pe-project-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(searchProjects, 300); });
function askScope(blockId,rowId,excluded,viewId=null){
  const block=snapshot.variants[selected].blocks.find(b=>b.id===blockId);
  const view=viewId&&viewSections(snapshot.variants[selected]).flatMap(s=>s.blocks).find(b=>b.id===viewId);
  scopeTarget={variant:selected,blockId,rowId,excluded,viewId};
  $('#pe-scope-title').textContent=excluded?'Исключить из сметы?':'Восстановить в смете?';
  $('#pe-scope-target').textContent=`${view?view.title:rowId?block.rows.find(r=>r.id===rowId).name:block.title} · ${titles[selected]}`;
  $('#pe-scope-dialog').showModal();
}
root.addEventListener('click',event=>{
 const button=event.target.closest('[data-scope-choice]');if(!button||busy||!scopeTarget)return;
 const t=scopeTarget;
 if(t.viewId){
  for(const [key,v] of Object.entries(snapshot.variants)){
   if(button.dataset.scopeChoice!=='all'&&key!==t.variant)continue;
   for(const block of v.blocks)if(!block.excluded)for(const row of block.rows)if(!row.excluded&&groupFor(block,row)===t.viewId)setExcluded(snapshot,key,block.id,row.id,true,false);
  }
 }else if(t.blockId==='construction' && !t.rowId) setConstructionExcluded(snapshot,t.variant,t.excluded,button.dataset.scopeChoice==='all');
 else setExcluded(snapshot,t.variant,t.blockId,t.rowId,t.excluded,button.dataset.scopeChoice==='all');
 $('#pe-scope-dialog').close();scopeTarget=null;changed();renderEditor();queueValidation();
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

for (const el of form.querySelectorAll('input[type=number]')) el.value=exactNumber(el.value);
const initial = new URL(location.href).searchParams;
$('#pe-project-search').value = initial.get('q') || '';
if (initial.get('id')) await loadProject(initial.get('id'), initial.get('version') || '');
else await generateNew();

form.elements.tech_insulation_wall_height.addEventListener('input', () => { form.elements.tech_insulation_wall_height_mode.value = 'manual'; });
