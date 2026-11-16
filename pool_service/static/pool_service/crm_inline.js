/* Compact field editing; persistence and revision checks belong to each card. */
(() => {
  'use strict';
  const paths = {edit:'m15 5 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L5 16z',
    save:'m5 12 4 4L19 6', cancel:'m6 6 12 12M18 6 6 18', add:'M12 5v14M5 12h14'};
  function button(kind, label) {
    const node = document.createElement('button'); node.type = 'button';
    node.className = 'crm-inline-button is-' + kind; node.dataset.noLoading = '';
    node.title = label; node.setAttribute('aria-label', label);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [key, value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true'})) svg.setAttribute(key,value);
    const path = document.createElementNS(svg.namespaceURI,'path'); path.setAttribute('d',paths[kind] || paths.cancel);
    svg.append(path); node.append(svg); return node;
  }
  function confirmRemoval(message, label = 'Удалить') {
    return new Promise(resolve => {
      const previous = document.activeElement, dialog = document.createElement('dialog');
      dialog.className = 'crm-inline-confirm'; dialog.setAttribute('aria-label', 'Подтверждение удаления');
      const title = document.createElement('h3'); title.textContent = 'Подтвердите удаление';
      const text = document.createElement('p'); text.textContent = message;
      const actions = document.createElement('div'); actions.className = 'crm-inline-confirm-actions';
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = 'Отменить';
      const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = label; remove.className = 'is-danger';
      for (const node of [cancel,remove]) node.dataset.noLoading = '';
      let answer = false;
      cancel.addEventListener('click', () => dialog.close());
      remove.addEventListener('click', () => { answer = true; dialog.close(); });
      dialog.addEventListener('close', () => { dialog.remove(); if(previous?.isConnected) previous.focus(); resolve(answer); }, {once:true});
      actions.append(cancel,remove); dialog.append(title,text,actions); document.body.append(dialog); dialog.showModal(); cancel.focus();
    });
  }
  function mount(host, {display, form, editLabel = 'Редактировать', saveLabel = 'Сохранить', clearLabel, clearMessage, onClear, busy = () => false, onOpen, onReset}) {
    host.classList.add('crm-inline');
    const view = document.createElement('div'); view.className = 'crm-inline-view';
    display.classList.add('crm-inline-value'); view.append(display);
    const actions = document.createElement('div'); actions.className = 'crm-inline-actions'; view.append(actions);
    const edit = button('edit', editLabel); actions.append(edit);
    form.classList.add('crm-inline-form'); form.hidden = true;
    const initial = [...form.querySelectorAll('input,select,textarea')].map(input => [input,input.value,input.checked]);
    const locked = () => busy() || form.dataset.busy === '1' || save.disabled;
    function reset() { for(const [input,value,checked] of initial) {input.value=value; input.checked=checked;} onReset?.(); }
    function open() { if(locked()) return; form.hidden=false; view.hidden=true; onOpen?.(); form.querySelector('input:not([type=hidden]),select,textarea')?.focus(); }
    function close() { if(locked()) return; reset(); form.hidden=true; view.hidden=false; edit.focus(); }
    edit.addEventListener('click', open);
    if(onClear) {
      const clear = button('delete', clearLabel || 'Удалить значение'); actions.append(clear);
      clear.addEventListener('click', async () => {
        if(locked()) return;
        if(await confirmRemoval(clearMessage || 'Удалить значение этого поля?')) {
          if(host.isConnected && !locked()) { open(); onClear(); }
        }
      });
    }
    const footer = document.createElement('div'); footer.className='crm-inline-actions crm-inline-controls';
    const save = button('save', saveLabel); save.type='submit';
    const cancel = button('cancel','Отменить редактирование'); footer.append(save,cancel); form.append(footer);
    cancel.addEventListener('click', close);
    form.addEventListener('keydown', event => {
      if(event.key==='Escape') { event.preventDefault(); event.stopPropagation(); close(); }
    });
    form.addEventListener('invalid', () => {form.hidden=false; view.hidden=true;}, true);
    host.append(view,form);
    const editor = {open, close, reset}; form.crmInlineEditor = editor; return editor;
  }
  window.CrmInline = {button, confirmRemoval, mount};
})();
