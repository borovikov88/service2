import {availabilityStatus} from './availability.js?v=1';

const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));

function productLink(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname === 'shop.aqualine22.ru' && !url.username && !url.password && (!url.port || url.port === '443') ? url.href : '';
  } catch { return ''; }
}

export function renderCatalogCard(product, {money, thumbnail}) {
  const p = product, stock = availabilityStatus(p), picture = thumbnail(p.image, p.name, true);
  const url = productLink(p.url), code = p.sku ? 'арт. ' + p.sku : 'код ' + p.id;
  const identity = url
    ? `<a class="pe-link pe-catalog-code" href="${esc(url)}" target="_blank" rel="noopener noreferrer" aria-label="Карточка товара: ${esc(code)}">${esc(code)}</a>`
    : `<span>${esc(code)}</span>`;
  const facts = [];
  if (p.brand) facts.push(`<span>${esc(p.brand)}</span>`);
  facts.push(identity);
  if (p.kind === 'circulation_pump') facts.push(`<span>${p.frequency_control === true ? 'Частотное регулирование по названию' : p.frequency_control === false ? 'Без частотного регулирования' : 'Регулирование не подтверждено'}</span>`);
  if (p.nominal_flow) facts.push(`<span>${money(p.nominal_flow)} м³/ч по каталогу${p.head ? ' при ' + money(p.head) + ' м' : ''}</span>`);
  else if (p.head) facts.push(`<span>при ${money(p.head)} м</span>`);
  if (p.heating_power) facts.push(`<span>${money(p.heating_power)} кВт</span>`);
  if (p.power && p.power !== 'unknown') facts.push(`<span>${esc(p.power)} В</span>`);
  if (p.roll_area) facts.push(`<span>${esc(p.roll_area)} м²/рулон; в смете за м²</span>`);
  const media = ['filter_media','filter_gravel'].includes(p.kind) ? p.filter_media || {} : null;
  if (media?.bag_kg) facts.push(`<span>${esc(media.bag_kg)} кг/меш.</span>`);
  if (media?.fraction_min && media?.fraction_max) facts.push(`<span>${esc(media.fraction_min)}–${esc(media.fraction_max)} мм</span>`);
  const warning = p.warning || (p.kind === 'other'
    ? 'Назначение, единицу измерения и количество проверьте в карточке.'
    : 'Предварительный кандидат. Проверьте рабочий режим, совместимость и комплектацию.');
  const priceUnit = media?.price_unit === 'bag' ? ' / меш.' : media?.price_unit === 'kg' ? ' / кг'
    : p.kind === 'liner' && p.liner?.price_unit === 'm2' ? ' / м²'
    : p.roll_area || p.kind === 'liner' && p.liner?.price_unit === 'roll' ? ' / рулон' : '';
  return `<article class="pe-catalog-item${picture ? '' : ' pe-catalog-no-image'}">
    ${picture ? `<div class="pe-catalog-photo">${picture}</div>` : ''}
    <strong class="pe-catalog-title">${esc(p.name)}</strong>
    <strong class="pe-catalog-cost">${money(p.price)} ₽${priceUnit}</strong>
    <div class="pe-catalog-meta">
      ${facts.join('<span class="pe-catalog-separator" aria-hidden="true">·</span>')}
      <span class="pe-catalog-icons">
        <span class="pe-catalog-stock pe-catalog-stock-${stock.tone}"><span data-help-source data-help-label="●">Наличие: ${esc(stock.detail)}</span></span>
        <span class="pe-catalog-check"><span data-help-source data-help-label="!">${esc(warning)}</span></span>
      </span>
    </div>
    <div class="pe-catalog-actions"><button class="pe-btn" type="button" data-catalog-product="${esc(p.id)}">Выбрать</button></div>
  </article>`;
}
