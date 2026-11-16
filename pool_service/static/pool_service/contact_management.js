/* Explicit contact writes; the server rechecks scope and every revision. */
(() => {
  'use strict';
  document.addEventListener('submit', async event => {
    const form = event.target.closest('[data-phone-form]');
    if (!form) return;
    event.preventDefault();
    if (form.dataset.busy) return;
    const data = new FormData(form), error = form.querySelector('[data-phone-error]');
    if (event.submitter?.name === 'delete_phone') {
      if (!confirm('Удалить этот номер из карточки? История звонков сохранится.')) return;
      data.set('operation', 'delete');
    }
    form.dataset.busy = '1'; error.hidden = true;
    form.querySelectorAll('button').forEach(b => b.disabled = true);
    try {
      const response = await fetch(form.action, {method:'POST', body:data, credentials:'same-origin'});
      const result = await response.json();
      if (!response.ok || response.redirected) throw new Error(result.error || 'Не удалось сохранить. Обновите карточку.');
      location.assign(result.url);
    } catch (failure) {
      error.textContent = failure.message || 'Не удалось сохранить. Повторите после обновления страницы.'; error.hidden = false;
      delete form.dataset.busy; form.querySelectorAll('button').forEach(b => b.disabled = false);
    }
  });
})();
