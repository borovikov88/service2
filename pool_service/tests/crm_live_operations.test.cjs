"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {OperationQueue, transitionIntent, deadlineISO, localDateTime, attentionFor, editableDeadline, conflictDraft} = require("../static/pool_service/crm_live/workspace.js");

const payload = (key = "7b40eeaa-1d41-470c-a470-a218f9db6a20", subject = 41) => ({
  operation_key: key, command_type: "deal.transition", subject_id: subject,
  expected_revision: 3, changes: {stage_id: 9, reason: ""},
});
const success = (id = 41) => ({result: {deal_id: id, event_id: 12, revision: 4}, deal: {id, revision: 4, stage_id: 9}});

test("lost response retains the original command across unrelated activity; retry reuses exact bytes", async () => {
  const calls = [];
  let first = true;
  const queue = new OperationQueue(async (body) => {
    calls.push(JSON.stringify(body));
    if (first) { first = false; throw new TypeError("Network connection lost after request"); }
    return success(body.subject_id);
  });
  const original = payload();
  const operation = queue.add(original, "Перенос сделки");
  await queue.run(original.operation_key);
  assert.equal(operation.status, "uncertain");
  assert.equal(queue.unresolved().length, 1);
  assert.equal(queue.dismiss(original.operation_key), false, "uncertain operation cannot disappear with a closed drawer");
  original.expected_revision = 17;
  original.changes.stage_id = 22;
  const other = queue.add(payload("eabf8586-2f46-4ca1-9fe5-61b1ef4db842", 42), "Другая открытая карточка");
  await queue.run(other.payload.operation_key);
  assert.equal(queue.forSubject(41), operation, "switching to another deal must preserve the first operation");
  await queue.run(operation.payload.operation_key);
  assert.equal(calls[0], calls[2]);
  assert.equal(operation.payload.expected_revision, 3);
  assert.equal(operation.payload.changes.stage_id, 9);
  assert.equal(operation.status, "succeeded");
  assert.equal(queue.unresolved().length, 0);
});

test("double submit sends once and a deal remains locked until an uncertain request resolves", async () => {
  let resolveRequest;
  let requests = 0;
  const queue = new OperationQueue(() => { requests += 1; return new Promise((resolve) => { resolveRequest = resolve; }); });
  const entry = queue.add(payload(), "Сохранение");
  const first = queue.run(entry.payload.operation_key);
  const second = queue.run(entry.payload.operation_key);
  await Promise.resolve();
  assert.equal(requests, 1);
  assert.throws(() => queue.add(payload("2804a86e-5bbe-4e6b-af58-ac39a2376a4e", 41), "Ещё одно изменение"), /предыдущего сохранения/);
  resolveRequest(success());
  await Promise.all([first, second]);
  assert.equal(entry.status, "succeeded");
  assert.equal(queue.forSubject(41), undefined);
});

test("an immediate transport failure is still retryable with the same operation key", async () => {
  let attempts = 0;
  const queue = new OperationQueue(() => {
    if (++attempts === 1) throw new Error("Transport unavailable");
    return Promise.resolve(success());
  });
  const entry = queue.add(payload(), "Сохранение");
  await queue.run(entry.payload.operation_key);
  assert.equal(entry.status, "uncertain");
  await queue.run(entry.payload.operation_key);
  assert.equal(attempts, 2);
  assert.equal(entry.status, "succeeded");
});

test("lost create response followed by lost access stays unresolved until same-key reconciliation succeeds", async () => {
  const calls = [];
  const queue = new OperationQueue(async (body) => {
    calls.push(JSON.stringify(body));
    if (calls.length === 1) throw new TypeError("Response lost after commit");
    if (calls.length === 2) {
      const error = new Error("Access revoked");
      Object.assign(error, {definitive: true, status: 403, code: "forbidden"});
      throw error;
    }
    return success(41);
  });
  const body = {...payload(undefined, null), command_type: "deal.create", expected_revision: null};
  const entry = queue.add(body, "Создание сделки");
  await queue.run(body.operation_key);
  await queue.run(body.operation_key);
  assert.equal(entry.status, "uncertain", "later access rejection does not disprove an earlier commit");
  assert.equal(entry.error.status, 403);
  assert.equal(queue.dismiss(body.operation_key), false);
  assert.equal(queue.forSubject(null), entry, "another create must remain blocked");
  await queue.run(body.operation_key);
  assert.equal(entry.status, "succeeded");
  assert.equal(new Set(calls).size, 1, "reconciliation reuses the same key and payload after access returns");
});

test("revision conflict is definitive: never automatically retry the stale command or invent a new key", async () => {
  let requests = 0;
  const queue = new OperationQueue(async () => {
    requests += 1;
    const error = new Error("Revision changed");
    Object.assign(error, {definitive: true, status: 409, code: "revision_conflict"});
    throw error;
  });
  const entry = queue.add(payload(), "Изменение этапа");
  await queue.run(entry.payload.operation_key);
  await queue.run(entry.payload.operation_key);
  assert.equal(requests, 1);
  assert.equal(entry.status, "failed");
  assert.equal(entry.error.status, 409);
  assert.equal(queue.unresolved().length, 0);
  assert.equal(queue.dismiss(entry.payload.operation_key), true);
});

test("a lost create response blocks another create without blocking existing deals", async () => {
  const queue = new OperationQueue(async () => { throw new TypeError("Network lost"); });
  const body = {...payload(undefined, null), command_type: "deal.create", expected_revision: null, changes: {title: "Test deal", client_id: 1, pipeline_id: 2, next_action: {title: "Call", due_at: "2026-10-27T10:00:00+07:00"}}};
  const entry = queue.add(body, "Новая сделка");
  await queue.run(entry.payload.operation_key);
  assert.throws(() => queue.add({...body, operation_key: "d4ebf2b0-5f7c-4f67-851d-111bf757a1f3"}, "Ещё одна сделка"));
  assert.doesNotThrow(() => queue.add(payload("44c665cb-a636-4573-a445-0ee990de8621", 99), "Другая сделка"));
  assert.equal(queue.forSubject(null), entry);
});

test("drag and stage selector share revision-aware transitions and require a backward reason", () => {
  const pipeline = {id: 1, stages: [{id: 1, position: 0, can_transition: true}, {id: 2, position: 1, can_transition: true}, {id: 3, position: 2, can_transition: true}, {id: 4, position: 3, can_transition: false}]};
  const deal = {id: 41, pipeline_id: 1, stage_id: 2, revision: 7, can_transition: true};
  assert.equal(transitionIntent(deal, "2", pipeline), null);
  const forward = transitionIntent(deal, "3", pipeline);
  assert.equal(forward.expected_revision, 7);
  assert.deepEqual(forward.changes, {stage_id: 3, reason: ""});
  assert.equal(forward.needsReason, false);
  assert.equal(transitionIntent(deal, 1, pipeline).needsReason, true);
  assert.equal(transitionIntent(deal, 1, pipeline, "  Уточнить размеры  ").changes.reason, "Уточнить размеры");
  assert.throws(() => transitionIntent(deal, 4, pipeline), /недоступен/);
  assert.throws(() => transitionIntent({...deal, can_transition: false}, 3, pipeline), /нет права/);
  assert.throws(() => transitionIntent({...deal, pipeline_id: 99}, 3, pipeline), /между направлениями/);
});

test("deadlines always use Barnaul time regardless of the browser's local timezone", () => {
  const iso = deadlineISO("2026-10-27T09:30", "+07:00");
  assert.equal(iso, "2026-10-27T09:30:00+07:00");
  assert.equal(new Date(iso).toISOString(), "2026-10-27T02:30:00.000Z");
  assert.equal(localDateTime("2026-10-27T02:30:00Z", "Asia/Barnaul"), "2026-10-27T09:30");
  assert.throws(() => deadlineISO("", "+07:00"), /дату/);
});

test("private next actions are not presented as missing or overdue; completed actions need a new step", () => {
  const now = Date.parse("2026-10-26T10:00:00Z");
  assert.equal(attentionFor({next_action: {restricted: true}}, now), "all");
  assert.equal(attentionFor({next_action: null}, now), "missing");
  assert.equal(attentionFor({outcome: "open", next_action: null}, now), "missing");
  assert.equal(attentionFor({outcome: "open", next_action: {completed: false, due_at: "2026-10-25T09:00:00Z"}}, now), "overdue");
  assert.equal(attentionFor({outcome: "won", next_action: null}, now), "all");
  assert.equal(attentionFor({next_action: {completed: true, due_at: "2026-10-25T09:00:00Z"}}, now), "missing");
  assert.equal(attentionFor({next_action: {completed: false, due_at: "2026-10-25T09:00:00Z"}}, now), "overdue");
  assert.equal(attentionFor({is_archived: true, next_action: null}, now), "all");
});

test("conflict restoration preserves the user's fields while using the fresh deal and task versions", () => {
  const rejected = {...payload(), command_type: "deal.next_action", expected_revision: 3,
    changes: {title: "My unsaved action", due_at: "2026-10-28T11:30:00+07:00", expected_task_version: "old-task"}};
  const canonical = {id: 41, revision: 4, next_action: {title: "Colleague's action", version: "new-task"}};
  const draft = conflictDraft(rejected, canonical, "Asia/Barnaul");
  assert.deepEqual(draft.fields, {next_title: "My unsaved action", due_at: "2026-10-28T11:30"});
  assert.equal(draft.revision, 4);
  assert.equal(draft.taskVersion, "new-task");
  assert.equal(draft.operation_key, undefined, "restoring a draft is not a command or an automatic replay");
  assert.equal(rejected.expected_revision, 3, "the original rejected payload remains immutable evidence");
  const titleDraft = conflictDraft({...rejected, command_type: "deal.edit", changes: {title: "My typed title"}}, canonical, "Asia/Barnaul");
  assert.equal(titleDraft.fields.title, "My typed title");
  const stageDraft = conflictDraft({...rejected, command_type: "deal.transition", changes: {stage_id: 2, reason: "My backward reason"}}, canonical, "Asia/Barnaul");
  assert.deepEqual(stageDraft.fields, {stage_id: "2", reason: "My backward reason"});
});

test("date-only next steps are overdue only after their Barnaul day, and waiting tasks stay out of overdue", () => {
  const now = Date.parse("2026-10-26T18:30:00Z"); // Already 27 October in Barnaul.
  assert.equal(attentionFor({outcome: "open", next_action: {due_at: null, due_date: "2026-10-27"}}, now), "all");
  assert.equal(attentionFor({outcome: "open", next_action: {due_at: null, due_date: "2026-10-26"}}, now), "overdue");
  assert.equal(attentionFor({outcome: "open", next_action: {waiting: true, due_at: null, due_date: null}}, now), "all");
  assert.equal(editableDeadline({due_at: null, due_date: "2026-10-27"}, "Asia/Barnaul", now), "2026-10-27T09:00");
});

test("commercial card conflict restores every entered field and the current task version", () => {
  const changes = {title: "Card", work_subject: "Install", specialization: "hammam", fulfillment_kind: "construction", object_kind: "pool", pool_id: 7,
    client_object_id: null, planned_object_name: "", planned_object_address: "", amount: "0.00", currency: "RUB", amount_basis: "agreed", expected_decision_at: "2026-10-27T02:30:00Z"};
  const fresh = {id: 41, revision: 9, next_action: {version: "fresh-task-version"}};
  const restored = conflictDraft({subject_id: 41, command_type: "deal.edit", changes}, fresh, "Asia/Barnaul");
  assert.equal(restored.revision, 9);
  assert.equal(restored.taskVersion, "fresh-task-version");
  assert.equal(restored.fields.object_ref, "pool:7");
  assert.equal(restored.fields.work_subject, "Install");
  assert.equal(restored.fields.amount, "0.00");
  assert.equal(restored.fields.expected_decision_at, "2026-10-27T09:30");
  const close = conflictDraft({subject_id: 41, command_type: "deal.close_negative", changes: {outcome: "lost", reason: "Refusal", expected_task_version: "stale-task-version"}}, fresh, "Asia/Barnaul");
  assert.deepEqual(close.fields, {outcome: "lost", reason: "Refusal"});
  assert.equal(close.taskVersion, "fresh-task-version");
});

test("uncertain negative outcome retries the frozen reason and original task version", async () => {
  const sent = [];
  const queue = new OperationQueue(async (body) => { sent.push(JSON.stringify(body)); if (sent.length === 1) throw new TypeError("Network lost"); return {result: {deal_id: 41}, deal: {id: 41}}; });
  const body = {...payload(), command_type: "deal.close_negative", changes: {outcome: "lost", reason: "Original refusal", expected_task_version: "original-task-version"}};
  const entry = queue.add(body, "Refusal");
  await queue.run(entry.payload.operation_key);
  body.changes.reason = "Changed form";
  body.changes.expected_task_version = "changed-version";
  await queue.run(entry.payload.operation_key);
  assert.equal(sent[0], sent[1]);
  assert.equal(entry.status, "succeeded");
});
