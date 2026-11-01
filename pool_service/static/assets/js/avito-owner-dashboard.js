(function () {
  'use strict';
  const root = document.querySelector('[data-avito-owner]');
  const payload = document.getElementById('avito-owner-chart-data');
  if (!root || !payload) return;
  let data;
  try { data = JSON.parse(payload.textContent); } catch (_) { return; }
  if (!data || !Array.isArray(data.labels)) return;
  const labels = data.labels.slice(0, 366);
  const namespace = 'http://www.w3.org/2000/svg';
  const formatter = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });
  const metricNames = {
    views: 'Просмотры', contacts: 'Контакты Авито',
    spending: 'Денежные расходы', cost_view: 'Стоимость просмотра', cost_contact: 'Стоимость контакта'
  };
  const monetary = new Set(['spending', 'cost_view', 'cost_contact']);
  function known(value) { return typeof value === 'number' && Number.isFinite(value) && value >= 0; }
  function series(key) {
    const values = Array.isArray(data[key]) ? data[key] : [];
    return labels.map(function (_, index) { return known(values[index]) ? values[index] : null; });
  }
  function valueText(value, key) { return known(value) ? formatter.format(value) + (monetary.has(key) ? ' ₽' : '') : 'Нет данных'; }
  function sourceDate(index) {
    const value = labels[index];
    return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : 'Дата источника';
  }
  function element(name, attributes, content) {
    const node = document.createElementNS(namespace, name);
    Object.keys(attributes || {}).forEach(function (key) { node.setAttribute(key, String(attributes[key])); });
    if (content !== undefined) node.textContent = content;
    return node;
  }
  function draw(target, key, height, outputName) {
    const svg = root.querySelector('[data-owner-chart="' + target + '"]');
    if (!svg) return;
    svg.replaceChildren();
    svg.setAttribute('aria-label', metricNames[key] + ' по датам источника. Пропуски означают неизвестные значения.');
    const values = series(key);
    const existing = values.filter(known);
    const empty = root.querySelector('[data-owner-empty="' + target + '"]');
    if (empty) empty.hidden = existing.length > 0;
    const width = 600, left = 46, right = 12, top = 12, bottom = 24;
    const plotHeight = height - top - bottom;
    const maximum = existing.length ? Math.max.apply(null, existing) : 1;
    const upper = maximum > 0 ? maximum * 1.12 : 1;
    const x = function (index) { return left + (labels.length > 1 ? index / (labels.length - 1) : 0.5) * (width - left - right); };
    const y = function (value) { return top + plotHeight - (value / upper) * plotHeight; };
    for (let index = 0; index < 3; index += 1) {
      const value = upper * index / 2;
      const position = y(value);
      svg.appendChild(element('line', { x1: left, y1: position, x2: width - right, y2: position, 'class': 'avito-owner-chart-grid' }));
      svg.appendChild(element('text', { x: left - 7, y: position + 4, 'text-anchor': 'end', 'class': 'avito-owner-chart-label' }, formatter.format(value)));
    }
    const tickIndexes = new Set([0, Math.floor((labels.length - 1) / 3), Math.floor(2 * (labels.length - 1) / 3), labels.length - 1]);
    tickIndexes.forEach(function (index) {
      if (index < 0 || index >= labels.length) return;
      const date = sourceDate(index);
      svg.appendChild(element('text', { x: x(index), y: height - 5, 'text-anchor': 'middle', 'class': 'avito-owner-chart-label' }, date === 'Дата источника' ? '—' : date.slice(8, 10) + '.' + date.slice(5, 7)));
    });
    let segment = [];
    function finishSegment() {
      if (segment.length > 1) svg.appendChild(element('polyline', { points: segment.join(' '), 'class': 'avito-owner-chart-line' }));
      segment = [];
    }
    if (key !== 'contacts') {
      values.forEach(function (value, index) {
        if (!known(value)) { finishSegment(); return; }
        segment.push(x(index) + ',' + y(value));
      });
      finishSegment();
    }
    const output = root.querySelector('[data-owner-chart-output="' + outputName + '"]');
    if (output) output.textContent = existing.length ? 'Выберите точку для просмотра значения' : 'Динамика не получена: неизвестные значения не заменяются нулями';
    values.forEach(function (value, index) {
      if (!known(value)) return;
      const label = sourceDate(index) + ' · ' + metricNames[key] + ': ' + valueText(value, key);
      if (key === 'contacts') {
        const barWidth = Math.max(2, Math.min(12, (width - left - right) / Math.max(labels.length, 1) * 0.55));
        svg.appendChild(element('rect', { x: x(index) - barWidth / 2, y: y(value), width: barWidth, height: top + plotHeight - y(value), 'class': 'avito-owner-chart-bars' }));
      }
      const point = element('circle', {
        cx: x(index), cy: y(value), r: key === 'contacts' ? 2 : 3,
        'class': 'avito-owner-chart-point' + (key === 'contacts' ? ' avito-owner-chart-point--contacts' : ''),
        tabindex: '0', 'aria-label': label
      });
      point.appendChild(element('title', {}, label));
      function showValue() { if (output) output.textContent = label; }
      point.addEventListener('focus', showValue);
      point.addEventListener('pointerenter', showValue);
      point.addEventListener('click', showValue);
      svg.appendChild(point);
    });
  }
  draw('views', 'views', 130, 'volume');
  draw('contacts', 'contacts', 130, 'volume');
  draw('cost', 'cost_view', 288, 'cost');

  const switches = Array.from(root.querySelectorAll('[data-owner-cost]'));
  switches.forEach(function (button, index) {
    button.addEventListener('click', function () {
      const key = button.dataset.ownerCost;
      if (!monetary.has(key)) return;
      switches.forEach(function (other) { other.setAttribute('aria-pressed', String(other === button)); });
      draw('cost', key, 288, 'cost');
    });
    button.addEventListener('keydown', function (event) {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const next = (index + (event.key === 'ArrowRight' ? 1 : -1) + switches.length) % switches.length;
      switches[next].focus();
      switches[next].click();
    });
  });
  const holder = root.querySelector('[data-owner-daily-table]');
  if (holder) {
    const table = document.createElement('table');
    table.className = 'table table-sm';
    const caption = document.createElement('caption');
    caption.textContent = 'Даты источника; часовой пояс статистики неизвестен. Прочерк означает неизвестное значение.';
    table.appendChild(caption);
    const head = document.createElement('thead');
    const headingRow = document.createElement('tr');
    ['Дата источника', 'Просмотры', 'Контакты Авито', 'Расходы, ₽', '₽ / просмотр', '₽ / контакт'].forEach(function (label) {
      const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = label; headingRow.appendChild(cell);
    });
    head.appendChild(headingRow); table.appendChild(head);
    const body = document.createElement('tbody');
    const keys = ['views', 'contacts', 'spending', 'cost_view', 'cost_contact'];
    const allValues = keys.map(series);
    labels.forEach(function (_, index) {
      const row = document.createElement('tr');
      const date = document.createElement('th'); date.scope = 'row'; date.textContent = sourceDate(index); row.appendChild(date);
      keys.forEach(function (key, metricIndex) {
        const cell = document.createElement('td'); cell.textContent = valueText(allValues[metricIndex][index], key); row.appendChild(cell);
      });
      body.appendChild(row);
    });
    table.appendChild(body); holder.replaceChildren(table);
  }
})();
