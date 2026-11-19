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
  function leadEditChanges(lead, values) {
    const changes = {...values};
    // Generated title and source-derived identity are projections. Sending an
    // unchanged projection back would accidentally turn it into a manual choice.
    if (changes.title === (lead.title || "").trim()) delete changes.title;
    if (changes.client_id === (lead.client?.id ?? null) && changes.contact_id === (lead.contact?.id ?? null)) {
      delete changes.client_id; delete changes.contact_id;
    }
    return changes;
  }
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
    async run(key, transport = this.transport) {
      const item = this.entries.get(key);
      if (!item) throw new Error("Операция не найдена.");
      if (item.running) return item.running;
      if (!["ready", "uncertain"].includes(item.status)) return item;
      item.status = "sending"; this.changed();
      item.running = Promise.resolve().then(async () => {
        try { item.response = await transport(item.payload); item.status = "succeeded"; }
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
    if (lead.is_archived) return lead.status === "closed" ? (lead.result === "spam" ? ["restore", "spam"] : ["restore"]) : ["restore", "unparsed", "working"];
    if (lead.result === "spam") return ["restore", "deleted"];
    if (lead.status === "closed" && lead.result !== "spam") return lead.needs_review ? ["deleted"] : ["unparsed", "deleted"];
    return ["unparsed", "working", ...((lead.status !== "closed") ? ["history", "failed"] : []), "spam", "deleted"].filter((key) => key !== lead.column);
  }
  function mount(doc) {
    const root = doc.getElementById("crm-leads"); if (!root) return;
    const win = doc.defaultView;
    const initial = JSON.parse(doc.getElementById("lead-bootstrap").textContent);
    const byId = (id) => doc.getElementById(id);
    const dialog = byId("lead-dialog"), content = byId("lead-dialog-content");
    const legacyOwners = {mine: "me", unassigned: "unassigned"};
    const state = {data: initial, q: initial.q, mode: initial.mode, status: initial.status,
      owner: initial.owner || legacyOwners[initial.mode] || "all", page: initial.page, pages: Object.fromEntries((initial.columns || []).map((column) => [column.key, column.page])),
      selected: null, detail: null, loading: false, sequence: 0, detailSequence: 0, listController: null,
      showArchiveQueues: new URL(win.location.href).searchParams.get("queues") === "all"};
    const el = (tag, cls, text) => { const node = doc.createElement(tag); if (cls) node.className = cls; if (text != null) node.textContent = text; return node; };
    const icon = (name) => {
      const paths = {
        user: ["M20 21v-2a7 7 0 0 0-14 0v2", "M17 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0"],
        phone: ["M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 2 .7 2.9a2 2 0 0 1-.5 2.1L8 10a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.5c.9.3 1.9.6 2.9.7a2 2 0 0 1 1.7 2Z"],
        message: ["M21 15a3 3 0 0 1-3 3H8l-5 4V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3Z", "M7 8h10M7 12h6"],
        clock: ["M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0", "M12 6v6l4 2"],
        check: ["M20 6 9 17l-5-5"],
        close: ["M18 6 6 18M6 6l12 12"],
        alert: ["M12 8v5m0 3h.01M11 3.5l-9 16A1 1 0 0 0 3 21h18a1 1 0 0 0 1-1.5l-9-16a1.1 1.1 0 0 0-2 0Z"],
        note: ["M8 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2h-3", "M8 2h8v4H8zM7 11h10M7 16h7"]
      };
      const svg = doc.createElementNS("http://www.w3.org/2000/svg", "svg");
      for (const [key, value] of Object.entries({viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false", class: "crm-icon"})) svg.setAttribute(key, value);
      for (const value of paths[name] || paths.note) { const path = doc.createElementNS("http://www.w3.org/2000/svg", "path"); path.setAttribute("d", value); svg.append(path); }
      return svg;
    };
    const button = (text, action, primary = false) => { const node = el("button", "crm-live-button" + (primary ? " crm-live-button-primary" : ""), text); node.type = "button"; if (action) node.addEventListener("click", action); return node; };
    const dateText = (value) => value ? new Intl.DateTimeFormat("ru-RU", {timeZone: initial.timezone, dateStyle: "medium", timeStyle: "short"}).format(new Date(value)) : "Срок не указан";
    const notice = (text) => { byId("lead-notice").textContent = text; byId("lead-notice").hidden = !text; };
    const errorNode = (form, error) => {
      let node = form.querySelector(".lead-error"); if (!node) { node = el("p", "lead-error"); node.setAttribute("role", "alert"); form.prepend(node); }
      node.textContent = error.message || "Не удалось выполнить действие.";
    };
    function urlState() {
      const url = new URL(win.location.href);
      for (const [key, value, base] of [["q", state.q, ""], ["mode", state.mode, "all"], ["owner", state.owner, "all"], ["status", state.status, ""], ["page", String(state.mode === "closed" ? state.page : 1), "1"]]) {
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
    async function write(url, payload) {
      const controller = new AbortController(), timer = win.setTimeout(() => controller.abort(), 20000);
      try {
        const response = await win.fetch(url, {method: "POST", credentials: "same-origin", signal: controller.signal,
          headers: {"Content-Type": "application/json", "X-CSRFToken": csrf(), Accept: "application/json"}, body: JSON.stringify(payload)});
        let body; try { body = await response.json(); } catch (_error) { throw new Error("Результат сохранения неизвестен. Проверьте эту же операцию."); }
        if (!response.ok) { const error = new Error(body.error || "Не удалось сохранить."); error.code = body.code; error.definitive = response.status >= 400 && response.status < 500 && ![408, 429].includes(response.status); throw error; }
        return body;
      } finally { win.clearTimeout(timer); }
    }
    const operations = new LeadOperations(async (payload) => {
        const url = new URL(initial.urls.command, win.location.origin);
        if (["lead.qualify", "lead.add_deal"].includes(payload.command_type)) url.searchParams.set("view", "conversion");
        const body = await write(url, payload);
        const transferred = body.result?.assignment_saved === true && body.result.can_open === false && Number.isInteger(body.result.lead_id);
        if (!transferred && (!body.result || !body.lead || body.result.lead_id !== body.lead.id || !Number.isInteger(body.lead.revision))) throw new Error("Сервер не подтвердил результат. Проверьте эту же операцию.");
        return body;
    }, () => { if (!bulk?.isRunning()) renderOperations(); });
    const bulk = win.CrmBulk?.mount({root, kind: "lead", offset: initial.utc_offset, deadlineISO, moveTargets,
      items: () => state.mode === "closed" ? state.data.leads : (state.data.columns || []).filter(column => state.showArchiveQueues || !["spam", "deleted"].includes(column.key)).flatMap(column => column.leads),
      loading: () => state.loading, pending: id => operations.forSubject(id), notice,
      detail: async (id, signal) => (await read(initial.urls.detail_prefix + id + "/?org_id=" + initial.organization_id, signal)).lead,
      candidates: async (id, signal) => {
        const url = new URL(initial.urls.responsibles, win.location.origin); url.searchParams.set("kind", "lead"); url.searchParams.set("subject_id", id); url.searchParams.set("org_id", initial.organization_id);
        return (await read(url, signal)).users;
      },
      run: async (payload, label) => { const item = operations.add(payload, label); const result = await operations.run(item.payload.operation_key); if (result.status === "succeeded") operations.dismiss(item.payload.operation_key); return result; },
      runBatch: records => win.CrmBulk.runBatch(operations, records, payload => write(initial.urls.bulk_command, payload)),
      reload: () => { renderOperations(); return loadList({keepNotice: true, keepSelection: true}); },
    });
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
      content.querySelectorAll("button[type=submit],.crm-inline button,.crm-inline input,.crm-inline select,.crm-inline textarea").forEach((node) => { node.disabled = Boolean(pending); });
      content.querySelectorAll("[data-assignment-form] input,[data-assignment-form] select,[data-assignment-form] button").forEach(node => { node.disabled = Boolean(pending); });
      byId("lead-create").disabled = Boolean(operations.forSubject(null));
    }
    function applyResult(item) {
      if (item.status === "succeeded") {
        bulk?.remove(item.payload.subject_id);
        if (item.response.result?.assignment_saved && item.response.result.can_open === false) {
          clearSelection(); dialog.close(); operations.dismiss(item.payload.operation_key);
          notice("Ответственный назначен. Карточка передана сотруднику и больше недоступна в вашем списке."); loadList({keepNotice:true}); return item;
        }
        const completed = ["lead.qualify", "lead.add_deal", "lead.complete_customer"].includes(item.payload.command_type)
          || item.payload.command_type === "lead.disposition" && (["history", "failed"].includes(item.payload.changes.column) || item.payload.changes.target_deal_id)
          || item.payload.command_type === "lead.transition" && item.payload.changes.status === "closed";
        if (completed && state.selected === item.payload.subject_id) {
          const dealId = item.response.result?.deal_ids?.[0];
          const deal = item.response.deals?.find(row => row.id === dealId);
          operations.dismiss(item.payload.operation_key);
          clearSelection();
          if (dialog.open) dialog.close();
          if (deal?.url && !operations.pending().length) {
            win.location.assign(deal.url);
            return item;
          }
          notice(dealId && !deal ? "Сделка создана и передана ответственному." : "Изменения сохранены.");
          if (deal?.url) {
            const link = el("a", "", "Открыть сделку"); link.href = deal.url; byId("lead-notice").append(" ", link);
          }
          loadList({keepNotice: true});
          return item;
        }
        if (state.selected === item.payload.subject_id) {
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
    const columnLabels = {unparsed: "Не разобраны", working: "В работе", history: "Завершённые", failed: "Лид провален", spam: "Спам", deleted: "Удалённые", restore: "Восстановить"};
    let dragged = null, suppressClickUntil = 0;
    const dragActions=el("div","crm-drag-actions");dragActions.hidden=true;root.append(dragActions);
    function endDrag(){if(dragged)suppressClickUntil=Date.now()+250;dragged=null;dragActions.hidden=true;root.querySelectorAll(".is-drop-target,.is-dragging").forEach(node=>node.classList.remove("is-drop-target","is-dragging"));}
    const canComplete = lead => Boolean(lead && (lead.can_qualify || lead.can_complete_customer || moveTargets(lead).includes("history")));
    const dropActions = [
      ["lead-conversion-drop", "Завершить лид", "check", canComplete, lead => conversionDialog(lead, false, null)],
      ["crm-failure-drop", "Лид провален", "close", lead => moveTargets(lead).includes("failed"), lead => moveLead(lead, "failed")],
      ["lead-spam-drop", "Спам", "alert", lead => moveTargets(lead).includes("spam"), lead => moveLead(lead, "spam")],
    ].map(([css, label, symbol, allowed, apply]) => {
      const zone = el("div", css + " crm-drag-target");
      zone.append(icon(symbol), el("span", "", label));
      zone.addEventListener("dragover", event => {
        if (dragged && !state.loading && allowed(dragged)) {
          event.preventDefault(); event.dataTransfer.dropEffect = "move"; zone.classList.add("is-drop-target");
        }
      });
      zone.addEventListener("dragleave", () => zone.classList.remove("is-drop-target"));
      zone.addEventListener("drop", event => {
        event.preventDefault();
        const lead = dragged; endDrag();
        if (lead && !state.loading && allowed(lead)) apply(lead);
      });
      dragActions.append(zone);
      return {zone, allowed};
    });
    win.addEventListener("keydown",e=>{if(e.key==="Escape")endDrag();});
    win.addEventListener("blur",endDrag);
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
      if (bulk) row.append(bulk.checkbox({...lead, title: displayTitle(lead)}));
      const title = el("div", "lead-card-main"), middle = el("div", "lead-card-owner"), next = el("footer", "lead-card-task");
      const heading = button(displayTitle(lead), () => {if(Date.now()>=suppressClickUntil)openLead(lead.id);}); heading.className = "lead-card-heading"; title.append(heading);
      if(lead.needs_review){const mark=el("span","lead-review-marker");mark.title="Есть обращение для разбора";mark.setAttribute("aria-label","Есть обращение для разбора");title.append(mark);}
      const customer = lead.client?.name || lead.source?.contact?.name || lead.source?.contact_name || lead.source?.phone;
      if (customer && !displayTitle(lead).split(" · ").includes(customer)) title.append(el("p", "lead-card-customer", customer));
      if(lead.need)title.append(el("p","lead-card-need",lead.need.slice(0,160)));
      const meta = el("div", "lead-card-meta"), source = el("span", "lead-card-source");
      source.append(icon(lead.source?.type === "call" ? "phone" : lead.source?.type === "message" ? "message" : "note"), el("span", "", lead.source?.label || "Обращение")); meta.append(source);
      const received = lead.source?.occurred_at || lead.source?.received_at;
      if (received) { const stamp = el("span", "lead-card-received", lead.source.occurred_at ? dateText(received) : "Получено: " + dateText(received) + " · время события неизвестно"); meta.append(stamp); }
      title.append(meta);
      if (lead.can_restore) title.append(button("Восстановить", () => moveLead(lead, "restore")));
      const avatar = el("span", "lead-card-avatar"); avatar.setAttribute("aria-hidden", "true");
      if (lead.assignee?.name) avatar.textContent = lead.assignee.name.trim().split(/\s+/).slice(0, 2).map(part => part[0]).join("").toLocaleUpperCase("ru-RU"); else avatar.append(icon("user"));
      middle.title = "Ответственный"; middle.append(avatar, el("span", "lead-card-owner-name", lead.assignee?.name || "Не назначен"));
      const action = lead.next_action;
      const overdue = action?.due_at && !action.restricted && !action.completed && !action.waiting && new Date(action.due_at).getTime() < Date.now();
      next.dataset.state = action?.restricted ? "restricted" : action?.completed ? "completed" : action?.waiting ? "waiting" : overdue ? "overdue" : action?.title ? "planned" : "missing";
      const taskCopy = el("div", "lead-card-task-copy");
      taskCopy.append(el("p", "lead-card-task-title", action?.restricted ? "Следующий шаг ограничен доступом" : action?.completed ? "Предыдущий шаг завершён" : action?.title || (lead.can_take ? "Можно взять в работу" : "Следующий шаг не указан")));
      if (action?.due_at && !action.restricted) taskCopy.append(el("p", "lead-card-due" + (overdue ? " is-overdue" : ""), (overdue ? "Просрочено · " : "") + dateText(action.due_at)));
      next.append(icon(action?.completed ? "check" : "clock"), taskCopy);
      row.append(title, middle, next);
      const targets = moveTargets(lead);
      if (targets.length || lead.can_qualify) {
        row.draggable = !state.loading && !operations.forSubject(lead.id);
        row.addEventListener("dragstart", (event) => {
          if (!row.draggable || bulk?.hasSelection() || operations.forSubject(lead.id)) { event.preventDefault(); return; }
          dragged = lead;
          for (const {zone, allowed} of dropActions) zone.hidden = !allowed(lead);
          dragActions.hidden = dropActions.every(({zone}) => zone.hidden);
          event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", String(lead.id)); row.classList.add("is-dragging");
        });
        row.addEventListener("dragend", endDrag);

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
      const statusFilter = byId("lead-legacy-status");
      statusFilter.hidden = !state.status;
      statusFilter.textContent = state.status ? (initial.states?.[state.status] || state.status) + " ×" : "";
      const intake = data.intake || {};
      byId("lead-intake-status").textContent = intakeStatusText(intake, dateText);
      root.querySelectorAll("[data-mode]").forEach((node) => { if (node.dataset.mode === (legacyOwners[state.mode] ? "all" : state.mode)) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current"); });
      if (state.mode === "closed") {
        list.append(el("h2", "", "История завершённых"));
        if (!data.leads.length) list.append(el("p", "lead-empty", "Завершённых лидов по этим условиям нет."));
        for (const lead of data.leads) list.append(card(lead));
      } else for (const column of data.columns || []) {
        if (!state.showArchiveQueues && ["spam","deleted"].includes(column.key)) continue;
        const section = el("section", "lead-column"); section.dataset.column = column.key;
        section.setAttribute("aria-label", column.label);
        const heading = el("h2", "lead-column-heading"); heading.setAttribute("aria-label", column.label + " · " + column.total);
        heading.append(el("span", "lead-column-label", column.label), el("span", "lead-column-count", String(column.total))); section.append(heading);
        const cards = el("div", "lead-column-cards");
        if (!column.leads.length) cards.append(el("p", "lead-empty", state.q || state.status || state.owner !== "all" ? "Нет совпадений" : "Пока пусто"));
        for (const lead of column.leads) cards.append(card(lead));
        section.append(cards);
        section.addEventListener("dragover", (event) => { if (dragged && !state.loading && moveTargets(dragged).includes(column.key)) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; section.classList.add("is-drop-target"); } });
        section.addEventListener("dragleave", (event) => { if (!section.contains(event.relatedTarget)) section.classList.remove("is-drop-target"); });
        section.addEventListener("drop", (event) => { event.preventDefault(); const lead = dragged; endDrag(); if (lead && !state.loading && moveTargets(lead).includes(column.key)) moveLead(lead, column.key); });
        if (column.cursor) {
          section.append(el("p", "lead-column-progress", `Загружено ${column.leads.length} из ${column.total}`));
          const more = button("Показать ещё 50", () => loadList({column:column.key}));
          more.dataset.action="column-more"; more.disabled=state.loading; section.append(more);
        }
        list.append(section);
      }
      list.scrollLeft=scrollLeft;
      bulk?.refresh();
      renderOperations();
    }
    async function moveLead(snapshot, target) {
      if (target === "history") { conversionDialog(snapshot, false, null); return; }
      if (operations.forSubject(snapshot.id)) { notice("Сначала проверьте предыдущее сохранение этой карточки."); return; }
      dialog.classList.toggle("lead-compact-dialog", ["failed", "spam"].includes(target));
      const sequence = ++state.detailSequence; state.selected = snapshot.id; urlState();
      const moveTitle = {failed: "Лид провален", spam: "Переместить в спам"};
      content.replaceChildren(el("h2", "", moveTitle[target] || "Перемещение лида")); content.firstChild.id = "lead-dialog-title";
      if (!dialog.open) dialog.showModal();
      try {
        const data = await read(initial.urls.detail_prefix + snapshot.id + "/?org_id=" + initial.organization_id);
        if (sequence !== state.detailSequence) return;
        const lead = data.lead;
        if (lead.revision !== snapshot.revision || !moveTargets(lead).includes(target)) throw new Error("Карточка уже изменена или действие недоступно. Обновите канбан.");
        const free = !lead.assignee;
        const form = makeForm(content, columnLabels[target], free ? "Первое успешное сохранение закрепит свободный лид за вами." : "Перемещение сохранится после подтверждения. История и связи останутся в карточке.");
        if (moveTitle[target]) form.parentElement.querySelector("h3").remove();
        const ranks={unparsed:0,working:1,history:2,failed:2,spam:3,deleted:4};
        const required=["failed","spam","deleted","restore"].includes(target)||ranks[target]<ranks[lead.column];
        const reasonLabel = {failed: "Причина провала", spam: "Почему это спам"};
        const reason = required ? field(form, reasonLabel[target] || "Причина перемещения", "", "textarea") : null;
        if(reason){reason.required=true;reason.maxLength=500;}
        const existingDeal = target === "restore" && (lead.result === "spam" || lead.status !== "closed")
          ? picker(form, "Активная сделка клиента, если есть", initial.urls.targets, "targets", null, () => "deal") : null;
        if(target==="history")form.append(el("p","lead-form-note","Лид будет завершён без создания сделки. Открытый следующий шаг отменится; сделки, выполнение и оплата не изменятся."));
        let next = null;
        if (target === "working" && (!lead.next_action || lead.next_action.completed)) next = nextFields(form, null);
        if (["failed","spam","deleted"].includes(target)) form.append(el("p", "lead-form-note", "Текущий незавершённый шаг будет отменён с сохранением истории."));
        const confirmLabel = {failed: "Подтвердить провал", spam: "Отправить в спам"};
        formSubmit(form, confirmLabel[target] || (free ? "Взять на себя и переместить" : "Подтвердить перемещение"), () => {
          const changes = {column: target, reason: reason ? reason.value.trim() : ""}; if (next) changes.next_action = next();
          if (existingDeal?.value()) changes.target_deal_id = existingDeal.value();
          if ((changes.target_deal_id || ["failed", "spam", "deleted", "history"].includes(target)) && lead.next_action?.version && !lead.next_action.completed) changes.expected_task_version = lead.next_action.version;
          submit(form, "lead.disposition", lead, changes);
        });
        form.append(button("Отмена",()=>{clearSelection();dialog.close();}));
        if(reason)reason.focus();
        renderOperations();
      } catch (error) { if (sequence === state.detailSequence) content.append(el("p", "lead-error", error.message)); }
    }
    async function loadList(options) {
      const more=options?.column ? (state.data.columns || []).find(c=>c.key===options.column) : null;
      if (options?.column && (!more?.cursor || state.loading)) return;
      if (!more && !options?.keepSelection) bulk?.reset();
      const previous=state.data;
      if (state.listController) state.listController.abort();
      const controller = new AbortController(), sequence = ++state.sequence; state.listController = controller; state.loading = true; renderList(); urlState();
      const url = new URL(initial.urls.data, win.location.origin);
      for (const [key, value] of Object.entries({q: state.q, mode: state.mode, owner: state.owner, status: state.status, page: state.mode === "closed" ? state.page : 1})) url.searchParams.set(key, value);
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
    function responsibleField(host, {kind="lead", subjectId=null, leadId=null, selectedId=null, notify=true} = {}) {
      if (!initial.urls.responsibles) return null;
      const input = select(host, "Ответственный", [["", "Загружаем сотрудников…"]]);
      input.name = "responsible_id"; input.required = true; input.setAttribute("aria-label", "Ответственный");
      const notification = notify ? "Выбранный сотрудник получит уведомление согласно своим настройкам." : "";
      const message = el("small", "lead-form-note"); input.parentElement.append(message);
      const retry = button("Повторить загрузку сотрудников"), wrap = input.parentElement;
      retry.hidden = true; wrap.append(retry);
      const load = async () => {
        retry.hidden = true; input.replaceChildren(el("option", "", "Загружаем сотрудников…")); input.firstChild.value = "";
        try {
          const url = new URL(initial.urls.responsibles, win.location.origin); url.searchParams.set("kind", kind);
          if (subjectId != null) url.searchParams.set("subject_id", subjectId); if (leadId != null) url.searchParams.set("lead_id", leadId);
          const data = await read(url); if (!wrap.isConnected) return;
          input.replaceChildren(); const placeholder = el("option", "", "Выберите сотрудника"); placeholder.value = ""; input.append(placeholder);
          for (const user of data.users || []) { const option = el("option", "", user.name); option.value = user.id; input.append(option); }
          input.value = String(selectedId ?? data.current_user_id ?? "");
          message.textContent = data.users?.length ? notification : "Нет сотрудников, которым доступна эта карточка.";
        } catch (error) { if (!wrap.isConnected) return; input.replaceChildren(); const empty = el("option", "", "Сотрудники не загружены"); empty.value = ""; input.append(empty); message.textContent = error.message; retry.hidden = false; }
      };
      retry.addEventListener("click", load); load(); return input;
    }
    function assignmentForm(host, lead) {
      const form = makeForm(host, "Назначить ответственного", lead.assignment_note || "Выберите нового ответственного. Передачу текущего следующего действия можно подтвердить отдельно.");
      form.dataset.assignmentForm = "";
      const fields = el("div", "lead-fields"); form.append(fields);
      const responsible = responsibleField(fields, {subjectId:lead.id, selectedId:lead.assignee?.id});
      const task = lead.assignment_task;
      const transfer = task?.can_transfer ? field(form, "Передать текущее следующее действие", "", "checkbox") : null;
      if (transfer) { transfer.checked = true; transfer.name = "transfer_next_action"; transfer.parentElement.classList.add("lead-checkbox"); form.append(el("p", "lead-form-note", (task.title || "Текущий шаг") + ". Остальные задачи сохранят своих исполнителей.")); }
      else if (task?.reason) form.append(el("p", "lead-form-note", task.reason));
      formSubmit(form, "Назначить ответственного", () => {
        if (!responsible?.value) throw new Error("Выберите сотрудника из списка.");
        const changes = {responsible_id:Number(responsible.value), transfer_next_action:Boolean(transfer?.checked)};
        if (transfer?.checked) changes.expected_task_version = task.version;
        submit(form, "lead.assign", lead, changes);
      });
      const cancel = button("Отмена", () => openLead(lead.id)); cancel.classList.add("lead-cancel"); form.append(cancel);
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
      const responsible = creating ? responsibleField(fields, {leadId:kind === "lead.new_need" ? lead.id : null}) : null;
      if (!creating) need.dataset.leadNeed = "true";
      if (!creating && lead.next_action?.restricted) {
        form.append(el("p", "lead-form-note", "Следующее действие осталось у прежнего исполнителя и ограничено доступом. Здесь можно изменить название и потребность лида."));
        formSubmit(form, "Сохранить изменения", () => submit(form, "lead.edit", lead, leadEditChanges(lead, {title:title.value.trim(), need:need.value.trim()})));
        return;
      }
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
        if (responsible) { if (!responsible.value) throw new Error("Выберите ответственного."); changes.responsible_id = Number(responsible.value); }
        if (!creating && canNext && lead.next_action && !lead.next_action.restricted && !lead.next_action.completed) changes.expected_task_version = lead.next_action.version;
        submit(form, kind, kind === "lead.create" ? null : lead, creating ? changes : leadEditChanges(lead, changes));
      });
    }
    function transitionForm(host, lead) {
      const form = makeForm(host, "Результат разбора / ожидание"), fields = el("div", "lead-fields"); form.append(fields);
      const status = select(fields, "Состояние", [["working", "В работе"], ["qualifying", "Уточнение"], ["waiting", "Ожидание"], ["deferred", "Отложен"], ["closed", "Разбор завершён"]], lead.status === "new" ? "working" : lead.status);
      const result = select(fields, "Результат завершения", [["non_target", "Нецелевой"], ["spam", "Спам"], ["duplicate", "Продолжение существующей потребности"], ["no_deal", "Закрыт без сделки"], ["linked", "Относится к существующей сделке"]]);
      if (lead.can_qualify) { const option = el("option", "", "Создать сделку, контакт/компанию"); option.value = "create_deal"; result.append(option); }
      if (lead.can_complete_customer) { const option = el("option", "", "Сохранить контакт/компанию без сделки"); option.value = "save_customer"; result.append(option); }
      const reason = field(fields, "Причина / пояснение", "", "textarea", true); reason.maxLength = 500;
      const follow = field(fields, "Когда вернуться · " + initial.timezone, localDateTime(lead.follow_up_at, initial.timezone), "datetime-local");
      fields.append(el("p", "lead-field wide lead-form-note", "Подтвердите, что это та же потребность: телефон сам по себе не основание для связи. История сохранится, открытый шаг исходного лида отменится. Задачи и сделки выбранной работы не изменятся. Доступны ваши активные независимые лиды."));
      const targets = picker(fields, "Подтверждённая потребность / сделка", initial.urls.targets + "&exclude_id=" + lead.id, "targets", null, () => result.value === "linked" ? "deal" : "lead");
      const update = () => { result.parentElement.hidden = status.value !== "closed"; follow.parentElement.hidden = !["waiting", "deferred"].includes(status.value); targets.element.hidden = status.value !== "closed" || !["duplicate", "linked"].includes(result.value); targets.clear(); };
      status.addEventListener("change", update); result.addEventListener("change", update); update();
      formSubmit(form, "Сохранить состояние", () => {
        if (status.value === "closed" && ["create_deal", "save_customer"].includes(result.value)) { conversionDialog(lead, false, result.value === "create_deal" ? "deal" : "customer"); return; }
        const changes = {status: status.value, reason: reason.value.trim()};
        if (["waiting", "deferred"].includes(status.value)) { changes.follow_up_at = deadlineISO(follow.value, initial.utc_offset); if (lead.next_action && !lead.next_action.restricted && !lead.next_action.completed) changes.expected_task_version = lead.next_action.version; }
        if (status.value === "closed") { changes.result = result.value; if (result.value === "linked") changes.target_deal_id = targets.value(); if (result.value === "duplicate") changes.target_lead_id = targets.value(); }
        submit(form, "lead.transition", lead, changes);
      });
    }
    function customerFields(host, lead, newByDefault = false, shared = null) {
      if (shared?.editor) { host.append(shared.editor.element); return shared.editor; }
      const savedContact = lead.contact || (lead.client?.client_type === "private" ? lead.client : null);
      const savedCompany = lead.company || (lead.client?.client_type === "legal" ? lead.client : null);
      const editor = win.Service2CustomerEditor.mount(host, {lookupUrl:initial.urls.clients, organizationId:initial.organization_id, canCategorize:initial.can_categorize_contacts,
        contact:savedContact || {name:lead.source?.contact_name || "", phone:lead.source?.phone || ""}, company:savedCompany,
        contactMode:newByDefault || lead.source?.phone || lead.source?.contact_name ? "new" : "existing"});
      if (shared) shared.editor = editor;
      return editor;
    }
    function qualificationForm(host, lead, additional = false, sharedCustomer = null, mode = "deal") {
      const form = makeForm(host, "Данные сделки");
      form.classList.add("lead-conversion-form");
      form.parentElement.classList.add("lead-conversion");
      const stepHeading=(number,label)=>{const heading=el("h3","lead-conversion-heading"),badge=el("span","lead-step-number",number);badge.setAttribute("aria-hidden","true");heading.append(badge,el("span","lead-step-label",label));return heading;};
      form.parentElement.firstElementChild.remove();
      const details=el("section","lead-conversion-section lead-conversion-details"),fields=el("div","lead-fields");details.append(stepHeading("1","Данные сделки"),fields);form.append(details);
      if(!lead.assignee)form.append(el("p","lead-form-note","При подтверждении вы возьмёте свободный лид на себя."));
      if(lead.is_archived)form.append(el("p","lead-form-note","При подтверждении лид будет восстановлен и конвертирован. История удаления сохранится."));
      const title=field(fields,"Название сделки",lead.title,"text",true);title.required=true;title.maxLength=255;
      const pipeline=select(fields,"Воронка",initial.pipelines.map(p=>[String(p.id),p.name]),String(initial.pipelines.find(p=>p.code===lead.direction)?.id || initial.pipelines[0]?.id || ""));pipeline.required=true;
      const responsible = responsibleField(fields, {kind:"deal", leadId:lead.id, notify:false});
      const amount = field(fields, "Коммерческая сумма, ₽", lead.budget_kind === "exact" && lead.currency === "RUB" ? win.CrmCardActivity.formatMoney(lead.budget_min) : "");
      amount.inputMode = "decimal"; amount.maxLength = 40;
      const amountBasis = select(fields, "Основание суммы", [["indicative","Предварительная оценка"],["agreed","Согласовано с клиентом"]], "indicative");
      const updateAmount = () => { amountBasis.parentElement.hidden = !amount.value.trim(); };
      amount.addEventListener("input", updateAmount); updateAmount();
      const need=additional?null:field(fields,"Потребность",lead.need||"","textarea",true);if(need){need.required=true;need.maxLength=10000;need.readOnly=!lead.can_edit_conversion_need;if(!lead.can_edit_conversion_need&&!lead.need)form.append(el("p","lead-error","Для уточнения потребности требуется право редактирования лида."));}
      const customerSection=el("section","lead-conversion-section lead-conversion-customer");customerSection.append(stepHeading("2","Контакт и компания"));form.append(customerSection);
      const customerEditor=customerFields(customerSection, lead, false, sharedCustomer);
      sharedCustomer?.hosts.set(mode, customerSection);
      const extra = el("details", "lead-conversion-section");
      extra.append(el("summary", "", "Объект и другие сведения")); form.append(extra);
      const work = field(extra, "Предмет работ / товаров", (lead.need || "").slice(0, 2000), "textarea"); work.maxLength = 2000;
      const specialization = field(extra, "Специализация", lead.specialization || ""); specialization.maxLength = 40;
      const fulfillment = select(extra, "Направление исполнения", [["unknown","Не уточнено"],["construction","Строительство / монтаж"],["service","Обслуживание"],["retail","Поставка"],["mixed","Смешанное"]], ["construction","service","retail"].includes(lead.direction) ? lead.direction : "unknown");
      const decision = field(extra, "Ожидаемое коммерческое решение · " + initial.timezone, "", "datetime-local");
      const object = select(extra, "Объект сделки", [["none","Не выбран"],["planned","Планируемый объект"]], "none");
      const objectName = field(extra, "Название объекта"); objectName.maxLength = 255;
      const address = field(extra, "Адрес объекта", lead.location || ""); address.maxLength = 500;
      const updateObject = () => {
        const planned = object.value === "planned"; objectName.required = planned;
        objectName.parentElement.hidden = address.parentElement.hidden = !planned;
      };
      object.addEventListener("change", updateObject); updateObject();
      let objectClient, objectGeneration = 0;
      if (initial.urls.objects) {
        const query = field(extra, "Поиск существующего объекта", "", "search"); query.maxLength = 200;
        const feedback = el("p", "crm-live-muted"); feedback.setAttribute("role", "status");
        extra.append(button("Найти объекты клиента", async () => {
          const selected = customerEditor.primarySelection();
          const mine = ++objectGeneration;
          for (const option of [...object.options].slice(2)) option.remove();
          updateObject();
          if (!selected) { feedback.textContent = "Сначала выберите существующего клиента."; return; }
          const url = new URL(initial.urls.objects, win.location.origin);
          url.searchParams.set("client_id", selected.id); url.searchParams.set("q", query.value.trim());
          feedback.textContent = "Ищем объекты…";
          try {
            const data = await read(url);
            if (mine !== objectGeneration || customerEditor.primarySelection()?.id !== selected.id || !extra.isConnected) return;
            objectClient = selected.id;
            for (const entry of data.objects) {
              const option = el("option", "", [entry.name, entry.address].filter(Boolean).join(" · "));
              option.value = entry.kind + ":" + entry.id; object.append(option);
            }
            feedback.textContent = data.objects.length ? "Выберите объект в списке." : "Объекты по запросу не найдены.";
          } catch (error) { if (mine === objectGeneration) feedback.textContent = error.message; }
        }), feedback);
      }
      const toggleFields=(container,visible)=>{container.hidden=!visible;container.querySelectorAll("input,select,textarea").forEach(input=>{input.disabled=!visible;});};
      const transferable=!additional&&lead.next_action&&!lead.next_action.completed&&!lead.next_action.restricted&&!lead.next_action.waiting;
      const nextSection=el("section","lead-conversion-section lead-conversion-next");nextSection.append(stepHeading("3","Следующее действие"));form.append(nextSection);
      const reuse=transferable?field(nextSection,"Сохранить открытый следующий шаг","","checkbox"):null;if(reuse){reuse.checked=true;reuse.parentElement.classList.add("lead-checkbox");}
      if (transferable) nextSection.append(el("p", "lead-form-note", "Сохранённый шаг будет передан выбранному ответственному за сделку. Остальные задачи сохранят своих исполнителей."));
      const transferred=transferable?el("p","lead-transfer-summary",lead.next_action.title+" · "+dateText(lead.next_action.due_at)):null;if(transferred)nextSection.append(transferred);
      const nextHost=el("div","lead-fields");nextSection.append(nextHost);const next=nextFields(nextHost,transferable?lead.next_action:null);
      const toggle=()=>{toggleFields(nextHost,!reuse?.checked);if(transferred)transferred.hidden=!reuse?.checked;};if(reuse)reuse.addEventListener("change",toggle);toggle();
      formSubmit(form,"Подтвердить создание сделки",()=>{
        const row={title:title.value.trim(),pipeline_id:Number(pipeline.value),next_action:reuse?.checked?{title:lead.next_action.title,due_at:lead.next_action.due_at}:next()};
        if (responsible) { if (!responsible.value) throw new Error("Выберите ответственного."); row.responsible_id = Number(responsible.value); }
        row.customer=customerEditor.value();
        const [objectKind, objectId] = object.value.split(":");
        if (objectId && objectClient !== customerEditor.primarySelection()?.id) throw new Error("Клиент изменился. Выберите объект заново.");
        row.card_fields = {work_subject:work.value.trim(), specialization:specialization.value.trim(), fulfillment_kind:fulfillment.value,
          expected_decision_at:decision.value ? deadlineISO(decision.value, initial.utc_offset) : null, object_kind:objectKind};
        if (objectKind === "planned") Object.assign(row.card_fields, {planned_object_name:objectName.value.trim(), planned_object_address:address.value.trim()});
        if (objectId) row.card_fields[objectKind === "pool" ? "pool_id" : "client_object_id"] = Number(objectId);
        if (amount.value.trim()) Object.assign(row.card_fields, {amount:win.CrmCardActivity.parseMoney(amount.value), currency:"RUB", amount_basis:amountBasis.value});
        const changes={deals:[row]};if(need)changes.need=need.value.trim();if(reuse?.checked){row.reuse_next_action=true;changes.expected_task_version=lead.next_action.version;}
        submit(form,additional?"lead.add_deal":"lead.qualify",lead,changes);
      });
      const cancel=button("Отмена",()=>openLead(lead.id));cancel.classList.add("lead-cancel");form.append(cancel);
    }
    function completeCustomerForm(host, lead, sharedCustomer = null) {
      const form = makeForm(host, "Контакт и компания", "Сохраните контакт, компанию или оба сразу. Они останутся связаны с этим лидом. Сделка не создаётся.");
      form.dataset.completionForm = "customer";
      if (!lead.assignee) form.append(el("p", "lead-form-note", "При подтверждении вы возьмёте свободный лид на себя."));
      const customerHost = el("div"); form.append(customerHost);
      const editor = customerFields(customerHost, lead, true, sharedCustomer);
      sharedCustomer?.hosts.set("customer", customerHost);
      const reason = field(form, "Комментарий к завершению", "", "textarea", true); reason.maxLength = 500;
      const summary = el("div", "lead-completion-summary");
      summary.append(el("strong", "", "Что произойдёт после сохранения"), el("p", "", "Лид перейдёт в «Завершённые». Выбранные записи сохранятся в справочнике."));
      if (lead.next_action && !lead.next_action.completed) summary.append(el("p", "", "Незавершённый шаг лида будет отменён с сохранением истории."));
      form.append(summary);
      formSubmit(form, "Сохранить и завершить лид", () => {
        const changes = {customer:editor.value(), reason:reason.value.trim()};
        if (lead.next_action?.version && !lead.next_action.completed) changes.expected_task_version = lead.next_action.version;
        submit(form, "lead.complete_customer", lead, changes);
      });
      const cancel = button("Отмена", () => openLead(lead.id)); cancel.classList.add("lead-cancel"); form.append(cancel);
    }
    function completeEmptyForm(host, lead) {
      const form = makeForm(host, "Завершить без создания", "Новые записи не создаются. История и уже связанные контакты, компании и сделки сохранятся.");
      form.dataset.completionForm = "empty";
      const reason = field(form, "Комментарий к завершению", "", "textarea", true); reason.maxLength = 500; reason.required = true;
      if (lead.next_action && !lead.next_action.completed) form.append(el("p", "lead-form-note", "Незавершённый шаг лида будет отменён с сохранением истории."));
      formSubmit(form, "Подтвердить завершение", () => {
        if (!reason.value.trim()) throw new Error("Напишите комментарий к завершению.");
        const changes = {column:"history", reason:reason.value.trim()};
        if (lead.next_action?.version && !lead.next_action.completed) changes.expected_task_version = lead.next_action.version;
        submit(form, "lead.disposition", lead, changes);
      });
      const cancel = button("Отмена", () => openLead(lead.id)); cancel.classList.add("lead-cancel"); form.append(cancel);
    }
    function completionOptions(host, snapshot, loadLead, isCurrent, selectedMode) {
      const choices = el("div", "lead-completion-options"); choices.setAttribute("role", "group"); choices.setAttribute("aria-label", "Результат завершения лида");
      host.append(choices);
      const panels = new Map(), rendered = new Set(), customer = {editor:null, hosts:new Map()};
      const dealModes = [
        ["deal", "Создать сделку"],
        ["deal_contact", "Создать сделку и контакт"],
        ["deal_contact_company", "Создать сделку, контакт и компанию"]
      ];
      const definitions = [
        ...dealModes.map(([key, name]) => [key, name, lead => lead.can_qualify,
          lead => qualificationForm(panels.get(key), lead, false, customer, key)]),
        ["customer", "Сохранить контакт/компанию", lead => lead.can_complete_customer,
          lead => completeCustomerForm(panels.get("customer"), lead, customer)],
        ["empty", "Завершить без создания", lead => moveTargets(lead).includes("history"),
          lead => completeEmptyForm(panels.get("empty"), lead)]
      ];
      let selection = 0;
      const back = button("К вариантам завершения", showChoices);
      back.classList.add("lead-completion-back"); back.hidden = true; host.append(back);
      function showChoices() {
        if (operations.forSubject(snapshot.id)) return;
        ++selection;
        for (const panel of panels.values()) panel.hidden = true;
        byId("lead-dialog-title").textContent = "Завершить лид";
        dialog.classList.add("lead-compact-dialog");
        choices.hidden = false; back.hidden = true;
        choices.querySelector("button")?.focus();
      }
      const activate = async ([key, name, allowed, build]) => {
        if (operations.forSubject(snapshot.id)) return;
        const selected = ++selection;
        choices.hidden = true; back.hidden = false;
        for (const [mode, panel] of panels) panel.hidden = mode !== key;
        const panel = panels.get(key);
        if (!rendered.has(key)) panel.replaceChildren(el("p", "lead-form-note", "Загружаем данные лида…"));
        byId("lead-dialog-title").textContent = name;
        try {
          const lead = await loadLead();
          if (!isCurrent() || selected !== selection) return;
          if (!allowed(lead)) throw new Error("Это действие сейчас недоступно.");
          if (!rendered.has(key)) { panel.replaceChildren(); build(lead); rendered.add(key); }
          // Preserve the shared contact editor when switching actions.
          if (customer.hosts.has(key)) {
            customer.hosts.get(key).append(customer.editor.element);
            const optional = {allowed:["existing","new","none"],fallback:"none"};
            const existing = {allowed:["existing","none"],fallback:"none"};
            const required = {allowed:["existing","new"],fallback:"new"};
            customer.editor.setModes({
              contact: key === "deal" ? existing : key === "customer" ? optional : required,
              company: key === "deal" || key === "deal_contact" ? existing : key === "customer" ? optional : required,
            });
          }
          dialog.classList.toggle("lead-compact-dialog", key === "empty");
          dialog.scrollTop = 0;
          renderOperations();
          (panel.querySelector("form input:not([type=hidden]), form textarea") || back).focus();
        } catch (error) {
          if (isCurrent() && selected === selection) panel.replaceChildren(el("p", "lead-error", error.message));
        }
      };
      for (const definition of definitions) {
        const [key, name, allowed] = definition;
        if (!allowed(snapshot)) continue;
        const option = button("", () => activate(definition)); option.className = "lead-completion-option"; option.dataset.completionMode = key;
        const symbol = el("span", "lead-completion-symbol"); symbol.append(icon(key.startsWith("deal") ? "note" : key === "customer" ? "user" : "check"));
        symbol.setAttribute("aria-hidden", "true");
        option.append(symbol, el("span", "", name)); choices.append(option);
        const panel = el("div", "lead-completion-panel"); panel.dataset.completionPanel = key; panel.hidden = true; panels.set(key, panel); host.append(panel);
      }
      const selected = definitions.find(([key]) => key === selectedMode && panels.has(key));
      if (selected) activate(selected);
      else if (panels.size) showChoices();
      else host.append(el("p", "lead-error", "Завершение этого лида сейчас недоступно."));
    }
    function budgetPanel(lead) {
      const box = el("section", "crm-card-money"), currency = lead.currency || "RUB";
      const money = value => win.CrmCardActivity.formatMoney(value) + " " + (currency === "RUB" ? "₽" : currency);
      const label = lead.budget_kind === "unknown" ? "Не указан" : lead.budget_kind === "range" ? money(lead.budget_min) + " — " + money(lead.budget_max) : money(lead.budget_min);
      box.append(el("p", "crm-card-money-label", "Бюджет лида"), el("p", "crm-card-money-value", label));
      if (lead.can_edit) {
        const form = el("form");
        const kind = select(form, "Бюджет лида", [["unknown", "Пока неизвестен"], ["exact", "Точная сумма"], ["range", "Диапазон"]], lead.budget_kind === "unknown" ? "exact" : lead.budget_kind);
        kind.setAttribute("aria-label", "Бюджет лида");
        const minimum = field(form, "Сумма / от, " + (currency === "RUB" ? "₽" : currency), win.CrmCardActivity.formatMoney(lead.budget_min));
        const maximum = field(form, "До, " + (currency === "RUB" ? "₽" : currency), win.CrmCardActivity.formatMoney(lead.budget_max));
        for (const input of [minimum, maximum]) { input.inputMode = "decimal"; input.maxLength = 40; }
        const update = () => { minimum.parentElement.hidden = kind.value === "unknown"; minimum.required = kind.value !== "unknown"; maximum.parentElement.hidden = kind.value !== "range"; maximum.required = kind.value === "range"; };
        kind.addEventListener("change", update); update();
        formSubmit(form, "Сохранить сумму", () => {
          if (!form.reportValidity()) return;
          const amount = kind.value === "unknown" ? null : win.CrmCardActivity.parseMoney(minimum.value);
          submit(form, "lead.edit", lead, {budget_kind:kind.value, budget_min:amount, budget_max:kind.value === "unknown" ? null : kind.value === "exact" ? amount : win.CrmCardActivity.parseMoney(maximum.value), currency:kind.value === "unknown" ? "" : currency});
        });
        form.querySelector('button[type=submit]').remove();
        win.CrmInline.mount(box, {display:box.querySelector('.crm-card-money-value'), form,
          editLabel:"Редактировать сумму", editText:"Изменить сумму", saveLabel:"Сохранить сумму", clearLabel:"Удалить сумму",
          clearMessage:"Убрать бюджет лида? В карточке будет «Не указан». Изменение сохранится в истории.",
          onClear:lead.budget_kind === "unknown" ? null : () => {kind.value="unknown"; update(); form.requestSubmit();},
          onReset:update, busy:() => Boolean(operations.forSubject(lead.id))});
      }
      return box;
    }
    function inlineLeadField(lead, name, label, type = "text", required = false) {
      const row=el("div","crm-inline-field"), value=lead[name]; row.append(el("p","crm-inline-label",label));
      const display=el("p",name === "need" ? "lead-need" : "",type === "datetime-local" ? (value ? dateText(value) : "Не указано") : value || "Не указано");
      if(!lead.can_edit) {row.append(display); return row;}
      const form=el("form"), input=field(form,label,type === "datetime-local" ? localDateTime(value,initial.timezone) : value || "",type,true);
      input.required=required; if(type === "textarea") input.rows=2;
      if(type === "text" || type === "textarea") input.maxLength=({title:255,need:10000,specialization:40,location:255})[name];
      if(name === "need") input.dataset.inlineLeadNeed="true";
      formSubmit(form,"Сохранить",()=>{
        if(!form.reportValidity())return;
        const raw=input.value.trim(), changes=leadEditChanges(lead,{[name]:type === "datetime-local" ? (raw ? deadlineISO(raw,initial.utc_offset) : null) : raw});
        if(!Object.keys(changes).length) {form.crmInlineEditor.close(); return;}
        submit(form,"lead.edit",lead,changes);
      });
      form.querySelector('button[type=submit]').remove();
      win.CrmInline.mount(row,{display,form,editLabel:"Редактировать: "+label,saveLabel:"Сохранить: "+label,
        clearLabel:"Удалить: "+label,clearMessage:"Удалить значение поля «"+label+"»? Изменение сохранится в истории.",
        onClear:required || !value ? null : ()=>{input.value="";form.requestSubmit();},busy:()=>Boolean(operations.forSubject(lead.id))});
      return row;
    }
    function renderDetail(data) {
      dialog.classList.remove("lead-compact-dialog");
      const lead=data.lead;state.detail=data;content.replaceChildren();
      content.append(el("h2","",displayTitle(lead)));content.firstChild.id="lead-dialog-title";
      content.append(el("p","lead-summary",[lead.status_label,lead.source?.label,lead.assignee?.name || "Не назначен"].filter(Boolean).join(" · ")));
      const primaryActions=el("div","lead-primary-actions");content.append(primaryActions);
      if(lead.can_qualify)primaryActions.append(button("Создать сделку",()=>conversionDialog(lead),true));
      if(lead.can_complete_customer || moveTargets(lead).includes("history")) primaryActions.append(button("Завершить лид",()=>conversionDialog(lead,false,null),!lead.can_qualify));
      if(lead.can_restore)primaryActions.append(button("Восстановить",()=>moveLead(lead,"restore")));
      if (lead.result === "customer_saved") {
        const result = el("div", "lead-completion-result");
        result.append(icon("check"), el("p", "", (lead.contact && lead.company ? "Контакт и компания сохранены." : lead.company ? "Компания сохранена." : "Контакт сохранён.") + " Сделка не создавалась."));
        content.append(result);
      }
      const layout=el("div","lead-detail-grid"), left=el("div","lead-detail-main"), right=el("section","lead-timeline");layout.append(left,right);content.append(layout);
      left.append(budgetPanel(lead));
      const need=el("section","lead-panel lead-overview");need.append(el("h3","","О лиде"),
        inlineLeadField(lead,"title","Название","text",true),inlineLeadField(lead,"need","Потребность","textarea",true),
        inlineLeadField(lead,"location","Место / объект"),inlineLeadField(lead,"specialization","Специализация"),inlineLeadField(lead,"desired_at","Желаемый срок","datetime-local"));left.append(need);
      if (lead.can_assign && initial.urls.responsibles) {
        const assignment = el("details", "lead-panel crm-assignment"); assignment.append(el("summary", "", "Ответственный · " + (lead.assignee?.name || "Не назначен"))); left.append(assignment); assignmentForm(assignment, lead);
        primaryActions.append(button(lead.assignee ? "Сменить ответственного" : "Назначить ответственного", () => { assignment.open = true; assignment.scrollIntoView({block:"nearest"}); assignment.querySelector("select")?.focus(); }));
      }
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
      win.CrmCardActivity.mount(right, {url:initial.urls.detail_prefix + lead.id + "/activity/?org_id=" + initial.organization_id, timezone:initial.timezone,
        commentForm: lead.can_comment ? () => {
          const form = el("form", "crm-activity-comment");
          const body = field(form, "Комментарий", "", "textarea", true); body.required = true; body.maxLength = 4000; body.rows = 3;
          body.placeholder = "Договорённости, результат разговора или заметка для коллег…";
          formSubmit(form, "Добавить комментарий", () => { if (form.reportValidity()) submit(form, "lead.comment", lead, {body:body.value.trim()}); }); return form;
        } : null,
        suggestNeed: lead.can_edit ? value => { const input = content.querySelector('[data-inline-lead-need="true"]'); if (input && (!input.value || input.value === lead.need)) { input.form.crmInlineEditor.open(); input.value = value; input.focus(); } } : null,
        analyze: async url => { const response = await win.fetch(url, {method:"POST", credentials:"same-origin", headers:{"X-CSRFToken":csrf(), Accept:"application/json"}}); if (!response.ok) throw new Error("Запуск недоступен. Проверьте права и состояние записи."); }
      });
      renderOperations();
    }
    async function conversionDialog(snapshot, additional = false, selectedMode = "deal") {
      if (operations.forSubject(snapshot.id)) return;
      const cached = dialog.open && state.detail?.lead === snapshot ? snapshot : null;
      const sequence = ++state.detailSequence;
      const isCurrent = () => sequence === state.detailSequence && dialog.open;
      state.selected = snapshot.id;
      urlState();
      const title = el("h2", "", additional ? "Дополнительная сделка" : "Завершить лид");
      title.id = "lead-dialog-title";
      content.replaceChildren(title);
      dialog.classList.toggle("lead-compact-dialog", !additional);
      if (!dialog.open) dialog.showModal();
      let conversionRead;
      const loadLead = () => {
        if (cached) return Promise.resolve(cached);
        if (!conversionRead) {
          const url = new URL(initial.urls.detail_prefix + snapshot.id + "/", win.location.origin);
          url.searchParams.set("org_id", initial.organization_id);
          url.searchParams.set("view", "conversion");
          conversionRead = read(url).then(data => {
            if (data.lead?.revision !== snapshot.revision) throw new Error("Лид изменился. Откройте его повторно.");
            return data.lead;
          }).catch(error => { conversionRead = null; throw error; });
        }
        return conversionRead;
      };
      if (!additional) {
        completionOptions(content, snapshot, loadLead, isCurrent, selectedMode);
        return;
      }
      const loading = el("p", "lead-form-note", "Загружаем данные лида…");
      content.append(loading);
      try {
        const lead = await loadLead();
        if (!isCurrent()) return;
        if (!lead.can_add_deal) throw new Error("Создание сделки сейчас недоступно. Обновите карточку лида.");
        loading.remove();
        qualificationForm(content, lead, true);
        renderOperations();
      } catch (error) {
        if (isCurrent()) { loading.remove(); errorNode(content, error); }
      }
    }
    async function openLead(id) {
      dialog.classList.remove("lead-compact-dialog");
      state.detail = null;
      const sequence = ++state.detailSequence; state.selected = Number(id); urlState(); content.replaceChildren(el("p", "", "Загружаем карточку…")); if (!dialog.open) dialog.showModal();
      try { const data = await read(initial.urls.detail_prefix + id + "/?org_id=" + initial.organization_id); if (sequence === state.detailSequence) renderDetail(data); }
      catch (error) { if (sequence === state.detailSequence) content.replaceChildren(el("p", "lead-error", error.message)); }
    }
    function createLead() {
      if (operations.forSubject(null)) { notice("Сначала проверьте результат предыдущего создания лида."); return; }
      dialog.classList.remove("lead-compact-dialog");
      ++state.detailSequence; state.selected = null; state.detail = null; urlState(); content.replaceChildren(el("h2", "", "Новое обращение")); content.firstChild.id = "lead-dialog-title";
      editForm(content, null, "lead.create"); if (!dialog.open) dialog.showModal();
    }
    byId("lead-create").addEventListener("click", createLead);
    function clearSelection() {
      if (state.selected !== null) { ++state.detailSequence; state.selected = null; }
      dialog.classList.remove("lead-compact-dialog");
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
    const ownerSelect = byId("lead-owner");
    const renderOwner = () => {
      if (![...ownerSelect.options].some(option => option.value === state.owner)) {
        const option = el("option", "", "Ответственный из ссылки"); option.value = state.owner; ownerSelect.append(option);
      }
      ownerSelect.value = state.owner;
    };
    byId("lead-search").value = state.q; renderOwner();
    byId("lead-search").addEventListener("input", (event) => { state.q = event.target.value; state.page = 1; state.pages = {}; if(state.listController)state.listController.abort(); ++state.sequence; state.loading=true; renderList(); win.clearTimeout(searchTimer); searchTimer = win.setTimeout(loadList, 300); });
    ownerSelect.addEventListener("change", () => { state.owner = ownerSelect.value; if (legacyOwners[state.mode]) state.mode = "all"; state.page = 1; state.pages = {}; loadList(); });
    byId("lead-legacy-status").addEventListener("click", () => { state.status = ""; state.page = 1; state.pages = {}; loadList(); });
    byId("lead-modes").addEventListener("click", (event) => { const target = event.target.closest("[data-mode]"); if (target) { state.mode = target.dataset.mode; state.page = 1; state.pages = {}; loadList(); } });
    byId("lead-archive-toggle")?.addEventListener("click",()=>{state.showArchiveQueues=!state.showArchiveQueues;urlState();renderList();});
    byId("lead-prev").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadList(); } });
    byId("lead-next").addEventListener("click", () => { if (state.page < state.data.pages) { state.page += 1; loadList(); } });
    win.addEventListener("beforeunload", (event) => { if (operations.pending().length) { event.preventDefault(); event.returnValue = ""; } });
    win.addEventListener("popstate",()=>{
      const params=new URL(win.location.href).searchParams;
      state.q=params.get("q") || ""; state.mode=params.get("mode") || "all"; state.status=params.get("status") || "";
      state.owner=params.get("owner") || legacyOwners[state.mode] || "all";
      state.showArchiveQueues=params.get("queues")==="all";
      state.page=Number(params.get("page")) || 1; state.pages={};
      byId("lead-search").value=state.q; renderOwner();
      const selected=params.get("lead"); state.selected=null;
      if(dialog.open) dialog.close(); loadList();
      if(selected && /^\d+$/.test(selected)) openLead(Number(selected));
    });
    const selected = new URL(win.location.href).searchParams.get("lead"); renderList(); if (selected && /^\d+$/.test(selected)) openLead(Number(selected));
    return {state, operations, loadList, openLead};
  }
  return {intakeStatusText, LeadOperations, localDateTime, deadlineISO, moveTargets, leadEditChanges, mount};
});
