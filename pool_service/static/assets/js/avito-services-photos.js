(() => {
  'use strict';
  const root = document.querySelector('[data-avito-photos]');
  if (!root) return;
  const form = root.closest('form'), textarea = form.querySelector('[name="image_urls"]');
  const upload = root.dataset.uploadUrl, list = root.querySelector('[data-photo-list]');
  const message = root.querySelector('[data-photo-error]'), files = root.querySelector('[data-photo-files]');
  const link = root.querySelector('[data-photo-url]'), add = root.querySelector('[data-photo-add]');
  const token = form.querySelector('[name="csrfmiddlewaretoken"]').value;
  const parsed = JSON.parse(document.getElementById('avito-photo-initial').textContent);
  const initial = Array.isArray(parsed) ? parsed : [];
  const previews = new Map(initial.map(item => [item.url, item.preview_url]));
  let rows = textarea.value.split(/[|\r\n]+/).map(value => value.trim()).filter(Boolean)
    .map(url => ({ url, preview_url: previews.get(url) || '' })), pending = 0;
  const error = text => { message.textContent = text || ''; message.hidden = !text; };
  const sync = () => { textarea.value = rows.map(item => item.url).join('\n'); };
  function render() {
    files.disabled = pending > 0; add.disabled = pending > 0; link.disabled = pending > 0;
    list.replaceChildren();
    rows.forEach((item, index) => {
      const card = document.createElement('li'); card.className = 'avito-photo-card';
      const image = document.createElement('img'); image.alt = 'Фото ' + (index + 1);
      image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
      if (item.preview_url) image.src = item.preview_url;
      else { image.hidden = true; const note = document.createElement('p'); note.textContent = 'Проверить ссылку для превью'; card.append(note); }
      image.addEventListener('error', () => { image.hidden = true; });
      card.append(image);
      const title = document.createElement('strong'); title.textContent = index === 0 ? 'Обложка' : 'Фото ' + (index + 1); card.append(title);
      const controls = document.createElement('div'); controls.className = 'avito-photo-controls';
      function button(label, run, disabled = false) {
        const element = document.createElement('button'); element.type = 'button';
        element.className = 'btn btn-sm btn-outline-secondary'; element.textContent = label;
        element.disabled = disabled || pending > 0; element.addEventListener('click', run); controls.append(element);
      }
      button('Обложка', () => { rows.splice(index, 1); rows.unshift(item); sync(); render(); }, index === 0);
      button('←', () => { [rows[index - 1], rows[index]] = [rows[index], rows[index - 1]]; sync(); render(); }, index === 0);
      button('→', () => { [rows[index + 1], rows[index]] = [rows[index], rows[index + 1]]; sync(); render(); }, index === rows.length - 1);
      if (!item.preview_url) button('Проверить', async () => {
        const photo = await send(null, item.url);
        if (photo) { item.preview_url = photo.preview_url; previews.set(item.url, photo.preview_url); render(); }
      });
      button('Убрать', () => { rows.splice(index, 1); sync(); render(); });
      card.append(controls); list.append(card);
    });
    root.querySelector('[data-photo-count]').textContent = rows.length + ' / 10';
  }
  async function send(file, url) {
    if (!upload) { error('Обновите страницу перед загрузкой фотографий.'); return null; }
    pending += 1; error(''); render();
    const data = new FormData(); data.append('revision', form.querySelector('[name="revision"]').value);
    data.append('avito_id', root.dataset.avitoId);
    if (file) data.append('photos', file); else data.append('image_url', url);
    try {
      const response = await fetch(upload, { method: 'POST', credentials: 'same-origin',
        headers: { 'X-CSRFToken': token }, body: data });
      let result;
      try { result = await response.json(); } catch { throw new Error('Не удалось проверить фотографию. Обновите страницу и повторите.'); }
      if (!response.ok) throw new Error(result.error || 'Не удалось загрузить фотографию.');
      return result.images[0];
    } catch (problem) { error(problem.message); return null; }
    finally { pending -= 1; render(); }
  }
  files.addEventListener('change', async () => {
    const selected = Array.from(files.files); files.value = '';
    if (selected.length + rows.length > 10) { error('Можно использовать максимум 10 фотографий.'); return; }
    for (const file of selected) {
      if (rows.length >= 10) break;
      const photo = await send(file, null);
      if (photo && !rows.some(item => item.url === photo.url)) rows.push(photo);
    }
    sync(); render();
  });
  add.addEventListener('click', async () => {
    if (rows.length >= 10) { error('Можно использовать максимум 10 фотографий.'); return; }
    const value = link.value.trim(); if (!value) return;
    const photo = await send(null, value);
    if (photo && !rows.some(item => item.url === photo.url)) { rows.push(photo); link.value = ''; sync(); render(); }
  });
  textarea.addEventListener('change', () => {
    const urls = textarea.value.split(/[|\r\n]+/).map(value => value.trim()).filter(Boolean);
    rows = urls.map(url => ({ url, preview_url: previews.get(url) || '' }));
    sync(); render();
    if (urls.length > 10) error('Можно использовать максимум 10 фотографий.');
  });
  form.addEventListener('submit', event => {
    if (pending || rows.length < 1 || rows.length > 10) {
      event.preventDefault(); error(pending ? 'Дождитесь загрузки фотографий.' : 'Укажите от 1 до 10 фотографий.');
    } else sync();
  });
  render();
})();
