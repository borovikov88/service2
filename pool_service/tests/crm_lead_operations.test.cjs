"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {LeadOperations, deadlineISO, localDateTime} = require("../static/pool_service/crm_live/leads.js");
const payload = () => ({operation_key: "synthetic-operation", command_type: "lead.qualify", subject_id: 12, expected_revision: 3,
  changes: {deals: [{title: "Synthetic deal", pipeline_id: 1, next_action: {title: "Call", due_at: "2026-10-27T12:00:00+07:00"}}]}});

test("lost qualification response retries its frozen payload, not the current form", async () => {
  const sent = []; let first = true;
  const queue = new LeadOperations(async (body) => { sent.push(JSON.stringify(body)); if (first) { first = false; throw new Error("response lost after commit"); } return {lead: {id: 12}}; });
  const input = payload(); const item = queue.add(input, "Synthetic lead"); input.changes.deals[0].title = "Edited form";
  await queue.run(item.payload.operation_key); assert.equal(item.status, "uncertain");
  assert.throws(() => queue.add({...payload(), operation_key: "different-operation"}, "Duplicate"));
  assert.equal(queue.dismiss(item.payload.operation_key), false);
  await queue.run(item.payload.operation_key); assert.equal(item.status, "succeeded"); assert.equal(sent[0], sent[1]); assert.match(sent[1], /Synthetic deal/);
});

test("a later denial cannot prove an earlier uncertain request rolled back", async () => {
  let count = 0; const queue = new LeadOperations(async () => { count += 1; const error = new Error("denied"); if (count > 1) error.definitive = true; throw error; });
  const item = queue.add(payload(), "Synthetic lead"); await queue.run(item.payload.operation_key); await queue.run(item.payload.operation_key);
  assert.equal(item.status, "uncertain"); assert.equal(queue.pending().length, 1); assert.equal(queue.dismiss(item.payload.operation_key), false);
});

test("double click shares the in-flight request and independent cards stay independent", async () => {
  let calls = 0, finish; const queue = new LeadOperations(() => { calls += 1; return new Promise((resolve) => { finish = resolve; }); });
  const item = queue.add(payload(), "Synthetic lead"); const first = queue.run(item.payload.operation_key), second = queue.run(item.payload.operation_key);
  await Promise.resolve(); assert.equal(calls, 1);
  queue.add({...payload(), operation_key: "other-card", subject_id: 13}, "Other card");
  finish({lead: {id: 12}}); await Promise.all([first, second]); assert.equal(item.status, "succeeded"); assert.equal(queue.pending().length, 1);
});

test("a definitive initial conflict can be dismissed without automatic resubmission", async () => {
  const queue = new LeadOperations(async () => { const error = new Error("stale revision"); error.definitive = true; error.code = "conflict"; throw error; });
  const item = queue.add(payload(), "Synthetic lead"); await queue.run(item.payload.operation_key); assert.equal(item.status, "failed"); assert.equal(queue.pending().length, 0); assert.equal(queue.dismiss(item.payload.operation_key), true);
});

test("manager-entered dates use the displayed organization offset and reject nonexistent dates", () => {
  assert.equal(deadlineISO("2026-10-27T12:00", "+07:00"), "2026-10-27T12:00:00+07:00");
  assert.equal(localDateTime("2026-10-27T05:00:00Z", "Asia/Barnaul"), "2026-10-27T12:00");
  assert.throws(() => deadlineISO("2026-02-31T12:00", "+07:00")); assert.throws(() => deadlineISO("", "+07:00"));
});


test("move options preserve final results and offer explicit archived restoration", () => {
  const {moveTargets} = require("../static/pool_service/crm_live/leads.js");
  assert.deepEqual(moveTargets({can_move:false}), []);
  assert.deepEqual(moveTargets({can_move:true, is_archived:true, status:"closed", result:"spam"}), ["restore", "spam"]);
  assert.deepEqual(moveTargets({can_move:true, status:"closed", result:"qualified", needs_review:true}), ["deleted"]);
  assert.deepEqual(moveTargets({can_move:true, status:"working", column:"working"}), ["unparsed", "spam", "deleted"]);
});
