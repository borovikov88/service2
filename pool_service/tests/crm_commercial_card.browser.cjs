/* Dedicated CI local-only browser fixture; no external accounts or services. */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const fixture = JSON.parse(input);
  assert.match(fixture.base, /^http:\/\/localhost:[0-9]+$|^http:\/\/127\.0\.0\.1:[0-9]+$/);
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, serviceWorkers: "block"});
    await context.addCookies([{name: fixture.sessionCookieName, value: fixture.sessionCookie, url: fixture.base}]);
    // Serve the shared base's exact Bootstrap URL from the pinned npm package.
    // Every other non-local request stays blocked; no CDN is contacted.
    const bootstrapURL = 'https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js';
    assert.equal(require('bootstrap/package.json').version, '5.2.3');
    const bootstrapBundle = readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'), 'utf8');
    await context.route('**/*', (route) => {
      const url = route.request().url();
      if (url === bootstrapURL) return route.fulfill({status: 200, contentType: 'application/javascript', body: bootstrapBundle});
      return new URL(url).origin === fixture.base ? route.continue() : route.abort();
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const json = async (url) => { const response = await context.request.get(fixture.base + url); assert.equal(response.status(), 200); return response.json(); };
    const open = async () => { await page.locator('.crm-live-card-open', {hasText: 'Browser synthetic card'}).click(); await page.locator('[data-command-form="edit"]').waitFor(); };
    const save = async (form) => {
      const responsePromise = page.waitForResponse((response) => response.url().includes('/crm/workspace/commands/') && response.request().method() === 'POST');
      await form.locator('button[type="submit"]').click();
      const response = await responsePromise;
      assert.equal(response.status(), 200, await response.text());
      // Wait for the canonical drawer refresh, not merely the successful POST.
      const result = await response.json();
      await page.waitForFunction((revision) => document.querySelector('#crm-live-dialog-content')?.dataset.revision === String(revision)
        && document.querySelector('[data-command-form="edit"] button[type="submit"]')?.disabled === false, result.deal.revision);
      return result;
    };
    const bootstrapResponsePromise = page.waitForResponse((response) => response.url() === bootstrapURL);
    await page.goto(fixture.base + fixture.workspace);
    const bootstrapResponse = await bootstrapResponsePromise;
    assert.equal(bootstrapResponse.status(), 200);
    assert.match(bootstrapResponse.headers()['content-type'], /application\/javascript/);
    assert.equal(await bootstrapResponse.text(), bootstrapBundle);
    assert.equal(await page.evaluate(() => window.bootstrap?.Tooltip?.VERSION), '5.2.3');
    await page.locator('#crm-live-create').click();
    const create = page.locator('[data-command-form="create"]');
    await create.locator('[name="title"]').fill('Browser synthetic card');
    await create.locator('[name="client_search"]').fill(fixture.clientName);
    await page.locator('.crm-live-client-results button', {hasText: fixture.clientName}).click();
    await create.locator('[name="next_title"]').fill('Browser synthetic followup');
    await create.locator('[name="due_at"]').fill('2026-11-01T11:00');
    const created = await save(create);
    assert.equal(created.deal.amount, null);
    const dealId = created.deal.id;
    let edit = page.locator('[data-command-form="edit"]');
    await edit.waitFor();
    await edit.locator('[name="work_subject"]').fill('Browser synthetic installation');
    await edit.locator('[name="specialization"]').fill('hammam');
    await edit.locator('[name="fulfillment_kind"]').selectOption('construction');
    await edit.locator('[name="object_ref"]').selectOption('planned');
    await edit.locator('[name="planned_object_name"]').fill('Browser planned synthetic hammam');
    await edit.locator('[name="planned_object_address"]').fill('Browser planned synthetic address');
    await edit.locator('[name="amount"]').fill('0.00');
    await edit.locator('[name="currency"]').fill('RUB');
    await edit.locator('[name="amount_basis"]').selectOption('indicative');
    await edit.locator('[name="expected_decision_at"]').fill('2026-11-02T12:30');
    const planned = await save(edit);
    assert.equal(planned.deal.object.kind, 'planned');
    assert.equal(planned.deal.amount, '0.00');
    await page.reload();
    await open();
    edit = page.locator('[data-command-form="edit"]');
    assert.equal(await edit.locator('[name="amount"]').inputValue(), '0.00');
    assert.equal(await edit.locator('[name="planned_object_name"]').inputValue(), 'Browser planned synthetic hammam');
    assert.equal(await edit.locator('[name="expected_decision_at"]').inputValue(), '2026-11-02T12:30');
    await edit.locator('[name="object_search"]').fill('Browser existing');
    await edit.locator(`[name="object_ref"] option[value="pool:${fixture.poolId}"]`).waitFor({state: 'attached'});
    await edit.locator('[name="object_ref"]').selectOption('pool:' + fixture.poolId);
    await edit.locator('[name="amount"]').fill('');
    const existing = await save(edit);
    assert.equal(existing.deal.object.kind, 'pool');
    assert.equal(existing.deal.object.id, fixture.poolId);
    assert.equal(existing.deal.amount, null);
    await page.locator('#crm-live-dialog [data-action="close"]').click();
    const card = page.locator(`.crm-live-card[data-deal-id="${dealId}"]`);
    const target = page.locator(`.crm-live-lane[data-stage-id="${fixture.stageId}"]`);
    const dragResponse = page.waitForResponse((response) => response.url().includes('/crm/workspace/commands/') && response.request().method() === 'POST');
    await card.dragTo(target);
    assert.equal((await dragResponse).status(), 200);
    await page.reload();
    const data = await json(fixture.data);
    const persisted = data.deals.find((deal) => deal.id === dealId);
    assert.equal(persisted.stage_id, fixture.stageId);
    assert.equal(persisted.amount, null);
    await open();
    assert.equal(await page.locator('[data-command-form="edit"] [name="amount"]').inputValue(), '');
    assert.equal(await page.locator('[data-command-form="edit"] [name="object_ref"]').inputValue(), 'pool:' + fixture.poolId);
    await page.setViewportSize({width: 390, height: 844});
    await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
    const reach = page.locator('[data-command-form="edit"] [name="expected_decision_at"]');
    await reach.scrollIntoViewIfNeeded();
    const bounds = await reach.boundingBox();
    assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= 391, 'Mobile field fits the viewport');
    const color = await reach.evaluate((node) => getComputedStyle(node).color);
    assert.ok(color && color !== 'rgba(0, 0, 0, 0)');
    await page.locator('[data-command-form="edit"] button[type="submit"]').scrollIntoViewIfNeeded();
    assert.ok(await page.locator('[data-command-form="edit"] button[type="submit"]').isVisible());
    assert.equal(await page.evaluate(() => window.bootstrap?.Tooltip?.VERSION), '5.2.3');
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error.stack || String(error)); process.exitCode = 1; });
