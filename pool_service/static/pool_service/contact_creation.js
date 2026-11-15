(function (win) {
  "use strict";
  const doc = win.document;
  const el = (tag, cls, text) => { const node = doc.createElement(tag); if (cls) node.className = cls; if (text != null) node.textContent = text; return node; };
  const button = (text, cls = "customer-button") => { const node = el("button", cls, text); node.type = "button"; return node; };
  const csrf = () => doc.querySelector('input[name="csrfmiddlewaretoken"]')?.value || "";
  async function bootstrap(url) {
    const response = await fetch(url, {credentials:"same-origin", headers:{Accept:"application/json"}});
    const data = await response.json(); if (!response.ok) throw new Error(data.error || "Форма недоступна. Обновите страницу и проверьте доступ.");
    return data.bootstrap;
  }
  function mount(host, config, cancel) {
    let editor, busy = false, dirty = false, kind = config.initial_kind || "contact";
    host.replaceChildren(); host.classList.add("contact-create-shell");
    const title = el("h2", "", kind === "company" ? "Новая компания" : "Новый контакт"); title.id = "contact-create-title";
    host.append(title, el("p", "contact-create-intro", "Заполните основные сведения. Контакт и компанию можно связать сразу. Остальные данные — в карточке после сохранения."));
    const form = el("form"); form.dataset.noLoading = ""; host.append(form);
    const kinds = el("div", "contact-create-types"); kinds.setAttribute("role","group"); kinds.setAttribute("aria-label","Что создать");
    const kindButtons = ["contact","company"].map(value => { const item = button(value === "contact" ? "Контакт" : "Компания", ""); item.dataset.kind = value; item.setAttribute("aria-pressed",String(value === kind)); kinds.append(item); return item; });
    form.append(kinds);
    const organization = el("label", "contact-create-organization"); organization.append(el("span", "", "Организация"));
    const org = el("select"); org.required = true; org.setAttribute("aria-label","Организация");
    const blank = el("option", "", "Выберите организацию"); blank.value = ""; org.append(blank);
    for (const item of config.organizations || []) { const option = el("option", "", item.name); option.value = item.id; org.append(option); }
    org.value = config.organization_id || ""; organization.append(org); form.append(organization);
    if ((config.organizations || []).length === 1) { organization.hidden = true; org.disabled = true; }
    const error = el("div", "contact-create-error"); error.setAttribute("role", "alert"); error.hidden = true; form.append(error);
    const editorHost = el("div"); form.append(editorHost);
    editor = win.Service2CustomerEditor.mount(editorHost, {lookupUrl:config.urls.clients, organizationId:config.organization_id,
      canCategorize:config.can_categorize_contacts, contactMode:kind === "company" ? "none" : "new", companyMode:kind === "company" ? "new" : "none",
      draft: config.customer?.contact?.mode || config.customer?.company?.mode ? config.customer : null,
      contact:config.customer?.contact?.name ? config.customer.contact : null, company:config.customer?.company?.name ? config.customer.company : null});
    editorHost.hidden = !config.organization_id; editor.setEnabled(Boolean(config.organization_id));
    const actions = el("div", "contact-create-actions"), save = button("Сохранить", "customer-button customer-button-primary"), cancelButton = button("Отмена");
    save.type = "submit"; save.dataset.noLoading = ""; save.disabled = !config.organization_id; actions.append(save, cancelButton); form.append(actions);
    const showError = message => { error.textContent = message; error.hidden = !message; if (message) error.scrollIntoView({block:"nearest"}); };
    if (config.errors?.length) showError(config.errors.join("\n"));
    const close = () => { if (busy) return; if (dirty && !win.confirm("Закрыть без сохранения? Введённые данные будут потеряны.")) return; editor.destroy(); cancel(); };
    cancelButton.addEventListener("click", close);
    form.addEventListener("input", () => { dirty = true; }); form.addEventListener("change", () => { dirty = true; });
    kindButtons.forEach(item => item.addEventListener("click", () => {
      if (busy) return; kind = item.dataset.kind; editor.setKind(kind);
      kindButtons.forEach(other => other.setAttribute("aria-pressed",String(other === item)));
      title.textContent = kind === "company" ? "Новая компания" : "Новый контакт"; dirty = true;
    }));
    org.addEventListener("change", async () => {
      const next = org.value;
      if (!next) { org.value = config.organization_id || ""; return; }
      if (dirty && config.organization_id && !win.confirm("Сменить организацию? Введённые данные будут сброшены.")) { org.value = config.organization_id; return; }
      busy = true; org.disabled = save.disabled = cancelButton.disabled = true; editor.setEnabled(false); kindButtons.forEach(item => { item.disabled = true; });
      try {
        const url = new URL(config.urls.create, win.location.origin); url.searchParams.set("org_id",next); url.searchParams.set("return_to",config.return_token || "clients_list"); url.searchParams.set("initial_kind",kind);
        const updated = await bootstrap(url); editor.destroy(); host._creation = mount(host, updated, cancel);
      } catch (failure) { showError(failure.message); org.value = config.organization_id || ""; busy = false; org.disabled = cancelButton.disabled = false; save.disabled = !config.organization_id; editor.setEnabled(Boolean(config.organization_id)); kindButtons.forEach(item => { item.disabled = false; }); }
    });
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (busy || !form.reportValidity()) return;
      let customer; try { customer = editor.value(); } catch (failure) { showError(failure.message); return; }
      busy = true; save.disabled = cancelButton.disabled = org.disabled = true; kindButtons.forEach(item => { item.disabled = true; }); editor.setEnabled(false); save.textContent = "Сохраняем…"; showError("");
      const body = new FormData(); body.set("customer",JSON.stringify(customer)); body.set("org_id",config.organization_id); body.set("return_to",config.return_token || "clients_list"); body.set("initial_kind",kind); body.set("csrfmiddlewaretoken",csrf());
      try {
        const response = await fetch(config.urls.create, {method:"POST",body,credentials:"same-origin",headers:{Accept:"application/json","X-CSRFToken":csrf()}});
        const result = await response.json();
        if (!response.ok) throw new Error(result.errors?.join("\n") || result.error || "Не удалось сохранить. Проверьте данные.");
        const destination = new URL(result.redirect_url || config.return_url, win.location.origin);
        if (destination.origin !== win.location.origin) throw new Error("Контакт сохранён. Откройте справочник контактов.");
        dirty = false; editor.destroy(); win.location.assign(destination.href);
      } catch (failure) { showError(failure instanceof TypeError ? "Не удалось подтвердить сохранение. Проверьте справочник перед повторной попыткой: запись могла сохраниться." : failure.message); }
      finally { busy = false; save.disabled = cancelButton.disabled = false; org.disabled = (config.organizations || []).length === 1; kindButtons.forEach(item => { item.disabled = false; }); editor.setEnabled(true); save.textContent = "Сохранить"; }
    });
    return {close, destroy:()=>editor.destroy()};
  }
  const standalone = doc.getElementById("contact-create-root"), data = doc.getElementById("contact-create-bootstrap");
  if (standalone && data) { const config = JSON.parse(data.textContent); standalone._creation = mount(standalone,config,()=>win.location.assign(config.return_url)); }
  const directory = doc.getElementById("contacts-directory");
  if (directory) {
    let dialog, busy = false;
    doc.addEventListener("click", async event => {
      const link = event.target.closest("a[href]"); if (!link || event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button) return;
      const url = new URL(link.href,win.location.origin); if (url.origin !== win.location.origin || url.pathname !== directory.dataset.contactCreateUrl) return;
      event.preventDefault(); if (busy) return; busy = true;
      try {
        const config = await bootstrap(url);
        if (dialog?.open) return;
        dialog = el("dialog","contact-create-dialog"); dialog.setAttribute("aria-labelledby","contact-create-title");
        const header = el("header"), close = button("×","contact-create-close"), host = el("div"); close.setAttribute("aria-label","Закрыть создание контакта");
        header.append(el("span","","Контакты и компании"),close); dialog.append(header,host); doc.body.append(dialog);
        const teardown = () => { host._creation?.destroy(); dialog.close(); dialog.remove(); link.focus(); };
        host._creation = mount(host,config,teardown);
        close.addEventListener("click",()=>host._creation.close()); dialog.addEventListener("cancel",e=>{e.preventDefault();host._creation.close();});
        dialog.showModal();
        if (win.matchMedia("(max-width:760px)").matches) { const heading = host.querySelector("h2"); heading.tabIndex = -1; heading.focus({preventScroll:true}); }
        else host.querySelector('input:not(:disabled)')?.focus({preventScroll:true});
        dialog.scrollTop = 0;
      } catch (_) { win.location.assign(url.href); }
      finally { busy = false; }
    });
  }
})(window);
