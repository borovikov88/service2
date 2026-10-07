"use strict";
/* Real scripts with a small DOM adapter; not a browser or production test. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const base = path.resolve(__dirname, "..");
const picker = fs.readFileSync(path.join(base, "static/assets/js/crm-client-picker.js"), "utf8");
const template = fs.readFileSync(path.join(base, "templates/pool_service/clients.html"), "utf8");
const listScript = Array.from(template.matchAll(/<script>([\s\S]*?)<\/script>/g))
  .map(match => match[1]).find(source => source.includes("data-client-search"));

function element() {
  const listeners = new Map(), attrs = new Map(), classes = new Set();
  return {
    dataset: {}, value: "", hidden: true, children: [], textContent: "", id: "",
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(callback);
    },
    fire(type, fields = {}) {
      const event = {key: "", target: this, prevented: false,
        preventDefault() {this.prevented = true;}, ...fields};
      for (const callback of listeners.get(type) || []) callback(event);
      return event;
    },
    setAttribute(key, value) {attrs.set(key, value);},
    getAttribute(key) {return attrs.get(key);},
    removeAttribute(key) {attrs.delete(key);},
    setCustomValidity(value) {this.validityMessage = value;},
    reportValidity() {this.reported = true;},
    focus() {}, scrollIntoView() {},
    append(child) {this.children.push(child);},
    replaceChildren() {this.children = [];},
    classList: {toggle(name, force) {if (force) classes.add(name); else classes.delete(name);}},
  };
}

function pickerPage() {
  const input = element(), selected = element(), results = element(), status = element();
  const form = element(), doc = element(), root = element();
  input.id = "lookup"; input.form = form;
  root.dataset.lookupUrl = "/clients/1/?lookup=relationships";
  root.querySelector = selector => ({
    "[data-lookup-input]": input, "[data-lookup-value]": selected,
    "[data-lookup-results]": results, "[data-lookup-status]": status,
  })[selector];
  root.contains = target => target === root || target === input || results.children.includes(target);
  doc.readyState = "loading"; doc.querySelectorAll = () => []; doc.createElement = () => element();
  const timers = new Map(), requests = []; let sequence = 0;
  const context = {
    module: {exports: {}}, document: doc, URL, AbortController,
    window: {location: {href: "https://crm.example.test/clients/1/"},
      setTimeout(fn) {timers.set(++sequence, fn); return sequence;},
      clearTimeout(id) {timers.delete(id);}},
    fetch(url, options) {
      let resolve, reject;
      const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
      requests.push({url, options, resolve, reject});
      return promise;
    },
  };
  vm.runInNewContext(picker, context, {timeout: 1000});
  context.module.exports.initPicker(root);
  return {input, selected, results, status, form, doc, requests,
    searchable: context.module.exports.searchable,
    type(value) {input.value = value; input.fire("input");},
    flush() {
      const callbacks = Array.from(timers.values()); timers.clear();
      return callbacks.map(callback => callback());
    },
    async answer(index, rows, hasMore = false) {
      requests[index].resolve({ok: true, json: async () => ({results: rows, has_more: hasMore})});
      await new Promise(resolve => setImmediate(resolve));
    },
  };
}

test("picker does not preload or request below three meaningful characters", () => {
  const page = pickerPage();
  page.input.fire("focus"); page.flush();
  for (const query of ["", "С", "Ст", " - ", "  Ст - "]) {page.type(query); page.flush();}
  assert.equal(page.requests.length, 0);
  assert.equal(page.results.children.length, 0);
  assert.equal(page.results.hidden, true);
  page.type("Стр"); page.flush();
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].url.searchParams.get("q"), "Стр");
});

test("selection is explicit, editing clears the ID, stale requests cannot repopulate", async () => {
  const page = pickerPage();
  page.type("Строй"); page.flush();
  await page.answer(0, [{id: 7, name: "Строй-инвест"}]);
  page.results.children[0].fire("click");
  assert.equal(page.selected.value, "7");
  assert.equal(page.input.validityMessage, "");
  page.type("Иван"); page.flush();
  assert.equal(page.selected.value, "");
  page.type("Ив"); page.flush();
  assert.equal(page.requests[1].options.signal.aborted, true);
  await page.answer(1, [{id: 9, name: "Late result"}]);
  assert.equal(page.results.hidden, true);
  assert.equal(page.results.children.length, 0);
  assert.equal(page.form.fire("submit").prevented, true);
});

test("old response loses to newer input; arrows select last then first", async () => {
  const page = pickerPage();
  page.type("Старый"); page.flush();
  page.type("Новый"); page.flush();
  await page.answer(1, [{id: 10, name: "New first"}, {id: 11, name: "New last"}]);
  await page.answer(0, [{id: 12, name: "Old"}]);
  page.input.fire("keydown", {key: "ArrowUp"});
  assert.equal(page.input.getAttribute("aria-activedescendant"), "lookup-option-1");
  page.input.fire("keydown", {key: "Enter"});
  assert.equal(page.selected.value, "11");
});

test("lookup errors recover, outside close and refocus only search when eligible", async () => {
  const page = pickerPage();
  page.type("Новый"); page.flush();
  page.requests[0].reject(new Error("network"));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(page.status.textContent, /Не удалось/);
  page.input.fire("focus"); page.flush();
  await page.answer(1, [{id: 1, name: "<img src=x onerror=alert(1)>"}]);
  const label = page.results.children[0].children[0];
  assert.equal(label.textContent, "<img src=x onerror=alert(1)>");
  assert.equal(label.children.length, 0);
  page.doc.fire("click", {target: element()});
  assert.equal(page.results.hidden, true);
  page.input.fire("focus"); page.flush();
  assert.equal(page.requests.length, 3);
});

function listPage(query) {
  const doc = element(), input = element(), counter = element(), row = element();
  input.dataset.clientSearch = "companies";
  row.dataset.search = "ООО «СТРОЙ–ИНВЕСТ» Семён +7 900 123 4567";
  doc.querySelectorAll = selector => selector === "[data-client-search]" ? [input] : [row];
  doc.querySelector = () => counter; doc.getElementById = () => null;
  const location = new URL("https://crm.example.test/clients/");
  const context = {document: doc, window: {location, clearTimeout() {}, setTimeout(fn) {fn();}},
    history: {replaceState(_state, _title, url) {location.href = new URL(url, location).href;}},
    URLSearchParams};
  vm.runInNewContext(listScript, context, {timeout: 1000});
  doc.fire("DOMContentLoaded");
  input.value = query; input.fire("input");
  return {row, location};
}

for (const query of ["Строй инвест", "Строй-инвест", "  строй   инвест ", "СТРОЙ\u00a0ИНВЕСТ", "семен", "9001234567"]) {
  test("client list normalizes search: " + query, () => assert.equal(listPage(query).row.hidden, false));
}
test("client list does not match unrelated words", () => assert.equal(listPage("Несуществующий").row.hidden, true));
test("client list preserves the typed query in the URL", () => {
  assert.equal(listPage("Строй инвест").location.searchParams.get("companies_q"), "Строй инвест");
});
