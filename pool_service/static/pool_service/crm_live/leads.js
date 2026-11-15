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
  function intakeStatusText(intake, dateText) {
    const parts = ["Приём обращений: " + (intake.enabled ? "включён" : "выключен")];
    const attempted = intake.last_attempt_at || intake.last_run_at;
    if (attempted) {
      parts.push("Последняя попытка обработки: " + dateText(attempted));
      if (Number.isFinite(intake.attempt_age_seconds)) parts.push("прошло " + Math.floor(intake.attempt_age_seconds / 60) + " мин");
      parts.push(intake.last_attempt_result === "complete" ? "проход завершён без ошибки" : intake.last_attempt_result === "error" ? "ошибка обработки" : "результат неизвестен");
    } else parts.push("попытка обработки ещё не зафиксирована");
    if (intake.last_successful_pass_at) parts.push("Последний известный успешный проход: " + dateText(intake.last_successful_pass_at));
    else parts.push("Время последнего успешного прохода неизвестно");
    parts.push("Необработанные события: оценка недоступна; успешный проход не означает пустую очередь");
    return parts.join(" · ");
  }
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
  function moveTargets(lead) {
    if (!lead.can_move) return [];
    if (lead.is_archived) return lead.status === "closed" ? (lead.result === "spam" ? ["restore", "spam"] : ["restore", "unparsed"]) : ["restore", "unparsed", "working"];
    if (lead.status === "closed" && lead.result !== "spam") return lead.needs_review ? ["deleted"] : ["unparsed", "deleted"];
    return ["unparsed", "working", ...((lead.status !== "closed") ? ["history"] : []), "spam", "deleted"].filter((key) => key !== lead.column);
  }
  function mount(doc) {
    const root = doc.getElementById("crm-leads"); if (!root) return;
    const win = doc.defaultView;
    const initial = JSON.parse(doc.getElementById("lead-bootstrap").textContent);
    const byId = (id) => doc.getElementById(id);
    const dialog = byId("lead-dialog"), content = byId("lead-dialog-content");
    const state = {data: initial, q: initial.q, mode: initial.mode, status: initial.status, page: initial.page, pages: Object.fromEntries((initial.columns || []).map((column) => [column.key, column.page])),
      selected: null, detail: null, loading: false, sequence: 0, detailSequence: 0, listController: null,
      showArchiveQueues: new URL(win.location.href).searchParams.get("queues") === "all"};
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
      for (const [key, value, base] of [["q", state.q, ""], ["mode", state.mode, "all"], ["status", state.status, ""], ["page", String(state.mode === "closed" ? state.page : 1), "1"]]) {
        if (value && value !== base) url.searchParams.set(key, value); else url.searchParams.delete(key);
      }
      for (const key of ["unparsed", "working", "history", "spam", "deleted"]) url.searchParams.delete("page_" + key);
      if (state.showArchiveQueues) url.searchParams.set("queues", "all"); else url.searchParams.delete("queues");
      if (state.selected) url.searchParams.set("lead", state.selected); else url.searchParams.delete("lead");
      win.history.replaceState({}, "", url);
    }
    async function read(url, signal) {
      const response = await win.fetch(url, {credentials: "same-origin", headers: {Accept: "application/json"}, signal});
      let body; try { body = await response.json(); } catch (_error) { throw new Error("Ответ сервера недоступен. Обновите данные."); }
      if (!response.ok) { const error = new Error(body.error || "Не удалось загрузить данные."); error.status=response.status; error.code=body.code; throw error; }
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
    const columnLabels = {unparsed: "Не разобраны", working: "В работе", history: "Завершённые", spam: "Спам", deleted: "Удалённые", restore: "Восстановить"};
    let dragged = null;
    const conversionDrop=el("div","lead-conversion-drop","Создать сделку");conversionDrop.hidden=true;root.append(conversionDrop);
    conversionDrop.addEventListener("dragover",e=>{if(dragged?.can_qualify){e.preventDefault();e.dataTransfer.dropEffect="move";}});
    conversionDrop.addEventListener("drop",e=>{e.preventDefault();const lead=dragged;dragged=null;conversionDrop.hidden=true;if(lead?.can_qualify)conversionDialog(lead);});
    win.addEventListener("keydown",e=>{if(e.key==="Escape"){dragged=null;conversionDrop.hidden=true;}});
    win.addEventListener("blur",()=>{conversionDrop.hidden=true;});
    function sourceLine(host, source) {
      if (!source) return;
      host.append(el("p", "lead-source", [source.label, source.phone || source.contact_name].filter(Boolean).join(" · ")));
      if (source.type === "call") host.append(el("p", "lead-source", (source.caller || "Кто звонил — неизвестно") + " → " + (source.callee || "Кому — неизвестно")));
      if (source.contact?.url) {
        const link = el("a", "lead-contact", source.contact.name); link.href = source.contact.url; host.append(link);
      }
      const stamp = source.occurred_at || source.received_at;
      if (stamp) host.append(el("p", "lead-date", (source.occurred_at ? "Событие: " : "Получено: ") + dateText(stamp) + (!source.occurred_at ? " · время события неизвестно" : "")));
      if (source.employee) host.append(el("p", "lead-source", "Сотрудник звонка: " + source.employee));
    }
    function displayTitle(lead) {
      if (!["Входящий звонок — требуется уточнение", "Входящее обращение — требуется уточнение"].includes(lead.title) || !lead.source) return lead.title;
      return [lead.source.label, lead.client?.name || lead.source.phone || lead.source.contact_name || "Контакт не определён"].join(" · ");
    }
    function card(lead) {
      const row = el("article", "lead-row"); row.dataset.leadId = lead.id;
      const title = el("div"), middle = el("div"), next = el("div");
      title.append(button(lead.client?.name || lead.source?.contact?.name || lead.source?.contact_name || lead.source?.phone || lead.title,()=>openLead(lead.id)));
      if(lead.needs_review){const mark=el("span","lead-review-marker","•");mark.title="Есть обращение для разбора";mark.setAttribute("aria-label","Есть обращение для разбора");title.append(mark);}
      if(lead.need)title.append(el("p","lead-card-need",lead.need.slice(0,160)));
      title.append(el("p","lead-date",[lead.source?.label,lead.source?.occurred_at?dateText(lead.source.occurred_at):lead.source?.received_at?dateText(lead.source.received_at)+" · время события неизвестно":""].filter(Boolean).join(" · ")));
      middle.append(el("p","",lead.assignee?.name || "Не назначен"));
      const action = lead.next_action;
      next.append(el("p", "", action?.restricted ? "Следующий шаг ограничен доступом" : action?.completed ? "Предыдущий шаг завершён" : action?.title || (lead.can_take ? "Можно взять в работу" : "Следующий шаг не указан")));
      if (action?.due_at && !action.restricted) next.append(el("p", "lead-date", dateText(action.due_at)));
      row.append(title, middle, next);
      const targets = moveTargets(lead);
      if (targets.length || lead.can_qualify) {
        row.draggable = !state.loading && !operations.forSubject(lead.id);
        row.addEventListener("dragstart", (event) => {
          if (!row.draggable || operations.forSubject(lead.id)) { event.preventDefault(); return; }
          dragged = lead; conversionDrop.hidden=!lead.can_qualify; event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", String(lead.id)); row.classList.add("is-dragging");
        });
        row.addEventListener("dragend", () => { dragged = null; conversionDrop.hidden=true; row.classList.remove("is-dragging"); root.querySelectorAll(".is-drop-target").forEach((node) => node.classList.remove("is-drop-target")); });

      }
      return row;
    }
    function renderList() {
      const data = state.data, list = byId("lead-list"), scrollLeft = list.scrollLeft; list.replaceChildren(); list.setAttribute("aria-busy", String(state.loading));
      list.className = state.mode === "closed" ? "lead-history-list" : "lead-board" + (state.showArchiveQueues ? " lead-board-all" : "");
      byId("lead-archive-toggle")?.setAttribute("aria-pressed",String(state.showArchiveQueues));
      const archivedMatches=state.mode!=="closed"&&!state.showArchiveQueues?(data.columns||[]).filter(column=>["spam","deleted"].includes(column.key)).reduce((total,column)=>total+column.total,0):0;
      byId("lead-count").textContent = state.loading ? "Обновляем…" : "Найдено: " + data.total + (archivedMatches ? " · " + archivedMatches + " в спаме и удалённых" : "");
      byId("lead-page").textContent = `${data.page} / ${data.pages}`;
      byId("lead-prev").parentElement.hidden = state.mode !== "closed";
      byId("lead-prev").disabled = state.loading || data.page <= 1; byId("lead-next").disabled = state.loading || data.page >= data.pages;
      byId("lead-create").hidden = !data.can_create;
      const intake = data.intake || {};
      byId("lead-intake-status").textContent = intakeStatusText(intake, dateText);
      root.querySelectorAll("[data-mode]").forEach((node) => { if (node.dataset.mode === state.mode) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current"); });
      if (state.mode === "closed") {
        list.append(el("h2", "", "История завершённых"));
        if (!data.leads.length) list.append(el("p", "lead-empty", "Завершённых лидов по этим условиям нет."));
        for (const lead of data.leads) list.append(card(lead));
      } else for (const column of data.columns || []) {
        if (!state.showArchiveQueues && ["spam","deleted"].includes(column.key)) continue;
        const section = el("section", "lead-column"); section.dataset.column = column.key;
        section.setAttribute("aria-label", column.label);
        section.append(el("h2", "", column.label + " · " + column.total));
        const cards = el("div", "lead-column-cards");
        if (!column.leads.length) cards.append(el("p", "lead-empty", state.q || state.status ? "Нет совпадений" : "Пока пусто"));
        for (const lead of column.leads) cards.append(card(lead));
        section.append(cards);
        section.addEventListener("dragover", (event) => { if (dragged && !state.loading && moveTargets(dragged).includes(column.key)) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; section.classList.add("is-drop-target"); } });
        section.addEventListener("dragleave", (event) => { if (!section.contains(event.relatedTarget)) section.classList.remove("is-drop-target"); });
        section.addEventListener("drop", (event) => { event.preventDefault(); section.classList.remove("is-drop-target"); const lead = dragged; dragged = null; conversionDrop.hidden=true; if (lead && !state.loading && moveTargets(lead).includes(column.key)) moveLead(lead, column.key); });
        section.append(el("p", "lead-column-progress", `Загружено ${column.leads.length} из ${column.total}`));
        if (column.cursor) {
          const more = button("Показать ещё 50", () => loadList({column:column.key}));
          more.dataset.action="column-more"; more.disabled=state.loading; section.append(more);
        }
        list.append(section);
      }
      list.scrollLeft=scrollLeft;
      renderOperations();
    }
    async function moveLead(snapshot, target) {
      if (operations.forSubject(snapshot.id)) { notice("Сначала проверьте предыдущее сохранение этой карточки."); return; }
      const sequence = ++state.detailSequence; state.selected = snapshot.id; urlState();
      content.replaceChildren(el("h2", "", "Перемещение лида")); content.firstChild.id = "lead-dialog-title";
      if (!dialog.open) dialog.showModal();
      try {
        const data = await read(initial.urls.detail_prefix + snapshot.id + "/?org_id=" + initial.organization_id);
        if (sequence !== state.detailSequence) return;
        const lead = data.lead;
        if (lead.revision !== snapshot.revision || !moveTargets(lead).includes(target)) throw new Error("Карточка уже изменена или действие недоступно. Обновите канбан.");
        const free = !lead.assignee;
        const form = makeForm(content, columnLabels[target], free ? "Первое успешное сохранение закрепит свободный лид за вами." : "Перемещение сохранится после подтверждения. История и связи останутся в карточке.");
        const ranks={unparsed:0,working:1,history:2,spam:3,deleted:4};
        const required=["spam","deleted","restore"].includes(target)||ranks[target]<ranks[lead.column];
        const reason = required ? field(form, "Причина перемещения", "", "textarea") : null;
        if(reason){reason.required=true;reason.maxLength=500;}
        if(target==="history")form.append(el("p","lead-form-note","Лид будет завершён без создания сделки. Открытый следующий шаг отменится; сделки, выполнение и оплата не изменятся."));
        let next = null;
        if (target === "working" && (!lead.next_action || lead.next_action.completed)) next = nextFields(form, null);
        if (target === "spam" || target === "deleted") form.append(el("p", "lead-form-note", "Текущий незавершённый шаг будет отменён с сохранением истории."));
        formSubmit(form, free ? "Взять на себя и переместить" : "Подтвердить перемещение", () => {
          const changes = {column: target, reason: reason ? reason.value.trim() : ""}; if (next) changes.next_action = next();
          if (["spam", "deleted", "history"].includes(target) && lead.next_action?.version && !lead.next_action.completed) changes.expected_task_version = lead.next_action.version;
          submit(form, "lead.disposition", lead, changes);
        });
        renderOperations();
      } catch (error) { if (sequence === state.detailSequence) content.append(el("p", "lead-error", error.message)); }
    }
    async function loadList(options) {
      const more=options?.column ? (state.data.columns || []).find(c=>c.key===options.column) : null;
      if (options?.column && (!more?.cursor || state.loading)) return;
      const previous=state.data;
      if (state.listController) state.listController.abort();
      const controller = new AbortController(), sequence = ++state.sequence; state.listController = controller; state.loading = true; renderList(); urlState();
      const url = new URL(initial.urls.data, win.location.origin);
      for (const [key, value] of Object.entries({q: state.q, mode: state.mode, status: state.status, page: state.mode === "closed" ? state.page : 1})) url.searchParams.set(key, value);
      if(state.mode!=="closed") url.searchParams.set("layout","board");
      if(more){url.searchParams.set("board_column",more.key);url.searchParams.set("cursor",more.cursor);}
      try {
        const data = await read(url, controller.signal); if (sequence !== state.sequence) return;
        if(more){
          const replacement=data.columns.find(c=>c.key===more.key);
          const rows=new Map(more.leads.map(row=>[String(row.id),row]));
          for(const row of replacement.leads) rows.set(String(row.id),row);
          const columns=previous.columns.map(c=>c.key===more.key?{...replacement,leads:Array.from(rows.values())}:c);
          state.data={...data,columns,leads:columns.flatMap(c=>c.leads)};
        }else state.data=data;
        state.page=data.page; urlState(); if(!options?.keepNotice) notice("");
      } catch(error) {
        if(error.name==="AbortError" || sequence!==state.sequence) return;
        if(error.code==="reset_required"){
          state.data={...state.data,leads:[],columns:[],total:0};
          notice("Доска изменилась. Загружаем актуальные первые порции; продолжите загрузку заново.");
          await loadList({keepNotice:true});return;
        }
        if([401,403,404].includes(error.status)) state.data={...state.data,leads:[],columns:[],total:0,can_create:false};
        notice(error.message);
      } finally { if(sequence===state.sequence){state.loading=false;renderList();} }
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
            for (const row of data[collection]) {
              const option = button([row.name || row.title, row.phone, ...(row.categories || []).map(value => typeof value === "string" ? ({personal:"Личный",internal:"Внутренний",landlord:"Арендодатель",supplier:"Поставщик",partner:"Партнёр",other:"Другой"}[value] || value) : value.label)].filter(Boolean).join(" · "), () => { selected = row; input.value = row.name || row.title; chosen.textContent = "Выбрано: " + input.value; results.replaceChildren(); });
              results.append(option);
            }
            if (!data[collection].length) results.append(el("span", "crm-live-muted", "Совпадений нет"));
          } catch (error) { if (error.name !== "AbortError" && sequence === current) chosen.textContent = error.message; }
        }, 300);
      });
      return {element: wrap, value: () => selected?.id ?? null, clear: () => { ++sequence; win.clearTimeout(timer); if (controller) controller.abort(); selected = null; input.value = ""; chosen.textContent = "Не выбрано"; results.replaceChildren(); }};
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
      const save = button(label, null, true); save.type = "submit"; save.dataset.noLoading=""; form.append(save);
      form.addEventListener("submit", (event) => { event.preventDefault(); try { handler(); } catch (error) { errorNode(form, error); } });
    }
    function editForm(host, lead, kind = "lead.edit") {
      const creating = kind !== "lead.edit", form = makeForm(host, creating ? kind === "lead.new_need" ? "Новая потребность" : "Новый лид" : "Потребность",
        creating ? "Опишите обращение и запланируйте следующий шаг. Остальные сведения можно заполнить позже." : "");
      const fields = el("div", "lead-fields"); form.append(fields);
      const title = field(fields, "Название", creating ? "" : lead.title, "text", true); title.required = true; title.maxLength = 255;
      const need = field(fields, "Что требуется / контекст", creating ? "" : lead.need, "textarea", true); need.required = true; need.maxLength = 10000;
      if (!creating) need.dataset.leadNeed = "true";
      const client = picker(fields, "Клиент / компания", initial.urls.clients, "clients", creating ? null : lead.client);
      const contact = picker(fields, "Контактное лицо", initial.urls.clients, "clients", creating ? null : lead.contact, () => "private");
      const direction = select(fields, "Направление", [["", "Пока не определено"], ...initial.pipelines.map((p) => [p.code, p.name])], creating ? "" : lead.direction);
      const specialization = field(fields, "Специализация", creating ? "" : lead.specialization); specialization.maxLength = 40;
      const location = field(fields, "Место / объект", creating ? "" : lead.location); location.maxLength = 255;
      const desired = field(fields, "Желаемый срок · " + initial.timezone, creating ? "" : localDateTime(lead.desired_at, initial.timezone), "datetime-local");
      const details=el("details"), business=el("div","lead-fields");details.append(el("summary","","Контакты и дополнительные сведения"),business);business.append(client.element,contact.element,direction.parentElement,specialization.parentElement,location.parentElement,desired.parentElement);form.append(details);
      const extra = el("details"), more = el("div", "lead-fields"); extra.append(el("summary", "", "Источник и бюджет"), more); form.append(extra);
      const source = field(more, "Источник привлечения", creating || lead.acquisition_source === "manual" ? "Ручное обращение" : lead.acquisition_source); source.maxLength = 80;
      const channel = field(more, "Канал первого обращения", creating || lead.first_contact_channel === "manual" ? "Ручной ввод" : lead.first_contact_channel); channel.maxLength = 40;
      const budgetKind = select(more, "Бюджет", [["unknown", "Неизвестен"], ["exact", "Точный"], ["range", "Диапазон"]], creating ? "unknown" : lead.budget_kind);
      const minimum = field(more, "Сумма / от", creating ? "" : lead.budget_min, "number"), maximum = field(more, "До", creating ? "" : lead.budget_max, "number");
      for (const input of [minimum, maximum]) { input.min = "0"; input.step = "0.01"; }
      const currency = field(more, "Валюта", creating ? "RUB" : lead.currency || "RUB"); currency.maxLength = 3;
      const next = el("div", "lead-fields lead-next-panel"); next.append(el("h3","","Следующее действие")); form.append(next);
      // A colleague's task stays private; editing the lead must not overwrite it.
      const canNext = creating || !(lead.next_action?.restricted || lead.next_action?.waiting);
      const nextAction = canNext ? nextFields(next, creating ? null : lead.next_action) : null;
      if (!canNext) form.append(el("p", "lead-form-note", lead.next_action?.waiting ? "Режим ожидания изменяется в карточке задачи. Эта форма сохраняет бизнес-поля лида." : "Следующее действие ограничено доступом и этой формой не изменяется."));
      formSubmit(form, creating ? "Создать лид" : "Сохранить изменения", () => {
        const changes = {title: title.value.trim(), need: need.value.trim(), client_id: client.value(), contact_id: contact.value(), direction: direction.value,
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
      const result = select(fields, "Результат завершения", [["non_target", "Нецелевой"], ["spam", "Спам"], ["duplicate", "Продолжение существующей потребности"], ["no_deal", "Закрыт без сделки"], ["linked", "Относится к существующей сделке"]]);
      const reason = field(fields, "Причина / пояснение", "", "textarea", true); reason.maxLength = 500;
      const follow = field(fields, "Когда вернуться · " + initial.timezone, localDateTime(lead.follow_up_at, initial.timezone), "datetime-local");
      fields.append(el("p", "lead-field wide lead-form-note", "Подтвердите, что это та же потребность: телефон сам по себе не основание для связи. История сохранится, открытый шаг исходного лида отменится. Задачи и сделки выбранной работы не изменятся. Доступны ваши активные независимые лиды."));
      const targets = picker(fields, "Подтверждённая потребность / сделка", initial.urls.targets + "&exclude_id=" + lead.id, "targets", null, () => result.value === "linked" ? "deal" : "lead");
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
      const form = makeForm(host, "Данные сделки", "Выберите или создайте контакт. Компания необязательна. Все сведения сохранятся при подтверждении.");
      form.classList.add("lead-conversion-form");
      const fields=el("div","lead-fields");form.append(fields);
      if(!lead.assignee)form.append(el("p","lead-form-note","При подтверждении вы возьмёте свободный лид на себя."));
      if(lead.is_archived)form.append(el("p","lead-form-note","При подтверждении лид будет восстановлен и конвертирован. История удаления сохранится."));
      const title=field(fields,"Название сделки",lead.title,"text",true);title.required=true;title.maxLength=255;
      const pipeline=select(fields,"Воронка",initial.pipelines.map(p=>[String(p.id),p.name]),String(initial.pipelines.find(p=>p.code===lead.direction)?.id || initial.pipelines[0]?.id || ""));pipeline.required=true;
      const need=additional?null:field(fields,"Потребность",lead.need||"","textarea",true);if(need){need.required=true;need.maxLength=10000;need.readOnly=!lead.can_edit_conversion_need;if(!lead.can_edit_conversion_need&&!lead.need)form.append(el("p","lead-error","Для уточнения потребности требуется право редактирования лида."));}
      const customer=el("div","lead-customer-grid");form.append(customer);
      const contactBox=el("fieldset","lead-customer-box"), companyBox=el("fieldset","lead-customer-box");
      contactBox.append(el("legend","","Контакт"));companyBox.append(el("legend","","Компания"));customer.append(contactBox,companyBox);
      const savedContact=lead.contact || (lead.client?.client_type==="private" ? lead.client : null);
      const savedCompany=lead.company || (lead.client?.client_type==="legal" ? lead.client : null);
      const mode=select(contactBox,"Контакт",[["existing","Выбрать существующий"],["new","Создать контакт"],["none","Без контактного лица"]],savedContact?"existing":lead.source?.phone||lead.source?.contact_name?"new":"existing");
      const contact=picker(contactBox,"Найти контакт",initial.urls.clients,"clients",savedContact,()=>"private");
      const fresh=el("div","lead-fields");contactBox.append(fresh);
      const person=field(fresh,"Имя контакта",lead.source?.contact_name || "","text",true), phone=field(fresh,"Телефон",lead.source?.phone || "","tel"), email=field(fresh,"Email",savedContact?.email || "","email");
      person.maxLength=255;person.required=true;phone.maxLength=20;email.maxLength=254;
      const category=select(fresh,"Категория контакта",[["","Клиент"],["personal","Личный"],["internal","Внутренний"],["landlord","Арендодатель"],["supplier","Поставщик"],["partner","Партнёр"],["other","Другой"]]);
      category.parentElement.hidden=!initial.can_categorize_contacts;
      const categoryNote=el("p","lead-form-note wide","Категория не меняет доступ к контакту.");fresh.append(categoryNote);
      category.addEventListener("change",()=>{categoryNote.hidden=category.value!=="personal";});categoryNote.hidden=true;
      const companyMode=select(companyBox,"Компания",[["none","Без компании"],["existing","Выбрать существующую"],["new","Создать компанию"]],savedCompany?"existing":"none");
      const company=picker(companyBox,"Найти компанию",initial.urls.clients,"clients",savedCompany,()=>"legal");
      const freshCompany=el("div","lead-fields");companyBox.append(freshCompany);
      const companyName=field(freshCompany,"Название компании","","text",true),inn=field(freshCompany,"ИНН","","text",true);companyName.maxLength=255;companyName.required=true;inn.maxLength=20;
      const companyHint=el("p","lead-form-note","Для частного клиента достаточно контакта.");companyBox.append(companyHint);
      const toggleFields=(container,visible)=>{container.hidden=!visible;container.querySelectorAll("input,select,textarea").forEach(input=>{input.disabled=!visible;});};
      const updateCustomer=()=>{toggleFields(contact.element,mode.value==="existing");toggleFields(fresh,mode.value==="new");toggleFields(company.element,companyMode.value==="existing");toggleFields(freshCompany,companyMode.value==="new");companyHint.hidden=companyMode.value!=="none";};
      mode.addEventListener("change",updateCustomer);companyMode.addEventListener("change",updateCustomer);updateCustomer();
      const transferable=!additional&&lead.next_action&&!lead.next_action.completed&&!lead.next_action.restricted&&!lead.next_action.waiting;
      const nextSection=el("div","lead-conversion-next");nextSection.append(el("h3","","Следующий шаг"));form.append(nextSection);
      const reuse=transferable?field(nextSection,"Сохранить открытый следующий шаг","","checkbox"):null;if(reuse){reuse.checked=true;reuse.parentElement.classList.add("lead-checkbox");}
      const transferred=transferable?el("p","lead-transfer-summary",lead.next_action.title+" · "+dateText(lead.next_action.due_at)):null;if(transferred)nextSection.append(transferred);
      const nextHost=el("div","lead-fields");nextSection.append(nextHost);const next=nextFields(nextHost,transferable?lead.next_action:null);
      const toggle=()=>{toggleFields(nextHost,!reuse?.checked);if(transferred)transferred.hidden=!reuse?.checked;};if(reuse)reuse.addEventListener("change",toggle);toggle();
      formSubmit(form,"Подтвердить создание сделки",()=>{
        const row={title:title.value.trim(),pipeline_id:Number(pipeline.value),next_action:reuse?.checked?{title:lead.next_action.title,due_at:lead.next_action.due_at}:next()};
        if(mode.value==="existing"&&!contact.value())throw new Error("Выберите контакт из результатов поиска или создайте новый.");
        if(companyMode.value==="existing"&&!company.value())throw new Error("Выберите компанию из результатов поиска или создайте новую.");
        if(mode.value==="none"&&companyMode.value==="none")throw new Error("Для сделки нужен контакт или компания.");
        row.customer={contact:mode.value==="none"?null:mode.value==="existing"?{id:contact.value()}:{name:person.value.trim(),phone:phone.value.trim(),email:email.value.trim(),category:initial.can_categorize_contacts?category.value:""},company:companyMode.value==="none"?null:companyMode.value==="existing"?{id:company.value()}:{name:companyName.value.trim(),inn:inn.value.trim()}};
        const changes={deals:[row]};if(need)changes.need=need.value.trim();if(reuse?.checked){row.reuse_next_action=true;changes.expected_task_version=lead.next_action.version;}
        submit(form,additional?"lead.add_deal":"lead.qualify",lead,changes);
      });
      const cancel=button("Отмена",()=>openLead(lead.id));cancel.classList.add("lead-cancel");form.append(cancel);
    }
    function renderDetail(data) {
      const lead=data.lead;state.detail=data;content.replaceChildren();
      content.append(el("h2","",displayTitle(lead)));content.firstChild.id="lead-dialog-title";
      content.append(el("p","lead-summary",[lead.status_label,lead.source?.label,lead.assignee?.name || "Не назначен"].filter(Boolean).join(" · ")));
      const primaryActions=el("div","lead-primary-actions");content.append(primaryActions);
      if(lead.can_qualify)primaryActions.append(button("Создать сделку",()=>conversionDialog(lead),true));
      const layout=el("div","lead-detail-grid"), left=el("div","lead-detail-main"), right=el("section","lead-timeline");layout.append(left,right);content.append(layout);
      const need=el("section","lead-panel lead-overview");need.append(el("h3","","Потребность"),el("p","lead-need",lead.need || "Уточните, что требуется клиенту."));left.append(need);
      const contact=el("section","lead-panel");contact.append(el("h3","","Контакт и компания"));
      const person=lead.contact || lead.source?.contact || lead.client;
      if(person?.url){const link=el("a","lead-contact",person.name);link.href=person.url;contact.append(link);}
      else contact.append(el("p","",person?.name || "Контакт не определён"));
      const companyRecord=lead.company || lead.client;
      if(companyRecord && person?.id!==companyRecord.id){const company=el(companyRecord.url?"a":"p","",companyRecord.name);if(companyRecord.url)company.href=companyRecord.url;contact.append(company);}
      if(person?.phone || lead.source?.phone)contact.append(el("p","",person?.phone || lead.source.phone));left.append(contact);
      let editor=null;
      if(lead.can_edit){editor=el("details","lead-panel lead-editor");editor.append(el("summary","","Изменить сведения"));editForm(editor,lead);primaryActions.append(button("Редактировать",()=>{editor.open=true;editor.scrollIntoView({block:"nearest"});editor.querySelector("input,textarea")?.focus();}));}
      const next=el("section","lead-panel lead-next-panel");next.append(el("h3","","Следующий шаг"),el("p","",lead.next_action?.restricted?"Ограничено доступом":lead.next_action?.completed?"Предыдущий шаг завершён":lead.next_action?.title || "Пока не назначен"));
      if(lead.next_action?.due_at&&!lead.next_action.restricted)next.append(el("p","lead-date",dateText(lead.next_action.due_at)));left.append(next);
      if(lead.can_take){
        const take=el("details","lead-take-form");take.append(el("summary","","Взять в работу"));next.append(take);
        primaryActions.append(button("Взять в работу",()=>{take.open=true;take.scrollIntoView({block:"nearest"});take.querySelector("input")?.focus();},!lead.can_qualify));
        const form=makeForm(take,"Запланируйте действие","");
        const fields=el("div","lead-fields");form.append(fields);const nextAction=nextFields(fields,null);
        formSubmit(form,"Взять в работу",()=>submit(form,"lead.take",lead,{next_action:nextAction()}));
      }
      if(editor)left.append(editor);
      const actions=el("details","lead-panel");actions.append(el("summary","","Действия с лидом"));
      if(lead.can_transition)transitionForm(actions,lead);
      if(moveTargets(lead).length){const movement=el("details","lead-detail-moves");movement.append(el("summary","","Изменить этап"));for(const target of moveTargets(lead))movement.append(button(columnLabels[target],()=>moveLead(lead,target)));left.append(movement);}
      if(lead.can_add_deal)actions.append(button("Дополнительная сделка",()=>conversionDialog(lead,true)));
      if(lead.can_acknowledge){const f=makeForm(actions,"Продолжение работы","");formSubmit(f,"Отметить разобранным",()=>submit(f,"lead.acknowledge",lead,{}));}
      if(lead.can_new_need){const fresh=el("details");fresh.append(el("summary","","Создать новую потребность"));editForm(fresh,lead,"lead.new_need");actions.append(fresh);}if(actions.childNodes.length>1)left.append(actions);
      if(data.duplicate_of){const related=el("section","lead-panel");related.append(el("h3","","Продолжение потребности"),button(data.duplicate_of.title,()=>openLead(data.duplicate_of.id)));left.append(related);}
      if(data.deals?.length){const links=el("section","lead-panel");links.append(el("h3","","Сделки"));for(const deal of data.deals){const a=el("a","",deal.title);a.href=deal.url;links.append(a);}left.append(links);}
      right.append(el("h3","","История общения"));const filters=el("div","lead-timeline-filters"), items=el("div");right.append(filters,items);
      let filter="all",sources=[...(data.sources||[])],cursor=data.source_cursor;
      const paint=()=>{
        items.replaceChildren();
        for(const source of sources.filter(x=>filter==="all"||x.type===filter)){
          const row=el("article","lead-communication");row.append(el("h4","",source.type==="call"?[(source.caller||"Неизвестно"),(source.callee||"Неизвестно")].join(" → "):source.message?.sender || source.contact_name || source.label));
          row.append(el("p","lead-date",[source.label,dateText(source.occurred_at||source.received_at),!source.occurred_at?"время получения":""].filter(Boolean).join(" · ")));
          const media=source.media;
          if(source.type==="call"){
            row.append(el("p","lead-call-status",(source.call_result==="missed"?"Пропущен":source.call_result==="answered"?"Отвечен":"Статус неизвестен")+(source.duration_seconds!=null?" · "+source.duration_seconds+" сек":"")));
            if(media?.recording_url){const audio=el("audio");audio.controls=true;audio.preload="none";audio.src=media.recording_url;audio.setAttribute("aria-label","Запись разговора");row.append(audio);}
            else row.append(el("p","lead-form-note",source.call_result==="missed"?"Разговор не состоялся; записи нет.":"Запись недоступна."));
            if(media?.summary||media?.transcript){const text=el("details");text.append(el("summary","","Резюме и расшифровка"));if(media.summary)text.append(el("p","lead-transcript",media.summary));if(media.transcript)text.append(el("p","lead-transcript",media.transcript));row.append(text);}
            if(media?.facts?.request && lead.can_edit)row.append(button("Предложить текст потребности",()=>{const input=content.querySelector('[data-lead-need="true"]');if(input&&(!input.value||input.value===lead.need)){editor.open=true;input.value=media.facts.request;input.focus();}}));
            if(media?.analysis_url){const ai=button(media.analysis_status==="ready"?"Повторить анализ":"Расшифровать",async()=>{
              if(ai.disabled)return;ai.disabled=true;
              try{const response=await win.fetch(media.analysis_url,{method:"POST",credentials:"same-origin",headers:{"X-CSRFToken":csrf(),Accept:"application/json"}});if(!response.ok)throw new Error("Запуск недоступен. Проверьте права и состояние записи.");ai.textContent="Запрос передан в очередь";}catch(error){if(row.isConnected){errorNode(row,error);ai.disabled=false;}}
            });ai.title="Явный запуск существующей очереди анализа";row.append(ai);}
          } else if(source.message){row.append(el("p","lead-message-body",source.message.body));if(source.message.truncated)row.append(el("small","","Показано начало сообщения."));}
          const link=el("a","lead-original-link","Открыть источник");link.href=source.url;row.append(link);items.append(row);
        }
        if(!items.childNodes.length)items.append(el("p","lead-form-note","Нет доступных обращений этого типа."));
      };
      for(const [key,label] of [["all","Все"],["call","Звонки"],["message","Сообщения"]])filters.append(button(label,()=>{filter=key;paint();}));paint();
      const more=button("Ещё обращения",async()=>{if(more.disabled||!cursor)return;more.disabled=true;const sequence=state.detailSequence;try{const page=await read(initial.urls.detail_prefix+lead.id+"/?org_id="+initial.organization_id+"&source_cursor="+encodeURIComponent(cursor));if(sequence!==state.detailSequence||!right.isConnected)return;const ids=new Set(sources.map(x=>x.id));sources.push(...page.sources.filter(x=>!ids.has(x.id)));cursor=page.source_cursor;paint();more.hidden=!cursor;}catch(error){if(right.isConnected)errorNode(right,error);}finally{more.disabled=false;}});more.hidden=!cursor;right.append(more);
      const audit=el("details","lead-audit");audit.append(el("summary","","Служебная история"));
      for(const event of data.events||[])audit.append(el("p","",[event.label,dateText(event.at),event.actor,event.movement,event.reason].filter(Boolean).join(" · ")));content.append(audit);renderOperations();
    }
    async function conversionDialog(snapshot, additional = false) {
      if(operations.forSubject(snapshot.id))return;
      const sequence=++state.detailSequence;state.selected=snapshot.id;urlState();content.replaceChildren(el("h2","",additional?"Дополнительная сделка":"Создать сделку"));content.firstChild.id="lead-dialog-title";if(!dialog.open)dialog.showModal();
      try{const data=await read(initial.urls.detail_prefix+snapshot.id+"/?org_id="+initial.organization_id);if(sequence!==state.detailSequence)return;if(!(additional?data.lead.can_add_deal:data.lead.can_qualify))throw new Error("Создание сделки сейчас недоступно. Обновите карточку лида.");if(data.lead.revision!==snapshot.revision)throw new Error("Лид изменился. Откройте его повторно.");qualificationForm(content,data.lead,additional);renderOperations();}catch(error){if(sequence===state.detailSequence)errorNode(content,error);}
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
    function clearSelection() {
      if (state.selected !== null) { ++state.detailSequence; state.selected = null; }
      urlState();
    }
    // close() hides synchronously; its native close event is queued. Clear the
    // URL before a user can reload, and invalidate the pending detail read now.
    byId("lead-close").addEventListener("click", () => { clearSelection(); dialog.close(); });
    dialog.addEventListener("cancel", clearSelection);
    // An older queued close must not clear a card opened in the meantime.
    dialog.addEventListener("close", () => { if (!dialog.open) clearSelection(); });
    byId("lead-refresh").addEventListener("click", loadList);
    let searchTimer;
    byId("lead-search").value = state.q; byId("lead-status").value = state.status;
    byId("lead-search").addEventListener("input", (event) => { state.q = event.target.value; state.page = 1; state.pages = {}; if(state.listController)state.listController.abort(); ++state.sequence; state.loading=true; renderList(); win.clearTimeout(searchTimer); searchTimer = win.setTimeout(loadList, 300); });
    byId("lead-status").addEventListener("change", (event) => { state.status = event.target.value; state.page = 1; state.pages = {}; loadList(); });
    byId("lead-modes").addEventListener("click", (event) => { const target = event.target.closest("[data-mode]"); if (target) { state.mode = target.dataset.mode; state.page = 1; state.pages = {}; loadList(); } });
    byId("lead-archive-toggle")?.addEventListener("click",()=>{state.showArchiveQueues=!state.showArchiveQueues;urlState();renderList();});
    byId("lead-prev").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadList(); } });
    byId("lead-next").addEventListener("click", () => { if (state.page < state.data.pages) { state.page += 1; loadList(); } });
    win.addEventListener("beforeunload", (event) => { if (operations.pending().length) { event.preventDefault(); event.returnValue = ""; } });
    win.addEventListener("popstate",()=>{
      const params=new URL(win.location.href).searchParams;
      state.q=params.get("q") || ""; state.mode=params.get("mode") || "all"; state.status=params.get("status") || "";
      state.showArchiveQueues=params.get("queues")==="all";
      state.page=Number(params.get("page")) || 1; state.pages={};
      byId("lead-search").value=state.q; byId("lead-status").value=state.status;
      const selected=params.get("lead"); state.selected=null;
      if(dialog.open) dialog.close(); loadList();
      if(selected && /^\d+$/.test(selected)) openLead(Number(selected));
    });
    const selected = new URL(win.location.href).searchParams.get("lead"); renderList(); if (selected && /^\d+$/.test(selected)) openLead(Number(selected));
    return {state, operations, loadList, openLead};
  }
  return {intakeStatusText, LeadOperations, localDateTime, deadlineISO, moveTargets, mount};
});
