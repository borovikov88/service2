(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => api.mount(document));
    else api.mount(document);
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function parseMoney(value) {
    const text = String(value ?? "").trim();
    if (!text) return "";
    if (!/^-?(?:[0-9]+|[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+)(?:[.,][0-9]{1,2})?$/.test(text)) throw new Error("Введите сумму: 1 234 567,00 (не более двух знаков после запятой).");
    const clean = text.replace(/[ \u00a0\u202f]/g, "").replace(",", ".");
    const [whole, fraction = ""] = clean.split(".");
    return whole + "." + fraction.padEnd(2, "0");
  }
  function formatMoney(value) {
    if (value == null || value === "") return "";
    const [whole, fraction] = parseMoney(value).split(".");
    return whole.replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0") + "," + fraction;
  }
  const moneyLabel = (amount, currency) => formatMoney(amount) + " " + (currency === "RUB" ? "₽" : currency);

  const sameId = (a, b) => a != null && b != null && String(a) === String(b);
  const isClosed = (deal) => deal.is_archived || ["won", "lost", "cancelled"].includes(deal.outcome);
  const freeze = (value) => {
    if (value && typeof value === "object") {
      Object.values(value).forEach(freeze);
      Object.freeze(value);
    }
    return value;
  };

  // A request belongs to the workspace, never to the currently open drawer.
  // An uncertain write can only be retried with this exact immutable payload.
  class OperationQueue {
    constructor(transport, onChange) {
      this.transport = transport;
      this.onChange = onChange || function () {};
      this.entries = new Map();
    }
    list() { return Array.from(this.entries.values()); }
    unresolved() { return this.list().filter((item) => ["ready", "sending", "uncertain"].includes(item.status)); }
    forSubject(subjectId) { return this.unresolved().find((item) => subjectId == null ? item.payload.subject_id == null : sameId(item.payload.subject_id, subjectId)); }
    add(payload, label) {
      if (this.entries.has(payload.operation_key)) return this.entries.get(payload.operation_key);
      if (this.forSubject(payload.subject_id)) throw new Error("Сначала проверьте результат предыдущего сохранения этой сделки.");
      const entry = {payload: freeze(JSON.parse(JSON.stringify(payload))), label, status: "ready", error: null, response: null, attempts: 0, everUncertain: false, running: null};
      this.entries.set(payload.operation_key, entry);
      this.onChange();
      return entry;
    }
    async run(operationKey, transport = this.transport) {
      const entry = this.entries.get(operationKey);
      if (!entry) throw new Error("Операция не найдена.");
      if (entry.running) return entry.running;
      if (!["ready", "uncertain"].includes(entry.status)) return entry;
      entry.status = "sending";
      entry.error = null;
      entry.attempts += 1;
      this.onChange();
      entry.running = Promise.resolve().then(async () => {
        try {
          entry.response = await transport(entry.payload);
          entry.status = "succeeded";
        } catch (error) {
          entry.error = error;
          // A later rejection cannot prove that an earlier, lost request did not
          // commit. Preserve its key even if access or validation has changed.
          entry.status = error.definitive && !entry.everUncertain ? "failed" : "uncertain";
          if (entry.status === "uncertain") entry.everUncertain = true;
        } finally {
          entry.running = null;
          this.onChange();
        }
        return entry;
      });
      return entry.running;
    }
    dismiss(operationKey) {
      const entry = this.entries.get(operationKey);
      if (!entry || !["failed", "succeeded"].includes(entry.status)) return false;
      this.entries.delete(operationKey);
      this.onChange();
      return true;
    }
  }

  function transitionIntent(deal, stageId, pipeline, reason) {
    if (!deal || !pipeline || !deal.can_transition || isClosed(deal)) throw new Error("У вас нет права менять этап этой сделки.");
    if (!sameId(deal.pipeline_id, pipeline.id)) throw new Error("Перенос между воронками здесь недоступен.");
    const from = pipeline.stages.find((stage) => sameId(stage.id, deal.stage_id));
    const to = pipeline.stages.find((stage) => sameId(stage.id, stageId));
    if (!from || !to) throw new Error("Этап изменился. Обновите список сделок.");
    if (sameId(from.id, to.id)) return null;
    if (!to.can_transition) throw new Error("Этот этап пока недоступен для переноса в рабочей версии CRM.");
    const backwards = Number(to.position) < Number(from.position);
    return {manualCompletion: Boolean(to.requires_manual_confirmation), subject_id: deal.id, expected_revision: deal.revision, changes: {stage_id: to.id, reason: String(reason || "").trim()}, backwards, needsReason: backwards && !String(reason || "").trim(), from, to};
  }

  function localDateTime(value, timezone) {
    if (!value) return "";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "";
    const parts = new Intl.DateTimeFormat("en-CA", {timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).formatToParts(date);
    const get = (type) => parts.find((part) => part.type === type).value;
    return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}`;
  }

  function deadlineISO(value, offset) {
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value || "") || !/^[+-]\d{2}:\d{2}$/.test(offset || "")) throw new Error("Укажите дату и время следующего шага.");
    const iso = value + ":00" + offset;
    if (!Number.isFinite(new Date(iso).getTime())) throw new Error("Проверьте дату следующего шага.");
    return iso;
  }

  function attentionFor(deal, now, timezone) {
    const next = deal.next_action;
    if (isClosed(deal) || (next && next.restricted)) return "all";
    if (!next || next.completed) return "missing";
    if (next.waiting) return "all";
    const timestamp = now == null ? Date.now() : now;
    if (next.due_at) return new Date(next.due_at).getTime() < timestamp ? "overdue" : "all";
    if (/^\d{4}-\d{2}-\d{2}$/.test(next.due_date || "")) {
      const today = localDateTime(timestamp, timezone || "Asia/Barnaul").slice(0, 10);
      return next.due_date < today ? "overdue" : "all";
    }
    return "all";
  }

  function editableDeadline(next, timezone, now) {
    if (next && !next.completed && next.due_at) return localDateTime(next.due_at, timezone);
    if (next && !next.completed && /^\d{4}-\d{2}-\d{2}$/.test(next.due_date || "")) return next.due_date + "T09:00";
    return localDateTime((now == null ? Date.now() : now) + 3600000, timezone);
  }

  function conflictDraft(payload, canonical, timezone) {
    if (!canonical || !sameId(payload.subject_id, canonical.id)) throw new Error("Откройте актуальную карточку этой сделки.");
    const changes = payload.changes || {};
    const draft = {revision: canonical.revision, taskVersion: canonical.next_action?.version, fields: {}};
    if (payload.command_type === "deal.edit") {
      draft.formKind = Object.keys(changes).length && Object.keys(changes).every(key => ["amount", "currency", "amount_basis"].includes(key)) ? "amount" : "edit";
      draft.fields = {...changes};
      if (Object.prototype.hasOwnProperty.call(changes, "expected_decision_at")) draft.fields.expected_decision_at = localDateTime(changes.expected_decision_at, timezone);
      if (changes.object_kind) draft.fields.object_ref = changes.object_kind === "pool" ? "pool:" + changes.pool_id : changes.object_kind === "client_object" ? "client_object:" + changes.client_object_id : changes.object_kind;
    } else if (payload.command_type === "deal.comment") {
      draft.formKind = "comment";
      draft.fields.body = changes.body;
    } else if (payload.command_type === "deal.next_action") {
      draft.formKind = "next_action";
      draft.fields.next_title = changes.title;
      draft.fields.due_at = localDateTime(changes.due_at, timezone);
    } else if (payload.command_type === "deal.close_negative") {
      draft.formKind = "close_negative";
      draft.fields.outcome = changes.outcome;
      draft.fields.reason = changes.reason;
    } else if (payload.command_type === "deal.assign") {
      draft.formKind = "assign";
      draft.fields.responsible_id = String(changes.responsible_id);
      draft.fields.transfer_next_action = Boolean(changes.transfer_next_action);
    } else if (payload.command_type === "deal.transition") {
      draft.formKind = "transition";
      draft.fields.stage_id = String(changes.stage_id);
      draft.fields.reason = changes.reason || "";
    } else if (payload.command_type === "deal.change_pipeline") {
      draft.formKind = "change_pipeline";
      draft.fields.pipeline_id = String(changes.pipeline_id);
      draft.fields.stage_id = String(changes.stage_id);
    } else throw new Error("Этот черновик нельзя восстановить в карточке.");
    return draft;
  }

  function mount(doc) {
    const root = doc.getElementById("crm-live");
    const bootstrapElement = doc.getElementById("crm-live-bootstrap");
    if (!root || !bootstrapElement || root.dataset.mounted) return;
    root.dataset.mounted = "true";
    let bootstrap;
    try { bootstrap = JSON.parse(bootstrapElement.textContent); }
    catch (_) { doc.getElementById("crm-live-content").textContent = "Не удалось открыть CRM. Обновите страницу."; return; }
    const win = doc.defaultView;
    const element = (tag, className, text) => {
      const node = doc.createElement(tag);
      if (className) node.className = className;
      if (text != null) node.textContent = String(text);
      return node;
    };
    const byId = (id) => doc.getElementById(id);
    const makeButton = (text, action, className) => {
      const node = element("button", className || "crm-live-button", text);
      node.type = "button";
      if (action) node.dataset.action = action;
      return node;
    };
    const params = new URL(win.location.href).searchParams;
    if (bootstrap.initial_notice) {
      if (bootstrap.initial_view === "table") params.set("view", "table");
      for (const key of ["q", "owner", "attention", "outcome", "object", "stage", "specialization", "decision_from", "decision_to"]) {
        params.set(key, bootstrap[key] == null ? "" : String(bootstrap[key]));
      }
    }
    const state = {
      data: bootstrap, pipelineId: bootstrap.selected_pipeline_id, page: bootstrap.page || 1,
      filters: Object.fromEntries(["outcome", "stage", "object", "specialization", "decision_from", "decision_to"].map((key) => [key, params.get(key) || (key === "outcome" ? "all" : "")])),
      query: params.get("q") || "", owner: bootstrap.owner || "all",
      view: params.get("view") === "table" ? "table" : "kanban",
      attention: ["today", "overdue", "missing"].includes(params.get("attention")) ? params.get("attention") : "all",
      loading: false, dataController: null, detailController: null, clientController: null,
      dataGeneration: 0, detailGeneration: 0, clientGeneration: 0,
      drawer: null, dragging: null, suppressClickUntil: 0,
    };
    const dialog = byId("crm-live-dialog");
    const dialogContent = byId("crm-live-dialog-content");
    const timezone = bootstrap.timezone || "Asia/Barnaul";
    const offset = bootstrap.utc_offset || "+07:00";
    const csrf = root.querySelector("[name=csrfmiddlewaretoken]");
    const dateFormatter = new Intl.DateTimeFormat("ru-RU", {timeZone: timezone, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
    const dayFormatter = new Intl.DateTimeFormat("ru-RU", {timeZone: timezone, day: "numeric", month: "short"});
    const dateText = (value) => {
      if (!value) return "Срок не задан";
      const date = new Date(value);
      return Number.isFinite(date.getTime()) ? dateFormatter.format(date) : "Срок не задан";
    };
    const nextDeadlineText = (next) => {
      if (next.waiting) return next.next_check_at ? "Контроль ожидания · " + dateFormatter.format(new Date(next.next_check_at)) : "Срок уточняется";
      if (next.due_at) return dateText(next.due_at);
      if (/^\d{4}-\d{2}-\d{2}$/.test(next.due_date || "")) return dayFormatter.format(new Date(next.due_date + "T12:00:00" + offset)) + " · время не задано";
      return "Срок не задан";
    };
    const pipelineFor = (id) => (state.data.pipelines || []).find((pipeline) => sameId(pipeline.id, id));
    const dealFor = (id) => (state.data.deals || []).find((deal) => sameId(deal.id, id));
    const stageFor = (deal) => (pipelineFor(deal.pipeline_id)?.stages || []).find((stage) => sameId(stage.id, deal.stage_id));
    const safeLink = (href) => {
      if (!href) return null;
      try { const url = new URL(href, win.location.href); return url.origin === win.location.origin && ["http:", "https:"].includes(url.protocol) ? url.href : null; }
      catch (_) { return null; }
    };
    const scopedURL = (base) => {
      const url = new URL(base, win.location.href);
      url.searchParams.set("org_id", String(bootstrap.organization_id));
      return url;
    };
    const detailURL = (id) => scopedURL(bootstrap.urls.detail_prefix + encodeURIComponent(id) + "/");
    const notice = (message, isError) => {
      const node = byId("crm-live-notice");
      node.textContent = message || "";
      node.classList.toggle("is-error", Boolean(isError));
      node.hidden = !message;
    };
    const friendlyError = (error) => {
      if (error.code === "auth_required" || error.status === 401) return "Вход в систему истёк. Войдите снова и проверьте результат сохранения.";
      if (error.status === 423) return "Сессия заблокирована. Разблокируйте сервис в другой вкладке, затем повторите действие здесь.";
      if (error.status === 403) return "Нет доступа к этому действию. Обновите страницу или обратитесь к администратору.";
      if (error.status === 409) return "Карточка уже изменилась. Проверьте актуальные данные перед новым сохранением.";
      return error.message || "Не удалось выполнить действие.";
    };
    async function requestJSON(url, options, externalSignal, timeoutMs = 20000) {
      const controller = new AbortController();
      const abort = () => controller.abort();
      if (externalSignal) {
        if (externalSignal.aborted) controller.abort();
        else externalSignal.addEventListener("abort", abort, {once: true});
      }
      const timer = win.setTimeout(abort, timeoutMs);
      try {
        const requestOptions = Object.assign({credentials: "same-origin", cache: "no-store"}, options || {}, {signal: controller.signal});
        requestOptions.headers = Object.assign({"X-Requested-With": "XMLHttpRequest"}, options?.headers || {});
        const response = await win.fetch(url.toString(), requestOptions);
        let body;
        try { body = await response.json(); }
        catch (_) {
          const error = new Error("Сервер не подтвердил результат. Проверьте сохранение повторно.");
          error.status = response.status;
          error.definitive = response.status >= 400 && response.status < 500 && response.status !== 408 && response.status !== 429;
          throw error;
        }
        if (!response.ok) {
          const error = new Error(typeof body.error === "string" ? body.error : "Не удалось сохранить изменения.");
          error.status = response.status;
          error.code = body.code;
          error.unlockUrl = body.unlock_url;
          error.definitive = response.status >= 400 && response.status < 500 && response.status !== 408 && response.status !== 429;
          throw error;
        }
        return body;
      } finally {
        win.clearTimeout(timer);
        if (externalSignal) externalSignal.removeEventListener("abort", abort);
      }
    }

    const queue = new OperationQueue(async (payload) => {
      const response = await requestJSON(scopedURL(bootstrap.urls.command), {
        method: "POST", headers: {"Content-Type": "application/json", "Accept": "application/json", "X-CSRFToken": csrf ? csrf.value : ""}, body: JSON.stringify(payload),
      });
      const transferred = response.result?.assignment_saved === true && response.result.can_open === false && Number.isInteger(response.result.deal_id);
      if (!transferred && (!response.result || !response.deal || !sameId(response.result.deal_id, response.deal.id))) throw new Error("Сервер не подтвердил результат сохранения.");
      return response;
    }, () => { if (!bulk?.isRunning()) { renderOperations(); renderContent(); updateFormLocks(); } });

    const bulk = win.CrmBulk?.mount({root, kind: "deal", offset, deadlineISO, transitionIntent, pipelineFor,
      items: () => (state.data.deals || []).filter(deal => sameId(deal.pipeline_id, state.pipelineId)),
      loading: () => state.loading, pending: id => queue.forSubject(id), notice,
      detail: async (id, signal) => (await requestJSON(detailURL(id), {headers: {Accept: "application/json"}}, signal)).deal,
      candidates: async (id, signal) => {
        const url = scopedURL(bootstrap.urls.responsibles); url.searchParams.set("kind", "deal"); url.searchParams.set("subject_id", id);
        return (await requestJSON(url, {headers: {Accept: "application/json"}}, signal)).users;
      },
      run: async (payload, label) => { const item = queue.add(payload, label); const result = await queue.run(item.payload.operation_key); if (result.status === "succeeded") queue.dismiss(item.payload.operation_key); return result; },
      runBatch: records => win.CrmBulk.runBatch(queue, records, payload => requestJSON(scopedURL(bootstrap.urls.bulk_command), {
        method: "POST", headers: {"Content-Type": "application/json", "Accept": "application/json", "X-CSRFToken": csrf ? csrf.value : ""}, body: JSON.stringify(payload),
      })),
      reload: () => { renderOperations(); return loadData({keepNotice: true, keepSelection: true}); },
    });

    function syncURL() {
      const url = new URL(win.location.href);
      url.searchParams.set("org_id", String(bootstrap.organization_id));
      if (state.pipelineId != null) url.searchParams.set("pipeline", state.pipelineId);
      else url.searchParams.delete("pipeline");
      for (const [key, value, defaultValue] of [["q", state.query, ""], ["owner", state.owner, "all"], ["view", state.view, "kanban"], ["attention", state.attention, "all"]]) {
        if (value === defaultValue) url.searchParams.delete(key);
        else url.searchParams.set(key, value);
      }
      if (state.view === "table" && state.page > 1) url.searchParams.set("page", state.page); else url.searchParams.delete("page");
      for (const [key, value] of Object.entries(state.filters)) {
        if (value && !(key === "outcome" && value === "all")) url.searchParams.set(key, value);
        else url.searchParams.delete(key);
      }
      win.history.replaceState(win.history.state, "", url);
    }

    function renderOperations() {
      for (const id of ["crm-live-operations", "crm-live-dialog-operations"]) {
        const container = byId(id);
        container.replaceChildren();
        for (const entry of queue.list().filter((item) => item.status !== "succeeded")) {
          const row = element("div", "crm-live-operation is-" + entry.status);
          row.dataset.operationKey = entry.payload.operation_key;
          const copy = element("div");
          copy.append(element("strong", "", entry.status === "sending" ? "Сохраняем: " + entry.label : entry.label));
          let text = entry.status === "uncertain" ? "Результат пока не подтверждён: изменение могло сохраниться. Кнопка проверит результат и безопасно повторит тот же запрос." : entry.status === "failed" ? friendlyError(entry.error) : "Дождитесь подтверждения сохранения.";
          if (entry.status === "uncertain" && entry.error?.status === 401) text = "Первый запрос мог сохраниться. Войдите в сервис в другой вкладке, затем проверьте результат этой кнопкой. Сохраните эту вкладку открытой.";
          else if (entry.status === "uncertain" && entry.error?.status === 403) text = "Первый запрос мог сохраниться, но сейчас нет доступа. После восстановления доступа проверьте результат этой кнопкой.";
          else if (entry.status === "uncertain" && entry.error?.status === 423) text = "Сессия заблокирована, а первый запрос мог сохраниться. Разблокируйте сервис в другой вкладке, затем проверьте результат этой кнопкой.";
          copy.append(element("p", "", text));
          row.append(copy);
          const actions = element("div", "crm-live-operation-actions");
          if (entry.error?.status === 423 && safeLink(entry.error.unlockUrl)) {
            const url = new URL(safeLink(entry.error.unlockUrl));
            url.searchParams.set("next", win.location.pathname + win.location.search);
            const unlock = element("a", "crm-live-button", "Разблокировать в новой вкладке");
            unlock.href = url.href;
            unlock.target = "_blank";
            unlock.rel = "noopener";
            actions.append(unlock);
          }
          if (entry.status === "uncertain" || entry.status === "ready") {
            const retry = makeButton("Проверить / повторить", "retry");
            retry.dataset.operationKey = entry.payload.operation_key;
            actions.append(retry);
          } else if (entry.status === "failed") {
            if (entry.error?.status === 409 && !["deal.bind_order", "deal.unbind_order"].includes(entry.payload.command_type)) {
              const restore = makeButton("Вернуть мои изменения", "restore-conflict");
              restore.dataset.operationKey = entry.payload.operation_key;
              actions.append(restore);
            }
            const close = makeButton("Понятно", "dismiss-operation");
            close.dataset.operationKey = entry.payload.operation_key;
            actions.append(close);
          }
          if (actions.childElementCount) row.append(actions);
          container.append(row);
        }
      }
    }

    function renderPipelines() {
      const nav = byId("crm-live-pipelines");
      nav.replaceChildren();
      for (const pipeline of state.data.pipelines || []) {
        const button = makeButton(pipeline.name);
        button.dataset.pipeline = pipeline.id;
        if (sameId(pipeline.id, state.pipelineId)) button.setAttribute("aria-current", "page");
        nav.append(button);
      }
      byId("crm-live-create").hidden = !state.data.capabilities?.create || !(state.data.pipelines || []).length;
      for (const button of root.querySelectorAll("[data-view]")) button.setAttribute("aria-pressed", String(button.dataset.view === state.view));
    }

    function nextElement(deal) {
      const next = deal.next_action;
      const attention = attentionFor(deal);
      const box = element("div", "crm-live-next" + (attention === "overdue" ? " is-overdue" : attention === "missing" ? " is-missing" : ""));
      if (next?.restricted) {
        box.classList.add("is-restricted");
        box.append(element("span", "crm-live-next-label", "Следующее действие"), element("span", "crm-live-next-title", "Доступно ответственному"));
      } else if (!next) {
        box.append(element("span", "crm-live-next-label", "Следующее действие"), element("span", "crm-live-next-title", isClosed(deal) ? "Сделка завершена" : "Нужно назначить следующий шаг"));
      } else {
        if (next.completed) box.classList.add("is-completed");
        box.append(element("span", "crm-live-next-label", next.completed ? "Предыдущий шаг завершён" : next.waiting ? "Ожидаем согласования даты" : "Следующее действие"), element("strong", "crm-live-next-title", next.title), element("span", "crm-live-next-date", (attention === "overdue" ? "Просрочено · " : "") + nextDeadlineText(next)));
      }
      return box;
    }

    function renderCard(deal, table) {
      const pending = queue.forSubject(deal.id);
      const card = element("article", "crm-live-card" + (pending ? " is-pending" : ""));
      card.dataset.dealId = deal.id;
      if (bulk) card.append(bulk.checkbox(deal));
      card.draggable = !table && !state.loading && Boolean(deal.can_transition || deal.can_close_negative) && !deal.is_archived && !pending;
      const main = element("div", "crm-live-card-main");
      const title = element("h3");
      const open = makeButton(deal.title, "open", "crm-live-card-open");
      open.dataset.dealId = deal.id;
      title.append(open);
      main.append(title, element("p", "crm-live-card-amount" + (deal.amount === null ? " is-unknown" : ""), deal.amount === null ? "Сумма не указана" : moneyLabel(deal.amount, deal.currency)), element("p", "crm-live-card-client", deal.client?.name || "Клиент не указан"));
      const object = deal.object;
      if (object) main.append(element("p", "crm-live-card-stage", object.restricted ? "Объект недоступен" : [object.name, object.address].filter(Boolean).join(" · ")));
      if (deal.specialization) main.append(element("p", "crm-live-card-specialization", deal.specialization));
      if (deal.unverified_completion_stage) main.append(element("p","crm-live-pill",(deal.manual_completion ? "Завершено вручную" : "Финальный этап CRM")+" · исполнение и оплата не подтверждены"));
      if (deal.outcome !== "open") main.append(element("p", "crm-live-pill", outcomeLabel(deal.outcome)));
      if (table) main.append(element("p", "crm-live-card-stage", stageFor(deal)?.name || "Этап не указан"));
      const owner = element("div", "crm-live-card-owner");
      const name = deal.manager?.name || "Без ответственного";
      const initials = deal.manager?.name ? name.trim().split(/\s+/).slice(0, 2).map((part) => part[0]).join("") : "—";
      owner.append(element("span", "crm-live-avatar", initials), element("span", "", name));
      card.append(main, owner, nextElement(deal));
      if (pending) card.append(element("span", "crm-live-pending-tag", pending.status === "sending" ? "Сохраняется…" : "Ожидает подтверждения сохранения"));
      return card;
    }

    function renderContent() {
      const container = byId("crm-live-content");
      const scrollLeft = container.querySelector(".crm-live-board")?.scrollLeft || 0;
      container.replaceChildren();
      container.setAttribute("aria-busy", String(state.loading));
      container.classList.toggle("crm-live-content-loading", state.loading);
      byId("crm-live-refresh").disabled = state.loading;
      const pipeline = pipelineFor(state.pipelineId);
      const all = (state.data.deals || []).filter((deal) => sameId(deal.pipeline_id, state.pipelineId));
      const filtered = all;
      bulk?.refresh();
      for (const kind of ["all", "today", "overdue", "missing"]) {
        root.querySelector(`[data-attention="${kind}"]`).setAttribute("aria-pressed", String(state.attention === kind));
      }
      byId("crm-live-result-status").textContent = state.loading ? "Обновляем…" : (state.view === "kanban" ? "Загружено: " : "На странице: ") + filtered.length + (state.data.attention_total == null ? "" : " · по фильтру: " + state.data.attention_total);
      byId("crm-live-linked-filters").hidden = !Object.entries(state.filters).some(([key, value]) => value && !(key === "outcome" && value === "all"));
      byId("crm-live-limit").hidden = !state.data.has_more;
      root.querySelector(".crm-live-pagination").hidden = state.view !== "table" || (!state.data.has_more && !state.data.has_previous);
      byId("crm-live-page-prev").disabled = state.loading || !state.data.has_previous;
      byId("crm-live-page-next").disabled = state.loading || !state.data.has_more;
      byId("crm-live-page-label").textContent = "Страница " + state.page;
      byId("crm-live-help").hidden = state.view !== "kanban" || !filtered.length;
      if (state.dataError) {
        byId("crm-live-result-status").textContent = "Результаты не загружены";
        byId("crm-live-limit").hidden = byId("crm-live-help").hidden = true;
        byId("crm-live-page-prev").disabled = byId("crm-live-page-next").disabled = true;
        const error = element("div", "crm-live-empty");
        error.append(element("h2", "", "Не удалось применить фильтр"), element("p", "", state.dataError), makeButton("Сбросить фильтры", "reset"));
        container.append(error); return;
      }
      if (!pipeline) {
        const empty = element("div", "crm-live-empty");
        empty.append(element("h2", "", "Воронки CRM ещё не настроены"), element("p", "", "После настройки воронок здесь появятся сделки и этапы работы."));
        container.append(empty);
        return;
      }
      if (!filtered.length && !(state.data.stage_totals || []).some(row => row.count > 0)) {
        const empty = element("div", "crm-live-empty");
        const hasFilters = state.query || state.owner !== "all" || state.attention !== "all" || Object.entries(state.filters).some(([key, value]) => value && !(key === "outcome" && value === "all"));
        empty.append(element("h2", "", hasFilters ? "По этим условиям сделок нет" : "Первая сделка начинается с клиента"), element("p", "", hasFilters ? "Измените поисковый запрос или сбросьте фильтры." : "Выберите существующего клиента, задайте следующий шаг и срок. Сделка появится на первом этапе."));
        if (hasFilters) empty.append(makeButton("Сбросить фильтры", "reset"));
        else if (state.data.capabilities?.create) empty.append(makeButton("Создать сделку", "create", "crm-live-button crm-live-button-primary"));
        container.append(empty);
        return;
      }
      if (state.view === "table") {
        const table = element("div", "crm-live-table");
        const head = element("div", "crm-live-table-head");
        ["Сделка / клиент / этап", "Ответственный", "Следующий шаг"].forEach((label) => head.append(element("span", "", label)));
        table.append(head);
        filtered.forEach((deal) => table.append(renderCard(deal, true)));
        container.append(table);
      } else {
        const board = element("div", "crm-live-board");
        board.setAttribute("aria-label", "Канбан: " + pipeline.name);
        for (const stage of pipeline.stages || []) {
          const deals = filtered.filter((deal) => sameId(deal.board_stage_id || deal.stage_id, stage.id));
          const lane = element("section", "crm-live-lane");
          lane.dataset.stageId = stage.id;
          lane.dataset.canTransition = String(Boolean(stage.can_transition));
          lane.dataset.failure = String(Boolean(stage.is_failure));
          lane.dataset.canCloseNegative = String(Boolean(stage.can_close_negative));
          const heading = element("h2");
          const totals = (state.data.stage_totals || []).filter(row => sameId(row.stage_id, stage.id));
          const count = totals.reduce((sum, row) => sum + row.count, 0);
          heading.append(element("span", "", stage.name), element("span", "crm-live-lane-count", count));
          lane.append(heading);
          const known = totals.filter(row => row.amount !== null).map(row => moneyLabel(row.amount, row.currency));
          const unknown = totals.reduce((sum, row) => sum + row.unknown_count, 0);
          lane.append(element("p", "crm-live-stage-total", (known.length ? known.join(" + ") : unknown ? "Сумма неизвестна" : "0,00 ₽") + (unknown ? " · без суммы: " + unknown : "")));
          if (!stage.can_transition && !stage.can_close_negative) lane.append(element("p", "crm-live-lane-unavailable", stage.transition_block_reason || "Перенос в этот этап недоступен"));
          if (!deals.length) lane.append(element("p", "crm-live-lane-empty", count > 0 ? "На этой странице нет сделок этого этапа" : "Нет сделок на этом этапе"));
          else deals.forEach((deal) => lane.append(renderCard(deal, false)));
          const window = (state.data.column_windows || []).find(item => sameId(item.stage_id, stage.id));
          if (window?.has_more) {
            lane.append(element("p", "crm-live-lane-progress", "Загружено " + deals.length + " из " + count));
            if (window.cursor) {
              const more = makeButton("Показать ещё 50", "column-more");
              more.dataset.stageId = stage.id;
              more.disabled = state.loading;
              lane.append(more);
            }
          }
          board.append(lane);
        }
        container.append(board);
        board.scrollLeft = scrollLeft;
      }
      bulk?.refresh();
    }

    async function loadData(options) {
      const more = options?.moreStage == null ? null : (state.data.column_windows || []).find(item => sameId(item.stage_id, options.moreStage));
      if (options?.moreStage != null && (!more?.cursor || state.loading)) return;
      if (!more && !options?.keepSelection) bulk?.reset();
      const previous = state.data;
      const generation = ++state.dataGeneration;
      if (state.dataController) state.dataController.abort();
      const controller = new AbortController();
      state.dataController = controller;
      state.loading = true; state.dataError = null;
      renderContent();
      const url = scopedURL(bootstrap.urls.data);
      if (state.pipelineId != null) url.searchParams.set("pipeline", state.pipelineId);
      url.searchParams.set("page", state.page);
      if (state.view === "kanban") {
        url.searchParams.set("layout", "board");
        if (more) { url.searchParams.set("board_column", more.stage_id); url.searchParams.set("cursor", more.cursor); }
      }
      url.searchParams.set("q", state.query);
      url.searchParams.set("owner", state.owner);
      url.searchParams.set("attention", state.attention);
      for (const [key, value] of Object.entries(state.filters)) if (value) url.searchParams.set(key, value);
      syncURL();
      try {
        const response = await requestJSON(url, {headers: {"Accept": "application/json"}}, controller.signal);
        if (generation !== state.dataGeneration) return;
        if (!Array.isArray(response.deals) || !Array.isArray(response.pipelines)) throw new Error("Сервер вернул неполные данные CRM.");
        if (more) {
          const rows = new Map((previous.deals || []).map(row => [String(row.id), row]));
          for (const row of response.deals) rows.set(String(row.id), row);
          state.data = {...response, deals: Array.from(rows.values()), column_windows: previous.column_windows.map(item =>
            (response.column_windows || []).find(next => sameId(next.stage_id, item.stage_id)) || item)};
        } else state.data = response;
        state.pipelineId = response.selected_pipeline_id;
        state.page = response.page || 1;
        state.attention = response.attention || "all";
        renderPipelines();
        syncURL();
        if (!options?.keepNotice) notice("");
      } catch (error) {
        if (controller.signal.aborted || generation !== state.dataGeneration) return;
        if (error.code === "reset_required") {
          state.data = {...state.data, deals: [], column_windows: [], stage_totals: []};
          notice("Доска изменилась. Загружаем актуальные первые порции; продолжите загрузку заново.", true);
          await loadData({keepNotice:true}); return;
        }
        if ([401,403,404].includes(error.status)) { state.data = {...state.data, deals: [], column_windows: [], stage_totals: []}; state.dataError = friendlyError(error); }
        state.attention = state.data.attention || "all"; syncURL();
        if (error.status === 400) state.dataError = friendlyError(error);
        notice("Не удалось обновить сделки. " + friendlyError(error) + " Нажмите кнопку обновления.", true);
      } finally {
        if (generation === state.dataGeneration) { state.loading = false; renderContent(); }
      }
    }

    function openDialog(caption) {
      byId("crm-live-dialog-caption").textContent = caption;
      if (!dialog.open) dialog.showModal();
      dialog.scrollTop = 0;
      renderOperations();
    }
    function setDealURL(id, push) {
      const url = new URL(win.location.href);
      if (id != null) url.searchParams.set("deal", String(id));
      else url.searchParams.delete("deal");
      if (url.href !== win.location.href) win.history[push ? "pushState" : "replaceState"](null, "", url);
    }
    function closeDialog(updateURL = true) {
      if (updateURL) setDealURL(null, true);
      if (state.detailController) state.detailController.abort();
      if (state.clientController) state.clientController.abort();
      state.drawer?.customerEditor?.destroy();
      state.detailGeneration += 1;
      state.clientGeneration += 1;
      state.drawer = null;
      dialog.close();
    }
    function titleNode(text) { const node = element("h2", "", text); node.id = "crm-live-dialog-title"; return node; }
    function section(title) { const node = element("section", "crm-live-section"); node.append(element("h3", "", title)); return node; }
    function disclosure(title) {
      const node = element("details", "crm-live-section crm-live-disclosure");
      node.append(element("summary", "", title)); return node;
    }
    function revealField(input) {
      input.crmInlineEditor?.open();
      for (let parent = input.parentElement; parent && parent !== dialogContent; parent = parent.parentElement) {
        if (parent.tagName === "DETAILS") parent.open = true;
        parent.crmInlineEditor?.open();
        if (parent.dataset.detailPanel) selectDetailPanel(parent.dataset.detailPanel);
      }
    }
    function selectDetailPanel(key) {
      for (const panel of dialogContent.querySelectorAll("[data-detail-panel]")) panel.hidden = panel.dataset.detailPanel !== key;
      for (const tab of dialogContent.querySelectorAll("[data-detail-tab]")) {
        const active = tab.dataset.detailTab === key;
        tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active ? 0 : -1;
      }
    }
    function amountPanel(deal) {
      const box = element("section", "crm-card-money");
      box.append(element("p", "crm-card-money-label", "Сумма сделки"));
      const display = element("p", "crm-card-money-value", deal.amount === null ? "Не указана" : moneyLabel(deal.amount, deal.currency));
      if (deal.can_edit && !deal.is_archived && ["", "RUB"].includes(deal.currency)) {
        const form = newForm("amount", deal.id);
        const amount = field("Сумма сделки, ₽", "text", "amount", deal.amount == null ? "" : formatMoney(deal.amount), false);
        amount.input.inputMode = "decimal"; amount.input.maxLength = 40; amount.input.placeholder = "Например, 125 000,00"; form.append(amount.wrapper);
        selectField(form, "Сумма", "amount_basis", deal.amount_basis === "agreed" ? "agreed" : "indicative", [["indicative", "Предварительная оценка"], ["agreed", "Согласовано с клиентом"]]);
        win.CrmInline.mount(box, {display, form, editLabel:"Редактировать сумму", saveLabel:"Сохранить сумму",
          clearLabel:"Удалить сумму", clearMessage:"Убрать сумму сделки? В карточке будет «Не указана». Изменение сохранится в истории.",
          onClear:deal.amount === null ? null : () => { amount.input.value=""; form.requestSubmit(); }, busy:() => Boolean(queue.forSubject(deal.id))});
      } else box.append(display);
      return box;
    }
    function inlineDealField(deal, name, label, type = "text", options = null) {
      const row = element("div", "crm-inline-field"), value = deal[name];
      row.append(element("p", "crm-inline-label", label));
      const display = element("p", "", options ? (options.find(([key]) => key === value)?.[1] || "Не указано") : type === "datetime-local" ? (value ? dateText(value) : "Не указано") : value || "Не указано");
      if (!deal.can_edit || deal.is_archived) { row.append(display); return row; }
      const form = newForm("field", deal.id); form.dataset.fieldName=name;
      const current = type === "datetime-local" ? localDateTime(value, timezone) : value || "";
      const input = options ? selectField(form, label, name, value, options).input : field(label, type, name, current, name === "title").input;
      if (!options) form.append(input.parentElement);
      if(type === "textarea") input.rows=2;
      if(type === "text" || type === "textarea") input.maxLength=({title:255,work_subject:2000,specialization:40})[name] || 255;
      win.CrmInline.mount(row, {display, form, editLabel:"Редактировать: " + label, saveLabel:"Сохранить: " + label,
        clearLabel:"Удалить: " + label, clearMessage:"Удалить значение поля «" + label + "»? Изменение сохранится в истории.",
        onClear:name === "title" || !value || value === "unknown" ? null : () => { input.value=options ? "unknown" : ""; form.requestSubmit(); }, busy:() => Boolean(queue.forSubject(deal.id))});
      return row;
    }
    function detailTabs() {
      const nav = element("div", "crm-detail-tabs"); nav.setAttribute("role", "tablist"); nav.setAttribute("aria-label", "Карточка сделки");
      const activity = dialogContent.querySelector(".crm-card-activity");
      const children = Array.from(dialogContent.children).filter(node => node !== activity);
      const panels = [];
      for (const [key, label] of [["overview", "Обзор"], ["tasks", "Дела"], ["edit", "Изменить"]]) {
        const nodes = children.filter(node => (node.dataset.detailSection || "overview") === key && !node.matches("h2,.crm-live-detail-meta,.crm-live-completion-warning"));
        if (!nodes.length) continue;
        const panel = element("section", "crm-detail-panel"); panel.dataset.detailPanel = key; panel.id = "crm-detail-panel-" + key;
        panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", "crm-detail-tab-" + key);
        panel.append(...nodes); panels.push(panel);
        const tab = element("button", "", label); tab.type = "button"; tab.dataset.detailTab = key; tab.id = "crm-detail-tab-" + key;
        tab.setAttribute("role", "tab"); tab.setAttribute("aria-controls", panel.id);
        tab.addEventListener("click", () => selectDetailPanel(key)); nav.append(tab);
      }
      nav.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        const tabs = Array.from(nav.children), current = tabs.indexOf(doc.activeElement);
        const index = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
        event.preventDefault(); tabs[index].click(); tabs[index].focus();
      });
      const layout = element("div", "crm-card-layout"), main = element("div", "crm-card-main");
      main.append(nav, ...panels); layout.append(main); if (activity) layout.append(activity);
      dialogContent.append(layout); selectDetailPanel("overview");
    }
    function field(label, type, name, value, required) {
      const wrapper = element("label", "crm-live-field");
      wrapper.append(element("span", "", label));
      const input = element(type === "textarea" ? "textarea" : type === "select" ? "select" : "input");
      if (!["textarea", "select"].includes(type)) input.type = type;
      input.name = name;
      if (value != null) input.value = value;
      input.required = Boolean(required);
      wrapper.append(input);
      return {wrapper, input};
    }
    function formFooter(form, label) {
      const footer = element("div", "crm-live-form-footer");
      const save = element("button", "crm-live-button crm-live-button-primary", label || "Сохранить");
      save.type = "submit";
      save.dataset.noLoading = ""; // This async form owns its queue/error lifecycle.
      footer.append(save);
      form.append(footer);
    }
    function newForm(kind, subjectId) {
      const form = element("form", "crm-live-fields");
      form.dataset.commandForm = kind;
      form.dataset.subjectId = subjectId == null ? "" : subjectId;
      form.noValidate = false;
      const error = element("div", "crm-live-form-error");
      error.hidden = true;
      error.setAttribute("role", "alert");
      form.append(error);
      form.addEventListener("invalid", event => revealField(event.target), true);
      return form;
    }
    function formError(form, message) {
      const error = form.querySelector(".crm-live-form-error");
      if (!error) return;
      error.textContent = message || "";
      error.hidden = !message;
    }
    function updateFormLocks() {
      // The retained detail header must wait for the same refresh as its forms.
      // Otherwise a late response can replace a just-reopened editor.
      const detailBusy = state.drawer?.kind === "loading" || Boolean(state.drawer?.id && queue.forSubject(state.drawer.id));
      for (const button of dialogContent.querySelectorAll(".crm-live-detail-meta :is(.crm-live-inline-action,.crm-live-pipeline-pill),.crm-inline-view button")) button.disabled = detailBusy;
      for (const form of dialogContent.querySelectorAll("[data-command-form]")) {
        const subjectId = form.dataset.subjectId || null;
        const busy = Boolean(queue.forSubject(subjectId)) || form.dataset.orderRefreshing === "true" || (state.drawer?.kind === "loading" && sameId(state.drawer.id, subjectId));
        for (const input of form.querySelectorAll("input, select, textarea, button")) input.disabled = busy;
        if (!busy && form.dataset.commandForm === "create" && state.drawer?.kind === "create") {
          form.elements.namedItem("client_search").disabled = state.drawer.useCustomer;
          state.drawer.customerEditor?.setEnabled(state.drawer.useCustomer);
        }
        form.updateOrderButtons?.(busy);
      }
      byId("crm-live-create").disabled = Boolean(queue.forSubject(null));
    }

    function nextFields(form, current) {
      const nextTitle = field("Следующий шаг", "text", "next_title", current && !current.completed ? current.title : "", true);
      nextTitle.input.placeholder = "Например, уточнить размеры бассейна";
      nextTitle.input.maxLength = 255;
      const deadline = field("Дата и время · Барнаул", "datetime-local", "due_at", editableDeadline(current, timezone), true);
      deadline.wrapper.append(element("small", "", "Срок указывается по времени Барнаула (UTC+7)."));
      if (current?.due_date && !current.due_at) deadline.wrapper.append(element("small", "", "У задачи пока нет времени. Проверьте время перед сохранением."));
      form.append(nextTitle.wrapper, deadline.wrapper);
    }

    function outcomeLabel(value) {
      return ({open: "Открыта", lost: "Сделка провалена", cancelled: "Отменена", won: "Успех", unknown: "Legacy: не определён"})[value] || value;
    }

    function selectField(form, label, name, selected, choices) {
      const item = field(label, "select", name, null, false);
      for (const [value, text] of choices) {
        const option = element("option", "", text); option.value = value; option.selected = value === selected; item.input.append(option);
      }
      form.append(item.wrapper); return item;
    }
    function responsibleField(form, deal = null) {
      if (!bootstrap.urls.responsibles) return null;
      const item = field("Ответственный", "select", "responsible_id", null, true);
      item.input.setAttribute("aria-label", "Ответственный");
      const placeholder = element("option", "", "Загружаем сотрудников…"); placeholder.value = ""; item.input.append(placeholder);
      const note = element("small", "crm-live-form-note", "Выбранный сотрудник получит уведомление согласно своим настройкам.");
      const retry = makeButton("Повторить загрузку сотрудников"); retry.hidden = true; item.wrapper.append(note, retry); form.append(item.wrapper);
      const load = async () => {
        retry.hidden = true;
        try {
          const url = scopedURL(bootstrap.urls.responsibles); url.searchParams.set("kind", "deal"); if (deal) url.searchParams.set("subject_id", deal.id);
          const data = await requestJSON(url, {headers:{Accept:"application/json"}}); if (!item.wrapper.isConnected) return;
          const empty = element("option", "", "Выберите сотрудника"); empty.value = ""; item.input.replaceChildren(empty);
          for (const user of data.users || []) { const option = element("option", "", user.name); option.value = user.id; item.input.append(option); }
          item.input.value = String(deal?.manager?.id ?? data.current_user_id ?? "");
          note.textContent = data.users?.length ? "Выбранный сотрудник получит уведомление согласно своим настройкам." : "Нет сотрудников, которым доступна эта карточка.";
        } catch (error) { if (!item.wrapper.isConnected) return; note.textContent = friendlyError(error); retry.hidden = false; }
      };
      retry.addEventListener("click", () => { form.responsiblesReady = load(); }); form.responsiblesReady = load(); return item.input;
    }

    function cardFields(form, deal, moneyHost = form) {
      form.append(element("p", "crm-live-form-note", "Эти сведения видит команда CRM. Планируемый объект — описание сделки; действующий объект и обслуживание здесь не создаются."));
      const subject = field("Предмет работ / товаров", "textarea", "work_subject", deal?.work_subject || "", false);
      subject.input.maxLength = 2000;
      const spec = field("Специализация", "text", "specialization", deal?.specialization || "", false);
      spec.input.maxLength = 40; spec.input.placeholder = "Бассейн, хамам, полив…";
      form.append(subject.wrapper, spec.wrapper);
      selectField(form, "Направление исполнения", "fulfillment_kind", deal?.fulfillment_kind || "unknown", [["unknown", "Не уточнено"], ["construction", "Строительство / монтаж"], ["service", "Обслуживание"], ["retail", "Поставка"], ["mixed", "Смешанное"]]);
      const object = deal?.object;
      const currentRef = object?.id ? object.kind + ":" + object.id : object?.kind || "none";
      const ref = selectField(form, "Объект сделки", "object_ref", currentRef, [["none", "Не выбран"], ["planned", "Планируемый объект"]]);
      if (object?.id) {
        const option = element("option", "", [object.name, object.address].filter(Boolean).join(" · "));
        option.value = currentRef; option.selected = true; ref.input.append(option);
      }
      const objectSearch = field("Найти существующий объект клиента", "search", "object_search", "", false);
      objectSearch.input.placeholder = "Адрес или название; до 20 каждого типа";
      const objectHint = element("small", "crm-live-muted", "Сначала выберите клиента.");
      objectSearch.wrapper.append(objectHint); form.append(objectSearch.wrapper);
      const plannedName = field("Название планируемого объекта", "text", "planned_object_name", object?.kind === "planned" ? object.name : "", false);
      plannedName.input.maxLength = 255;
      const plannedAddress = field("Адрес планируемого объекта", "text", "planned_object_address", object?.kind === "planned" ? object.address : "", false);
      plannedAddress.input.maxLength = 500;
      form.append(plannedName.wrapper, plannedAddress.wrapper);
      const updatePlanned = () => {
        const planned = ref.input.value === "planned";
        plannedName.wrapper.hidden = plannedAddress.wrapper.hidden = !planned;
        plannedName.input.required = planned;
      };
      ref.input.addEventListener("change", updatePlanned); updatePlanned();
      let timer, generation = 0, controller;
      form.refreshObjects = async () => {
        const clientId = deal?.client?.id || state.drawer?.selectedClient?.id;
        const mine = ++generation;
        if (controller) controller.abort();
        if (!clientId) { objectHint.textContent = "Сначала выберите клиента."; return; }
        controller = new AbortController();
        const url = scopedURL(bootstrap.urls.objects); url.searchParams.set("client_id", clientId);
        if (deal) url.searchParams.set("deal_id", deal.id);
        url.searchParams.set("q", objectSearch.input.value.trim());
        try {
          const response = await requestJSON(url, {headers: {"Accept": "application/json"}}, controller.signal);
          if (mine !== generation || !form.isConnected || String(clientId) !== String(deal?.client?.id || state.drawer?.selectedClient?.id)) return;
          const selected = ref.input.value;
          for (const option of Array.from(ref.input.options)) if (option.value !== "none" && option.value !== "planned" && option.value !== selected) option.remove();
          for (const item of response.objects || []) {
            const value = item.kind + ":" + item.id;
            if (Array.from(ref.input.options).some((option) => option.value === value)) continue;
            const option = element("option", "", [item.name, item.address].filter(Boolean).join(" · ")); option.value = value; ref.input.append(option);
          }
          objectHint.textContent = "Найдено: " + (response.objects || []).length + ". Уточните поиск, если нужного объекта нет.";
        } catch (error) {
          if (!controller.signal.aborted && mine === generation) objectHint.textContent = "Существующие объекты сейчас недоступны.";
        }
      };
      objectSearch.input.addEventListener("input", () => { win.clearTimeout(timer); if (controller) controller.abort(); timer = win.setTimeout(form.refreshObjects, 300); });
      const legacyCurrency = deal?.currency && deal.currency !== "RUB";
      const amount = field(legacyCurrency ? "Историческая сумма · " + deal.currency : "Коммерческая сумма, ₽ (пусто = неизвестна)", "text", "amount", deal?.amount == null ? "" : formatMoney(deal.amount), false);
      amount.input.inputMode = "decimal"; amount.input.maxLength = 40;
      const currency = document.createElement("input"); currency.type = "hidden"; currency.name = "currency"; currency.value = legacyCurrency ? deal.currency : "RUB";
      moneyHost.append(amount.wrapper, currency);
      const basis = selectField(moneyHost, "Основание суммы", "amount_basis", deal?.amount_basis || "unknown", [["unknown", "Неизвестна"], ["indicative", "Предварительная оценка"], ["agreed", "Согласовано с клиентом"]]);
      if (legacyCurrency) {
        amount.input.readOnly = true; basis.input.disabled = true;
        const savedBasis = document.createElement("input"); savedBasis.type = "hidden"; savedBasis.name = "amount_basis"; savedBasis.value = deal.amount_basis; form.append(savedBasis);
        form.append(element("small", "crm-live-muted", "Историческая сумма сохранена в исходной валюте. Пересчёт или смена валюты требуют отдельного решения."));
      }
      const updateAmount = () => { const known = amount.input.value.trim() !== ""; basis.input.required = known; };
      amount.input.addEventListener("input", updateAmount); updateAmount();
      form.append(element("small", "crm-live-muted", "Согласованная сумма — коммерческая оценка менеджера, не договор и не подтверждение оплаты."));
      const decision = field("Ожидаемое коммерческое решение (" + timezone + ")", "datetime-local", "expected_decision_at", localDateTime(deal?.expected_decision_at, timezone), false);
      form.append(decision.wrapper);
      win.setTimeout(form.refreshObjects, 0);
    }

    function cardChanges(values) {
      const ref = String(values.get("object_ref") || "none");
      const [kind, id] = ref.split(":");
      const amount = parseMoney(values.get("amount"));
      const expected = String(values.get("expected_decision_at") || "");
      return {work_subject: String(values.get("work_subject") || "").trim(), specialization: String(values.get("specialization") || "").trim(),
        fulfillment_kind: String(values.get("fulfillment_kind") || "unknown"), object_kind: kind,
        pool_id: kind === "pool" ? Number(id) : null, client_object_id: kind === "client_object" ? Number(id) : null,
        planned_object_name: kind === "planned" ? String(values.get("planned_object_name") || "").trim() : "",
        planned_object_address: kind === "planned" ? String(values.get("planned_object_address") || "").trim() : "",
        amount: amount || null, currency: String(values.get("currency") || "").trim(), amount_basis: amount ? String(values.get("amount_basis")) : "unknown",
        expected_decision_at: expected ? deadlineISO(expected, offset) : null};
    }

    function accountingDate(value) {
      const instant = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value || "") ? value + "T12:00:00" + offset : value);
      return Number.isFinite(instant.getTime()) ? new Intl.DateTimeFormat("ru-RU", {timeZone:timezone, day:"numeric", month:"short", year:"numeric"}).format(instant) : "";
    }
    function orderCaption(order) {
      return [order.number ? "Заказ № " + order.number : "Заказ без номера", accountingDate(order.date)].filter(Boolean).join(" · ");
    }
    function orderBindingForm(host, deal, finance) {
      const part = disclosure(finance.order ? "Параметры заказа для расчётов" : "Выбрать заказ для расчётов");
      part.classList.add("crm-onec-binding"); host.append(part);
      const form = newForm("bind_order", deal.id); part.append(form);
      const search = field("Найти заказ клиента", "search", "order_search", "", false);
      search.input.placeholder = "Номер заказа"; search.input.autocomplete = "off"; search.input.maxLength = 80;
      const searchRow = element("div", "crm-onec-search-row"), refresh = makeButton("Найти в 1С");
      refresh.dataset.orderRefresh = ""; refresh.hidden = !bootstrap.urls.order_refresh;
      searchRow.append(search.wrapper, refresh); searchRow.hidden = Boolean(finance.order);
      form.updateOrderButtons = busy => { refresh.disabled = busy || !search.input.value.trim(); };
      const results = element("div", "crm-onec-order-results"), selected = element("div", "crm-onec-order-selected");
      results.setAttribute("aria-live", "polite"); selected.hidden = true;
      const recurring = field("Постоянное обслуживание", "checkbox", "recurring", "yes", false);
      recurring.wrapper.classList.add("crm-onec-checkbox");
      recurring.input.checked = typeof finance.recurring === "boolean" ? finance.recurring : deal.fulfillment_kind === "service" || pipelineFor(deal.pipeline_id)?.code === "service";
      const behavior = element("p", "crm-live-form-note crm-onec-behavior");
      const explain = () => { behavior.textContent = recurring.input.checked ? "Постоянная сделка останется открытой после выполнения и оплаты текущего заказа. Новый период можно связать со следующим заказом." : "Разовая сделка завершится автоматически, когда 1С подтвердит полное выполнение и полную оплату заказа."; };
      recurring.input.addEventListener("change", explain); explain();
      const confirm = field("Подтверждаю связь выбранного заказа со сделкой", "checkbox", "confirm_order", "yes", true);
      confirm.wrapper.classList.add("crm-onec-checkbox");
      form.append(element("p", "crm-live-form-note", finance.order ? "Чтобы выбрать другой заказ или перейти к следующему периоду, сначала отвяжите текущий заказ. Прежняя связь останется в истории." : "В поиске — последние 200 заказов и заказы, уже связанные со сделками. Показаны заказы выбранного клиента."), searchRow);
      if (!finance.order && bootstrap.urls.order_refresh) form.append(element("p", "crm-live-form-note", "Для более раннего заказа введите полный номер и нажмите «Найти в 1С». Связь со сделкой сохранится только после вашего подтверждения."));
      form.append(results, selected, recurring.wrapper, behavior, confirm.wrapper);
      formFooter(form, finance.order ? "Сохранить связь" : "Привязать заказ");
      const cancel = makeButton("Отмена"); form.querySelector(".crm-live-form-footer").append(cancel);
      let timer, controller, generation = 0;
      const active = () => form.isConnected && dialog.open && state.drawer?.kind === "detail" && sameId(state.drawer.id, deal.id);
      const choose = (order) => {
        if (queue.forSubject(deal.id)) return;
        form.selectedOrder = order; search.input.value = order.number || ""; confirm.input.checked = false;
        selected.replaceChildren(element("strong", "", orderCaption(order)));
        if (order.amount != null && order.currency) selected.append(element("span", "", moneyLabel(order.amount, order.currency)));
        if (order.state) selected.append(element("small", "crm-live-muted", order.state));
        selected.hidden = false; results.replaceChildren(); formError(form, ""); form.updateOrderButtons(false);
      };
      if (finance.order) choose(finance.order);
      const showResults = response => {
        results.replaceChildren();
        for (const order of response.orders || []) {
          const button = makeButton(orderCaption(order), null, "crm-onec-order-option");
          if (order.amount != null && order.currency) button.append(element("small", "", moneyLabel(order.amount, order.currency)));
          button.addEventListener("click", () => choose(order)); results.append(button);
        }
        if (!response.orders?.length) results.append(element("p", "crm-live-muted", response.reason || "Заказы не найдены. Проверьте номер и связь клиента с 1С."));
      };
      const load = () => {
        if (finance.order || form.dataset.orderRefreshing === "true") return;
        win.clearTimeout(timer); controller?.abort(); const mine = ++generation;
        results.replaceChildren();
        timer = win.setTimeout(async () => {
          if (!active() || !part.open || queue.forSubject(deal.id)) return;
          controller = new AbortController();
          const url = scopedURL(bootstrap.urls.order_candidates); url.searchParams.set("deal_id", deal.id); url.searchParams.set("q", search.input.value.trim());
          results.append(element("p", "crm-live-muted", "Ищем заказы…"));
          try {
            const response = await requestJSON(url, {headers:{Accept:"application/json"}}, controller.signal);
            if (!active() || mine !== generation) return;
            showResults(response);
          } catch (error) {
            if (!controller.signal.aborted && active() && mine === generation) results.replaceChildren(element("p", "crm-live-form-error", friendlyError(error)));
          }
        }, 300);
      };
      search.input.addEventListener("input", () => { form.selectedOrder = null; selected.hidden = true; confirm.input.checked = false; form.updateOrderButtons(false); load(); });
      refresh.addEventListener("click", async () => {
        const query = search.input.value.trim();
        if (!query || !bootstrap.urls.order_refresh || !active() || queue.forSubject(deal.id) || form.dataset.orderRefreshing === "true") return;
        win.clearTimeout(timer); controller?.abort(); const mine = ++generation;
        form.dataset.orderRefreshing = "true"; refresh.textContent = "Поиск в 1С…"; updateFormLocks(); formError(form, "");
        const progress = element("p", "crm-live-muted", "Проверяем заказ в 1С…"); results.prepend(progress);
        try {
          const response = await requestJSON(scopedURL(bootstrap.urls.order_refresh), {method:"POST", headers:{"Content-Type":"application/json",Accept:"application/json","X-CSRFToken":csrf ? csrf.value : ""}, body:JSON.stringify({deal_id:deal.id,q:query})}, undefined, 120000);
          if (active() && mine === generation) showResults(response);
        } catch (error) {
          if (active() && mine === generation) formError(form, error.name === "AbortError" ? "Поиск занял больше времени. Сохранённые результаты доступны." : friendlyError(error));
        } finally { progress.remove(); delete form.dataset.orderRefreshing; refresh.textContent = "Найти в 1С"; updateFormLocks(); }
      });
      part.addEventListener("toggle", () => { if (part.open) load(); else if (form.dataset.orderRefreshing !== "true") { win.clearTimeout(timer); controller?.abort(); ++generation; } });
      cancel.addEventListener("click", () => { if (queue.forSubject(deal.id)) return; openDetail(deal.id, true); });
      form.updateOrderButtons(false);
    }
    function documentSection(deal) {
      const details = deal.onec_documents;
      if (!bootstrap.urls.document_candidates) return null;
      const box = section("Документы 1С"), linked = element("div", "crm-onec-documents");
      if (!details?.visible) {
        box.append(element("p", "crm-live-muted", "Документы доступны ответственному за сделку и владельцу организации."));
        return box;
      }
      box.append(linked);
      if (details.reason) box.append(element("p", "crm-live-muted", details.reason));
      const part = disclosure("Найти и привязать документ"), form = newForm("documents", deal.id);
      const filters = element("div", "crm-live-fields"); part.append(form); form.append(filters);
      const kind = selectField(filters, "Тип документа", "kind", "order", [["order","Заказ"],["realization","Реализация"],["check","Чек ККМ"]]);
      const search = field("Номер или контрагент", "search", "number", "", false);
      search.input.maxLength = 80; search.input.autocomplete = "off";
      const refresh = makeButton("⟳"); refresh.type = "button";
      refresh.title = "Обновить данные 1С по полному номеру"; refresh.setAttribute("aria-label", refresh.title);
      const row = element("div", "crm-onec-search-row"); row.append(search.wrapper, refresh); filters.append(row);
      const day = field("Дата документа", "date", "date", "", false);
      const amount = field("Сумма документа", "text", "amount", "", false); amount.input.inputMode = "decimal"; amount.input.maxLength = 40;
      filters.append(day.wrapper, amount.wrapper);
      const results = element("div", "crm-onec-order-results"), status = element("p", "crm-live-muted");
      status.setAttribute("role", "status");
      const selection = element("p", "crm-onec-order-selected");
      const reason = field("Причина снятия или замены", "textarea", "reason", "", false); reason.input.maxLength = 500;
      const confirm = field("Подтверждаю изменение связи в CRM", "checkbox", "confirm_document", "yes", true);
      form.append(status, results, selection, reason.wrapper, confirm.wrapper);
      formFooter(form, "Привязать документ");
      const save = form.querySelector("button[type=submit]");
      const clear = makeButton("Новая привязка"); form.querySelector(".crm-live-form-footer").append(clear);
      let mode = "bind", target = null, selected = null, generation = 0, timer, controller;
      const active = () => form.isConnected && dialog.open && sameId(state.drawer?.id, deal.id);
      const caption = item => item.unverified ? item.label + " · требуется сверка" :
        [item.label, item.number, accountingDate(item.date), item.amount && item.currency && moneyLabel(item.amount, item.currency)].filter(Boolean).join(" · ");
      const update = () => {
        const busy = Boolean(queue.forSubject(deal.id)) || form.dataset.orderRefreshing === "true";
        filters.hidden = mode === "remove"; results.hidden = filters.hidden;
        refresh.disabled = busy || !search.input.value.trim() || !bootstrap.urls.document_refresh;
        reason.wrapper.hidden = mode === "bind"; reason.input.required = mode !== "bind";
        save.textContent = mode === "remove" ? "Отвязать документ" : mode === "replace" ? "Заменить документ" : "Привязать документ";
        save.disabled = busy || (mode !== "remove" && !selected);
        clear.hidden = mode === "bind" || !details.can_search;
        selection.textContent = [target && "Текущая связь · " + caption(target), selected && "Выбран документ · " + caption(selected)].filter(Boolean).join(" / ");
        selection.hidden = !selection.textContent;
        form.documentChanges = { ...(selected ? {kind:selected.kind, record_id:selected.id} : {}),
          ...(target?.automatic ? {exclude_id:target.id} : target ? {remove_link_id:target.link_id} : {}) };
      };
      form.updateOrderButtons = update;
      const showMode = (next, item = null) => {
        win.clearTimeout(timer); controller?.abort(); ++generation;
        mode = next; target = item; selected = null;
        confirm.input.checked = false; reason.input.value = ""; results.replaceChildren(); status.textContent = "";
        formError(form, ""); update(); part.open = true;
        if (mode === "remove") reason.input.focus(); else { search.input.focus(); load(1, 0); }
      };
      clear.addEventListener("click", () => showMode("bind"));
      const showResults = (response, page = 1) => {
        results.replaceChildren();
        for (const item of response.items || []) {
          const button = makeButton(caption(item), null, "crm-onec-order-option");
          button.append(element("small", "", [item.customer, item.basis, item.state, "Сохранён " + dateText(item.source_at)].filter(Boolean).join(" · ")));
          button.addEventListener("click", () => {
            selected = item; confirm.input.checked = false;
            for (const option of results.querySelectorAll("[aria-pressed]")) option.setAttribute("aria-pressed", String(option === button));
            update();
          });
          button.setAttribute("aria-pressed", "false"); results.append(button);
        }
        if (response.reason) results.append(element("p", "crm-live-muted", response.reason));
        if (page > 1) results.append(makeButton("Предыдущие документы", () => load(page - 1, 0)));
        if (response.has_more) results.append(makeButton("Следующие документы", () => load(page + 1, 0)));
        status.textContent = response.checked_at ? "Проверено в 1С " + dateText(response.checked_at) :
          response.last_failed ? "Доступны сохранённые данные. Последнее обновление завершилось ошибкой." : "";
      };
      function load(page = 1, delay = 250) {
        if (mode === "remove" || !details.can_search || form.dataset.orderRefreshing === "true") return;
        win.clearTimeout(timer); controller?.abort(); const ticket = ++generation;
        update();
        timer = win.setTimeout(async () => {
          if (!active() || !part.open) return;
          controller = new AbortController();
          const signal = controller.signal, url = scopedURL(bootstrap.urls.document_candidates);
          for (const [key, value] of Object.entries({deal_id:deal.id, kind:kind.input.value, q:search.input.value.trim(),
            amount:amount.input.value.trim(), date:day.input.value, page})) url.searchParams.set(key, value);
          status.textContent = "Ищем в сохранённых данных…";
          try {
            const response = await requestJSON(url, {headers:{Accept:"application/json"}}, signal);
            if (active() && ticket === generation) showResults(response, page);
          } catch (error) {
            if (!signal.aborted && active() && ticket === generation) status.textContent = friendlyError(error);
          }
        }, delay);
      }
      for (const input of [kind.input, search.input, day.input, amount.input]) input.addEventListener("input", () => {
        selected = null; confirm.input.checked = false; results.replaceChildren(); load();
      });
      const paintLinks = projection => {
        linked.replaceChildren();
        if (projection.review) linked.append(element("p", "crm-onec-warning", "Некоторые связи требуют сверки."));
        if (!projection.items?.length && !projection.reason) linked.append(element("p", "crm-live-muted", "Связанных документов пока нет."));
        for (const item of projection.items || []) {
          const entry = element("div", "crm-onec-document");
          entry.append(element("strong", "", caption(item)));
          if (item.automatic) entry.append(element("span", "crm-live-muted", item.basis));
          if (item.anchor) entry.append(element("span", "crm-live-muted", "Заказ для расчётов"));
          if (projection.can_edit && !item.anchor) {
            entry.append(makeButton("Отвязать", () => showMode("remove", item)));
            if (projection.can_search) entry.append(makeButton("Заменить", () => showMode("replace", item)));
          }
          linked.append(entry);
        }
      };
      refresh.addEventListener("click", async () => {
        const query = search.input.value.trim();
        if (!active() || !query || refresh.disabled) return;
        win.clearTimeout(timer); controller?.abort(); const ticket = ++generation;
        form.dataset.orderRefreshing = "true"; updateFormLocks();
        status.textContent = "Проверяем полный номер в 1С…";
        try {
          const response = await requestJSON(scopedURL(bootstrap.urls.document_refresh), {method:"POST",
            headers:{"Content-Type":"application/json", Accept:"application/json", "X-CSRFToken":csrf?.value || ""},
            body:JSON.stringify({deal_id:deal.id, kind:kind.input.value, q:query})}, undefined, 45000);
          if (active() && ticket === generation) { showResults(response); if (response.links) paintLinks(response.links); }
        } catch (error) {
          if (active() && ticket === generation) status.textContent = error.name === "AbortError" ?
            "Проверка заняла больше времени. Сохранённые результаты доступны." : friendlyError(error);
        } finally { delete form.dataset.orderRefreshing; updateFormLocks(); }
      });
      part.addEventListener("toggle", () => { if (part.open && mode === "bind") load(1, 0); });
      paintLinks(details);
      if (details.can_search || details.can_edit && details.items?.length) {
        box.append(part);
        if (!details.can_search) part.querySelector("summary").hidden = true;
        update();
      }
      return box;
    }

    function financeSection(deal) {
      const finance = deal.onec_finance;
      if (!finance) return null;
      const box = section("Расчёты по заказу 1С"); box.classList.add("crm-onec-finance"); box.dataset.financeState = finance.state;
      if (finance.state === "restricted" && bootstrap.urls.document_candidates) return null;
      if (finance.state === "restricted") {
        box.append(element("p", "crm-live-muted", "Данные 1С доступны сотрудникам с правом просмотра финансов.")); return box;
      }
      const order = finance.order;
      if (order) {
        const heading = element("div", "crm-onec-order"); heading.append(element("strong", "", orderCaption(order)));
        if (order.amount != null && order.currency) heading.append(element("span", "", moneyLabel(order.amount, order.currency)));
        box.append(heading);
        if (order.state) box.append(element("p", "crm-live-muted crm-onec-order-state", order.state));
      } else box.append(element("p", "crm-live-muted", finance.state === "unlinked" ? "Заказ клиента пока не привязан." : "Данные связанного заказа сейчас недоступны."));
      if (["stale", "review"].includes(finance.state)) {
        box.append(element("p", "crm-onec-warning", finance.state === "stale" ? "Данные 1С требуют обновления" : "Связь или документы требуют сверки"));
      }
      const hasPolicyNote = order && (finance.completed_automatically || finance.recurring || finance.auto_complete);
      if (finance.reason && (finance.state !== "current" || !hasPolicyNote)) box.append(element("p", "crm-live-form-note", finance.reason));
      if (order) {
        if (finance.period_label) box.append(element("p", "crm-onec-period", finance.period_label));
        const metrics = element("div", "crm-onec-metrics");
        for (const [key, label, percent, amount] of [["completion", "Выполнение по 1С", finance.completion_percent, finance.realized_amount], ["payment", "Оплата", finance.payment_percent, finance.paid_amount]]) {
          const known = percent !== null && percent !== undefined && percent !== "" && Number.isFinite(Number(percent)) && Number(percent) >= 0;
          const metric = element("div", "crm-onec-metric"), header = element("div", "crm-onec-metric-heading"); metric.dataset.financeMetric = key;
          const value = known ? new Intl.NumberFormat("ru-RU", {maximumFractionDigits:2}).format(Number(percent)) + "%" : "Не подтверждено";
          header.append(element("span", "", label), element("strong", "", value)); metric.append(header);
          const track = element("div", "crm-onec-progress" + (known ? "" : " is-unknown"));
          if (known) { track.setAttribute("role", "progressbar"); track.setAttribute("aria-label", label); track.setAttribute("aria-valuemin", "0"); track.setAttribute("aria-valuemax", "100"); track.setAttribute("aria-valuenow", String(Math.min(100, Number(percent)))); track.setAttribute("aria-valuetext", value); const fill = element("span"); fill.style.width = Math.min(100, Number(percent)) + "%"; track.append(fill); }
          else track.setAttribute("aria-hidden", "true");
          metric.append(track);
          if (amount != null && order.currency) metric.append(element("small", "crm-live-muted", moneyLabel(amount, order.currency) + (order.amount != null ? " из " + moneyLabel(order.amount, order.currency) : "")));
          metrics.append(metric);
        }
        box.append(metrics);
        if (finance.source_at) box.append(element("p", "crm-onec-source", "Данные 1С на " + dateText(finance.source_at)));
        if (finance.completed_automatically) box.append(element("p", "crm-onec-result", "Сделка завершена автоматически: выполнение и оплата подтверждены 1С."));
        else if (finance.recurring) box.append(element("p", "crm-live-form-note", "Постоянное обслуживание: выполнение и оплата текущего заказа не закрывают сделку."));
        else if (finance.auto_complete) box.append(element("p", "crm-live-form-note", "Сделка завершится автоматически после подтверждения полного выполнения и полной оплаты в 1С."));
        if (finance.open_action_preserved) box.append(element("p", "crm-live-form-note", "Открытые задачи сохранены — проверьте их отдельно."));
      }
      if (finance.documents?.length) {
        const documents = disclosure("Распределение по заказу · " + finance.documents.length); documents.classList.add("crm-onec-documents");
        for (const document of finance.documents) {
          const row = element("div", "crm-onec-document"); row.append(element("strong", "", document.number || "Документ без номера"));
          if (document.date) row.append(element("span", "crm-live-muted", accountingDate(document.date)));
          if (document.amount != null && order?.currency) row.append(element("span", "", moneyLabel(document.amount, order.currency)));
          if (document.paid_amount != null && order?.currency) row.append(element("small", "crm-live-muted", "Оплачено: " + moneyLabel(document.paid_amount, order.currency)));
          documents.append(row);
        }
        box.append(documents);
      }
      if (finance.can_bind && bootstrap.urls.order_candidates
          && (order || deal.onec_documents?.can_search) && (!finance.can_unbind || order)) orderBindingForm(box, deal, finance);
      if (finance.can_unbind) {
        const part = disclosure("Отвязать заказ"), form = newForm("unbind_order", deal.id);
        part.classList.add("crm-onec-binding");
        form.append(element("p", "crm-live-form-note", "Связь сохранится в истории сделки. Документы и суммы 1С не изменятся."));
        const reason = field("Причина отвязки", "textarea", "reason", "", true); reason.input.maxLength = 500;
        form.append(reason.wrapper); formFooter(form, "Отвязать заказ"); part.append(form); box.append(part);
      }
      return box;
    }

    function pipelineEditor(meta, deal) {
      const name = pipelineFor(deal.pipeline_id)?.name || "Воронка";
      const stages = pipeline => pipeline.stages.filter(stage => stage.can_transition && stage.code !== "completed");
      const destinations = (state.data.pipelines || []).filter(pipeline =>
        !sameId(pipeline.id, deal.pipeline_id) && stages(pipeline).length);
      if (!deal.can_change_pipeline || !destinations.length) {
        const pill = element("span", "crm-live-pill", name);
        if (deal.pipeline_change_block_reason) pill.title = deal.pipeline_change_block_reason;
        meta.append(pill);
        return null;
      }
      const trigger = makeButton(name, null, "crm-live-pill crm-live-pipeline-pill");
      trigger.setAttribute("aria-label", "Сменить воронку. Текущая воронка — " + name);
      trigger.setAttribute("aria-controls", "crm-live-pipeline-editor");
      meta.append(trigger);
      const form = newForm("change_pipeline", deal.id);
      form.id = "crm-live-pipeline-editor";
      const fields = element("div", "crm-live-fields"); form.append(fields);
      const pipeline = selectField(fields, "Новая воронка", "pipeline_id", "", [
        ["", "Выберите воронку"], ...destinations.map(item => [item.id, item.name]),
      ]).input;
      const stage = selectField(fields, "Этап в новой воронке", "stage_id", "", []).input;
      pipeline.required = stage.required = true;
      const updateStages = () => {
        const choice = destinations.find(item => sameId(item.id, pipeline.value));
        const options = choice ? stages(choice) : [];
        stage.replaceChildren(...options.map(item => {
          const option = element("option", "", item.name); option.value = item.id; return option;
        }));
        if (!options.length) {
          const empty = element("option", "", "Сначала выберите воронку"); empty.value = ""; stage.append(empty);
        }
        const corresponding = options.find(item => item.code === stageFor(deal)?.code);
        stage.value = corresponding?.id ?? options[0]?.id ?? "";
      };
      pipeline.addEventListener("change", updateStages);
      updateStages();
      formFooter(form, "Сменить воронку");
      const cancel = makeButton("Отмена"); form.querySelector(".crm-live-form-footer").append(cancel);
      form.showPipelineEditor = (open, focus = true) => {
        if (open) selectDetailPanel("overview");
        form.hidden = !open;
        trigger.setAttribute("aria-expanded", String(open));
        if (focus) (open ? pipeline : trigger).focus();
      };
      trigger.addEventListener("click", () => form.showPipelineEditor(form.hidden));
      cancel.addEventListener("click", () => form.showPipelineEditor(false));
      form.showPipelineEditor(false, false);
      return form;
    }

    function renderDetail(deal, events) {
      dialogContent.replaceChildren();
      state.drawer = {kind: "detail", id: deal.id, deal};
      dialogContent.dataset.revision = String(deal.revision);
      dialogContent.append(titleNode(deal.title));
      dialogContent.append(amountPanel(deal));
      if(deal.unverified_completion_stage)dialogContent.append(element("p","crm-live-completion-warning",(deal.manual_completion ? "CRM завершена вручную. " : "Финальный этап без подтверждённого итога. ")+deal.completion_warning));
      byId("crm-live-dialog-caption").textContent = "Сделка №" + deal.id;
      const client = element("p", "crm-live-detail-client");
      const href = safeLink(deal.client?.url);
      if (href) { const link = element("a", "", deal.client.name); link.href = href; client.append(link); }
      else client.textContent = deal.client?.name || "Клиент не указан";
      dialogContent.append(client);
      if (deal.contact && !sameId(deal.contact.id, deal.client?.id)) {
        const contact = element("p", "crm-live-detail-contact"); contact.append(element("span", "crm-live-muted", "Контакт: "));
        const href = safeLink(deal.contact.url), name = element(href ? "a" : "span", "", deal.contact.name);
        if (href) name.href = href; contact.append(name); dialogContent.append(contact);
      }
      const quoteUrl = safeLink(deal.quotes_url);
      if (quoteUrl) { const link = element("a", "crm-live-button crm-live-document-link", "КП: версии и решения"); link.href = quoteUrl; dialogContent.append(link); }
      const serviceUrl = safeLink(deal.service_documents_url);
      if (serviceUrl) { const link = element("a", "crm-live-button crm-live-document-link", "Документы обслуживания и периоды"); link.href = serviceUrl; dialogContent.append(link); }
      const meta = element("div", "crm-live-detail-meta");
      const movePipeline = pipelineEditor(meta, deal);
      meta.append(element("span", "crm-live-pill", stageFor(deal)?.name || ""), element("span", "crm-live-pill", deal.manager?.name || "Без ответственного"));
      if (deal.can_assign && bootstrap.urls.responsibles) {
        const assign = makeButton(deal.manager ? "Сменить ответственного" : "Назначить ответственного", null, "crm-live-inline-action");
        assign.addEventListener("click", () => { selectDetailPanel("edit"); const form = dialogContent.querySelector('[data-command-form="assign"]'); form?.scrollIntoView({block:"nearest"}); form?.querySelector("select")?.focus(); }); meta.append(assign);
      }
      if (deal.can_transition && !deal.is_archived) {
        const changeStage = element("button", "crm-live-inline-action", "Изменить этап"); changeStage.type = "button";
        changeStage.addEventListener("click", () => {
          selectDetailPanel("edit"); const form = dialogContent.querySelector('[data-command-form="transition"]');
          if (form) { revealField(form); form.querySelector("select")?.focus(); }
        }); meta.append(changeStage);
      }
      dialogContent.append(meta);
      if (movePipeline) dialogContent.append(movePipeline);
      const documents = documentSection(deal); if (documents) dialogContent.append(documents);
      const finance = financeSection(deal); if (finance) dialogContent.append(finance);
      const overview = section("О сделке");
      const object = deal.object;
      overview.append(inlineDealField(deal,"title","Название"), inlineDealField(deal,"work_subject","Предмет","textarea"),
        inlineDealField(deal,"specialization","Специализация"),
        inlineDealField(deal,"fulfillment_kind","Исполнение","select",[["unknown","Не уточнено"],["construction","Строительство / монтаж"],["service","Обслуживание"],["retail","Поставка"],["mixed","Смешанное"]]),
        inlineDealField(deal,"expected_decision_at","Ожидаемое решение","datetime-local"));
      const facts = [["Объект", !object ? "Не выбран" : object.restricted ? "Недоступен" : (object.kind === "planned" ? "Планируемый: " : "") + [object.name, object.address].filter(Boolean).join(" · ")],
        ["Исход", deal.unverified_completion_stage ? (deal.manual_completion ? "CRM завершена вручную; подтверждённый итог ещё открыт" : "Итог не подтверждён") : outcomeLabel(deal.outcome)]];
      for (const [label,value] of facts) {
        const row = element("div", "crm-live-fact"); row.append(element("span", "crm-live-fact-label", label), element("span", "", value)); overview.append(row);
      }
      if (deal.closed_at) overview.append(element("p", "crm-live-fact", "Завершена: " + dateText(deal.closed_at)));
      if (deal.close_reason) overview.append(element("p", "crm-live-fact", "Причина: " + deal.close_reason));
      dialogContent.append(overview);
      if (deal.need_provenance) {
        const provenance = section("Потребность объекта");
        if (deal.need_provenance.restricted) provenance.append(element("p", "crm-live-muted", "Исходная потребность сейчас недоступна."));
        else {
          for (const position of deal.need_provenance.positions || []) provenance.append(element("p", "crm-live-fact", "#" + position.line_id + " · " + position.description + " · " + position.reason + " · состав #" + position.source_revision + ", разбор #" + position.review_revision));
          const sourceURL = safeLink(deal.need_provenance.url);
          if (sourceURL) { const sourceLink = element("a", "", "Открыть разбор потребности"); sourceLink.href = sourceURL; provenance.append(sourceLink); }
        }
        dialogContent.append(provenance);
      }
      const next = element("div", "crm-live-detail-next is-" + attentionFor(deal));
      next.append(nextElement(deal));
      if (deal.next_action?.can_open && safeLink(deal.next_action.url)) {
        const link = element("a", "crm-live-button crm-live-button-quiet", "Открыть задачу");
        link.href = safeLink(deal.next_action.url);
        link.style.marginTop = "12px";
        next.append(link);
      }
      dialogContent.append(next);
      if (deal.can_assign && bootstrap.urls.responsibles) {
        const part = section("Ответственный"); part.dataset.detailSection = "edit"; part.classList.add("crm-assignment");
        const form = newForm("assign", deal.id); responsibleField(form, deal);
        form.append(element("p", "crm-live-form-note", deal.assignment_note || "Передачу текущего следующего действия можно подтвердить отдельно."));
        const task = deal.assignment_task;
        if (task?.can_transfer) {
          const transfer = field("Передать текущее следующее действие", "checkbox", "transfer_next_action", "yes", false); transfer.input.checked = true; transfer.wrapper.classList.add("crm-onec-checkbox"); form.append(transfer.wrapper);
          form.append(element("p", "crm-live-form-note", (task.title || "Текущий шаг") + ". Остальные задачи сохранят своих исполнителей."));
        } else if (task?.reason) form.append(element("p", "crm-live-form-note", task.reason));
        formFooter(form, "Назначить ответственного"); const cancel = makeButton("Отмена"); cancel.addEventListener("click", () => openDetail(deal.id, true)); form.querySelector(".crm-live-form-footer").append(cancel); part.append(form); dialogContent.append(part);
      }
      if (deal.can_next_action && !deal.is_archived && !deal.next_action?.restricted && !deal.next_action?.waiting) {
        const schedule = element("button", "crm-live-button", "Запланировать дело"); schedule.type = "button";
        schedule.addEventListener("click", () => { selectDetailPanel("tasks"); dialogContent.querySelector('[name="next_title"]')?.focus(); });
        next.append(schedule);
      }
      if (deal.can_transition && !deal.is_archived) {
        const part = disclosure("Изменить этап"); part.dataset.detailSection = "edit";
        const form = newForm("transition", deal.id);
        form.append(element("p", "crm-live-form-note", "Ручной этап отражает работу менеджера. Он не подтверждает оплату, документы, выполнение или разрешение на запуск работ."));
        const stages = field("Перевести на этап", "select", "stage_id", null, true);
        for (const stage of pipelineFor(deal.pipeline_id)?.stages || []) {

          const option = element("option", "", stage.name);
          option.value = stage.id;
          option.disabled = !stage.can_transition && !sameId(stage.id, deal.stage_id);
          if (option.disabled) option.textContent += " · " + (stage.transition_block_reason || "Недоступен");
          option.selected = sameId(stage.id, deal.stage_id);
          stages.input.append(option);
        }
        const reason = field("Почему возвращаемся на предыдущий этап", "textarea", "reason", "", false);
        reason.wrapper.hidden = true;
        reason.input.maxLength = 2000;
        stages.input.addEventListener("change", () => {
          try {
            const intent = transitionIntent(deal, stages.input.value, pipelineFor(deal.pipeline_id));
            reason.wrapper.hidden = !intent?.backwards;
            reason.input.required = Boolean(intent?.backwards);
          } catch (error) { formError(form, error.message); }
        });
        form.append(stages.wrapper, reason.wrapper);
        formFooter(form, "Изменить этап");
        part.append(form);
        dialogContent.append(part);
      }
      if (deal.can_next_action && !deal.is_archived && !deal.next_action?.restricted && !deal.next_action?.waiting) {
        const part = section("Следующее действие");
        part.dataset.detailSection = "tasks";
        const form = newForm("next_action", deal.id);
        nextFields(form, deal.next_action);
        formFooter(form, deal.next_action && !deal.next_action.completed ? "Сохранить следующий шаг" : "Назначить следующий шаг");
        part.append(form);
        dialogContent.append(part);
      }
      if (deal.can_edit && !deal.is_archived) {
        const part = section("Изменить карточку сделки");
        part.dataset.detailSection = "edit";
        const form = newForm("edit", deal.id);
        const title = field("Название", "text", "title", deal.title, true);
        title.input.maxLength = 255;
        form.append(title.wrapper);
        cardFields(form, deal);
        formFooter(form, "Сохранить карточку");
        part.append(form);
        dialogContent.append(part);
      }
      if (deal.can_close_negative) {
        const part = disclosure("Сделка провалена / отменена"); part.dataset.detailSection = "edit";
        const form = newForm("close_negative", deal.id);
        form.append(element("p", "crm-live-form-note", "Сделка завершится, а её активный следующий шаг будет отменён. Выполненным он не станет."));
        selectField(form, "Исход", "outcome", "lost", [["lost", "Сделка провалена"], ["cancelled", "Отмена"]]);
        const reason = field("Причина", "textarea", "reason", "", true); reason.input.maxLength = 500;
        form.append(reason.wrapper); formFooter(form, "Зафиксировать исход"); part.append(form); dialogContent.append(part);
      }
      const activity = element("section"); dialogContent.append(activity);
      win.CrmCardActivity.mount(activity, {url: bootstrap.urls.detail_prefix + deal.id + "/activity/?org_id=" + bootstrap.organization_id, timezone:bootstrap.timezone,
        commentForm: deal.can_comment ? () => {
          const form = newForm("comment", deal.id); form.classList.add("crm-activity-comment");
          const body = field("Комментарий", "textarea", "body", "", true); body.input.maxLength = 4000; body.input.rows = 3;
          body.input.placeholder = "Договорённости, результат разговора или заметка для коллег…";
          form.append(body.wrapper); formFooter(form, "Добавить комментарий"); return form;
        } : null});
      detailTabs();
      updateFormLocks();
    }

    async function openDetail(id, keepOpen, updateURL = true) {
      if (updateURL) setDealURL(id, !keepOpen);
      const generation = ++state.detailGeneration;
      if (state.detailController) state.detailController.abort();
      const controller = new AbortController();
      state.detailController = controller;
      state.drawer = {kind: "loading", id};
      updateFormLocks();
      if (!keepOpen) {
        dialogContent.replaceChildren(titleNode(dealFor(id)?.title || "Карточка сделки"), element("p", "crm-live-muted", "Загружаем актуальные данные…"));
        openDialog("Карточка сделки");
      }
      try {
        const response = await requestJSON(detailURL(id), {headers: {"Accept": "application/json"}}, controller.signal);
        if (generation !== state.detailGeneration || !dialog.open) return;
        if (!response.deal || !sameId(response.deal.id, id)) throw new Error("Не удалось прочитать карточку сделки.");
        renderDetail(response.deal, response.events || []);
      } catch (error) {
        if (controller.signal.aborted || generation !== state.detailGeneration) return;
        byId("crm-live-dialog-caption").textContent = "Карточка сделки";
        state.drawer = {kind: "error", id};
        const retry = makeButton("Повторить загрузку", "open");
        retry.dataset.dealId = id;
        dialogContent.replaceChildren(titleNode("Карточка недоступна"), element("p", "crm-live-form-error", friendlyError(error)), retry);
      }
    }

    async function failureDialog(snapshot) {
      if (!snapshot?.can_close_negative || state.loading || queue.forSubject(snapshot.id)) return;
      await openDetail(snapshot.id);
      if (!dialog.open || state.drawer?.kind !== "detail" || !sameId(state.drawer.id, snapshot.id)) return;
      const deal = state.drawer.deal;
      if (deal.revision !== snapshot.revision || !deal.can_close_negative) {
        notice("Сделка изменилась или завершение недоступно. Проверьте актуальную карточку.", true); return;
      }
      dialogContent.replaceChildren(titleNode("Сделка провалена"), element("p", "crm-live-detail-client", deal.title));
      const form = newForm("close_negative", deal.id);
      const outcome = element("input"); outcome.type = "hidden"; outcome.name = "outcome"; outcome.value = "lost"; form.append(outcome);
      form.append(element("p", "crm-live-form-note", "Сделка перейдёт в проваленные после подтверждения. Активный следующий шаг будет отменён; история, контакты и документы сохранятся."));
      const reason = field("Причина провала", "textarea", "reason", "", true); reason.input.maxLength = 500;
      form.append(reason.wrapper); formFooter(form, "Подтвердить провал");
      form.querySelector(".crm-live-form-footer").append(makeButton("Отмена", "close"));
      dialogContent.append(form); openDialog("Завершение сделки"); updateFormLocks(); reason.input.focus();
    }

    function createDialog() {
      if (!state.data.capabilities?.create || queue.forSubject(null)) return;
      if (state.detailController) state.detailController.abort();
      state.detailGeneration += 1;
      setDealURL(null, false);
      state.drawer = {kind: "create", selectedClient: null, useCustomer: false, customerEditor: null};
      dialogContent.replaceChildren(titleNode("Новая сделка"), element("p", "crm-live-form-note", "Выберите клиента, ответственного и договоритесь о следующем шаге."));
      const form = newForm("create", null);
      const title = field("Название сделки", "text", "title", "", true);
      title.input.placeholder = "Например, строительство бассейна";
      title.input.maxLength = 255;
      const client = field("Клиент", "search", "client_search", "", true);
      client.input.autocomplete = "off";
      client.input.placeholder = "Не менее трёх символов";
      client.input.setAttribute("aria-describedby", "crm-live-client-hint");
      const hint = element("small", "", "Выберите клиента из результатов поиска.");
      hint.id = "crm-live-client-hint";
      client.wrapper.append(hint);
      const clientResults = element("div", "crm-live-client-results");
      clientResults.hidden = true;
      clientResults.setAttribute("aria-live", "polite");
      const selected = element("small", "crm-live-client-selected");
      selected.hidden = true;
      client.wrapper.append(selected);
      const pipeline = field("Воронка", "select", "pipeline_id", null, true);
      (state.data.pipelines || []).forEach((item) => {
        const option = element("option", "", item.name);
        option.value = item.id;
        option.selected = sameId(item.id, state.pipelineId);
        pipeline.input.append(option);
      });
      form.append(title.wrapper, client.wrapper, clientResults, pipeline.wrapper);
      responsibleField(form);
      const customerToggle = makeButton("Создать контакт или компанию", null, "crm-live-button");
      const customerHost = element("div", "crm-live-customer-host"); customerHost.hidden = true;
      form.append(customerToggle, customerHost);
      const moneyHost = element("div", "crm-live-fields"); form.append(moneyHost);
      const extra = disclosure("Объект и другие сведения");
      const extraFields = element("div", "crm-live-fields"); extra.append(extraFields); form.append(extra);
      cardFields(extraFields, null, moneyHost); form.refreshObjects = extraFields.refreshObjects;
      nextFields(form, null);
      formFooter(form, "Создать сделку");
      dialogContent.append(form);
      const updateCustomerSelection = item => {
        if (state.drawer?.kind !== "create") return;
        const previousId = state.drawer.selectedClient?.id ?? null;
        state.drawer.selectedClient = item;
        if (previousId === (item?.id ?? null)) return;
        const objects = form.elements.namedItem("object_ref");
        for (const option of Array.from(objects.options)) if (!["none", "planned"].includes(option.value)) option.remove();
        if (!["none", "planned"].includes(objects.value)) objects.value = "none";
        form.refreshObjects?.();
      };
      customerToggle.addEventListener("click", () => {
        if (state.drawer?.kind !== "create") return;
        state.drawer.useCustomer = !state.drawer.useCustomer;
        const active = state.drawer.useCustomer;
        client.wrapper.hidden = active; client.input.disabled = active; clientResults.hidden = true;
        state.clientController?.abort(); state.clientGeneration += 1;
        customerHost.hidden = !active; customerToggle.textContent = active ? "Выбрать существующего клиента" : "Создать контакт или компанию";
        if (active && !state.drawer.customerEditor) state.drawer.customerEditor = win.Service2CustomerEditor.mount(customerHost, {
          lookupUrl: bootstrap.urls.clients, organizationId: bootstrap.organization_id, canCategorize: state.data.can_categorize_contacts ?? bootstrap.can_categorize_contacts,
          contactMode: "new", onChange: item => {
            if (state.drawer?.kind !== "create" || !state.drawer.useCustomer) return;
            updateCustomerSelection(item);
          }
        });
        state.drawer.customerEditor?.setEnabled(active);
        if (active) updateCustomerSelection(state.drawer.customerEditor.primarySelection());
        else { updateCustomerSelection(null); client.input.value = ""; selected.hidden = true; }
        form.refreshObjects?.();
      });
      let clientTimer;
      client.input.addEventListener("input", () => {
        win.clearTimeout(clientTimer);
        if (state.drawer?.kind !== "create") return;
        state.drawer.selectedClient = null;
        const objects = form.elements.namedItem("object_ref");
        for (const option of Array.from(objects.options)) if (!["none", "planned"].includes(option.value)) option.remove();
        if (!["none", "planned"].includes(objects.value)) objects.value = "none";
        selected.hidden = true;
        const generation = ++state.clientGeneration;
        if (state.clientController) state.clientController.abort();
        clientResults.replaceChildren();
        const query = client.input.value.trim();
        clientResults.hidden = query.length < 3;
        if (query.length < 3) return;
        clientResults.append(element("p", "", "Ищем клиентов…"));
        clientTimer = win.setTimeout(async () => {
          if (generation !== state.clientGeneration || state.drawer?.kind !== "create") return;
          const controller = new AbortController();
          state.clientController = controller;
          const url = scopedURL(bootstrap.urls.clients);
          url.searchParams.set("q", query);
          try {
            const response = await requestJSON(url, {headers: {"Accept": "application/json"}}, controller.signal);
            if (generation !== state.clientGeneration || state.drawer?.kind !== "create") return;
            clientResults.replaceChildren();
            if (!response.clients?.length) clientResults.append(element("p", "", "Клиенты не найдены. Попробуйте другое имя или название."));
            else response.clients.forEach((item) => {
              const button = makeButton(item.name);
              button.addEventListener("click", () => {
                if (state.drawer?.kind !== "create") return;
                state.drawer.selectedClient = item;
                client.input.value = item.name;
                selected.textContent = "Выбран: " + item.name;
                selected.hidden = false;
                clientResults.hidden = true;
                formError(form, "");
                form.refreshObjects?.();
              });
              clientResults.append(button);
            });
          } catch (error) {
            if (controller.signal.aborted || generation !== state.clientGeneration) return;
            clientResults.replaceChildren(element("p", "", "Не удалось найти клиентов. Измените запрос, чтобы повторить поиск."));
          }
        }, 300);
      });
      openDialog("Создание сделки");
      title.input.focus();
    }

    function transitionDialog(deal, intent) {
      if (state.detailController) state.detailController.abort();
      state.detailGeneration += 1;
      state.drawer = {kind: "transition", id: deal.id, deal, intent};
      dialogContent.replaceChildren(titleNode(intent.manualCompletion ? "Завершить CRM вручную" : "Вернуть на предыдущий этап"), element("p", "crm-live-detail-client", deal.title), element("p", "crm-live-form-note", intent.from.name + " → " + intent.to.name));
      const form = newForm("transition-confirm", deal.id);
      const reason = field(intent.manualCompletion ? "Комментарий к ручному завершению" : "Причина возврата", "textarea", "reason", "", !intent.manualCompletion);
      if(intent.manualCompletion){form.append(element("p","crm-live-completion-warning","Исполнение и оплата не подтверждаются. Договорные периоды, документы и открытые задачи не изменятся."));const confirm=field("Подтверждаю ручное завершение без подтверждения исполнения и оплаты", "checkbox", "confirm_manual_completion", "yes", true);form.append(confirm.wrapper);}
      reason.input.maxLength = 2000;
      reason.input.placeholder = "Что нужно уточнить или исправить";
      form.append(reason.wrapper);
      formFooter(form, intent.manualCompletion ? "Подтвердить ручное завершение" : "Вернуть сделку");
      dialogContent.append(form);
      openDialog("Изменение этапа");
      reason.input.focus();
      updateFormLocks();
    }

    async function performExisting(key) {
      const entry = await queue.run(key);
      if (entry.status === "succeeded") {
        bulk?.remove(entry.payload.subject_id);
        if (entry.response.result?.assignment_saved && entry.response.result.can_open === false) {
          queue.dismiss(key); closeDialog(); notice("Ответственный назначен. Карточка передана сотруднику и больше недоступна в вашем списке."); await loadData({keepNotice:true}); return entry;
        }
        const deal = entry.response.deal;
        if (entry.payload.command_type === "deal.create") {
          state.pipelineId = deal.pipeline_id;
          state.page = 1; state.filters.outcome = "all"; state.filters.stage = "";
          state.query = "";
          state.attention = "all";
          byId("crm-live-search").value = "";
          renderPipelines();
        }
        if (entry.payload.command_type === "deal.change_pipeline") {
          state.pipelineId = deal.pipeline_id;
          state.page = 1;
          state.filters.stage = "";
          renderPipelines();
          syncURL();
        }
        const index = (state.data.deals || []).findIndex((item) => sameId(item.id, deal.id));
        if (index >= 0) state.data.deals[index] = deal;
        queue.dismiss(key);
        notice(entry.payload.command_type === "deal.create" ? "Сделка создана." : "Изменения сохранены.");
        if (dialog.open && ((state.drawer?.kind === "create" && entry.payload.command_type === "deal.create") || sameId(state.drawer?.id, deal.id))) await openDetail(deal.id, true);
        await loadData({keepNotice: true});
      } else if (entry.status === "failed" && entry.error.status === 409) {
        if (dialog.open && sameId(state.drawer?.id, entry.payload.subject_id)) await openDetail(entry.payload.subject_id, true);
        await loadData({keepNotice: true});
      }
      return entry;
    }

    async function restoreConflict(key) {
      const entry = queue.entries.get(key);
      if (!entry || entry.status !== "failed" || entry.error?.status !== 409) return;
      try {
        if (!dialog.open || state.drawer?.kind !== "detail" || !sameId(state.drawer.id, entry.payload.subject_id)) await openDetail(entry.payload.subject_id);
        if (!dialog.open || state.drawer?.kind !== "detail") return;
        const draft = conflictDraft(entry.payload, state.drawer.deal, timezone);
        const form = dialogContent.querySelector(`[data-command-form="${draft.formKind}"]`);
        if (!form) throw new Error("Сейчас нет доступа к изменению этих полей. Ваш черновик сохранён в уведомлении о конфликте.");
        if (draft.formKind === "assign") {
          await form.responsiblesReady;
          if (!Array.from(form.elements.namedItem("responsible_id").options).some(option => option.value === draft.fields.responsible_id)) throw new Error("Ранее выбранный сотрудник больше недоступен для назначения. Выберите другого ответственного.");
        }
        if (draft.formKind === "change_pipeline") {
          const pipeline = form.elements.namedItem("pipeline_id");
          if (!Array.from(pipeline.options).some(option => option.value === draft.fields.pipeline_id)) throw new Error("Выбранная воронка больше недоступна.");
          form.showPipelineEditor(true);
          pipeline.value = draft.fields.pipeline_id;
          pipeline.dispatchEvent(new Event("change", {bubbles: true}));
        } else revealField(form);
        if (["transition", "change_pipeline"].includes(draft.formKind)) {
          const select = form.elements.namedItem("stage_id");
          if (!Array.from(select.options).some((option) => option.value === draft.fields.stage_id)) throw new Error("Выбранный ранее этап больше недоступен. Проверьте текущий этап; черновик сохранён в уведомлении.");
          select.value = draft.fields.stage_id;
          select.dispatchEvent(new Event("change", {bubbles: true}));
        }
        for (const [name, value] of Object.entries(draft.fields)) {
          const input = form.elements.namedItem(name);
          if (input) {
            if (input.type === "checkbox") { input.checked = Boolean(value); input.dispatchEvent(new Event("change", {bubbles:true})); continue; }
            if (name === "object_ref" && !Array.from(input.options).some((option) => option.value === value)) {
              const option = element("option", "", "Ранее выбранный объект — проверьте доступность"); option.value = value; input.append(option);
            }
            input.value = value == null ? "" : value;
            input.dispatchEvent(new Event("change", {bubbles: true}));
          }
        }
        queue.dismiss(key);
        formError(form, "Ваши изменения возвращены. Проверьте актуальную карточку и сохраните их ещё раз, если всё верно.");
        const input = form.querySelector("input, select, textarea");
        input?.focus();
        form.scrollIntoView({block: "nearest"});
      } catch (error) {
        notice(error.message, true);
        if (dialog.open) {
          const message = element("p", "crm-live-form-error", error.message);
          dialogContent.prepend(message);
        }
      }
    }

    async function send(commandType, subjectId, revision, changes, label) {
      if (!win.crypto?.randomUUID) throw new Error("Для сохранения нужен защищённый HTTPS-доступ к сервису.");
      const payload = {operation_key: win.crypto.randomUUID().toLowerCase(), command_type: commandType, subject_id: subjectId, expected_revision: revision, changes};
      const entry = queue.add(payload, label);
      return performExisting(entry.payload.operation_key);
    }

    async function submitForm(form) {
      formError(form, "");
      if (!form.reportValidity()) return;
      const values = new FormData(form);
      const kind = form.dataset.commandForm;
      const deal = state.drawer?.deal;
      try {
        if (kind === "create") {
          if (!state.drawer?.useCustomer && !state.drawer?.selectedClient) throw new Error("Выберите клиента из результатов поиска.");
          const changes = {title: String(values.get("title") || "").trim(), pipeline_id: Number(values.get("pipeline_id")), next_action: {title: String(values.get("next_title") || "").trim(), due_at: deadlineISO(values.get("due_at"), offset)}};
          if (form.elements.namedItem("responsible_id")) { if (!values.get("responsible_id")) throw new Error("Выберите ответственного."); changes.responsible_id = Number(values.get("responsible_id")); }
          if (state.drawer.useCustomer) changes.customer = state.drawer.customerEditor.value();
          else changes.client_id = state.drawer.selectedClient.id;
          changes.card_fields = cardChanges(values);
          if (!changes.title || !changes.next_action.title) throw new Error("Заполните название сделки и следующий шаг.");
          await send("deal.create", null, null, changes, "Новая сделка «" + changes.title + "»");
        } else if (!deal) throw new Error("Обновите карточку перед сохранением.");
        else if (kind === "change_pipeline") {
          if (!deal.can_change_pipeline || !values.get("pipeline_id") || !values.get("stage_id")) throw new Error("Выберите воронку и этап.");
          await send("deal.change_pipeline", deal.id, deal.revision,
            {pipeline_id: Number(values.get("pipeline_id")), stage_id: Number(values.get("stage_id"))},
            "Воронка сделки «" + deal.title + "»");
        }
        else if (kind === "assign") {
          if (!deal.can_assign || !values.get("responsible_id")) throw new Error("Выберите доступного сотрудника.");
          const changes = {responsible_id:Number(values.get("responsible_id")), transfer_next_action:Boolean(form.elements.namedItem("transfer_next_action")?.checked)};
          if (changes.transfer_next_action) changes.expected_task_version = deal.assignment_task.version;
          await send("deal.assign", deal.id, deal.revision, changes, "Ответственный за «" + deal.title + "»");
        } else if (kind === "comment") {
          const body = String(values.get("body") || "").trim(); if (!body) throw new Error("Напишите комментарий.");
          await send("deal.comment", deal.id, deal.revision, {body}, "Комментарий к «" + deal.title + "»");
        } else if (kind === "amount") {
          const amount = parseMoney(values.get("amount"));
          await send("deal.edit", deal.id, deal.revision, {amount:amount || null, currency:amount ? "RUB" : "", amount_basis:amount ? String(values.get("amount_basis")) : "unknown"}, "Сумма сделки «" + deal.title + "»");
        } else if (kind === "field") {
          const name = form.dataset.fieldName;
          if (!["title","work_subject","specialization","fulfillment_kind","expected_decision_at"].includes(name)) throw new Error("Обновите карточку.");
          const raw = String(values.get(name) || "").trim();
          const value = name === "expected_decision_at" ? (raw ? deadlineISO(raw, offset) : null) : raw;
          await send("deal.edit", deal.id, deal.revision, {[name]:value}, "Карточка сделки «" + deal.title + "»");
        } else if (kind === "edit") {
          const title = String(values.get("title") || "").trim();
          if (!title) throw new Error("Укажите название сделки.");
          await send("deal.edit", deal.id, deal.revision, {title, ...cardChanges(values)}, "Карточка сделки «" + deal.title + "»");
        } else if (kind === "bind_order") {
          if (!deal.onec_finance?.can_bind || !form.selectedOrder) throw new Error("Выберите заказ из результатов поиска.");
          if (!form.elements.namedItem("confirm_order").checked) throw new Error("Подтвердите связь заказа со сделкой.");
          await send("deal.bind_order", deal.id, deal.revision, {order_id:form.selectedOrder.id, recurring:form.elements.namedItem("recurring").checked}, "Заказ 1С для «" + deal.title + "»");
        } else if (kind === "unbind_order") {
          if (!deal.onec_finance?.can_unbind) throw new Error("Отвязка заказа сейчас недоступна.");
          const reason = String(values.get("reason") || "").trim(); if (!reason) throw new Error("Укажите причину отвязки.");
          await send("deal.unbind_order", deal.id, deal.revision, {reason}, "Отвязка заказа 1С от «" + deal.title + "»");
        } else if (kind === "documents") {
          if (!deal.onec_documents?.can_edit || !form.elements.namedItem("confirm_document").checked) throw new Error("Подтвердите изменение связи.");
          await send("deal.documents", deal.id, deal.revision, {...form.documentChanges, reason:String(values.get("reason") || "").trim()}, "Документы 1С для «" + deal.title + "»");
        } else if (kind === "close_negative") {
          const reason = String(values.get("reason") || "").trim();
          if (!reason) throw new Error("Укажите причину отказа или отмены.");
          await send("deal.close_negative", deal.id, deal.revision, {outcome: String(values.get("outcome")), reason,
            expected_task_version: deal.next_action?.version || null}, "Исход сделки «" + deal.title + "»");
        } else if (kind === "next_action") {
          const title = String(values.get("next_title") || "").trim();
          if (!title) throw new Error("Опишите следующий шаг.");
          const changes = {title, due_at: deadlineISO(values.get("due_at"), offset)};
          if (deal.next_action?.version) changes.expected_task_version = deal.next_action.version;
          await send("deal.next_action", deal.id, deal.revision, changes, "Следующий шаг: «" + deal.title + "»");
        } else if (kind === "transition" || kind === "transition-confirm") {
          const stageId = kind === "transition-confirm" ? state.drawer.intent.to.id : form.elements.namedItem("stage_id").value;
          const intent = transitionIntent(deal, stageId, pipelineFor(deal.pipeline_id), values.get("reason"));
          if (!intent) { formError(form, "Сделка уже находится на выбранном этапе."); return; }
          if(intent.manualCompletion && kind!=="transition-confirm"){transitionDialog(deal,intent);return;}
          if(intent.manualCompletion){if(!form.elements.namedItem("confirm_manual_completion")?.checked)throw new Error("Подтвердите предупреждение.");intent.changes.confirm_manual_completion=true;}
          if (intent.needsReason) throw new Error("Укажите причину возврата на предыдущий этап.");
          await send("deal.transition", deal.id, deal.revision, intent.changes, "Перенос «" + deal.title + "» → " + intent.to.name);
        }
      } catch (error) { formError(form, friendlyError(error)); }
    }

    root.addEventListener("submit", (event) => {
      const form = event.target.closest("[data-command-form]");
      if (!form) return;
      event.preventDefault();
      submitForm(form);
    });
    root.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button || button.disabled) return;
      if (button.dataset.pipeline) {
        state.pipelineId = button.dataset.pipeline;
        state.filters.stage = ""; state.page = 1;
        state.attention = "all";
        renderPipelines();
        loadData();
      } else if (button.dataset.view) {
        state.view = button.dataset.view;
        state.page = 1; renderPipelines(); loadData();
      } else if (button.dataset.attention) {
        state.attention = button.dataset.attention; state.page = 1;
        loadData();
      } else if (button.dataset.action === "column-more") {
        const window = (state.data.column_windows || []).find(item => sameId(item.stage_id, button.dataset.stageId));
        if (state.loading || !window?.cursor) return;
        loadData({moreStage:window.stage_id});
      } else if (button.dataset.action === "page-prev" || button.dataset.action === "page-next") {
        state.page = Math.max(1, state.page + (button.dataset.action === "page-next" ? 1 : -1)); loadData();
      } else if (button.dataset.action === "open") {
        if (Date.now() >= state.suppressClickUntil) openDetail(button.dataset.dealId);
      } else if (button.dataset.action === "create") createDialog();
      else if (button.dataset.action === "close") closeDialog();
      else if (button.dataset.action === "retry") performExisting(button.dataset.operationKey);
      else if (button.dataset.action === "restore-conflict") restoreConflict(button.dataset.operationKey);
      else if (button.dataset.action === "dismiss-operation") queue.dismiss(button.dataset.operationKey);
      else if (button.dataset.action === "clear-linked-filters") {
        for (const key of Object.keys(state.filters)) state.filters[key] = key === "outcome" ? "all" : "";
        state.page = 1; loadData();
      }
      else if (button.dataset.action === "reset") {
        state.query = ""; state.owner = "all"; state.attention = "all"; state.page = 1;
        for (const key of Object.keys(state.filters)) { state.filters[key] = key === "outcome" ? "all" : ""; }
        byId("crm-live-search").value = ""; byId("crm-live-owner").value = "all";
        loadData();
      }
    });
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeDialog(); });
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeDialog();
    });
    let searchTimer;
    byId("crm-live-search").value = state.query;
    byId("crm-live-search").addEventListener("input", (event) => {
      state.query = event.target.value.trim(); state.page = 1;
      if(state.dataController)state.dataController.abort(); ++state.dataGeneration; state.loading=true; renderContent();
      win.clearTimeout(searchTimer);
      if (state.dataController) state.dataController.abort();
      searchTimer = win.setTimeout(loadData, 300);
    });
    const ownerSelect = byId("crm-live-owner");
    const renderOwner = () => {
      if (![...ownerSelect.options].some(option => option.value === state.owner)) {
        const option = element("option", "", "Ответственный из ссылки"); option.value = state.owner; ownerSelect.append(option);
      }
      ownerSelect.value = state.owner;
    };
    renderOwner();
    ownerSelect.addEventListener("change", () => { state.owner = ownerSelect.value; state.page = 1; loadData(); });
    byId("crm-live-refresh").addEventListener("click", () => loadData());

    const dragActions = element("div", "crm-drag-actions"); dragActions.hidden = true;
    const failureDrop = element("div", "crm-failure-drop crm-drag-target", "Сделка провалена");
    dragActions.append(failureDrop); root.append(dragActions);
    failureDrop.addEventListener("dragover", event => {
      if (state.loading || !state.dragging || !dealFor(state.dragging.id)?.can_close_negative) return;
      event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect = "move";
      failureDrop.classList.add("is-drop-target");
    });
    failureDrop.addEventListener("dragleave", () => failureDrop.classList.remove("is-drop-target"));
    failureDrop.addEventListener("drop", event => {
      event.preventDefault(); event.stopPropagation(); const dragging = state.dragging; endDrag();
      const deal = dragging && dealFor(dragging.id);
      if (deal?.revision === dragging?.revision) failureDialog(deal);
    });
    root.addEventListener("dragstart", (event) => {
      const card = event.target.closest(".crm-live-card[draggable=true]");
      if (!card || !event.dataTransfer) return;
      const deal = dealFor(card.dataset.dealId);
      if (!deal || state.loading || bulk?.hasSelection() || isClosed(deal) || queue.forSubject(deal.id)) { event.preventDefault(); return; }
      state.dragging = {id: deal.id, revision: deal.revision};
      dragActions.hidden = !deal.can_close_negative;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("application/x-service2-crm-deal", String(deal.id));
      card.classList.add("is-dragging");
    });
    root.addEventListener("dragover", (event) => {
      const lane = event.target.closest(".crm-live-lane");
      if (!lane || state.loading || !state.dragging) return;
      const deal = dealFor(state.dragging.id);
      if (lane.dataset.failure === "true" ? !(lane.dataset.canCloseNegative === "true" && deal?.can_close_negative) : !(lane.dataset.canTransition === "true" && deal?.can_transition)) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      root.querySelectorAll(".is-drop-target").forEach((item) => { if (item !== lane) item.classList.remove("is-drop-target"); });
      lane.classList.add("is-drop-target");
    });
    root.addEventListener("dragleave", (event) => {
      const lane = event.target.closest(".crm-live-lane");
      if (lane && !lane.contains(event.relatedTarget)) lane.classList.remove("is-drop-target");
    });
    function endDrag() {
      root.querySelectorAll(".is-drop-target,.is-dragging").forEach((item) => item.classList.remove("is-drop-target", "is-dragging"));
      state.dragging = null;
      dragActions.hidden = true;
      state.suppressClickUntil = Date.now() + 250;
    }
    root.addEventListener("dragend", endDrag);
    win.addEventListener("blur", endDrag);
    win.addEventListener("keydown", event => { if (event.key === "Escape") endDrag(); });
    root.addEventListener("drop", async (event) => {
      const lane = event.target.closest(".crm-live-lane");
      const dragging = state.dragging;
      if (!lane || !dragging) return;
      event.preventDefault();
      endDrag();
      try {
        const deal = dealFor(dragging.id);
        if (!deal || deal.revision !== dragging.revision) throw new Error("Сделка изменилась во время переноса. Обновите данные и повторите действие.");
        if (queue.forSubject(deal.id)) throw new Error("Сначала проверьте результат предыдущего сохранения этой сделки.");
        if (lane.dataset.failure === "true") {
          if (lane.dataset.canCloseNegative === "true") await failureDialog(deal);
          return;
        }
        const intent = transitionIntent(deal, lane.dataset.stageId, pipelineFor(deal.pipeline_id));
        if (!intent) return;
        if (intent.needsReason || intent.manualCompletion) transitionDialog(deal, intent);
        else await send("deal.transition", deal.id, deal.revision, intent.changes, "Перенос «" + deal.title + "» → " + intent.to.name);
      } catch (error) { notice(friendlyError(error), true); }
    });
    win.addEventListener("beforeunload", (event) => {
      if (!queue.unresolved().length) return;
      event.preventDefault();
      event.returnValue = "";
    });
    win.addEventListener("popstate", () => {
      const current = new URL(win.location.href).searchParams;
      state.query = current.get("q") || "";
      const pageNumber = Number(current.get("page"));
      state.page = Number.isInteger(pageNumber) && pageNumber >= 1 && pageNumber <= 10000 ? pageNumber : 1;
      state.owner = current.get("owner") || "all";
      state.attention = ["today", "overdue", "missing"].includes(current.get("attention")) ? current.get("attention") : "all";
      state.view = current.get("view") === "table" ? "table" : "kanban";
      state.pipelineId = current.get("pipeline") || state.pipelineId;
      for (const key of Object.keys(state.filters)) { state.filters[key] = current.get(key) || (key === "outcome" ? "all" : ""); }
      byId("crm-live-search").value = state.query;
      renderOwner();
      const linked = current.get("deal");
      if (linked && /^[1-9][0-9]{0,17}$/.test(linked)) openDetail(linked, false, false);
      else closeDialog(false);
      loadData();
    });
    renderPipelines();
    renderContent();
    syncURL();
    if (bootstrap.initial_notice) notice(bootstrap.initial_notice, true);
    const linkedDeal = new URL(win.location.href).searchParams.get("deal");
    if (linkedDeal && /^[1-9][0-9]{0,17}$/.test(linkedDeal)) openDetail(linkedDeal, false, false);
    if (bootstrap.deferred_list) loadData({keepNotice: true, keepSelection: true});
  }

  return {parseMoney, formatMoney, OperationQueue, transitionIntent, deadlineISO, localDateTime, attentionFor, editableDeadline, conflictDraft, mount};
});
