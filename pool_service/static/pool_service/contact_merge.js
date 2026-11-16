(() => {
  'use strict';
  const selected = new Set();
  let dialog = null, previewToken = null, generation = 0, merging = false;
  const root = () => document.getElementById('contacts-directory');
  function node(tag, cls, text) {const el = document.createElement(tag); el.className = cls || ''; if (text != null) el.textContent = text; return el;}
  function sync() {
    document.querySelectorAll('[data-merge-select]').forEach(input => {
      input.checked = selected.has(Number(input.dataset.mergeSelect));
      input.closest('tr').classList.toggle('contact-selected-row', input.checked);
    });
    const bar = document.querySelector('[data-contact-bulk]');
    if (!bar) return;
    bar.hidden = !selected.size;
    bar.querySelector('[data-contact-selected-count]').textContent = `Выбрано: ${selected.size}`;
    bar.querySelector('[data-contact-merge]').disabled = selected.size < 2;
  }
  async function request(data) {
    const response = await fetch(root().dataset.contactMergeUrl, {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json', 'X-CSRFToken':root().querySelector('[name=csrfmiddlewaretoken]').value}, body:JSON.stringify(data)});
    let result;
    try {result = await response.json();} catch {throw new Error('Не удалось получить результат. Обновите страницу и проверьте карточки.');}
    if (!response.ok || response.redirected) throw new Error(result.error || 'Не удалось выполнить объединение.');
    return result;
  }
  function close() {if (merging) return; generation++; previewToken = null; dialog?.close(); dialog?.remove(); dialog = null;}
  async function preview(targetId) {
    const id = ++generation, body = dialog.querySelector('[data-merge-body]'), button = dialog.querySelector('[data-merge-confirm]');
    previewToken = null; button.disabled = true; body.replaceChildren(node('p','text-muted','Проверяем выбранные карточки…'));
    try {
      const result = await request({action:'preview', ids:[...selected], target_id:targetId});
      if (id !== generation || !dialog) return;
      body.replaceChildren(node('p','contact-merge-summary','Выберите основную карточку. Её заполненные поля сохранятся, пустые дополнятся. Телефоны, email, заметки и связи будут собраны вместе.'));
      const cards = node('div','contact-merge-cards'), imported = result.cards.some(card => card.onec);
      result.cards.forEach(card => {
        const label = node('label','contact-merge-card'), radio = document.createElement('input');
        radio.type = 'radio'; radio.name = 'merge_target'; radio.value = card.id; radio.checked = card.id === result.target_id;
        radio.disabled = imported && !card.onec; radio.className = 'form-check-input';
        radio.addEventListener('change', () => preview(card.id));
        const content = node('div'); content.append(node('strong','',card.name + (card.onec ? ' · 1С' : '')));
        content.append(node('small','',[card.phone || 'Без телефона',card.email,card.inn ? `ИНН ${card.inn}` : ''].filter(Boolean).join(' · ')));
        label.append(radio, content); cards.append(label);
      });
      body.append(cards);
      if (imported) body.append(node('p','small text-muted mt-2','Карточка 1С остаётся основной для сохранения связи с учётом.'));
      const summary = node('div','contact-merge-result');
      summary.append(node('strong','','После объединения'));
      summary.append(node('p','small mt-2 mb-1',`Телефоны: ${result.phones.join(', ') || 'не указаны'}`));
      summary.append(node('p','small mb-1',`Email: ${result.emails.join(', ') || 'не указаны'}`));
      summary.append(node('p','small mb-0',`Лиды: ${result.leads} · Сделки: ${result.deals}. История звонков и сообщений сохранится.`));
      body.append(summary, node('p','small text-muted mt-3 mb-0','Остальные карточки исчезнут из каталога, а старые ссылки откроют основную.'));
      previewToken = result.token; button.disabled = false; button.textContent = 'Объединить выбранные';
    } catch (error) {
      if (id !== generation || !dialog) return;
      const retry = node('button','btn btn-outline-primary btn-sm','Обновить предпросмотр'); retry.type = 'button'; retry.addEventListener('click', () => preview(targetId));
      const message = node('p','text-danger',error.message); message.setAttribute('role','alert'); body.replaceChildren(message,retry);
    }
  }
  function open() {
    close(); dialog = node('dialog','contact-merge-dialog'); dialog.setAttribute('aria-labelledby','contact-merge-title');
    const head=node('div','contact-merge-head'), title=node('h5','mb-0 fw-bold','Объединение карточек'), cross=node('button','btn-close');
    title.id='contact-merge-title'; cross.type='button'; cross.setAttribute('aria-label','Закрыть'); cross.addEventListener('click',close); head.append(title,cross);
    const body=node('div','contact-merge-body'); body.dataset.mergeBody='';
    const foot=node('div','contact-merge-foot'), cancel=node('button','btn btn-outline-secondary','Отмена'), confirm=node('button','btn btn-primary','Объединить выбранные');
    cancel.type=confirm.type='button'; cancel.addEventListener('click',close); confirm.dataset.mergeConfirm=''; confirm.disabled=true;
    confirm.addEventListener('click',async () => {
      if (!previewToken || merging) return;
      merging=true; confirm.disabled=cancel.disabled=cross.disabled=true;
      try {const result=await request({action:'merge',token:previewToken}); location.assign(result.url);}
      catch(error) {merging=false; cancel.disabled=cross.disabled=false; confirm.disabled=false; confirm.textContent='Проверить результат';
        const message=node('p','text-danger mt-3',error.message); message.setAttribute('role','alert'); body.append(message);
        const retry=node('button','btn btn-outline-primary btn-sm','Проверить заново'); retry.addEventListener('click',()=>preview()); body.append(retry);}
    });
    foot.append(cancel,confirm); dialog.append(head,body,foot); dialog.addEventListener('cancel', event=>{event.preventDefault();close();});
    document.body.append(dialog); dialog.showModal(); preview();
  }
  document.addEventListener('change',event=>{
    const input=event.target.closest('[data-merge-select]'); if(!input) return;
    const id=Number(input.dataset.mergeSelect);
    if(input.checked && selected.size<20) selected.add(id); else selected.delete(id);
    if(input.checked && !selected.has(id)) alert('За один раз можно объединить до 20 карточек.');
    sync();
  });
  document.addEventListener('click',event=>{
    if(event.target.closest('[data-contact-clear]')) {selected.clear();sync();}
    if(event.target.closest('[data-contact-select-page]')) {
      root().querySelectorAll('.tab-pane.active [data-merge-select]').forEach(input=>{if(selected.size<20) selected.add(Number(input.dataset.mergeSelect));});sync();
    }
    if(event.target.closest('[data-contact-merge]') && selected.size>=2) open();
  });
  document.addEventListener('contacts:loaded',sync); document.addEventListener('DOMContentLoaded',sync);
})();
