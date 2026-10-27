/* Dedicated CI local fixture: actual pinned Bootstrap, Chromium, no providers. */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const fixture = JSON.parse(input);
  assert.match(fixture.base, /^http:\/\/localhost:[0-9]+$|^http:\/\/127\.0\.0\.1:[0-9]+$/);
  assert.equal(require('bootstrap/package.json').version, '5.2.3');
  const bundle = readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'), 'utf8');
  const bootstrapURL = 'https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js';
  const browser = await chromium.launch({headless:true});
  let page;
  const errors = [], network = [];
  const record = (event) => { network.push(event); if (network.length > 20) network.shift(); };
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000}, serviceWorkers:'block'});
    await context.addCookies([{name:fixture.sessionCookieName, value:fixture.sessionCookie, url:fixture.base}]);
    await context.route('**/*', (route) => {
      const url = route.request().url();
      if (url === bootstrapURL) return route.fulfill({status:200, contentType:'application/javascript', body:bundle});
      return new URL(url).origin === fixture.base ? route.continue() : route.abort();
    });
    page = await context.newPage();
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('response', (response) => {
      const url = new URL(response.url());
      if (url.origin === fixture.base && url.pathname.startsWith('/crm/workspace/needs/'))
        record({path: url.pathname + url.search, status: response.status()});
    });
    page.on('requestfailed', (request) => {
      const url = new URL(request.url());
      if (url.origin === fixture.base && url.pathname.startsWith('/crm/workspace/needs/'))
        record({path: url.pathname + url.search, failure: request.failure()?.errorText});
    });
    const bootstrapResponse = page.waitForResponse((response) => response.url() === bootstrapURL);
    await page.goto(fixture.base + fixture.workspace);
    assert.equal(await (await bootstrapResponse).text(), bundle);
    assert.equal(await page.evaluate(() => window.bootstrap?.Tooltip?.VERSION), '5.2.3');
    await page.locator('#needs-rows button').first().waitFor();
    await page.locator('#needs-search').fill('Chlorine');
    await page.waitForURL((url) => url.searchParams.get('q') === 'Chlorine');
    await page.waitForFunction(() => document.querySelector('#needs-status').textContent === 'Доступно: 1');
    await page.locator('#needs-rows button').click();
    await page.locator('#needs-detail:not([hidden])').waitFor();
    const lines = page.locator('.needs-line');
    await lines.nth(0).locator('[name="description"]').fill('Chlorine');
    await lines.nth(0).locator('[name="mode"]').selectOption('chemistry_monthly');
    await lines.nth(0).locator('[name="reason"]').fill('Manager checked monthly service conditions');
    await page.locator('#needs-add').click();
    await lines.nth(1).locator('[name="description"]').fill('Pump');
    await lines.nth(1).locator('[name="mode"]').selectOption('approval_required');
    await lines.nth(1).locator('[name="reason"]').fill('Scope and price require explicit approval');
    const sent = [];
    let loseFirst = true;
    await context.route('**/crm/workspace/needs/review/**', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      sent.push(route.request().postData());
      if (loseFirst) {
        loseFirst = false;
        const response = await route.fetch();
        assert.equal(response.status(), 200, await response.text());
        return route.abort('failed'); // Server committed; browser loses response.
      }
      return route.continue();
    });
    await page.locator('#needs-form button[type="submit"]').click();
    await page.locator('#needs-retry:not([hidden])').waitFor();
    // Playwright's native disabled-control whitelist excludes FIELDSET itself.
    // Check its DOM state and the inherited disabled state of every real input
    // and action, so this still proves that an uncertain operation is frozen.
    assert.equal(await page.locator('#needs-fields').evaluate((fieldset) => fieldset.disabled), true);
    const frozenControls = page.locator('#needs-fields input, #needs-fields textarea, #needs-fields select, #needs-fields button');
    assert.equal(await frozenControls.count(), 10); // two lines, add and submit
    for (const control of await frozenControls.all()) assert.equal(await control.isDisabled(), true);
    assert.equal(await page.locator('#needs-close').isDisabled(), true);
    assert.equal(await page.locator('#needs-retry').isEnabled(), true);
    // Success feedback precedes send()'s awaited queue refresh. Finish that
    // specific response and render before navigating, rather than aborting an
    // in-flight live-server request when reload races the success message.
    const refreshedQueue = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.origin === fixture.base && url.pathname === new URL(fixture.data, fixture.base).pathname
        && url.searchParams.get('q') === 'Chlorine' && response.request().method() === 'GET';
    });
    await page.locator('#needs-retry').click();
    await page.waitForFunction(() => document.querySelector('#needs-feedback').textContent.startsWith('Разбор сохранён'));
    const refreshedResponse = await refreshedQueue;
    assert.equal(refreshedResponse.status(), 200, await refreshedResponse.text());
    assert.equal(await refreshedResponse.finished(), null);
    const refreshedData = await refreshedResponse.json();
    assert.equal(refreshedData.total, 1);
    assert.equal(refreshedData.rows.length, 1);
    assert.equal(refreshedData.rows[0].id, fixture.taskId);
    assert.equal(refreshedData.rows[0].state, 'reviewed');
    await page.waitForFunction(() => document.querySelector('#needs-status').textContent === 'Доступно: 1'
      && document.querySelector('#needs-rows').children.length === 1);
    assert.equal(sent.length, 2);
    assert.equal(sent[0], sent[1]);
    await page.reload();
    await page.locator('#needs-rows button').first().waitFor();
    await page.locator('#needs-rows button').click();
    await page.locator('#needs-detail:not([hidden])').waitFor();
    assert.equal(await page.locator('.needs-line').count(), 2);
    assert.equal(await lines.nth(1).locator('[name="mode"]').inputValue(), 'approval_required');
    assert.match(await page.locator('#needs-meta').textContent(), /Разобрано/);
    await page.locator('#needs-state').selectOption('reviewed');
    await page.waitForURL((url) => url.searchParams.get('state') === 'reviewed');
    await page.waitForFunction(() => document.querySelector('#needs-status').textContent === 'Доступно: 1');
    await page.setViewportSize({width:375,height:900});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2), 'Mobile document overflow');
    assert.equal(errors.length, 0, errors.join('\n'));
  } catch (error) {
    const state = page ? await page.evaluate(() => ({
      url: location.href, title: document.title,
      status: document.querySelector('#needs-status')?.textContent,
      feedback: document.querySelector('#needs-feedback')?.textContent,
      rows: document.querySelector('#needs-rows')?.children.length,
    })).catch(() => ({unavailable: true})) : {unavailable: true};
    process.stderr.write('Needs browser diagnostics: ' + JSON.stringify({state, pageErrors: errors, network}) + '\n');
    throw error;
  } finally { await browser.close(); }
})().catch((error) => { process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1; });
