"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.resolve(__dirname, "../static/assets/js/crm-client-picker.js"), "utf8");

function node() {
  const listeners = new Map(), attrs = new Map();
  return {dataset: {}, value: "", hidden: true, children: [], id: "", textContent: "",
    addEventListener(name, fn) {if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(fn);},
    fire(name, properties = {}) {const event = {target: this, prevented: false, preventDefault(){this.prevented = true;}, ...properties}; for (const fn of listeners.get(name) || []) fn(event); return event;},
    setCustomValidity(message) {this.message = message;}, reportValidity() {}, focus() {}, scrollIntoView() {},
    setAttribute(key, value) {attrs.set(key, value);}, removeAttribute(key) {attrs.delete(key);},
    getAttribute(key) {return attrs.get(key);}, replaceChildren() {this.children = [];}, append(item) {this.children.push(item);},
    classList: {toggle() {}},
  };
}
function page({required = false, id = "", name = ""} = {}) {
  const root = node(), input = node(), selected = node(), results = node(), status = node(), clear = node(), form = node(), doc = node();
  root.dataset = {lookupUrl: "/communications/client-lookup/telephony/", lookupRequired: required ? "true" : "false"};
  input.form = form; input.id = "test-lookup"; input.value = name; selected.value = id;
  root.querySelector = value => ({"[data-lookup-input]": input, "[data-lookup-value]": selected, "[data-lookup-results]": results, "[data-lookup-status]": status, "[data-lookup-clear]": clear})[value];
  root.contains = value => [root, input, selected, clear, results].includes(value) || results.children.includes(value);
  doc.readyState = "loading"; doc.querySelectorAll = () => []; doc.createElement = node;
  const timers = new Map(), requests = []; let next = 0;
  const context = {document: doc, module: {exports: {}}, URL, AbortController,
    window: {location: {href: "https://example.test/communications/calls/"}, setTimeout(fn) {timers.set(++next, fn); return next;}, clearTimeout(id) {timers.delete(id);}},
    fetch(url, options) {let resolve; const promise = new Promise(yes => {resolve = yes;}); requests.push({url, options, resolve}); return promise;},
  };
  vm.runInNewContext(source, context);
  context.module.exports.initPicker(root);
  return {root, input, selected, results, status, clear, form, requests,
    init() {context.module.exports.initPicker(root);},
    type(value) {input.value = value; input.fire("input");},
    flush() {const all = [...timers.values()]; timers.clear(); for (const fn of all) fn();},
    async answer(index, items) {requests[index].resolve({ok: true, json: async () => ({results: items})}); await new Promise(resolve => setImmediate(resolve));},
  };
}

test("empty optional filter submits and sends no initial request", () => {
  const p = page(); p.input.fire("focus"); p.flush();
  assert.equal(p.requests.length, 0); assert.equal(p.input.message, "");
  assert.equal(p.form.fire("submit").prevented, false);
});
test("required relationship still requires explicit selection", () => {
  const p = page({required: true});
  assert.notEqual(p.input.message, ""); assert.equal(p.form.fire("submit").prevented, true);
});
test("preselected client is visible without loading suggestions", () => {
  const p = page({id: "42", name: "Selected client"}); p.init(); p.input.fire("focus"); p.flush();
  assert.equal(p.input.value, "Selected client"); assert.equal(p.selected.value, "42");
  assert.equal(p.requests.length, 0); assert.equal(p.form.fire("submit").prevented, false);
});
test("clear button makes optional field empty and valid", () => {
  const p = page({id: "42", name: "Selected client"}); p.clear.fire("click"); p.flush();
  assert.equal(p.input.value, ""); assert.equal(p.selected.value, "");
  assert.equal(p.form.fire("submit").prevented, false); assert.equal(p.requests.length, 0);
});
test("typed but unselected optional query cannot silently become All", () => {
  const p = page(); p.type("ab"); p.flush();
  assert.equal(p.requests.length, 0); assert.equal(p.form.fire("submit").prevented, true);
  p.type(""); assert.equal(p.form.fire("submit").prevented, false);
});
test("independent filter and upload widgets do not share selection", async () => {
  const a = page(), b = page(); a.type("Alpha"); a.flush();
  await a.answer(0, [{id: 9, name: "Alpha"}]); a.results.children[0].fire("click");
  assert.equal(a.selected.value, "9"); assert.equal(b.selected.value, "");
  assert.equal(b.requests.length, 0); assert.equal(b.form.fire("submit").prevented, false);
});
test("clear cancels stale request and cannot resurrect a client", async () => {
  const p = page(); p.type("Alpha"); p.flush(); p.clear.fire("click");
  assert.equal(p.requests[0].options.signal.aborted, true);
  await p.answer(0, [{id: 9, name: "Alpha"}]);
  assert.equal(p.results.hidden, true); assert.equal(p.results.children.length, 0);
  assert.equal(p.selected.value, ""); assert.equal(p.form.fire("submit").prevented, false);
});
