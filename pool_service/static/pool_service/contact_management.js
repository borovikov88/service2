/* Explicit contact writes; the server rechecks scope and every revision. */
(() => {
  'use strict';
  async function savePhone(form, operation) {
    if (form.dataset.busy) return;
    const data = new FormData(form), error = form.querySelector('[data-phone-error]');
    data.set('operation', operation);
    form.dataset.busy = '1'; error.hidden = true;
    const manager = form.closest('[data-phone-manager]');
    manager.querySelectorAll('button').forEach(b => b.disabled = true);
    try {
      const response = await fetch(form.action, {method:'POST', body:data, credentials:'same-origin'});
      const result = await response.json();
      if (!response.ok || response.redirected) throw new Error(result.error || 'Не удалось сохранить. Обновите карточку.');
      location.assign(result.url);
    } catch (failure) {
      delete form.dataset.busy; manager.querySelectorAll('button').forEach(b => b.disabled = false);
      form.crmInlineEditor.open();
      error.textContent = failure.message || 'Не удалось сохранить. Повторите после обновления страницы.'; error.hidden = false;
    }
  }
  for (const row of document.querySelectorAll('[data-phone-row]')) {
    const form = row.querySelector('[data-phone-form]'); if (!form) continue;
    const add = form.elements.operation.value === 'add', number = form.elements.phone.value;
    window.CrmInline.mount(row, {display:row.querySelector('.contact-phone-summary'), form,
      editLabel:add ? 'Добавить телефон' : 'Редактировать номер', saveLabel:add ? 'Добавить номер' : 'Сохранить номер',
      clearLabel:'Удалить номер', clearMessage:'Удалить номер ' + number + ' из карточки? История звонков сохранится.',
      onClear:add ? null : () => savePhone(form, 'delete'),
      busy:() => Boolean(row.closest('[data-phone-manager]').querySelector('[data-busy]')),
      onReset:() => { form.querySelector('[data-phone-error]').hidden = true; }});
    if (add) row.querySelector('.crm-inline-view button').replaceChildren(...window.CrmInline.button('add','Добавить телефон').childNodes);
    form.addEventListener('submit', event => { event.preventDefault(); if(form.reportValidity()) savePhone(form,form.elements.operation.value); });
  }
  for (const row of document.querySelectorAll('[data-client-field]')) {
    const form=row.querySelector('form'), input=form.querySelector('input:not([type=hidden]),textarea,select'), label=row.dataset.fieldLabel;
    const error=form.querySelector('[data-field-error]');
    window.CrmInline.mount(row,{display:row.querySelector('[data-field-display]'),form,editLabel:'Редактировать: '+label,saveLabel:'Сохранить: '+label,
      clearLabel:'Удалить: '+label,clearMessage:'Удалить значение поля «'+label+'»?',
      onClear:row.hasAttribute('data-clearable') ? ()=>{input.value='';form.requestSubmit();} : null,
      busy:()=>Boolean(document.querySelector('[data-client-field] [data-busy]')),
      onReset:()=>{error.hidden=true;}});
    form.addEventListener('submit',async event=>{
      event.preventDefault(); if(form.dataset.busy || !form.reportValidity())return;
      const data=new FormData(form); form.dataset.busy='1'; error.hidden=true;
      document.querySelectorAll('[data-client-field] button').forEach(button=>button.disabled=true);
      try {
        const response=await fetch(form.action,{method:'POST',body:data,credentials:'same-origin',headers:{Accept:'application/json'}});
        if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('Не удалось сохранить. Обновите карточку и проверьте доступ.');
        const result=await response.json(); if(!response.ok || response.redirected || !result.url)throw new Error(result.error || 'Не удалось сохранить.');
        location.assign(result.url);
      }catch(failure){
        delete form.dataset.busy;document.querySelectorAll('[data-client-field] button').forEach(button=>button.disabled=false);
        form.crmInlineEditor.open(); error.textContent=failure.message; error.hidden=false;
      }
    });
  }
})();
