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
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000}, serviceWorkers:'block'});
    await context.addCookies([{name:fixture.sessionCookieName, value:fixture.sessionCookie, url:fixture.base}]);
    await context.route('**/*', (route) => {
      const url = route.request().url();
      if (url === bootstrapURL) return route.fulfill({status:200, contentType:'application/javascript', body:bundle});
      return new URL(url).origin === fixture.base ? route.continue() : route.abort();
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
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
    await page.locator('#needs-retry').click();
    await page.waitForFunction(() => document.querySelector('#needs-feedback').textContent.startsWith('Разбор сохранён'));
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
  } finally { await browser.close(); }
})().catch((error) => { process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1; });
