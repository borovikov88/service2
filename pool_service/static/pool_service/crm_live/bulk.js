(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.CrmBulk = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const ranks = {unparsed: 0, working: 1, history: 2, failed: 2, spam: 3, deleted: 4};
  const leadStages = [{id: "unparsed", name: "Не разобраны"}, {id: "working", name: "В работе"}, {id: "failed", name: "Лид провален"}, {id: "spam", name: "Спам"}];

  function changesFor(config, action, subject, values) {
    const kind = config.kind;
    if (action === "assign") {
      if (!subject.can_assign) throw new Error("Нет права менять ответственного этой карточки.");
      const responsible = Number(values.responsible_id);
      if (!Number.isInteger(responsible) || responsible <= 0) throw new Error("Выберите ответственного.");
      if ((kind === "lead" ? subject.assignee : subject.manager)?.id === responsible) return null;
      const changes = {responsible_id: responsible, transfer_next_action: Boolean(values.transfer)};
      if (changes.transfer_next_action) {
        if (!subject.assignment_task?.can_transfer) throw new Error("Текущий шаг этой карточки нельзя передать вместе с ней.");
        changes.expected_task_version = subject.assignment_task.version;
      }
      return {command_type: kind + ".assign", changes};
    }
    const target = action === "fail" ? "failed" : values.stage;
    const reason = (values.reason || "").trim();
    if (reason.length > 500) throw new Error("Комментарий должен содержать не больше 500 символов.");
    if (kind === "lead") {
      if (subject.column === target) return null;
      if (!config.moveTargets(subject).includes(target)) throw new Error("Перенос этой карточки в выбранную стадию недоступен.");
      if ((["failed", "spam"].includes(target) || ranks[target] < ranks[subject.column]) && !reason) throw new Error("Укажите причину перемещения.");
      const changes = {column: target, reason};
      if (target === "working" && (!subject.next_action || subject.next_action.completed)) {
        if (!(values.next_title || "").trim()) throw new Error("Укажите следующий шаг для лидов без открытого дела.");
        changes.next_action = {title: values.next_title.trim(), due_at: config.deadlineISO(values.due_at, config.offset)};
      }
      if (["failed", "spam"].includes(target) && subject.next_action?.version && !subject.next_action.completed) changes.expected_task_version = subject.next_action.version;
      return {command_type: "lead.disposition", changes};
    }
    if (target === "failed") {
      if (!subject.can_close_negative) throw new Error("Завершение этой сделки недоступно.");
      if (!reason) throw new Error("Укажите причину провала.");
      return {command_type: "deal.close_negative", changes: {outcome: "lost", reason, expected_task_version: subject.next_action?.version || null}};
    }
    const intent = config.transitionIntent(subject, target, config.pipelineFor(subject.pipeline_id), reason);
    if (!intent) return null;
    if (intent.needsReason) throw new Error("Укажите причину возврата на предыдущий этап.");
    if (intent.manualCompletion) {
      if (!values.confirm) throw new Error("Подтвердите ручное завершение без подтверждения исполнения и оплаты.");
      intent.changes.confirm_manual_completion = true;
    }
    return {command_type: "deal.transition", changes: intent.changes};
  }

  function mount(config) {
    const {root} = config, doc = root.ownerDocument, win = doc.defaultView;
    const selected = new Map();
    let generation = 0, busy = false, running = false, stopped = false, controller = null;
    const node = (tag, cls, text) => { const item = doc.createElement(tag); item.className = cls || ""; if (text != null) item.textContent = text; return item; };
    const button = (text, action, cls = "crm-live-button") => { const item = node("button", cls, text); item.type = "button"; item.addEventListener("click", action); return item; };
    const bar = node("div", "crm-bulk-bar"); bar.hidden = true; bar.setAttribute("role", "region"); bar.setAttribute("aria-label", "Массовые действия");
    const count = node("strong"), scope = node("span", "crm-bulk-scope", "Выбор на текущей доске · до 100 карточек");
    count.setAttribute("aria-live", "polite");
    const all = button("Выбрать все", () => {
      const rows = config.items();
      if (rows.length > 100) { config.notice("Загружено больше 100 карточек. Уточните фильтр или выберите до 100 вручную."); return; }
      for (const row of rows) if (!selected.has(row.id)) selected.set(row.id, {...row});
      refresh();
    });
    all.title = "Выбрать все загруженные карточки по текущим условиям";
    bar.append(count, all, button("Изменить стадию", () => open("stage")), button("Сменить ответственного", () => open("assign")), button("Провалить", () => open("fail"), "crm-live-button crm-bulk-fail"), button("Снять выделение", () => reset()), scope);
    const dialog = node("dialog", "crm-bulk-dialog"); dialog.setAttribute("aria-labelledby", config.kind + "-bulk-title");
    root.append(bar, dialog);

    function refresh() {
      const visible = new Set(config.items().map(item => item.id));
      for (const id of selected.keys()) if (!visible.has(id) && !busy) selected.delete(id);
      bar.hidden = !selected.size;
      root.classList.toggle("has-bulk-selection", selected.size > 0);
      count.textContent = "Выбрано: " + selected.size;
      bar.querySelectorAll("button").forEach(item => { item.disabled = busy || config.loading(); });
      root.querySelectorAll("input[data-bulk-id]").forEach(input => {
        input.checked = selected.has(Number(input.dataset.bulkId));
        input.disabled = busy || config.loading();
        input.closest("article")?.classList.toggle("is-selected", input.checked);
      });
    }
    function checkbox(subject) {
      const label = node("label", "crm-card-select"), input = node("input"); input.type = "checkbox"; input.dataset.bulkId = subject.id;
      input.setAttribute("aria-label", "Выбрать «" + subject.title + "»"); label.title = "Выбрать карточку";
      label.append(input); label.addEventListener("click", event => event.stopPropagation());
      label.addEventListener("dragstart", event => event.preventDefault());
      input.addEventListener("change", () => {
        if (input.checked && selected.size >= 100) config.notice("За один раз можно выбрать до 100 карточек.");
        else if (input.checked) selected.set(subject.id, {...subject});
        else selected.delete(subject.id);
        refresh();
      });
      return label;
    }
    function reset() { if (busy) return; selected.clear(); refresh(); }
    function close() {
      if (running) { stopped = true; return; }
      ++generation; controller?.abort(); busy = false; dialog.close(); refresh();
    }
    dialog.addEventListener("cancel", event => { event.preventDefault(); close(); });
    win.addEventListener("beforeunload", event => { if (running) { event.preventDefault(); event.returnValue = ""; } });
    function field(form, title, name, type = "text", choices = []) {
      const label = node("label", "crm-bulk-field"), caption = node("span", "", title);
      const input = node(type === "select" ? "select" : type === "textarea" ? "textarea" : "input"); input.name = name;
      if (!["select", "textarea"].includes(type)) input.type = type;
      for (const choice of choices) { const option = node("option", "", choice.name); option.value = choice.id; input.append(option); }
      if (type === "checkbox") { label.classList.add("crm-bulk-check"); label.append(input, caption); }
      else label.append(caption, input);
      form.append(label); return {input, label};
    }

    async function open(action) {
      if (busy || config.loading() || !selected.size) return;
      busy = true; stopped = false; refresh();
      const token = ++generation, snapshots = Array.from(selected.values()); controller = new AbortController();
      const title = node("h2", "", action === "assign" ? "Сменить ответственного" : action === "fail" ? "Провалить выбранные карточки" : "Изменить стадию"); title.id = config.kind + "-bulk-title";
      const status = node("p", "crm-bulk-status", "Проверяем выбранные карточки…"); status.setAttribute("role", "status");
      const report = node("ul", "crm-bulk-report"), form = node("form", "crm-bulk-form"), footer = node("div", "crm-bulk-footer");
      const cancel = button("Отмена", () => { if (running) { stopped = true; cancel.disabled = true; status.textContent = "Завершаем текущую карточку…"; } else close(); });
      footer.append(cancel); dialog.replaceChildren(title, status, report, form, footer); dialog.showModal();
      const active = () => token === generation && dialog.open;
      const records = [];
      const reportRow = (snapshot, text) => { const row = node("li", "", snapshot.title + ": " + text); report.append(row); return row; };
      try {
        for (const snapshot of snapshots) {
          if (!active()) return;
          try {
            if (config.pending(snapshot.id)) throw new Error("Сначала проверьте предыдущее сохранение.");
            const subject = await config.detail(snapshot.id, controller.signal);
            if (!active()) return;
            if (subject.revision !== snapshot.revision) throw new Error("Карточка изменилась. Обновите доску и выберите её заново.");
            const allowed = action === "assign" ? subject.can_assign : config.kind === "lead" ? config.moveTargets(subject).length : action === "fail" ? subject.can_close_negative : subject.can_transition;
            if (!allowed) throw new Error("Действие для этой карточки недоступно.");
            let users = [];
            if (action === "assign") users = await config.candidates(subject.id, controller.signal);
            if (!active()) return;
            records.push({snapshot, subject, users});
          } catch (error) { if (!active()) return; reportRow(snapshot, error.message); }
          status.textContent = "Проверено " + (records.length + report.children.length) + " из " + snapshots.length;
        }
        if (!active()) return;
        if (!records.length) throw new Error("Нет карточек, доступных для этого действия.");
        status.textContent = "Будет обработано: " + records.length + " из " + snapshots.length;
        const controls = {};
        if (action === "assign") {
          const users = records[0].users.filter(user => records.every(record => record.users.some(other => other.id === user.id)));
          if (!users.length) throw new Error("Нет сотрудника с доступом ко всем выбранным карточкам. Выберите меньшую группу.");
          controls.responsible_id = field(form, "Новый ответственный", "responsible_id", "select", [{id: "", name: "Выберите сотрудника"}, ...users]).input; controls.responsible_id.required = true;
          form.append(node("p", "crm-live-form-note", "Новый ответственный получит уведомление по каждой переданной карточке. Исполнители существующих задач сохранятся."));
          if (records.every(record => record.subject.assignment_task?.can_transfer)) controls.transfer = field(form, "Также передать текущее следующее действие в каждой карточке", "transfer", "checkbox").input;
        } else {
          const stages = config.kind === "lead" ? leadStages : (config.pipelineFor(records[0].subject.pipeline_id)?.stages || []).filter(stage => stage.can_transition || stage.is_failure && stage.can_close_negative).map(stage => ({...stage, id: stage.is_failure ? "failed" : stage.id}));
          if (action === "stage") { controls.stage = field(form, "Новая стадия", "stage", "select", [{id: "", name: "Выберите стадию"}, ...stages]).input; controls.stage.required = true; }
          const reason = field(form, "Комментарий / причина перемещения", "reason", "textarea"); controls.reason = reason.input; controls.reason.maxLength = 500;
          const next = field(form, "Следующий шаг для лидов без открытого дела", "next_title"), due = field(form, "Срок следующего шага", "due_at", "datetime-local");
          controls.next_title = next.input; controls.due_at = due.input;
          const confirm = field(form, "Подтверждаю ручное завершение без подтверждения исполнения и оплаты", "confirm", "checkbox"); controls.confirm = confirm.input;
          const warning = node("p", "crm-live-form-note"); form.append(warning);
          const update = () => {
            const target = action === "fail" ? "failed" : controls.stage.value;
            const stage = stages.find(item => String(item.id) === target);
            const failure = ["failed", "spam"].includes(target), manual = Boolean(stage?.requires_manual_confirmation);
            const needsNext = config.kind === "lead" && target === "working" && records.some(({subject}) => !subject.next_action || subject.next_action.completed);
            next.label.hidden = due.label.hidden = !needsNext; next.input.required = due.input.required = needsNext;
            confirm.label.hidden = !manual; confirm.input.required = manual; confirm.input.checked = false;
            reason.input.required = failure || records.some(({subject}) => config.kind === "lead" ? ranks[target] < ranks[subject.column] : stage && stage.position < (config.pipelineFor(subject.pipeline_id)?.stages.find(item => item.id === subject.stage_id)?.position ?? 0));
            warning.textContent = failure ? "Открытый следующий шаг будет отменён с сохранением истории." : manual ? "Исполнение и оплата не подтверждаются. Документы и открытые задачи сохранятся." : config.kind === "lead" ? "Свободные лиды при первом перемещении закрепятся за вами." : "Изменится этап работы менеджера. Данные 1С сохранятся.";
          };
          controls.stage?.addEventListener("change", update); update();
        }
        const apply = node("button", "crm-live-button crm-live-button-primary", "Применить к " + records.length); apply.type = "submit"; form.append(apply);
        form.addEventListener("submit", async event => {
          event.preventDefault(); if (running || !form.reportValidity()) return;
          const values = Object.fromEntries(Object.entries(controls).map(([key, input]) => [key, input.type === "checkbox" ? input.checked : input.value]));
          // Capture every immutable payload before the first write. Each server
          // command still validates current rights and the selected revision.
          const prepared = records.map(record => {
            try { return {...record, command: changesFor(config, action, record.subject, values)}; }
            catch (error) { return {...record, error}; }
          });
          running = true; cancel.textContent = "Остановить"; form.querySelectorAll("input,select,textarea,button").forEach(item => { item.disabled = true; });
          let saved = 0, unchanged = 0, failed = snapshots.length - records.length, uncertain = 0, remaining = 0;
          for (const record of prepared) {
            if (stopped) { remaining++; continue; }
            if (record.error) { failed++; reportRow(record.snapshot, record.error.message); continue; }
            if (!record.command) { unchanged++; selected.delete(record.subject.id); reportRow(record.snapshot, "уже соответствует выбранному значению"); continue; }
            status.textContent = "Сохраняем: " + record.snapshot.title;
            try {
              const payload = {operation_key: win.crypto.randomUUID(), ...record.command, subject_id: record.subject.id, expected_revision: record.subject.revision};
              const result = await config.run(payload, record.snapshot.title);
              if (result.status === "succeeded") { saved++; selected.delete(record.subject.id); reportRow(record.snapshot, "сохранено"); }
              else if (result.status === "uncertain") { uncertain++; stopped = true; reportRow(record.snapshot, "результат неизвестен — используйте «Проверить результат» на доске"); }
              else { failed++; reportRow(record.snapshot, result.error?.message || "не сохранено"); }
            } catch (error) { failed++; reportRow(record.snapshot, error.message); }
          }
          running = false; busy = false; cancel.disabled = false; cancel.textContent = "Закрыть"; form.hidden = true;
          const message = `Сохранено: ${saved}. Без изменений: ${unchanged}. Не выполнено: ${failed}.` + (uncertain ? ` Требует проверки: ${uncertain}.` : "") + (remaining ? ` Не начато: ${remaining}.` : "");
          status.textContent = message; config.notice(message); refresh();
          await config.reload();
        });
      } catch (error) { if (active()) { status.textContent = error.message; cancel.textContent = "Закрыть"; } }
    }
    return {checkbox, refresh, reset, hasSelection: () => selected.size > 0, remove: id => { selected.delete(id); refresh(); }};
  }
  return {mount, changesFor};
});
