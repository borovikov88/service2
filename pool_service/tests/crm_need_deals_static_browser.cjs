/* Actual Chromium + current template/controllers, synthetic local API only.
 * Not Django integration, production styling, or server authorization acceptance.
 * Run: CRM_TEST_CHROMIUM_PATH=/usr/bin/chromium node <this file>
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require('playwright');
const baseDir = path.join(__dirname, '..');
const modes = [['approval_required', 'Требуется согласование'], ['included', 'Включено'], ['chemistry_monthly', 'Месячный расчёт']];
let html = fs.readFileSync(path.join(baseDir, 'templates/pool_service/crm_live/object_needs.html'), 'utf8');
html = html.replace(/{% for key,label in modes.items %}([\s\S]*?){% endfor %}/g, (_, body) => modes.map(([key, label]) => body.replaceAll('{{ key }}', key).replaceAll('{{ label }}', label)).join(''))
  .replace(/{% for key,label in states.items %}[\s\S]*?{% endfor %}/g, '')
  .replace(/{% for pipeline in pipelines %}[\s\S]*?{% endfor %}/g, '<option value="7">Synthetic retail</option>')
  .replace(/{% static '([^']+)' %}/g, '/static/$1').replace(/{% url '[^']+' %}/g, '/workspace/').replace(/{%[^%]*%}/g, '');
for (const [key, value] of Object.entries({data_url:'/data', command_url:'/review', detail_base:'/detail/', deal_command_url:'/deal', 'organization.pk':'1', communication_timezone:'Asia/Barnaul', communication_timezone_offset:'+07:00'})) html = html.replaceAll('{{ ' + key + ' }}', value);
assert.doesNotMatch(html, /{%|{{/);
const need = id => ({id, client:'Synthetic client ' + id, object:'Synthetic pool ' + id, author:'Synthetic', recorded_at:'2026-01-01', state_label:'Reviewed', source_revision:1, source_hash:'a'.repeat(64), review_id:id, review_revision:1, task_version:'b'.repeat(64), materials:'Pump', task_url:'/task/' + id, readonly_reason:'', can_review:true,
  positions:[{line_token:'pump', description:'Synthetic pump', mode:'approval_required', reason:'Agree scope'}], sources:[], history:[], need_deals:{can_convert:true, client_id:id, bindings:[{line_token:'pump', line_id:10 * id, consumed:false}]}});
const posts = []; let heldDetail = null; let resolveHeld;
const heldStarted = new Promise(resolve => resolveHeld = resolve);
const json = (res, data) => {res.writeHead(200, {'Content-Type':'application/json'}); res.end(JSON.stringify(data));};
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/needs') {res.writeHead(200, {'Content-Type':'text/html; charset=utf-8'}); return res.end('<!doctype html><meta charset="utf-8">' + html);}
  if (url.pathname.startsWith('/static/assets/js/')) { const filename = path.basename(url.pathname); if (!['crm-object-needs.js', 'crm-need-deals.js'].includes(filename)) {res.writeHead(404);return res.end();} res.writeHead(200, {'Content-Type':'text/javascript; charset=utf-8'}); return res.end(fs.readFileSync(path.join(baseDir, 'static/assets/js', filename))); }
  if (url.pathname === '/data') return json(res, {rows:[need(1), need(2)], total:2, page:1, pages:1});
  if (url.pathname.includes('/lines/')) return json(res, {rows:[], page:1, pages:1, total:0});
  if (url.pathname === '/detail/1/') return json(res, {need:need(1)});
  if (url.pathname === '/detail/2/') { heldDetail = () => json(res, {need:need(2)}); resolveHeld(); return; }
  if (url.pathname === '/deal') { let body=''; for await (const chunk of req) body += chunk; posts.push(body); return json(res, posts.length === 1 ? {unexpected:'Uncertain malformed success'} : {result:{deal_id:42, review_id:1}}); }
  res.writeHead(404); res.end();
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({headless:true, ...(process.env.CRM_TEST_CHROMIUM_PATH ? {executablePath:process.env.CRM_TEST_CHROMIUM_PATH} : {})});
  try {
    const context = await browser.newContext({serviceWorkers:'block'});
    await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    const page = await context.newPage(); const errors=[]; page.on('pageerror', error => errors.push(error.message));
    const ready = async () => {await page.locator('#needs-title').filter({hasText:'Synthetic client 1'}).waitFor(); await page.locator('#needs-deal-fields:not([disabled])').waitFor();};
    await page.goto(base + '/needs?need_task=1'); await ready();
    await page.locator('#needs-add').click();
    assert.equal(await page.locator('#object-needs').getAttribute('data-review-dirty'), 'true');
    assert.equal(await page.locator('#needs-deal-fields').isDisabled(), true);
    await page.reload(); await ready();
    await page.locator('#needs-lines [data-remove]').click();
    assert.equal(await page.locator('#object-needs').getAttribute('data-review-dirty'), 'true');
    assert.equal(await page.locator('#needs-deal-fields').isDisabled(), true);
    await page.reload(); await ready();
    await page.locator('#needs-deal-lines [name="selected"]').check();
    await page.locator('#needs-deal-pipeline').selectOption('7');
    await page.locator('#needs-deal-title').fill('Synthetic explicit deal');
    await page.locator('#needs-deal-next').fill('Synthetic next action');
    await page.locator('#needs-deal-due').fill('2026-11-01T12:30');
    await page.locator('#needs-rows button').nth(1).click(); await heldStarted;
    await page.locator('#needs-deal-form button[type="submit"]').click();
    await page.locator('#needs-deal-retry:not([hidden])').waitFor();
    heldDetail();
    assert.equal(await page.locator('#needs-title').innerText(), 'Synthetic client 1 · Synthetic pool 1');
    assert.equal(JSON.parse(posts[0]).task_id, 1);
    await page.locator('#needs-deal-retry').click();
    await page.locator('#needs-deal-result a').waitFor(); await ready();
    assert.equal(posts.length, 2); assert.equal(posts[0], posts[1]);
    assert.match(await page.locator('#needs-deal-result a').getAttribute('href'), /pipeline=7&deal=42/);
    assert.equal(await page.locator('#needs-title').innerText(), 'Synthetic client 1 · Synthetic pool 1');
    assert.deepEqual(errors, []);
    console.log('PASS Chromium static synthetic smoke: current template + JS; dirty add/remove; delayed detail versus POST; malformed-success exact retry; payload refresh. NOT Django E2E.');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode=1;}).finally(() => {server.closeAllConnections(); server.close();});
