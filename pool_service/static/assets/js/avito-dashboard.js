(function () {
  'use strict';
  const root = document.querySelector('[data-avito-dashboard]');
  if (!root) return;
  // Convert only ISO timestamps with an explicit offset. A naive provider time
  // has no known timezone: make it readable without inventing an offset.
  const displayTimeZone = root.dataset.avitoTimezone || '';
  root.querySelectorAll('time[data-avito-time]').forEach(function (element) {
    const raw = element.getAttribute('datetime') || '';
    const match = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?$/.exec(raw);
    if (!match) return;
    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    const hour = Number(match[4] || 0);
    const minute = Number(match[5] || 0);
    const second = Number(match[6] || 0);
    const calendar = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
    if (calendar.getUTCFullYear() !== year || calendar.getUTCMonth() !== month - 1 ||
        calendar.getUTCDate() !== day || hour > 23 || minute > 59 || second > 59) return;
    if (match[7]) {
      if (!displayTimeZone) return;
      const value = new Date(raw);
      if (!Number.isFinite(value.getTime())) return;
      try {
        element.textContent = new Intl.DateTimeFormat('ru-RU', {
          timeZone: displayTimeZone, day: '2-digit', month: '2-digit', year: 'numeric',
          hour: '2-digit', minute: '2-digit', timeZoneName: 'short'
        }).format(value);
      } catch (_) { return; }
    } else {
      element.textContent = match[3] + '.' + match[2] + '.' + match[1];
      if (match[4]) element.textContent += ', ' + match[4] + ':' + match[5] + ' · часовой пояс API не указан';
    }
  });

  const search = root.querySelector('[data-avito-search]');
  const rows = Array.from(root.querySelectorAll('[data-avito-item]'));
  const detailRows = Array.from(root.querySelectorAll('[data-avito-detail-row]'));
  const count = root.querySelector('[data-avito-search-count]');
  const empty = root.querySelector('[data-avito-search-empty]');
  const clear = root.querySelector('[data-avito-search-clear]');
  let timer;

  function filterItems(updateUrl) {
    if (!search) return;
    const query = search.value.trim().toLocaleLowerCase('ru');
    let visible = 0;
    rows.forEach(function (row) {
      row.hidden = !row.dataset.avitoItem.toLocaleLowerCase('ru').includes(query);
      if (!row.hidden) visible += 1;
    });
    detailRows.forEach(function (detail) {
      const item = rows.find(function (row) { return row.dataset.avitoItemId === detail.dataset.avitoDetailRow; });
      detail.hidden = Boolean(item && item.hidden);
    });
    if (count) count.textContent = query ? 'Найдено на странице: ' + visible + ' из ' + rows.length : 'На странице: ' + rows.length;
    if (empty) empty.hidden = !query || visible !== 0 || rows.length === 0;
    if (clear) clear.hidden = search.value.length === 0;
    root.querySelectorAll('[data-avito-query]').forEach(function (field) { field.value = search.value.trim(); });
    if (updateUrl) {
      const url = new URL(window.location.href);
      if (query) url.searchParams.set('q', search.value.trim());
      else url.searchParams.delete('q');
      window.history.replaceState(window.history.state, '', url);
    }
  }

  function restoreSearch() {
    if (!search) return;
    window.clearTimeout(timer);
    search.value = new URL(window.location.href).searchParams.get('q') || '';
    filterItems(false);
  }

  if (search) {
    search.addEventListener('input', function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(function () { filterItems(true); }, 300);
    });
    if (clear) clear.addEventListener('click', function () {
      window.clearTimeout(timer);
      search.value = '';
      filterItems(true);
      search.focus();
    });
    window.addEventListener('popstate', restoreSearch);
    restoreSearch();
  }

  function revealSelectedDetail() {
    const detail = root.querySelector('[data-avito-selected-detail]');
    if (!detail || window.location.hash !== '#' + detail.id) return;
    const item = rows.find(function (row) { return row.dataset.avitoItemId === detail.dataset.avitoDetailFor; });
    if (item && item.hidden && search) {
      search.value = '';
      filterItems(true);
    }
    detail.scrollIntoView({ block: 'nearest' });
    detail.focus({ preventScroll: true });
  }
  window.addEventListener('hashchange', revealSelectedDetail);

  // Keep each form independently busy. A failed validation never reaches submit.
  const forms = Array.from(root.querySelectorAll('form[data-avito-submit]'));
  forms.forEach(function (form) {
    form.addEventListener('submit', function (event) {
      if (form.dataset.busy === 'true') {
        event.preventDefault();
        return;
      }
      if (search) filterItems(true);
      form.dataset.busy = 'true';
      form.setAttribute('aria-busy', 'true');
      form.querySelectorAll('button[type="submit"]').forEach(function (button) {
        if (button.disabled) return;
        button.dataset.avitoWasEnabled = 'true';
        button.disabled = true;
        const spinner = document.createElement('span');
        spinner.className = 'spinner-border spinner-border-sm avito-submit-spinner';
        spinner.setAttribute('aria-hidden', 'true');
        button.prepend(spinner);
      });
      const progress = root.querySelector('[data-avito-progress]');
      if (progress) progress.textContent = form.method.toLowerCase() === 'post' ? 'Загружаем данные. Дождитесь ответа Авито.' : 'Открываем выбранные данные.';
    });
  });

  // Back/Forward cache may restore the submitted DOM, including disabled buttons.
  window.addEventListener('pageshow', function (event) {
    forms.forEach(function (form) {
      delete form.dataset.busy;
      form.removeAttribute('aria-busy');
      form.querySelectorAll('[data-avito-was-enabled]').forEach(function (button) {
        button.disabled = false;
        delete button.dataset.avitoWasEnabled;
      });
      form.querySelectorAll('.avito-submit-spinner').forEach(function (spinner) { spinner.remove(); });
    });
    const progress = root.querySelector('[data-avito-progress]');
    if (progress) progress.textContent = '';
    restoreSearch();
    if (!event.persisted) revealSelectedDetail();
  });
})();
