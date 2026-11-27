// Contract test for the isolated host bridge; no browser or business writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.resolve(__dirname, '../../templates/onec_drafts/chat_identity_widget.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const events = {}, clicks = {}, nodes = {status: {}, confirm: {addEventListener: (name, fn) => {clicks[name] = fn;}}};
const calls = [];
let response;
const window = {addEventListener: (name, fn) => {events[name] = fn;}, openai: {
  toolOutput: {status: 'identity_required'},
  toolResponseMetadata: {confirmation_url: 'https://service2.example/onec/drafts/identity/signed-old/'},
  openExternal: async options => {calls.push(options);},
  callTool: async (name, args) => {assert.equal(name, 'get_chat_identity');assert.deepEqual(Object.keys(args), []);return response;},
}};
vm.runInNewContext(source, {window, document: {
  getElementById: name => nodes[name], addEventListener: (name, fn) => {events[name] = fn;}, hidden: false,
}});
(async () => {
  assert.equal(nodes.confirm.hidden, false);
  await clicks.click();
  assert.equal(calls[0].href, window.openai.toolResponseMetadata.confirmation_url);
  assert.equal(Object.keys(calls[0]).length, 1); // Host adds its own return URL.
  response = {structuredContent: {status: 'identity_required'}, _meta: {confirmation_url: 'https://service2.example/onec/drafts/identity/signed-new/'}};
  await events.focus();
  await clicks.click();
  assert.equal(calls[1].href, response._meta.confirmation_url);
  response = {structuredContent: {status: 'confirmed', employee_name: 'Сотрудник А'}};
  await events.focus();
  assert.equal(nodes.confirm.hidden, true);
  assert.equal(nodes.status.textContent, 'Сотрудник: Сотрудник А');
  assert.equal(calls.length, 2);
  console.log('Identity widget host contract passed');
})().catch(error => {console.error(error);process.exitCode = 1;});
