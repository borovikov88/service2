(function (factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => api.mount(document));
    else api.mount(document);
  }
})(function () {
  "use strict";
  const freeze = (value) => {
    if (value && typeof value === "object") { Object.values(value).forEach(freeze); Object.freeze(value); }
    return value;
  };
  // Pending writes belong to this page, not to a drawer. A lost response keeps
  // its immutable payload and key even after a later denial or navigation.
  class LeadOperations {
    constructor(transport, changed = () => {}) { this.transport = transport; this.changed = changed; this.entries = new Map(); }
    list() { return Array.from(this.entries.values()); }
    pending() { return this.list().filter((item) => ["ready", "sending", "uncertain"].includes(item.status)); }
    forSubject(id) { return this.pending().find((item) => item.payload.subject_id === id); }
    add(payload, label) {
      if (this.entries.has(payload.operation_key)) return this.entries.get(payload.operation_key);
      if (this.forSubject(payload.subject_id)) throw new Error("Сначала проверьте предыдущее сохранение этой карточки.");
      const item = {payload: freeze(JSON.parse(JSON.stringify(payload))), label, status: "ready", everUncertain: false, running: null};
      this.entries.set(payload.operation_key, item); this.changed(); return item;
    }
    async run(key) {
      const item = this.entries.get(key);
      if (!item) throw new Error("Операция не найдена.");
      if (item.running) return item.running;
      if (!["ready", "uncertain"].includes(item.status)) return item;
      item.status = "sending"; this.changed();
      item.running = Promise.resolve().then(async () => {
        try { item.response = await this.transport(item.payload); item.status = "succeeded"; }
        catch (error) { item.error = error; item.status = error.definitive && !item.everUncertain ? "failed" : "uncertain"; if (item.status === "uncertain") item.everUncertain = true; }
        finally { item.running = null; this.changed(); }
        return item;
      });
      return item.running;
    }
    dismiss(key) {
      const item = this.entries.get(key);
      if (!item || !["failed", "succeeded"].includes(item.status)) return false;
      this.entries.delete(key); this.changed(); return true;
    }
  }
  function localDateTime(value, zone) {
    if (!value) return "";
    const date = new Date(value); if (!Number.isFinite(date.getTime())) return "";
    const parts = new Intl.DateTimeFormat("en-CA", {timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).formatToParts(date);
    const part = (type) => parts.find((item) => item.type === type).value;
    return `${part("year")}-${part("month")}-${part("day")}T${part("hour")}:${part("minute")}`;
  }
  function deadlineISO(value, offset) {
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value || "") || !/^[+-]\d{2}:\d{2}$/.test(offset || "")) throw new Error("Укажите дату и время следующего шага.");
    const calendar = new Date(value + ":00Z");
    if (!Number.isFinite(calendar.getTime()) || calendar.toISOString().slice(0, 16) !== value) throw new Error("Некорректная дата.");
    const iso = value + ":00" + offset;
    if (!Number.isFinite(new Date(iso).getTime())) throw new Error("Некорректная дата.");
    return iso;
  }
  function mount(doc) {
    const root = doc.getElementById("crm-leads"); if (!root) return;
    const win = doc.defaultView;
    const initial = JSON.parse(doc.getElementById("lead-bootstrap").textContent);
    const byId = (id) => doc.getElementById(id);
    const dialog = byId("lead-dialog"), content = byId("lead-dialog-content");
    const state = {data: initial, q: initial.q, mode: initial.mode, status: initial.status, page: initial.page,
      selected: null, detail: null, loading: false, sequence: 0, detailSequence: 0, listController: null};
    const el = (tag, cls, text) => { const node = doc.createElement(tag); if (cls) node.className = cls; if (text != null) node.textContent = text; return node; };
    const button = (text, action, primary = false) => { const node = el("button", "crm-live-button" + (primary ? " crm-live-button-primary" : ""), text); node.type = "button"; if (action) node.addEventListener("click", action); return node; };
    const dateText = (value) => value ? new Intl.DateTimeFormat("ru-RU", {timeZone: initial.timezone, dateStyle: "medium", timeStyle: "short"}).format(new Date(value)) : "Срок не указан";
    const notice = (text) => { byId("lead-notice").textContent = text; byId("lead-notice").hidden = !text; };
    const errorNode = (form, error) => {
      let node = form.querySelector(".lead-error"); if (!node) { node = el("p", "lead-error"); node.setAttribute("role", "alert"); form.prepend(node); }
      node.textContent = error.message || "Не удалось выполнить действие.";
    };
    function urlState() {
      const url = new URL(win.location.href);
      for (const [key, value, base] of [["q", state.q, ""], ["mode", state.mode, "unassigned"], ["status", state.status, ""], ["page", String(state.page), "1"]]) {
        if (value && value !== base) url.searchParams.set(key, value); else url.searchParams.delete(key);
      }
      if (state.selected) url.searchParams.set("lead", state.selected); else url.searchParams.delete("lead");
      win.history.replaceState({}, "", url);
    }
    async function read(url, signal) {
      const response = await win.fetch(url, {credentials: "same-origin", headers: {Accept: "application/json"}, signal});
      let body; try { body = await response.json(); } catch (_error) { throw new Error("Ответ сервера недоступен. Обновите данные."); }
      if (!response.ok) throw new Error(body.error || "Не удалось загрузить данные.");
      return body;
    }
    const csrf = () => root.querySelector("[name=csrfmiddlewaretoken]").value;
    const operations = new LeadOperations(async (payload) => {
      const controller = new AbortController(), timer = win.setTimeout(() => controller.abort(), 20000);
      try {
        const response = await win.fetch(initial.urls.command, {method: "POST", credentials: "same-origin", signal: controller.signal,
          headers: {"Content-Type": "application/json", "X-CSRFToken": csrf(), Accept: "application/json"}, body: JSON.stringify(payload)});
        let body; try { body = await response.json(); } catch (_error) { throw new Error("Результат сохранения неизвестен. Проверьте эту же операцию."); }
        if (!response.ok) { const error = new Error(body.error || "Не удалось сохранить."); error.code = body.code; error.definitive = response.status >= 400 && response.status < 500 && ![408, 429].includes(response.status); throw error; }
        if (!body.result || !body.lead || body.result.lead_id !== body.lead.id || !Number.isInteger(body.lead.revision)) throw new Error("Сервер не подтвердил результат. Проверьте эту же операцию.");
        return body;
      } finally { win.clearTimeout(timer); }
    }, renderOperations);
    function renderOperations() {
      const host = byId("lead-operations"); host.replaceChildren();
      for (const item of operations.list()) {
        const row = el("div", "lead-operation"), actions = el("div", "lead-operation-actions");
        const message = item.status === "sending" ? "Сохраняем…" : item.status === "uncertain" ? "Ответ не получен. Результат пока неизвестен." : item.status === "failed" ? item.error.message : item.status === "succeeded" ? "Сохранено" : "Ожидает сохранения";
        row.append(el("span", "", item.label + ". " + message));
        if (item.status === "uncertain") actions.append(button("Проверить результат", async () => applyResult(await operations.run(item.payload.operation_key))));
        if (["failed", "succeeded"].includes(item.status)) actions.append(button("Убрать", () => operations.dismiss(item.payload.operation_key)));
        row.append(actions); host.append(row);
      }
      const pending = operations.forSubject(state.selected);
      content.querySelectorAll("button[type=submit]").forEach((node) => { node.disabled = Boolean(pending); });
      byId("lead-create").disabled = Boolean(operations.forSubject(null));
    }
    function applyResult(item) {
      if (item.status === "succeeded") {
        if (state.selected === item.payload.subject_id || state.selected === null && item.payload.subject_id === null) {
          state.selected = item.response.lead.id; state.detail = item.response; renderDetail(item.response); dialog.scrollTop = 0; urlState();
        }
        notice("Изменения сохранены."); operations.dismiss(item.payload.operation_key); loadList();
      } else if (item.status === "uncertain") notice("Результат сохранения неизвестен. Используйте «Проверить результат» — операция будет проверена с прежним ключом.");
      return item;
    }
    async function submit(form, kind, lead, changes) {
      if (operations.forSubject(lead?.id ?? null)) { errorNode(form, new Error("Сначала проверьте результат предыдущего сохранения.")); return; }
      try {
        const payload = {operation_key: win.crypto.randomUUID(), command_type: kind,
          subject_id: lead?.id ?? null, expected_revision: lead?.revision ?? null, changes};
        const item = operations.add(payload, kind === "lead.create" ? "Новый лид" : "Лид №" + lead.id);
        const result = applyResult(await operations.run(item.payload.operation_key));
        if (result.status === "failed") {
          errorNode(form, result.error);
          if (result.error.code === "conflict") {
            const warning = el("div", "lead-conflict", "Введённые значения остаются в форме. Загрузите актуальную карточку перед новой правкой; это заменит содержимое формы.");
            warning.append(button("Загрузить актуальную карточку", () => openLead(lead.id))); form.prepend(warning);
          }
        }
      } catch (error) { errorNode(form, error); }
    }
    function renderList() {
      const data = state.data, list = byId("lead-list"); list.replaceChildren(); list.setAttribute("aria-busy", String(state.loading));
      byId("lead-count").textContent = state.loading ? "Обновляем…" : "Найдено: " + data.total;
      byId("lead-page").textContent = `${data.page} / ${data.pages}`;
      byId("lead-prev").disabled = state.loading || data.page <= 1; byId("lead-next").disabled = state.loading || data.page >= data.pages;
      byId("lead-create").hidden = !data.can_create;
      const intake = data.intake || {};
      byId("lead-intake-status").textContent = "Приём обращений: " + (!intake.enabled ? "выключен" : intake.needs_attention ? "требует проверки" : !intake.last_run_at ? "ожидает первого запуска" : "включён") + (intake.last_run_at ? " · Последний запуск: " + dateText(intake.last_run_at) : "");
      root.querySelectorAll("[data-mode]").forEach((node) => { if (node.dataset.mode === state.mode) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current"); });
      if (!data.leads.length) list.append(el("div", "lead-empty", state.q || state.status ? "По этим условиям лидов нет. Измените поиск или фильтры." : "В этом представлении пока нет обращений."));
      for (const lead of data.leads) {
        const row = el("article", "lead-row"), title = el("div"), middle = el("div"), next = el("div");
        const open = button(lead.title, () => openLead(lead.id)); title.append(open);
        title.append(el("p", "", lead.client?.name || "Клиент пока не определён"));
        middle.append(el("span", "lead-state", lead.status_label), el("p", "", lead.assignee?.name || "Не назначен"));
        if (lead.intake_reason_label) middle.append(el("p", "", lead.intake_reason_label));
        if (lead.needs_review) middle.append(el("span", "lead-state review", "Новое обращение · нужен разбор"));
        const action = lead.next_action;
        next.append(el("p", "", action?.restricted ? "Следующий шаг ограничен доступом" : action?.completed ? "Предыдущий шаг завершён" : action?.title || (lead.can_take ? "Можно взять в работу" : "Следующий шаг не указан")));
        if (action?.due_at && !action.restricted) next.append(el("p", "lead-date", dateText(action.due_at)));
        row.append(title, middle, next); list.append(row);
      }
      renderOperations();
    }
    async function loadList() {
      if (state.listController) state.listController.abort();
      const controller = new AbortController(), sequence = ++state.sequence; state.listController = controller; state.loading = true; renderList(); urlState();
      const url = new URL(initial.urls.data, win.location.origin);
      for (const [key, value] of Object.entries({q: state.q, mode: state.mode, status: state.status, page: state.page})) url.searchParams.set(key, value);
      try { const data = await read(url, controller.signal); if (sequence !== state.sequence) return; state.data = data; state.page = data.page; urlState(); }
      catch (error) { if (error.name !== "AbortError" && sequence === state.sequence) notice(error.message); }
      finally { if (sequence === state.sequence) { state.loading = false; renderList(); } }
    }
    function field(host, label, value = "", type = "text", wide = false) {
      const wrap = el("label", "lead-field" + (wide ? " wide" : "")), input = el(type === "textarea" ? "textarea" : "input");
      wrap.append(el("span", "", label)); if (type !== "textarea") input.type = type; input.value = value || "";
      wrap.append(input); host.append(wrap); return input;
    }
    function select(host, label, choices, value = "") {
      const wrap = el("label", "lead-field"), input = el("select"); wrap.append(el("span", "", label));
      for (const [id, text] of choices) { const option = el("option", "", text); option.value = id; input.append(option); } input.value = value;
      wrap.append(input); host.append(wrap); return input;
    }
    function picker(host, label, endpoint, collection, initialValue, kind) {
      const wrap = el("div", "lead-field wide"), inputLabel = el("label"), input = el("input"), results = el("div", "lead-picker-results"), chosen = el("div", "lead-picker-selection");
      let selected = initialValue || null, timer, controller, sequence = 0;
      input.type = "search"; input.autocomplete = "off"; input.placeholder = "Начните вводить: не менее 3 символов";
      input.value = selected?.name || selected?.title || ""; chosen.textContent = selected ? "Выбрано: " + input.value : "Не выбрано";
      inputLabel.append(el("span", "", label), input); wrap.append(inputLabel, chosen, results); host.append(wrap);
      input.addEventListener("input", () => {
        selected = null; chosen.textContent = "Не выбрано"; results.replaceChildren(); win.clearTimeout(timer); if (controller) controller.abort(); const current = ++sequence;
        timer = win.setTimeout(async () => {
          if (input.value.trim().length < 3) return;
          controller = new AbortController(); const url = new URL(endpoint, win.location.origin); url.searchParams.set("q", input.value.trim()); if (kind) url.searchParams.set("kind", kind());
          try { const data = await read(url, controller.signal); if (sequence !== current) return; results.replaceChildren();
            for (const row of data[collection]) results.append(button(row.name || row.title, () => { selected = row; input.value = row.name || row.title; chosen.textContent = "Выбрано: " + input.value; results.replaceChildren(); }));
            if (!data[collection].length) results.append(el("span", "crm-live-muted", "Совпадений нет"));
          } catch (error) { if (error.name !== "AbortError" && sequence === current) chosen.textContent = error.message; }
        }, 300);
      });
      return {element: wrap, value: () => selected?.id ?? null, clear: () => { selected = null; input.value = ""; chosen.textContent = "Не выбрано"; results.replaceChildren(); }};
    }
    function nextFields(host, current) {
      const title = field(host, "Следующий шаг", current?.restricted || current?.completed ? "" : current?.title); title.required = true; title.maxLength = 255;
      const due = field(host, "Дата и время · " + initial.timezone, localDateTime(current?.restricted || current?.completed ? null : current?.due_at, initial.timezone), "datetime-local"); due.required = true;
      return () => ({title: title.value.trim(), due_at: deadlineISO(due.value, initial.utc_offset)});
    }
    function makeForm(host, title, text) {
      const section = el("section"), form = el("form"); section.append(el("h3", "", title));
      if (text) section.append(el("p", "lead-form-note", text)); section.append(form); host.append(section); return form;
    }
    function formSubmit(form, label, handler) {
      const save = button(label, null, true); save.type = "submit"; form.append(save);
      form.addEventListener("submit", (event) => { event.preventDefault(); try { handler(); } catch (error) { errorNode(form, error); } });
    }
    function editForm(host, lead, kind = "lead.edit") {
      const creating = kind !== "lead.edit", form = makeForm(host, creating ? kind === "lead.new_need" ? "Новая потребность" : "Новый лид" : "Потребность и следующий шаг",
        creating ? "Телефон и клиент не обязательны. Опишите контекст и назначьте следующий шаг. Для создания сделки потребуется выбрать клиента." : "Это общие бизнес-поля лида. Приватные подробности оставляйте в исходной коммуникации.");
      const fields = el("div", "lead-fields"); form.append(fields);
      const title = field(fields, "Название", creating ? "" : lead.title, "text", true); title.required = true; title.maxLength = 255;
      const need = field(fields, "Что требуется / контекст", creating ? "" : lead.need, "textarea", true); need.required = true; need.maxLength = 10000;
      const client = picker(fields, "Клиент · можно уточнить позже", initial.urls.clients, "clients", creating ? null : lead.client);
      const direction = select(fields, "Направление", [["", "Пока не определено"], ...initial.pipelines.map((p) => [p.code, p.name])], creating ? "" : lead.direction);
      const specialization = field(fields, "Специализация", creating ? "" : lead.specialization); specialization.maxLength = 40;
      const location = field(fields, "Место / объект", creating ? "" : lead.location); location.maxLength = 255;
      const desired = field(fields, "Желаемый срок · " + initial.timezone, creating ? "" : localDateTime(lead.desired_at, initial.timezone), "datetime-local");
      const extra = el("details"), more = el("div", "lead-fields"); extra.append(el("summary", "", "Источник и бюджет"), more); form.append(extra);
      const source = field(more, "Источник привлечения", creating || lead.acquisition_source === "manual" ? "Ручное обращение" : lead.acquisition_source); source.maxLength = 80;
      const channel = field(more, "Канал первого обращения", creating || lead.first_contact_channel === "manual" ? "Ручной ввод" : lead.first_contact_channel); channel.maxLength = 40;
      const budgetKind = select(more, "Бюджет", [["unknown", "Неизвестен"], ["exact", "Точный"], ["range", "Диапазон"]], creating ? "unknown" : lead.budget_kind);
      const minimum = field(more, "Сумма / от", creating ? "" : lead.budget_min, "number"), maximum = field(more, "До", creating ? "" : lead.budget_max, "number");
      for (const input of [minimum, maximum]) { input.min = "0"; input.step = "0.01"; }
      const currency = field(more, "Валюта", creating ? "RUB" : lead.currency || "RUB"); currency.maxLength = 3;
      const next = el("div", "lead-fields"); form.append(next);
      // A colleague's task stays private; editing the lead must not overwrite it.
      const canNext = creating || !(lead.next_action?.restricted || lead.next_action?.waiting);
      const nextAction = canNext ? nextFields(next, creating ? null : lead.next_action) : null;
      if (!canNext) form.append(el("p", "lead-form-note", lead.next_action?.waiting ? "Режим ожидания изменяется в карточке задачи. Эта форма сохраняет бизнес-поля лида." : "Следующее действие ограничено доступом и этой формой не изменяется."));
      formSubmit(form, creating ? "Создать лид" : "Сохранить изменения", () => {
        const changes = {title: title.value.trim(), need: need.value.trim(), client_id: client.value(), direction: direction.value,
          specialization: specialization.value.trim(), location: location.value.trim(), desired_at: desired.value ? deadlineISO(desired.value, initial.utc_offset) : null,
          acquisition_source: source.value.trim() === "Ручное обращение" ? "manual" : source.value.trim(),
          first_contact_channel: channel.value.trim() === "Ручной ввод" ? "manual" : channel.value.trim(), budget_kind: budgetKind.value,
          budget_min: budgetKind.value === "unknown" ? null : minimum.value,
          budget_max: budgetKind.value === "unknown" ? null : budgetKind.value === "exact" ? minimum.value : maximum.value,
          currency: budgetKind.value === "unknown" ? "" : currency.value.trim()};
        if (nextAction) changes.next_action = nextAction();
        if (!creating && canNext && lead.next_action && !lead.next_action.restricted && !lead.next_action.completed) changes.expected_task_version = lead.next_action.version;
        submit(form, kind, kind === "lead.create" ? null : lead, changes);
      });
    }
    function transitionForm(host, lead) {
      const form = makeForm(host, "Результат разбора / ожидание"), fields = el("div", "lead-fields"); form.append(fields);
      const status = select(fields, "Состояние", [["working", "В работе"], ["qualifying", "Уточнение"], ["waiting", "Ожидание"], ["deferred", "Отложен"], ["closed", "Разбор завершён"]], lead.status === "new" ? "working" : lead.status);
      const result = select(fields, "Результат завершения", [["non_target", "Нецелевой"], ["spam", "Спам"], ["duplicate", "Дубль другого лида"], ["no_deal", "Закрыт без сделки"], ["linked", "Относится к существующей сделке"]]);
      const reason = field(fields, "Причина / пояснение", "", "textarea", true); reason.maxLength = 500;
      const follow = field(fields, "Когда вернуться · " + initial.timezone, localDateTime(lead.follow_up_at, initial.timezone), "datetime-local");
      const targets = picker(fields, "Связанный лид / сделка · для дубля или текущей работы", initial.urls.targets, "targets", null, () => result.value === "linked" ? "deal" : "lead");
      const update = () => { result.parentElement.hidden = status.value !== "closed"; follow.parentElement.hidden = !["waiting", "deferred"].includes(status.value); targets.element.hidden = status.value !== "closed" || !["duplicate", "linked"].includes(result.value); targets.clear(); };
      status.addEventListener("change", update); result.addEventListener("change", update); update();
      formSubmit(form, "Сохранить состояние", () => {
        const changes = {status: status.value, reason: reason.value.trim()};
        if (["waiting", "deferred"].includes(status.value)) { changes.follow_up_at = deadlineISO(follow.value, initial.utc_offset); if (lead.next_action && !lead.next_action.restricted && !lead.next_action.completed) changes.expected_task_version = lead.next_action.version; }
        if (status.value === "closed") { changes.result = result.value; if (result.value === "linked") changes.target_deal_id = targets.value(); if (result.value === "duplicate") changes.target_lead_id = targets.value(); }
        submit(form, "lead.transition", lead, changes);
      });
    }
    function qualificationForm(host, lead, additional = false) {
      const form = makeForm(host, additional ? "Дополнительная самостоятельная сделка" : "Квалифицировать в сделки",
        "Каждая строка — отдельная потребность, а не вариант одного КП. Сделки создаются один раз и получают собственный следующий шаг.");
      const rows = el("div"), entries = []; form.append(rows);
      function addRow() {
        if (entries.length >= 10) return;
        const box = el("div", "lead-deal-form"), fields = el("div", "lead-fields"); box.append(fields);
        const title = field(fields, "Название сделки", entries.length ? "" : lead.title, "text", true); title.required = true; title.maxLength = 255;
        const pipeline = select(fields, "Воронка", initial.pipelines.map((p) => [String(p.id), p.name]), String(initial.pipelines.find((p) => p.code === lead.direction)?.id || initial.pipelines[0]?.id || "")); pipeline.required = true;
        const client = picker(fields, "Клиент · обязателен для сделки", initial.urls.clients, "clients", lead.client);
        const next = nextFields(fields, null), entry = {box, value: () => { if (!client.value()) throw new Error("Выберите клиента для каждой сделки."); return {title: title.value.trim(), pipeline_id: Number(pipeline.value), client_id: client.value(), next_action: next()}; }};
        box.append(button("Убрать эту сделку", () => { if (entries.length === 1) return; entries.splice(entries.indexOf(entry), 1); box.remove(); })); entries.push(entry); rows.append(box);
      }
      addRow(); form.append(button("+ Ещё одна потребность", addRow));
      formSubmit(form, additional ? "Добавить сделки" : "Завершить квалификацию", () => submit(form, additional ? "lead.add_deal" : "lead.qualify", lead, {deals: entries.map((entry) => entry.value())}));
    }
    function renderDetail(data) {
      const lead = data.lead; state.detail = data; content.replaceChildren();
      content.append(el("h2", "", lead.title)); content.firstChild.id = "lead-dialog-title";
      content.append(el("div", "lead-summary", lead.status_label + (lead.result ? " · " + lead.result_label : "") + " · " + (lead.assignee?.name || "Не назначен")));
      if (lead.intake_reason_label) content.append(el("p", "crm-live-muted", lead.intake_reason_label));
      if (lead.needs_review) content.append(el("p", "lead-conflict", lead.status === "closed" ? "После разбора поступило новое обращение. Проверьте исходную коммуникацию: продолжение текущей работы или новая потребность." : "Обращение требует разбора: уточните потребность и следующий шаг."));
      if (lead.can_acknowledge) {
        const form = makeForm(content, "Продолжение текущей работы", "Если это уточнение уже разобранного вопроса, отметьте обращение разобранным. Новая сделка и повторная квалификация не создаются.");
        formSubmit(form, "Отметить разобранным", () => submit(form, "lead.acknowledge", lead, {}));
      }
      if (lead.need) content.append(el("p", "lead-need", lead.need));
      if (lead.reason) content.append(el("p", "", "Результат / причина: " + lead.reason));
      if (lead.next_action) {
        const next = lead.next_action, line = el("p", "crm-live-muted");
        if (next.restricted) line.textContent = "Следующий шаг ограничен доступом";
        else { line.append(el("span", "", (next.completed ? "Предыдущий шаг: " : "Следующий шаг: ") + next.title + " · " + dateText(next.due_at) + " ")); const link = el("a", "", "Открыть задачу"); link.href = next.url; line.append(link); }
        content.append(line);
      }
      if (data.sources?.length) {
        const section = el("section"), links = el("div", "lead-link-list"); section.append(el("h3", "", "Исходные обращения"), links);
        for (const source of data.sources) {
          const link = el("a", "", source.label || "Открыть коммуникацию"); link.href = source.url; links.append(link);
          if (source.occurred_at || source.received_at) {
            const timestamp = source.occurred_at ? "Событие " + dateText(source.occurred_at) : "Получено " + dateText(source.received_at) + " · время события не передано";
            links.append(el("span", "lead-date", timestamp + (source.historical ? " · История" : "")));
          }
        }
        content.append(section);
      }
      if (data.deals?.length) {
        const section = el("section"), links = el("div", "lead-link-list"); section.append(el("h3", "", "Связанные сделки"), links);
        for (const deal of data.deals) { const link = el("a", "", deal.title); link.href = deal.url; links.append(link); } content.append(section);
      }
      if (data.duplicate_of) { const line = el("p", "", "Дубль обращения: "); line.append(button(data.duplicate_of.title, () => openLead(data.duplicate_of.id))); content.append(line); }
      if (lead.can_take) {
        const form = makeForm(content, "Взять в работу", "Просмотр карточки не назначает ответственного. Первый успешный захват закрепит обращение за вами."), fields = el("div", "lead-fields"); form.append(fields); const next = nextFields(fields, null);
        formSubmit(form, "Взять в работу", () => submit(form, "lead.take", lead, {next_action: next()}));
      }
      if (lead.can_edit) editForm(content, lead);
      if (lead.can_transition) transitionForm(content, lead);
      if (lead.can_qualify) qualificationForm(content, lead);
      if (lead.can_add_deal) qualificationForm(content, lead, true);
      if (lead.can_new_need) { const details = el("details"); details.append(el("summary", "", "Новое обращение — самостоятельная потребность")); editForm(details, lead, "lead.new_need"); content.append(details); }
      if (data.events?.length) {
        const section = el("section"), history = el("ol", "lead-history"); section.append(el("h3", "", "История"), history);
        for (const event of data.events) { const row = el("li", "", event.label + " · " + dateText(event.at) + " · " + event.actor); if (event.reason) row.append(el("p", "", event.reason)); history.append(row); } content.append(section);
      }
      renderOperations();
    }
    async function openLead(id) {
      const sequence = ++state.detailSequence; state.selected = Number(id); urlState(); content.replaceChildren(el("p", "", "Загружаем карточку…")); if (!dialog.open) dialog.showModal();
      try { const data = await read(initial.urls.detail_prefix + id + "/?org_id=" + initial.organization_id); if (sequence === state.detailSequence) renderDetail(data); }
      catch (error) { if (sequence === state.detailSequence) content.replaceChildren(el("p", "lead-error", error.message)); }
    }
    function createLead() {
      if (operations.forSubject(null)) { notice("Сначала проверьте результат предыдущего создания лида."); return; }
      ++state.detailSequence; state.selected = null; state.detail = null; urlState(); content.replaceChildren(el("h2", "", "Новое обращение")); content.firstChild.id = "lead-dialog-title";
      editForm(content, null, "lead.create"); if (!dialog.open) dialog.showModal();
    }
    byId("lead-create").addEventListener("click", createLead);
    byId("lead-close").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => { ++state.detailSequence; state.selected = null; urlState(); });
    byId("lead-refresh").addEventListener("click", loadList);
    let searchTimer;
    byId("lead-search").value = state.q; byId("lead-status").value = state.status;
    byId("lead-search").addEventListener("input", (event) => { state.q = event.target.value; state.page = 1; win.clearTimeout(searchTimer); searchTimer = win.setTimeout(loadList, 300); });
    byId("lead-status").addEventListener("change", (event) => { state.status = event.target.value; state.page = 1; loadList(); });
    byId("lead-modes").addEventListener("click", (event) => { const target = event.target.closest("[data-mode]"); if (target) { state.mode = target.dataset.mode; state.page = 1; loadList(); } });
    byId("lead-prev").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadList(); } });
    byId("lead-next").addEventListener("click", () => { if (state.page < state.data.pages) { state.page += 1; loadList(); } });
    win.addEventListener("beforeunload", (event) => { if (operations.pending().length) { event.preventDefault(); event.returnValue = ""; } });
    const selected = new URL(win.location.href).searchParams.get("lead"); renderList(); if (selected && /^\d+$/.test(selected)) openLead(Number(selected));
    return {state, operations, loadList, openLead};
  }
  return {LeadOperations, localDateTime, deadlineISO, mount};
});
