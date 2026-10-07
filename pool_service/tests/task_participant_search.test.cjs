"use strict";
// Execute the real shared script with a minimal DOM adapter, not a browser.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../static/assets/js/base-shell-post-b.js"), "utf8");

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentElement = null;
    this.attrs = new Map(); this.listeners = new Map(); this.dataset = {}; this.style = {};
    this.hidden = false; this.disabled = false; this.checked = false; this.value = "";
    this.text = ""; this.classes = new Set();
    this.classList = {
      contains: name => this.classes.has(name),
      add: (...names) => names.forEach(name => this.classes.add(name)),
      remove: (...names) => names.forEach(name => this.classes.delete(name)),
      toggle: (name, force) => {
        const active = force === undefined ? !this.classes.has(name) : force;
        if (active) this.classes.add(name); else this.classes.delete(name);
        return active;
      },
    };
  }
  set className(value) {this.classes = new Set(value.split(/\s+/).filter(Boolean));}
  get className() {return [...this.classes].join(" ");}
  set textContent(value) {this.text = value; this.children = [];}
  get textContent() {return this.text + this.children.map(child => child.textContent).join("");}
  append(...children) {children.forEach(child => {child.parentElement = this; this.children.push(child);});}
  prepend(...children) {children.forEach(child => {child.parentElement = this;}); this.children.unshift(...children);}
  setAttribute(name, value) {this.attrs.set(name, String(value));}
  getAttribute(name) {return this.attrs.get(name) ?? (name === "type" ? this.type : null);}
  hasAttribute(name) {return this.attrs.has(name);}
  matches(selector) {
    if (selector.startsWith(".")) return this.classes.has(selector.slice(1));
    const parsed = selector.match(/^(\w+)?(?:\[([\w-]+)(?:=['"]?([^'"\]]+)['"]?)?\])?(:checked)?$/);
    if (!parsed) throw new Error("Unsupported test selector: " + selector);
    const [, tag, attr, value, checked] = parsed;
    return (!tag || this.tagName === tag.toUpperCase()) &&
      (!attr || (value === undefined ? this.hasAttribute(attr) : this.getAttribute(attr) === value)) &&
      (!checked || this.checked);
  }
  closest(selector) {return this.matches(selector) ? this : this.parentElement?.closest(selector) || null;}
  querySelectorAll(selector) {
    return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
  contains(node) {return this === node || this.children.some(child => child.contains(node));}
  getBoundingClientRect() {return {top: 80, bottom: 120};}
  focus() {this.focused = true;}
  addEventListener(type, fn) {if (!this.listeners.has(type)) this.listeners.set(type, []); this.listeners.get(type).push(fn);}
  fire(type, fields = {}) {
    const event = {target: this, prevented: false, stopped: false,
      preventDefault() {this.prevented = true;}, stopPropagation() {this.stopped = true;}, ...fields};
    for (let node = this; node; node = node.parentElement) {
      for (const fn of node.listeners.get(type) || []) fn(event);
      if (event.stopped) break;
    }
    return event;
  }
}
const marked = (tag, attribute) => {const el = new Element(tag); el.setAttribute(attribute, ""); return el;};
function page(rows, {task = true, responsibleBlock = true, viaTaskInit = false} = {}) {
  const doc = new Element("document"); doc.createElement = tag => new Element(tag); doc.getElementById = () => null;
  doc.documentElement = {clientHeight: 900};
  const form = new Element("form"), block = new Element(), root = marked("div", "data-multi-select");
  if (task) form.setAttribute("data-task-form", "");
  if (responsibleBlock) block.setAttribute("data-responsibles-block", "");
  const trigger = marked("button", "data-multi-select-trigger");
  const value = new Element("span"); value.className = "multi-select__value";
  const list = marked("div", "data-multi-select-list");
  trigger.append(value); root.append(trigger, list); block.append(root); form.append(block); doc.append(form);
  const entries = rows.map((row, i) => {
    const option = new Element("label"); option.className = "multi-select__option"; option.textContent = row.name;
    const checkbox = new Element("input"); checkbox.type = "checkbox"; checkbox.name = "responsibles"; checkbox.value = String(i + 1);
    checkbox.checked = Boolean(row.checked); checkbox.disabled = Boolean(row.disabled);
    if (row.locked) checkbox.setAttribute("data-locked", "1");
    if (row.hidden) {option.hidden = true; option.classList.add("d-none");}
    option.append(checkbox); list.append(option); return {option, checkbox};
  });
  const win = {innerHeight: 900, getComputedStyle: () => ({maxHeight: "260px"}), matchMedia: () => ({matches: false})};
  vm.runInNewContext(source, {document: doc, window: win, setTimeout}, {timeout: 1000});
  if (viaTaskInit) win.initTaskForms(doc); else win.initMultiSelect(root);
  return {doc, win, form, root, list, trigger, value, entries,
    get input() {return list.querySelector("[data-participant-search]");},
    get status() {return list.querySelector("[role='status']");},
    type(query) {this.input.value = query; this.input.fire("input");},
    visible() {return entries.filter(entry => !entry.option.hidden && !entry.option.classList.contains("d-none"));},
  };
}

test("task suggestions require three letters or numbers; checked participants stay visible", () => {
  const p = page([{name: "One", checked: true}, {name: "Three"}]);
  assert.equal(p.input.type, "search"); assert.equal(p.input.name, undefined);
  assert.ok(p.input.getAttribute("aria-label")); assert.equal(p.status.getAttribute("aria-live"), "polite");
  for (const query of ["", "T", "Th", " - ", " T-h "]) {p.type(query); assert.equal(p.visible().length, 1);}
  p.type("Thr"); assert.equal(p.visible().length, 2);
  p.type(""); assert.equal(p.visible().length, 1);
});

test("search handles Cyrillic case, e/yo, spaces and hyphens", () => {
  const p = page([{name: "\u0421\u0435\u043c\u0451\u043d \u0418\u0432\u0430\u043d\u043e\u0432-\u041f\u0435\u0442\u0440\u043e\u0432"}, {name: "Unrelated"}]);
  for (const query of ["\u0441\u0435\u043c\u0435\u043d", "\u0421\u0415\u041c\u0401\u041d", "\u0438\u0432\u0430\u043d\u043e\u0432 \u043f\u0435\u0442\u0440\u043e\u0432", " \u0418\u0432\u0430\u043d\u043e\u0432-\u041f\u0435\u0442\u0440\u043e\u0432 "]) {
    p.type(query); assert.deepEqual(p.visible(), [p.entries[0]]);
  }
  p.type("Absent"); assert.equal(p.visible().length, 0);
});

test("cap is twenty unselected suggestions, without hiding existing selections", () => {
  const p = page([...Array.from({length: 35}, (_, i) => ({name: "Worker " + i})), {name: "Selected", checked: true}]);
  p.type("Worker"); assert.equal(p.visible().length, 21); assert.match(p.status.textContent, /20/);
  assert.equal(p.entries[35].option.hidden, false);
  p.type("Worker 34"); assert.deepEqual(p.visible(), [p.entries[34], p.entries[35]]);
});

test("filter never changes submitted values, checked state or locked disabled participants", () => {
  const p = page([{name: "Required", checked: true, disabled: true, locked: true}, {name: "Worker"}]);
  const before = p.entries.map(({checkbox: c}) => [c.name, c.value, c.checked, c.disabled, c.getAttribute("data-locked")]);
  p.type("Missing"); p.type("Worker"); p.type("");
  assert.deepEqual(p.entries.map(({checkbox: c}) => [c.name, c.value, c.checked, c.disabled, c.getAttribute("data-locked")]), before);
  assert.equal(p.entries[0].option.hidden, false);
});

test("selection and deselection keep the original label and checkbox change behavior", () => {
  const p = page([{name: "Alpha"}, {name: "Beta"}]);
  p.type("Alpha"); const c = p.entries[0].checkbox; c.checked = true; c.fire("change");
  assert.equal(p.value.textContent, "Alpha"); p.type("Beta"); assert.equal(p.visible().length, 2);
  p.type(""); c.checked = false; c.fire("change"); assert.equal(p.visible().length, 0);
  assert.notEqual(p.value.textContent, "Alpha");
});

test("keyboard searches do not submit the form and Escape closes only the picker", () => {
  const p = page([{name: "Locked", checked: true, disabled: true}, {name: "Worker"}]);
  p.trigger.fire("click"); assert.equal(p.input.focused, true); assert.equal(p.trigger.getAttribute("aria-expanded"), "true");
  p.type("Worker"); assert.equal(p.input.fire("keydown", {key: "Enter"}).prevented, true);
  assert.equal(p.input.fire("keydown", {key: "ArrowDown"}).prevented, true); assert.equal(p.entries[1].checkbox.focused, true);
  const e = p.entries[1].checkbox.fire("keydown", {key: "Escape"}); assert.equal(e.stopped, true);
  assert.equal(p.root.classList.contains("multi-select--open"), false); assert.equal(p.trigger.focused, true);
  assert.equal(p.trigger.getAttribute("aria-expanded"), "false");
});

test("outside click updates expanded state and reopening retains the query", () => {
  const p = page([{name: "Alpha"}]); p.trigger.fire("click"); p.type("Alp");
  p.doc.fire("click"); assert.equal(p.trigger.getAttribute("aria-expanded"), "false");
  p.trigger.fire("click"); assert.equal(p.input.value, "Alp"); assert.equal(p.visible().length, 1);
});

test("non-task and non-participant multi-selects remain unchanged", () => {
  for (const options of [{task: false}, {responsibleBlock: false}]) {
    const p = page([{name: "Alpha"}, {name: "Beta"}], options);
    assert.equal(p.input, null); assert.equal(p.visible().length, 2);
    p.entries[1].checkbox.checked = true; p.entries[1].checkbox.fire("change");
    assert.equal(p.value.textContent, "Beta");
  }
});

test("empty and initially hidden options are not exposed by search", () => {
  assert.equal(page([]).input, null);
  const p = page([{name: "Hidden", hidden: true, checked: true}, {name: "Worker"}]);
  p.type("Hidden"); assert.equal(p.visible().length, 0); assert.equal(p.entries[0].option.hidden, true);
});

test("modal task initializer is idempotent and preserves required participants", () => {
  const p = page([{name: "Required", checked: true, disabled: true, locked: true}, {name: "Worker"}], {viaTaskInit: true});
  p.win.initTaskForms(p.doc); p.win.initAllMultiSelects(p.form); p.win.initMultiSelect(p.root);
  assert.equal(p.list.querySelectorAll("[data-participant-search]").length, 1);
  assert.equal(p.input.listeners.get("input").length, 1); assert.equal(p.entries[0].checkbox.checked, true);
  assert.equal(p.entries[0].checkbox.fire("click").prevented, true);
});
