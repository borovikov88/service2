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
    const nextListResponse = () => page.waitForResponse((response) =>
      new URL(response.url()).pathname === new URL(fixture.base + fixture.data).pathname
      && response.request().method() === 'GET');
    const reviewedList = async (responsePromise) => {
      const response = await responsePromise;
      const actualURL = new URL(response.url());
      const expectedOrg = new URL(fixture.base + fixture.data).searchParams.get('org_id');
      assert.deepEqual(actualURL.searchParams.getAll('org_id'), [expectedOrg], 'Reviewed list must preserve its explicit organization');
      const diagnostic = {status: response.status(), path: actualURL.pathname,
        org: actualURL.searchParams.getAll('org_id'),
        documentOrg: await page.locator('#object-needs').getAttribute('data-org-id')};
      // This harness permits localhost fixtures only. Keep failure evidence
      // structural: no response body, cookies, credentials or source content.
      assert.equal(response.status(), 200, 'Reviewed list must load successfully: ' + JSON.stringify(diagnostic));
      const data = await response.json();
      assert.equal(data.total, 1);
      const row = data.rows.find((item) => item.id === fixture.taskId);
      assert.ok(row, 'Reviewed task must remain in the canonical list');
      assert.equal(row.state, 'reviewed');
      assert.equal(row.review_revision, 1);
      assert.deepEqual(row.modes, ['chemistry_monthly', 'approval_required']);
      await page.waitForFunction((revision) => document.querySelector('#needs-status')?.textContent === 'Доступно: 1'
        && document.querySelectorAll('#needs-rows button').length === 1
        && document.querySelector('#needs-rows')?.textContent.includes('Разобрано · редакция ' + revision), row.source_revision);
    };
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
    const savedList = nextListResponse();
    await page.locator('#needs-retry').click();
    await page.waitForFunction(() => document.querySelector('#needs-feedback').textContent.startsWith('Разбор сохранён'));
    // Feedback precedes the list refresh; finish that request and render before reload.
    await reviewedList(savedList);
    assert.equal(sent.length, 2);
    assert.equal(sent[0], sent[1]);
    const reloadedList = nextListResponse();
    const reloaded = await page.reload();
    assert.ok(reloaded, 'Reload must navigate to a new document');
    assert.equal(reloaded.status(), 200, 'Reload must return a usable document');
    await reviewedList(reloadedList);
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
    // Real Django conversion: chemistry cannot be selected; one pump gets one
    // permanent identity, deal and actual next task, even after a lost response.
    await page.locator('#needs-deal-fields:not([disabled])').waitFor();
    assert.equal(await page.locator('#needs-deal-lines [name="selected"]').count(), 1);
    await page.locator('#needs-deal-lines [name="selected"]').check();
    await page.locator('#needs-deal-lines [name="identity"]').selectOption('new');
    await page.locator('#needs-reconcile').click();
    await page.locator('#needs-deal-lines [data-line-id]').waitFor();
    assert.equal(await page.locator('#needs-deal-lines [name="selected"]').isChecked(), true);
    await page.locator('#needs-deal-pipeline').selectOption(String(fixture.pipelineId));
    await page.locator('#needs-deal-title').fill('Synthetic agreed scope');
    await page.locator('#needs-deal-next').fill('Confirm scope with client');
    await page.locator('#needs-deal-due').fill('2026-11-01T12:30');
    const conversions = [];
    await context.route('**/crm/workspace/needs/deal-command/**', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      conversions.push(route.request().postData());
      if (conversions.length === 1) {
        const response = await route.fetch();
        assert.equal(response.status(), 200, await response.text());
        return route.abort('failed');
      }
      return route.continue();
    });
    await page.locator('#needs-deal-form button[type="submit"]').click();
    await page.locator('#needs-deal-retry:not([hidden])').waitFor();
    assert.equal(await page.locator('#needs-deal-title').isDisabled(), true);
    assert.equal(await page.locator('#needs-close').isDisabled(), true);
    await page.locator('#needs-deal-retry').click();
    await page.locator('#needs-deal-result a').waitFor();
    await page.waitForFunction(() => document.querySelector('#needs-deal-lines [name="selected"]')?.disabled);
    assert.equal(conversions.length, 2);
    assert.equal(conversions[0], conversions[1]);
    const dealURL = await page.locator('#needs-deal-result a').getAttribute('href');
    assert.match(dealURL, /&deal=\d+/);
    await page.reload();
    await page.locator('#needs-deal-lines [name="selected"]:disabled').waitFor();
    await page.locator('#needs-line-search').fill(JSON.parse(conversions[0]).selections[0].line_token);
    await page.waitForFunction(() => document.querySelector('#needs-line-status').textContent === 'Прежних позиций: 1');
    assert.equal(await page.locator('#needs-line-history a').getAttribute('href'), dealURL);
    await page.evaluate(() => document.documentElement.setAttribute('data-bs-theme', 'dark'));
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2), 'Converted mobile overflow');
    assert.equal(errors.length, 0, errors.join('\n'));
  } finally { await browser.close(); }
})().catch((error) => { process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1; });
