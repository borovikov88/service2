/* One contact/company editor; callers own the transaction and saving context. */
(function (win) {
  "use strict";
  const categories = [["", "Клиент"], ["personal", "Личный"], ["internal", "Внутренний"], ["landlord", "Арендодатель"], ["supplier", "Поставщик"], ["partner", "Партнёр"], ["other", "Другой"]];
  function mount(host, options = {}) {
    const doc = host.ownerDocument;
    const node = (tag, cls, text) => { const item = doc.createElement(tag); if (cls) item.className = cls; if (text != null) item.textContent = text; return item; };
    const root = node("div", "crm-customer-editor lead-customer-grid"); host.append(root);
    const fields = {}, cleanups = [];
    let disposed = false;
    function changed() { options.onChange?.(primarySelection()); }
    function field(parent, label, type = "text", value = "", required = false, max = 255) {
      const wrap = node("label", "customer-field lead-field"), input = node(type === "select" ? "select" : "input");
      wrap.append(node("span", "customer-field-label", label), input); parent.append(wrap);
      if (type !== "select") { input.type = type; input.maxLength = max; }
      input.value = value || ""; input.required = required;
      if (type === "tel") { input.autocomplete = "tel"; input.inputMode = "tel"; input.dataset.crmPhone = "true"; }
      if (type === "email") input.autocomplete = "email";
      return {input, wrap};
    }
    function select(parent, label, choices, value) {
      const result = field(parent, label, "select");
      for (const [key, text] of choices) { const option = node("option", "", text); option.value = key; result.input.append(option); }
      result.input.value = value; return result;
    }
    function visibility(parent, visible) {
      parent.hidden = !visible;
      parent.querySelectorAll("input,select,textarea").forEach(input => { input.disabled = !visible; });
    }
    function search(query, kind, signal) {
      const url = new URL(options.lookupUrl, win.location.origin);
      url.searchParams.set("q", query); url.searchParams.set("kind", kind === "contact" ? "private" : "legal");
      if (options.organizationId) url.searchParams.set("org_id", options.organizationId);
      return fetch(url, {signal, credentials: "same-origin", headers: {Accept: "application/json"}}).then(async response => {
        const data = await response.json(); if (!response.ok) throw new Error(data.error || "Не удалось проверить справочник. Повторите поиск.");
        return data.clients || [];
      });
    }
    function makeIdentity(kind) {
      const person = kind === "contact", caption = person ? "Контакт" : "Компания";
      const initial = options[kind] || null, draft = options.draft?.[kind];
      const box = node("fieldset", "customer-box lead-customer-box"); box.dataset.customerKind = kind;
      box.append(node("legend", "", caption)); root.append(box);
      const defaults = options.onlyKind ? "new" : initial?.id ? "existing" : person ? options.contactMode || "existing" : options.companyMode || "none";
      const mode = select(box, caption, [["existing", "Выбрать существующую" + (person ? " запись" : "")], ["new", person ? "Создать контакт" : "Создать компанию"], ["none", person ? "Без контактного лица" : "Без компании"]], draft?.mode || defaults);
      if (person) mode.input.options[0].textContent = "Выбрать существующий";
      let selected = draft?.selected || (initial?.id ? initial : null);
      if (options.onlyKind) { mode.wrap.hidden = true; mode.input.value = "new"; }
      const existing = node("div", "customer-existing"), fresh = node("div", "customer-fields lead-fields"), empty = node("p", "customer-note", person ? "Контакт можно добавить позже, если указана компания." : "Для частного клиента достаточно контакта.");
      box.append(existing, fresh, empty);
      const lookup = field(existing, person ? "Найти контакт" : "Найти компанию", "search", selected?.name || "");
      lookup.input.autocomplete = "off"; lookup.input.placeholder = "Имя, название, телефон или email";
      const selection = node("p", "customer-selection lead-picker-selection"), results = node("div", "customer-results lead-picker-results");
      results.setAttribute("aria-live", "polite"); existing.append(selection, results);
      const values = draft?.values || initial || {};
      const name = field(fresh, person ? "Имя контакта" : "Название компании", "text", values.name, true); name.wrap.classList.add("wide");
      name.input.autocomplete = person ? "name" : "organization";
      const phone = field(fresh, person ? "Телефон" : "Телефон компании", "tel", options.lockedPhone ?? values.phone, false, 40);
      if (options.lockedPhone != null) { phone.input.readOnly = true; phone.wrap.append(node("small", "customer-note", "Номер из звонка")); }
      const email = field(fresh, person ? "Email" : "Email компании", "email", values.email, false, 254);
      const inn = person ? null : field(fresh, "ИНН", "text", values.inn, false, 12);
      if (inn) { inn.input.inputMode = "numeric"; inn.input.pattern = "(?:[0-9]{10}|[0-9]{12})"; inn.input.title = "10 или 12 цифр"; }
      const category = person ? select(fresh, "Категория контакта", categories, values.category || "") : null;
      const categoryNote = node("p", "customer-note wide", "Категория «Личный» не меняет права доступа к записи.");
      if (category) { category.wrap.hidden = !options.canCategorize; fresh.append(categoryNote); }
      const matches = node("div", "customer-matches"); matches.setAttribute("aria-live", "polite"); fresh.append(matches);
      const showSelected = () => { selection.textContent = selected ? "Выбрано: " + selected.name : "Введите не менее 3 символов и выберите запись."; };
      const toggle = (silent = false) => {
        visibility(existing, mode.input.value === "existing"); visibility(fresh, mode.input.value === "new"); empty.hidden = mode.input.value !== "none";
        if (category) { category.input.disabled = mode.input.value !== "new" || !options.canCategorize; categoryNote.hidden = !options.canCategorize || category.input.value !== "personal"; }
        showSelected(); if (!silent) changed();
      };
      const choose = item => { selected = item; lookup.input.value = item.name; mode.input.value = "existing"; results.replaceChildren(); matches.replaceChildren(); toggle(); };
      const resultButton = item => {
        const button = node("button", "customer-result", [item.name, item.phone, item.email].filter(Boolean).join(" · "));
        button.type = "button"; button.addEventListener("click", () => choose(item)); return button;
      };
      let timer, controller, generation = 0;
      function schedule(input, duplicateCheck) {
        win.clearTimeout(timer); controller?.abort(); const mine = ++generation;
        const destination = duplicateCheck ? matches : results; destination.replaceChildren();
        if (!duplicateCheck) { selected = null; showSelected(); changed(); }
        const query = input.value.trim();
        if (!options.lookupUrl || query.length < 3) return;
        timer = win.setTimeout(async () => {
          if (disposed || !root.isConnected) return;
          controller = new AbortController();
          try {
            const rows = await search(query, kind, controller.signal);
            if (disposed || !root.isConnected || mine !== generation) return;
            destination.replaceChildren();
            if (duplicateCheck && rows.length) destination.append(node("p", "customer-note", "Похожие записи уже есть. Выберите подходящую, чтобы не создавать дубль."));
            rows.forEach(item => destination.append(resultButton(item)));
            if (!duplicateCheck && !rows.length) destination.append(node("p", "customer-note", "Совпадений нет. Выберите «" + (person ? "Создать контакт" : "Создать компанию") + "»."));
          } catch (error) { if (error.name !== "AbortError" && mine === generation && root.isConnected) destination.replaceChildren(node("p", "customer-note", error.message)); }
        }, 300);
      }
      lookup.input.addEventListener("input", () => schedule(lookup.input, false));
      [name, phone, email, inn].filter(Boolean).forEach(item => item.input.addEventListener("input", () => schedule(item.input, true)));
      mode.input.addEventListener("change", () => { ++generation; controller?.abort(); win.clearTimeout(timer); results.replaceChildren(); matches.replaceChildren(); toggle(); });
      category?.input.addEventListener("change", () => { categoryNote.hidden = category.input.value !== "personal"; });
      const raw = () => ({name: name.input.value.trim(), phone: phone.input.value.trim(), email: email.input.value.trim(), ...(person ? {category: options.canCategorize ? category.input.value : ""} : {inn: inn.input.value.trim()})});
      const entry = {box, mode, selected: () => selected, raw, toggle, choose, getDraft: () => ({mode: mode.input.value, selected, values: raw()}), value: () => {
        if (mode.input.value === "none") return null;
        if (mode.input.value === "existing") { if (!selected) throw new Error(person ? "Выберите контакт из результатов поиска или создайте новый." : "Выберите компанию из результатов поиска или создайте новую."); return {id: selected.id}; }
        return raw();
      }};
      fields[kind] = entry;
      cleanups.push(() => { win.clearTimeout(timer); controller?.abort(); });
      return entry;
    }
    function primarySelection() {
      const company = fields.company, person = fields.contact;
      if (company && company.mode.input.value !== "none") return company.mode.input.value === "existing" ? company.selected() : null;
      return person?.mode.input.value === "existing" ? person.selected() : null;
    }
    if (options.onlyKind) makeIdentity(options.onlyKind);
    else if (options.companyMode === "new" && options.contactMode === "none") { makeIdentity("company"); makeIdentity("contact"); }
    else { makeIdentity("contact"); makeIdentity("company"); }
    if (options.onlyKind) root.classList.add("customer-single");
    Object.values(fields).forEach(entry => entry.toggle());
    return {
      element: root,
      value() { const value = {contact: fields.contact?.value() || null, company: fields.company?.value() || null}; if (!value.contact && !value.company) throw new Error("Выберите или создайте контакт либо компанию."); return value; },
      draft: () => Object.fromEntries(Object.entries(fields).map(([key, entry]) => [key, entry.getDraft()])),
      primarySelection,
      setModes(rules) {
        for (const [kind, entry] of Object.entries(fields)) {
          const {allowed, fallback} = rules[kind];
          for (const option of entry.mode.input.options) {
            option.disabled = !allowed.includes(option.value);
            option.hidden = option.disabled;
          }
          if (!allowed.includes(entry.mode.input.value)) entry.mode.input.value = fallback;
          entry.toggle(true);
        }
        changed();
      },
      setEnabled(enabled) { if (enabled) Object.values(fields).forEach(entry => entry.toggle(true)); else root.querySelectorAll("input,select,textarea").forEach(input => { input.disabled = true; }); },
      setKind(kind) { fields.contact.mode.input.value = kind === "company" ? "none" : "new"; fields.company.mode.input.value = kind === "company" ? "new" : "none"; root.append(fields[kind].box, fields[kind === "company" ? "contact" : "company"].box); Object.values(fields).forEach(entry => entry.toggle()); },
      destroy() { disposed = true; cleanups.forEach(fn => fn()); }
    };
  }
  win.Service2CustomerEditor = {mount};
})(window);
