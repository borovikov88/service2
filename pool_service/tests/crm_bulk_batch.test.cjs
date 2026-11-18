"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const {runBatch} = require("../static/pool_service/crm_live/bulk.js");
const {LeadOperations} = require("../static/pool_service/crm_live/leads.js");
const {OperationQueue} = require("../static/pool_service/crm_live/workspace.js");

for (const [kind, Queue] of [["lead", LeadOperations], ["deal", OperationQueue]]) {
  const payload = id => ({operation_key: "synthetic-" + id, subject_id: id, expected_revision: 1,
    command_type: kind + (kind === "lead" ? ".disposition" : ".transition"), changes: {stage: 3}});
  const receipt = command => ({operation_key: command.operation_key, status: 200,
    result: {[kind + "_id"]: command.subject_id, revision: 2}, [kind]: {id: command.subject_id, revision: 2}});
  test(kind + " sends one group and keeps individual conflict results", async () => {
    let requests = 0;
    const queue = new Queue(() => { throw new Error("Unexpected single-card request"); });
    const records = [1, 2, 3].map(id => ({payload: payload(id), label: "Synthetic card " + id}));
    const result = await runBatch(queue, records, async body => {
      requests++;
      assert.equal(body.commands.length, 3);
      for (const record of records) assert.equal(queue.forSubject(record.payload.subject_id).status, "sending");
      return {results: body.commands.map(command => command.subject_id === 2
        ? {operation_key: command.operation_key, status: 409, code: "conflict", error: "Synthetic concurrent edit"} : receipt(command))};
    });
    assert.equal(requests, 1);
    assert.deepEqual(result.map(item => item.status), ["succeeded", "failed", "succeeded"]);
    assert.equal(queue.forSubject(2), undefined);
    assert.equal(queue.list().length, 1);
  });
  test(kind + " retains every key after lost group response and reconciles via normal commands", async () => {
    const retried = [], records = [1, 2].map(id => ({payload: payload(id), label: "Synthetic card " + id}));
    const queue = new Queue(async body => { retried.push(JSON.stringify(body)); return receipt(body); });
    const result = await runBatch(queue, records, async () => { throw new TypeError("Synthetic response lost after commit"); });
    assert.deepEqual(result.map(item => item.status), ["uncertain", "uncertain"]);
    for (const item of result) {
      assert.equal(queue.dismiss(item.payload.operation_key), false);
      assert.throws(() => queue.add({...payload(item.payload.subject_id), operation_key: "new-key"}, "Duplicate"));
      await queue.run(item.payload.operation_key);
    }
    assert.deepEqual(retried, records.map(record => JSON.stringify(record.payload)));
    assert.deepEqual(result.map(item => item.status), ["succeeded", "succeeded"]);
  });
  test(kind + " incomplete group receipt cannot claim unconfirmed cards succeeded", async () => {
    const queue = new Queue(async () => {}), records = [1, 2].map(id => ({payload: payload(id), label: "Synthetic"}));
    const result = await runBatch(queue, records, async body => ({results: [receipt(body.commands[0])]}));
    assert.deepEqual(result.map(item => item.status), ["uncertain", "uncertain"]);
  });
}
