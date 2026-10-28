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
    async run(operationKey) {
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
          entry.response = await this.transport(entry.payload);
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
    return {subject_id: deal.id, expected_revision: deal.revision, changes: {stage_id: to.id, reason: String(reason || "").trim()}, backwards, needsReason: backwards && !String(reason || "").trim(), from, to};
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
      draft.formKind = "edit";
      draft.fields = {...changes};
      if (Object.prototype.hasOwnProperty.call(changes, "expected_decision_at")) draft.fields.expected_decision_at = localDateTime(changes.expected_decision_at, timezone);
      if (changes.object_kind) draft.fields.object_ref = changes.object_kind === "pool" ? "pool:" + changes.pool_id : changes.object_kind === "client_object" ? "client_object:" + changes.client_object_id : changes.object_kind;
    } else if (payload.command_type === "deal.next_action") {
      draft.formKind = "next_action";
      draft.fields.next_title = changes.title;
      draft.fields.due_at = localDateTime(changes.due_at, timezone);
    } else if (payload.command_type === "deal.close_negative") {
      draft.formKind = "close_negative";
      draft.fields.outcome = changes.outcome;
      draft.fields.reason = changes.reason;
    } else if (payload.command_type === "deal.transition") {
      draft.formKind = "transition";
      draft.fields.stage_id = String(changes.stage_id);
      draft.fields.reason = changes.reason || "";
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
      for (const key of ["q", "owner", "attention", "outcome", "object", "stage", "specialization", "decision_from", "decision_to"]) {
        params.set(key, bootstrap[key] == null ? "" : String(bootstrap[key]));
      }
    }
    const state = {
      data: bootstrap, pipelineId: bootstrap.selected_pipeline_id, page: bootstrap.page || 1,
      filters: Object.fromEntries(["outcome", "stage", "object", "specialization", "decision_from", "decision_to"].map((key) => [key, params.get(key) || (key === "outcome" ? "open" : "")])),
      query: params.get("q") || "", owner: params.get("owner") === "me" ? "me" : "all",
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
    async function requestJSON(url, options, externalSignal) {
      const controller = new AbortController();
      const abort = () => controller.abort();
      if (externalSignal) {
        if (externalSignal.aborted) controller.abort();
        else externalSignal.addEventListener("abort", abort, {once: true});
      }
      const timer = win.setTimeout(abort, 20000);
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
      if (!response.result || !response.deal || !sameId(response.result.deal_id, response.deal.id)) throw new Error("Сервер не подтвердил результат сохранения.");
      return response;
    }, () => { renderOperations(); renderContent(); updateFormLocks(); });

    function syncURL() {
      const url = new URL(win.location.href);
      url.searchParams.set("org_id", String(bootstrap.organization_id));
      if (state.pipelineId != null) url.searchParams.set("pipeline", state.pipelineId);
      else url.searchParams.delete("pipeline");
      for (const [key, value, defaultValue] of [["q", state.query, ""], ["owner", state.owner, "all"], ["view", state.view, "kanban"], ["attention", state.attention, "all"]]) {
        if (value === defaultValue) url.searchParams.delete(key);
        else url.searchParams.set(key, value);
      }
      if (state.page > 1) url.searchParams.set("page", state.page); else url.searchParams.delete("page");
      for (const [key, value] of Object.entries(state.filters)) {
        if (value && !(key === "outcome" && value === "open")) url.searchParams.set(key, value);
        else url.searchParams.delete(key);
      }
      win.history.replaceState({}, "", url);
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
            if (entry.error?.status === 409) {
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
      const stages = byId("crm-live-stage");
      stages.replaceChildren(element("option", "", "Все этапы"));
      stages.firstChild.value = "";
      for (const stage of pipelineFor(state.pipelineId)?.stages || []) {
        const option = element("option", "", stage.name); option.value = String(stage.id); stages.append(option);
      }
      stages.value = state.filters.stage;
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
      card.draggable = !table && Boolean(deal.can_transition) && !deal.is_archived && !pending;
      const main = element("div", "crm-live-card-main");
      const title = element("h3");
      const open = makeButton(deal.title, "open", "crm-live-card-open");
      open.dataset.dealId = deal.id;
      title.append(open);
      main.append(title, element("p", "crm-live-card-client", deal.client?.name || "Клиент не указан"));
      const object = deal.object;
      if (object) main.append(element("p", "crm-live-card-stage", object.restricted ? "Объект недоступен" : [object.name, object.address].filter(Boolean).join(" · ")));
      main.append(element("p", "crm-live-card-stage", (deal.amount === null ? "Сумма неизвестна" : deal.amount + " " + deal.currency) + (deal.specialization ? " · " + deal.specialization : "")));
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
      for (const kind of ["all", "today", "overdue", "missing"]) {
        root.querySelector(`[data-attention="${kind}"]`).setAttribute("aria-pressed", String(state.attention === kind));
      }
      byId("crm-live-result-status").textContent = state.loading ? "Обновляем…" : "На странице: " + filtered.length + (state.data.attention_total == null ? "" : " · по фильтру: " + state.data.attention_total);
      byId("crm-live-limit").hidden = !state.data.has_more;
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
        const hasFilters = state.query || state.owner === "me" || state.attention !== "all" || Object.entries(state.filters).some(([key, value]) => value && !(key === "outcome" && value === "open"));
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
          const deals = filtered.filter((deal) => sameId(deal.stage_id, stage.id));
          const lane = element("section", "crm-live-lane");
          lane.dataset.stageId = stage.id;
          lane.dataset.canTransition = String(Boolean(stage.can_transition));
          const heading = element("h2");
          const totals = (state.data.stage_totals || []).filter(row => sameId(row.stage_id, stage.id));
          const count = totals.reduce((sum, row) => sum + row.count, 0);
          heading.append(element("span", "", stage.name), element("span", "crm-live-lane-count", count));
          lane.append(heading);
          const known = totals.filter(row => row.amount !== null).map(row => row.amount + " " + (row.currency === "RUB" ? "₽" : row.currency));
          const unknown = totals.reduce((sum, row) => sum + row.unknown_count, 0);
          lane.append(element("p", "crm-live-stage-total", (known.length ? known.join(" + ") : unknown ? "Сумма неизвестна" : "0 ₽") + (unknown ? " · без суммы: " + unknown : "")));
          if (!stage.can_transition) lane.append(element("p", "crm-live-lane-unavailable", "Перенос в этот этап пока недоступен"));
          if (!deals.length) lane.append(element("p", "crm-live-lane-empty", count > 0 ? "На этой странице нет сделок этого этапа" : stage.can_transition ? "Нет сделок на этом этапе" : ""));
          else deals.forEach((deal) => lane.append(renderCard(deal, false)));
          board.append(lane);
        }
        container.append(board);
        board.scrollLeft = scrollLeft;
      }
    }

    async function loadData(options) {
      const generation = ++state.dataGeneration;
      if (state.dataController) state.dataController.abort();
      const controller = new AbortController();
      state.dataController = controller;
      state.loading = true; state.dataError = null;
      renderContent();
      const url = scopedURL(bootstrap.urls.data);
      if (state.pipelineId != null) url.searchParams.set("pipeline", state.pipelineId);
      url.searchParams.set("page", state.page);
      url.searchParams.set("q", state.query);
      url.searchParams.set("owner", state.owner);
      url.searchParams.set("attention", state.attention);
      for (const [key, value] of Object.entries(state.filters)) if (value) url.searchParams.set(key, value);
      syncURL();
      try {
        const response = await requestJSON(url, {headers: {"Accept": "application/json"}}, controller.signal);
        if (generation !== state.dataGeneration) return;
        if (!Array.isArray(response.deals) || !Array.isArray(response.pipelines)) throw new Error("Сервер вернул неполные данные CRM.");
        state.data = response;
        state.pipelineId = response.selected_pipeline_id;
        state.page = response.page || 1;
        state.attention = response.attention || "all";
        renderPipelines();
        syncURL();
        if (!options?.keepNotice) notice("");
      } catch (error) {
        if (controller.signal.aborted || generation !== state.dataGeneration) return;
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
    function closeDialog() {
      if (state.detailController) state.detailController.abort();
      if (state.clientController) state.clientController.abort();
      state.detailGeneration += 1;
      state.clientGeneration += 1;
      state.drawer = null;
      dialog.close();
    }
    function titleNode(text) { const node = element("h2", "", text); node.id = "crm-live-dialog-title"; return node; }
    function section(title) { const node = element("section", "crm-live-section"); node.append(element("h3", "", title)); return node; }
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
      return form;
    }
    function formError(form, message) {
      const error = form.querySelector(".crm-live-form-error");
      if (!error) return;
      error.textContent = message || "";
      error.hidden = !message;
    }
    function updateFormLocks() {
      for (const form of dialogContent.querySelectorAll("[data-command-form]")) {
        const subjectId = form.dataset.subjectId || null;
        const busy = Boolean(queue.forSubject(subjectId)) || (state.drawer?.kind === "loading" && sameId(state.drawer.id, subjectId));
        for (const input of form.querySelectorAll("input, select, textarea, button")) input.disabled = busy;
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
      return ({open: "Открыта", lost: "Отказ / проигрыш", cancelled: "Отменена", won: "Успех", unknown: "Legacy: не определён"})[value] || value;
    }

    function selectField(form, label, name, selected, choices) {
      const item = field(label, "select", name, null, false);
      for (const [value, text] of choices) {
        const option = element("option", "", text); option.value = value; option.selected = value === selected; item.input.append(option);
      }
      form.append(item.wrapper); return item;
    }

    function cardFields(form, deal) {
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
      const amount = field(legacyCurrency ? "Историческая сумма · " + deal.currency : "Коммерческая сумма, ₽ (пусто = неизвестна)", "text", "amount", deal?.amount ?? "", false);
      amount.input.inputMode = "decimal"; amount.input.maxLength = 19; amount.input.pattern = "[0-9]{1,16}([.][0-9]{1,2})?";
      const currency = document.createElement("input"); currency.type = "hidden"; currency.name = "currency"; currency.value = legacyCurrency ? deal.currency : "RUB";
      form.append(amount.wrapper, currency);
      const basis = selectField(form, "Основание суммы", "amount_basis", deal?.amount_basis || "unknown", [["unknown", "Неизвестна"], ["indicative", "Предварительная оценка"], ["agreed", "Согласовано с клиентом"]]);
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
      const amount = String(values.get("amount") || "").trim();
      const expected = String(values.get("expected_decision_at") || "");
      return {work_subject: String(values.get("work_subject") || "").trim(), specialization: String(values.get("specialization") || "").trim(),
        fulfillment_kind: String(values.get("fulfillment_kind") || "unknown"), object_kind: kind,
        pool_id: kind === "pool" ? Number(id) : null, client_object_id: kind === "client_object" ? Number(id) : null,
        planned_object_name: kind === "planned" ? String(values.get("planned_object_name") || "").trim() : "",
        planned_object_address: kind === "planned" ? String(values.get("planned_object_address") || "").trim() : "",
        amount: amount || null, currency: String(values.get("currency") || "").trim(), amount_basis: amount ? String(values.get("amount_basis")) : "unknown",
        expected_decision_at: expected ? deadlineISO(expected, offset) : null};
    }

    function renderDetail(deal, events) {
      dialogContent.replaceChildren();
      state.drawer = {kind: "detail", id: deal.id, deal};
      dialogContent.dataset.revision = String(deal.revision);
      dialogContent.append(titleNode(deal.title));
      const client = element("p", "crm-live-detail-client");
      const href = safeLink(deal.client?.url);
      if (href) { const link = element("a", "", deal.client.name); link.href = href; client.append(link); }
      else client.textContent = deal.client?.name || "Клиент не указан";
      dialogContent.append(client);
      const meta = element("div", "crm-live-detail-meta");
      meta.append(element("span", "crm-live-pill", pipelineFor(deal.pipeline_id)?.name || ""), element("span", "crm-live-pill", stageFor(deal)?.name || ""), element("span", "crm-live-pill", deal.manager?.name || "Без ответственного"));
      dialogContent.append(meta);
      const overview = section("Коммерческие сведения");
      const object = deal.object;
      const facts = [["Предмет", deal.work_subject || "Не уточнён"], ["Специализация", deal.specialization || "Не уточнена"],
        ["Исполнение", ({unknown: "Не уточнено", construction: "Строительство / монтаж", service: "Обслуживание", retail: "Поставка", mixed: "Смешанное"})[deal.fulfillment_kind]],
        ["Объект", !object ? "Не выбран" : object.restricted ? "Недоступен" : (object.kind === "planned" ? "Планируемый: " : "") + [object.name, object.address].filter(Boolean).join(" · ")],
        ["Сумма", deal.amount === null ? "Неизвестна" : deal.amount + " " + deal.currency + " · " + (deal.amount_basis === "agreed" ? "Согласовано" : "Предварительная оценка")],
        ["Ожидаемое решение", deal.expected_decision_at ? dateText(deal.expected_decision_at) : "Не задано"], ["Исход", outcomeLabel(deal.outcome)]];
      for (const [label, value] of facts) overview.append(element("p", "crm-live-fact", label + ": " + (value || "Не уточнено")));
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
      if (deal.can_transition && !deal.is_archived) {
        const part = section("Этап сделки");
        const form = newForm("transition", deal.id);
        const stages = field("Перевести на этап", "select", "stage_id", null, true);
        for (const stage of pipelineFor(deal.pipeline_id)?.stages || []) {
          if (!stage.can_transition && !sameId(stage.id, deal.stage_id)) continue;
          const option = element("option", "", stage.name);
          option.value = stage.id;
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
        const form = newForm("next_action", deal.id);
        nextFields(form, deal.next_action);
        formFooter(form, deal.next_action && !deal.next_action.completed ? "Сохранить следующий шаг" : "Назначить следующий шаг");
        part.append(form);
        dialogContent.append(part);
      }
      if (deal.can_edit && !deal.is_archived) {
        const part = section("Изменить карточку сделки");
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
        const part = section("Отказ или отмена");
        const form = newForm("close_negative", deal.id);
        form.append(element("p", "crm-live-form-note", "Сделка завершится, а её активный следующий шаг будет отменён. Выполненным он не станет."));
        selectField(form, "Исход", "outcome", "lost", [["lost", "Отказ / проигрыш"], ["cancelled", "Отмена"]]);
        const reason = field("Причина", "textarea", "reason", "", true); reason.input.maxLength = 500;
        form.append(reason.wrapper); formFooter(form, "Зафиксировать исход"); part.append(form); dialogContent.append(part);
      }
      const history = section("История изменений");
      if (!events?.length) history.append(element("p", "crm-live-muted", "Изменения сделки будут отображаться здесь."));
      else {
        const list = element("ol", "crm-live-history");
        events.forEach((event) => {
          const item = element("li", "", event.label || "Изменение сделки");
          const actorName = typeof event.actor === "string" ? event.actor : event.actor?.name;
          item.append(element("small", "", dateText(event.at) + (actorName ? " · " + actorName : "")));
          if (event.reason) item.append(element("p", "", event.reason));
          list.append(item);
        });
        history.append(list);
      }
      dialogContent.append(history);
      updateFormLocks();
    }

    async function openDetail(id, keepOpen) {
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
        state.drawer = {kind: "error", id};
        const retry = makeButton("Повторить загрузку", "open");
        retry.dataset.dealId = id;
        dialogContent.replaceChildren(titleNode("Карточка недоступна"), element("p", "crm-live-form-error", friendlyError(error)), retry);
      }
    }

    function createDialog() {
      if (!state.data.capabilities?.create || queue.forSubject(null)) return;
      if (state.detailController) state.detailController.abort();
      state.detailGeneration += 1;
      state.drawer = {kind: "create", selectedClient: null};
      dialogContent.replaceChildren(titleNode("Новая сделка"), element("p", "crm-live-form-note", "Выберите клиента и договоритесь о следующем шаге. Ответственным станете вы."));
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
      cardFields(form, null);
      nextFields(form, null);
      formFooter(form, "Создать сделку");
      dialogContent.append(form);
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
      dialogContent.replaceChildren(titleNode("Вернуть на предыдущий этап"), element("p", "crm-live-detail-client", deal.title), element("p", "crm-live-form-note", intent.from.name + " → " + intent.to.name));
      const form = newForm("transition-confirm", deal.id);
      const reason = field("Причина возврата", "textarea", "reason", "", true);
      reason.input.maxLength = 2000;
      reason.input.placeholder = "Что нужно уточнить или исправить";
      form.append(reason.wrapper);
      formFooter(form, "Вернуть сделку");
      dialogContent.append(form);
      openDialog("Изменение этапа");
      reason.input.focus();
      updateFormLocks();
    }

    async function performExisting(key) {
      const entry = await queue.run(key);
      if (entry.status === "succeeded") {
        const deal = entry.response.deal;
        if (entry.payload.command_type === "deal.create") {
          state.pipelineId = deal.pipeline_id;
          state.page = 1; state.filters.outcome = "open"; state.filters.stage = "";
          byId("crm-live-outcome").value = "open";
          state.query = "";
          state.attention = "all";
          byId("crm-live-search").value = "";
          renderPipelines();
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
        if (draft.formKind === "transition") {
          const select = form.elements.namedItem("stage_id");
          if (!Array.from(select.options).some((option) => option.value === draft.fields.stage_id)) throw new Error("Выбранный ранее этап больше недоступен. Проверьте текущий этап; черновик сохранён в уведомлении.");
          select.value = draft.fields.stage_id;
          select.dispatchEvent(new Event("change", {bubbles: true}));
        }
        for (const [name, value] of Object.entries(draft.fields)) {
          const input = form.elements.namedItem(name);
          if (input) {
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
          if (!state.drawer?.selectedClient) throw new Error("Выберите клиента из результатов поиска.");
          const changes = {title: String(values.get("title") || "").trim(), client_id: state.drawer.selectedClient.id, pipeline_id: Number(values.get("pipeline_id")), next_action: {title: String(values.get("next_title") || "").trim(), due_at: deadlineISO(values.get("due_at"), offset)}};
          changes.card_fields = cardChanges(values);
          if (!changes.title || !changes.next_action.title) throw new Error("Заполните название сделки и следующий шаг.");
          await send("deal.create", null, null, changes, "Новая сделка «" + changes.title + "»");
        } else if (!deal) throw new Error("Обновите карточку перед сохранением.");
        else if (kind === "edit") {
          const title = String(values.get("title") || "").trim();
          if (!title) throw new Error("Укажите название сделки.");
          await send("deal.edit", deal.id, deal.revision, {title, ...cardChanges(values)}, "Карточка сделки «" + deal.title + "»");
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
          const stageId = kind === "transition-confirm" ? state.drawer.intent.to.id : values.get("stage_id");
          const intent = transitionIntent(deal, stageId, pipelineFor(deal.pipeline_id), values.get("reason"));
          if (!intent) { formError(form, "Сделка уже находится на выбранном этапе."); return; }
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
        renderPipelines(); renderContent(); syncURL();
      } else if (button.dataset.attention) {
        state.attention = button.dataset.attention; state.page = 1;
        loadData();
      } else if (button.dataset.action === "page-prev" || button.dataset.action === "page-next") {
        state.page = Math.max(1, state.page + (button.dataset.action === "page-next" ? 1 : -1)); loadData();
      } else if (button.dataset.action === "open") {
        if (Date.now() >= state.suppressClickUntil) openDetail(button.dataset.dealId);
      } else if (button.dataset.action === "create") createDialog();
      else if (button.dataset.action === "close") closeDialog();
      else if (button.dataset.action === "retry") performExisting(button.dataset.operationKey);
      else if (button.dataset.action === "restore-conflict") restoreConflict(button.dataset.operationKey);
      else if (button.dataset.action === "dismiss-operation") queue.dismiss(button.dataset.operationKey);
      else if (button.dataset.action === "reset") {
        state.query = ""; state.owner = "all"; state.attention = "all"; state.page = 1;
        for (const key of Object.keys(state.filters)) { state.filters[key] = key === "outcome" ? "open" : ""; byId("crm-live-" + key).value = state.filters[key]; }
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
      win.clearTimeout(searchTimer);
      if (state.dataController) state.dataController.abort();
      searchTimer = win.setTimeout(loadData, 300);
    });
    byId("crm-live-owner").value = state.owner;
    byId("crm-live-owner").addEventListener("change", (event) => { state.owner = event.target.value; state.page = 1; loadData(); });
    for (const key of Object.keys(state.filters)) {
      const input = byId("crm-live-" + key); input.value = state.filters[key];
      input.addEventListener(key === "specialization" ? "input" : "change", () => {
        state.filters[key] = input.value; state.page = 1;
        win.clearTimeout(searchTimer); if (state.dataController) state.dataController.abort();
        searchTimer = win.setTimeout(loadData, key === "specialization" ? 300 : 0);
      });
    }
    byId("crm-live-refresh").addEventListener("click", () => loadData());

    root.addEventListener("dragstart", (event) => {
      const card = event.target.closest(".crm-live-card[draggable=true]");
      if (!card || !event.dataTransfer) return;
      const deal = dealFor(card.dataset.dealId);
      if (!deal || queue.forSubject(deal.id)) { event.preventDefault(); return; }
      state.dragging = {id: deal.id, revision: deal.revision};
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("application/x-service2-crm-deal", String(deal.id));
      card.classList.add("is-dragging");
    });
    root.addEventListener("dragover", (event) => {
      const lane = event.target.closest(".crm-live-lane");
      if (!lane || !state.dragging || lane.dataset.canTransition !== "true") return;
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
      state.suppressClickUntil = Date.now() + 250;
    }
    root.addEventListener("dragend", endDrag);
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
        const intent = transitionIntent(deal, lane.dataset.stageId, pipelineFor(deal.pipeline_id));
        if (!intent) return;
        if (intent.needsReason) transitionDialog(deal, intent);
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
      state.owner = current.get("owner") === "me" ? "me" : "all";
      state.attention = ["today", "overdue", "missing"].includes(current.get("attention")) ? current.get("attention") : "all";
      state.view = current.get("view") === "table" ? "table" : "kanban";
      state.pipelineId = current.get("pipeline") || state.pipelineId;
      for (const key of Object.keys(state.filters)) { state.filters[key] = current.get(key) || (key === "outcome" ? "open" : ""); byId("crm-live-" + key).value = state.filters[key]; }
      byId("crm-live-search").value = state.query;
      byId("crm-live-owner").value = state.owner;
      loadData();
    });
    renderPipelines();
    renderContent();
    syncURL();
    if (bootstrap.initial_notice) notice(bootstrap.initial_notice, true);
    const linkedDeal = new URL(win.location.href).searchParams.get("deal");
    if (linkedDeal && /^[1-9][0-9]{0,17}$/.test(linkedDeal)) openDetail(linkedDeal);
  }

  return {OperationQueue, transitionIntent, deadlineISO, localDateTime, attentionFor, editableDeadline, conflictDraft, mount};
});
