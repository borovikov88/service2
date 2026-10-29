/* Dependency-free controller regressions, not real-browser acceptance. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.value = ''; this.hidden = false; this.disabled = false; this.textContent = ''; }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  dispatchEvent(event) { for (const fn of this.listeners[event.type] || []) fn(event); }
  querySelector(selector) {
    const name = selector.match(/\[name="([^"]+)"\]/)?.[1];
    for (const node of this.children) {
      if (name && node.name === name && (!selector.endsWith(':checked') || node.checked)) return node;
      if (selector === '[data-remove]' && node.removeButton) return node;
      const child = node.querySelector(selector); if (child) return child;
    }
    return null;
  }
  reportValidity() { return true; }
  scrollIntoView() {}
}
class Event { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } preventDefault() {} }
const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture(id = 1) {
  return {id, client: 'Client ' + id, object: 'Pool ' + id, author: 'Synthetic', recorded_at: '2026-01-01', state_label: 'Reviewed', source_revision: 1,
    source_hash: 'a'.repeat(64), review_id: id, review_revision: 1, task_version: 'b'.repeat(64), materials: 'Pump', task_url: '/task/' + id, readonly_reason: '', can_review: true,
    positions: [{line_token: 'pump', description: 'Pump ' + id, mode: 'approval_required', reason: 'Agree'}], sources: [], history: [],
    need_deals: {can_convert: true, client_id: id, bindings: [{line_token: 'pump', line_id: id * 10, consumed: false}]}};
}
function harness(query = 'need_task=1') {
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const $ = name => get('needs-' + name);
  const root = get('object-needs'); Object.assign(root.dataset, {orgId: '1', dataUrl: '/data', detailBase: '/detail/', commandUrl: '/review', dealCommandUrl: '/deal', timezoneOffset: '+07:00'});
  const requests = []; let uuid = 0; let timerId = 0; const timers = new Map();
  const location = {href: 'https://example.test/needs?' + query};
  const document = {cookie: '', getElementById: get, createElement: tag => new Element(tag), querySelector(selector) {
    if (selector !== '#needs-line-template') return null;
    return {content: {firstElementChild: {cloneNode() { const row = new Element(); for (const name of ['description', 'mode', 'reason']) { const field = new Element(); field.name = name; row.append(field); } const remove = new Element('button'); remove.removeButton = true; row.append(remove); return row; }}}};
  }};
  const context = {document, location, history: {replaceState(_a, _b, url) {location.href = String(url);}}, URL, URLSearchParams, AbortController,
    Event, CustomEvent: Event, crypto: {randomUUID: () => String(++uuid)}, setTimeout(fn) {timers.set(++timerId, fn); return timerId;}, clearTimeout(id) {timers.delete(id);},
    fetch(url, options = {}) { return new Promise((resolve, reject) => requests.push({url: String(url), options, resolve, reject})); }};
  vm.createContext(context);
  for (const name of ['crm-object-needs.js', 'crm-need-deals.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/assets/js', name), 'utf8'), context);
  const respond = (request, data, status = 200) => request.resolve({ok: status < 400, status, json: async () => data});
  const fire = (element, type = 'click') => element.dispatchEvent(new Event(type));
  const pending = match => requests.filter(request => !request.used && match(request));
  const take = match => { const request = pending(match)[0]; assert.ok(request, 'Expected matching request'); request.used = true; return request; };
  const detail = id => take(r => r.url.startsWith('/detail/' + id + '/?'));
  const lines = () => take(r => r.url.includes('/lines/'));
  const post = () => take(r => r.options.method === 'POST');
  const setDeal = () => { $('deal-lines').children[0].querySelector('[name="selected"]').checked = true; $('deal-pipeline').value = '7'; $('deal-title').value = 'Explicit'; $('deal-next').value = 'Call'; $('deal-due').value = '2026-11-01T12:30'; };
  async function boot() {
    respond(take(r => r.url.startsWith('/data')), {rows: [fixture(1), fixture(2), fixture(3)], page: 1, pages: 1, total: 3});
    respond(detail(1), {need: fixture()}); await tick();
    respond(lines(), {rows: [], page: 1, pages: 1, total: 0}); await tick(); setDeal();
  }
  const open = index => fire($('rows').children[index - 1].children[4].children[0]);
  const runTimers = () => { const fns = [...timers.values()]; timers.clear(); fns.forEach(fn => fn()); };
  return {$, root, requests, respond, fire, take, detail, lines, post, pending, setDeal, boot, open, runTimers};
}
(async () => {
  for (const mutation of ['add', 'remove', 'input', 'change']) {
    const h = harness(); await h.boot();
    assert.equal(h.root.dataset.reviewDirty, 'false');
    if (mutation === 'remove') h.fire(h.$('lines').children[0].querySelector('[data-remove]'));
    else if (mutation === 'add') h.fire(h.$('add'));
    else h.fire(h.$('form'), mutation);
    assert.equal(h.root.dataset.reviewDirty, 'true', mutation);
    assert.equal(h.$('deal-fields').disabled, true, mutation);
    h.fire(h.$('deal-form'), 'submit');
    assert.equal(h.pending(r => r.options.method === 'POST').length, 0, mutation + ' blocks stale conversion');
  }
  {
    const h = harness(); await h.boot(); h.open(2); const oldGet = h.detail(2);
    h.fire(h.$('deal-form'), 'submit'); const first = h.post(); const payload = JSON.parse(first.options.body);
    assert.equal(payload.task_id, 1); assert.equal(oldGet.options.signal.aborted, true);
    h.fire(h.$('deal-retry')); assert.equal(h.pending(r => r.options.method === 'POST').length, 0, 'No overlapping retry');
    h.respond(oldGet, {need: fixture(2)}); await tick(); assert.equal(h.$('title').textContent, 'Client 1 · Pool 1');
    first.reject(Error('lost response')); await tick();
    h.fire(h.$('deal-retry')); const retry = h.post(); assert.equal(retry.options.body, first.options.body, 'Exact bytes retry');
    h.$('deal-pipeline').value = '99'; h.respond(retry, {result: {deal_id: 42}}); await tick();
    const refresh = h.detail(1); assert.ok(h.$('deal-result').children[0].href.includes('pipeline=7&deal=42'));
    h.respond(refresh, {need: fixture(1)}); await tick();
    assert.equal(h.$('title').textContent, 'Client 1 · Pool 1');
  }
  {
    const h = harness(); await h.boot(); h.open(2); const older = h.detail(2); h.open(3); const newer = h.detail(3);
    h.respond(newer, {need: fixture(3)}); await tick(); h.respond(older, {need: fixture(2)}); await tick();
    assert.equal(h.$('title').textContent, 'Client 3 · Pool 3', 'Newest detail wins');
    h.open(2); const closed = h.detail(2); h.fire(h.$('close')); h.respond(closed, {need: fixture(2)}); await tick();
    assert.equal(h.$('detail').hidden, true, 'Delayed GET cannot reopen closed detail');
  }
  {
    const h = harness(); await h.boot(); h.open(2); const older = h.detail(2); h.fire(h.$('form'), 'submit'); const review = h.post();
    h.respond(older, {need: fixture(2)}); await tick(); assert.equal(h.$('title').textContent, 'Client 1 · Pool 1');
    h.respond(review, {result: {review_id: 2}}); await tick(); h.respond(h.detail(1), {need: fixture(1)}); await tick();
    assert.equal(JSON.parse(review.options.body).task_id, 1, 'Review refresh belongs to submitted source');
  }
  {
    const h = harness(); await h.boot(); h.$('line-search').value = 'old'; h.fire(h.$('line-search'), 'input'); h.runTimers(); const older = h.lines();
    h.$('line-search').value = 'new'; h.fire(h.$('line-search'), 'input');
    h.respond(older, {rows: [{id: 9, description: 'stale', source_revision: 1}], page: 1, pages: 1, total: 1}); await tick();
    assert.equal(h.$('line-history').children.length, 0, 'Debounce invalidates previous response immediately');
    h.runTimers(); const newer = h.lines(); h.respond(newer, {rows: [{id: 10, description: 'fresh', source_revision: 1}], page: 1, pages: 1, total: 1}); await tick();
    assert.match(h.$('line-history').children[0].textContent, /fresh/);
  }
  {
    const h = harness(); await h.boot(); h.fire(h.$('form'), 'submit'); const first = h.post();
    h.respond(first, {result: {review_id: 2}}); await tick();
    // During the detail refresh the first request still owns 'sending'. A
    // synthetic submit must not replace its pending payload even then.
    h.fire(h.$('form'), 'submit');
    assert.equal(h.pending(r => r.options.method === 'POST').length, 0);
    h.respond(h.detail(1), {need: fixture(1)}); await tick();
    const delayedList = h.take(r => r.url.startsWith('/data'));
    assert.equal(h.$('fields').disabled, false, 'Confirmed detail is editable');
    h.fire(h.$('form'), 'submit'); const second = h.post();
    assert.notEqual(JSON.parse(second.options.body).operation_key, JSON.parse(first.options.body).operation_key);
    h.respond(delayedList, {rows: [], page: 1, pages: 1, total: 0}); await tick();
    assert.equal(h.root.dataset.reviewPending, 'true', 'List completion cannot unlock second POST');
  }
  {
    const h = harness('need_task=1&line_page=3'); await h.boot();
    assert.ok(h.requests.some(r => r.url.includes('/lines/') && new URL(r.url, 'https://example.test').searchParams.get('page') === '3'), 'Initial history page survives first detail');
  }
  for (const mismatch of ['none', 'review', 'task', 'source', 'consumed']) {
    const h = harness(); await h.boot(); h.fire(h.$('reconcile')); const request = h.post();
    h.respond(request, {result: {review_id: 1}}); await tick();
    const refreshed = fixture(mismatch === 'task' ? 2 : 1);
    if (mismatch === 'review') refreshed.review_id = 2;
    if (mismatch === 'source') refreshed.source_revision = 2;
    if (mismatch === 'consumed') refreshed.need_deals.bindings[0].consumed = true;
    h.respond(h.detail(1), {need: refreshed}); await tick();
    const selected = h.$('deal-lines').children[0].querySelector('[name="selected"]');
    assert.equal(selected.checked, mismatch === 'none', 'Reconcile selection scoped to matching unconsumed detail: ' + mismatch);
    if (mismatch === 'none') assert.equal(h.$('deal-lines').children[0].dataset.lineId, 10);
  }
  for (const form of ['form', 'deal-form']) {
    const h = harness(); await h.boot(); h.fire(h.$(form), 'submit'); const first = h.post();
    first.reject(Error('lost response')); await tick();
    const retryName = form === 'form' ? 'retry' : 'deal-retry';
    h.fire(h.$(retryName)); const retry = h.post();
    assert.equal(retry.options.body, first.options.body);
    h.respond(retry, {error: 'Newer source version'}, 409); await tick();
    const feedback = h.$(form === 'form' ? 'feedback' : 'deal-feedback').textContent;
    assert.match(feedback, /могла быть выполнена ранее/);
    assert.match(feedback, /историю/);
    assert.equal(h.$(retryName).hidden, true);
    assert.equal(h.$(form === 'form' ? 'refresh' : 'deal-refresh').hidden, false);
    assert.equal(h.pending(r => r.options.method === 'POST').length, 0, 'Conflict never auto-creates another operation');
    if (form === 'deal-form') { h.fire(h.$(form), 'submit'); assert.equal(h.pending(r => r.options.method === 'POST').length, 0, 'Conversion blocked until refresh'); }
  }
  for (const form of ['form', 'deal-form']) {
    const h = harness(); await h.boot(); h.fire(h.$(form), 'submit'); const first = h.post();
    h.respond(first, {unexpected: 'malformed success'}); await tick();
    h.fire(h.$(form === 'form' ? 'retry' : 'deal-retry')); const retry = h.post();
    assert.equal(retry.options.body, first.options.body, 'Malformed success retains exact request');
  }
  console.log('PASS: dirty add/remove/input/change; detail reorder/close/pending; frozen retries; payload-bound refresh; history generations; delayed list ownership; reconcile selection scope; initial history page; uncertain-to-409 recovery');
})().catch(error => { console.error(error); process.exitCode = 1; });
