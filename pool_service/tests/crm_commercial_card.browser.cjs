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
    const documents = [];
    const assets = [];
    const failedRequests = [];
    let phase = 'initial navigation';
    let expectedDealId = null;
    const safeLocation = (value) => {
      const url = new URL(value);
      const keys = ['org_id', 'pipeline', 'page', 'q', 'owner', 'view', 'attention', 'outcome', 'object', 'stage', 'specialization', 'decision_from', 'decision_to'];
      return {path: url.pathname, filters: Object.fromEntries(keys.filter((key) => url.searchParams.has(key)).map((key) => [key, url.searchParams.get(key).slice(0, 80)]))};
    };
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('response', (response) => {
      if (response.request().isNavigationRequest() && response.frame() === page.mainFrame()) {
        documents.push({...safeLocation(response.url()), status: response.status()});
        if (documents.length > 6) documents.shift();
      } else if (response.url() === bootstrapURL || new URL(response.url()).pathname.endsWith('/crm_live/workspace.js')) {
        assets.push({...safeLocation(response.url()), status: response.status()});
        if (assets.length > 6) assets.shift();
      }
    });
    page.on('requestfailed', (request) => {
      if (new URL(request.url()).origin === fixture.base) {
        failedRequests.push({...safeLocation(request.url()), error: String(request.failure()?.errorText || '').slice(0, 160)});
        if (failedRequests.length > 6) failedRequests.shift();
      }
    });
    const waitForSettledCard = async (deal) => {
      // A successful response precedes queue reconciliation and the list reload.
      // Require their visible result before starting another navigation/action.
      await page.waitForFunction(({id, stageId}) => {
        const content = document.querySelector('#crm-live-content');
        const lane = content?.querySelector(`.crm-live-lane[data-stage-id="${stageId}"]`);
        const card = lane?.querySelector(`.crm-live-card[data-deal-id="${id}"]`);
        return content?.getAttribute('aria-busy') === 'false' && card
          && !card.classList.contains('is-pending')
          && document.querySelectorAll('#crm-live-operations .crm-live-operation').length === 0;
      }, {id: deal.id, stageId: deal.stage_id});
    };
    const reloadWorkspace = async () => {
      const response = await page.reload();
      assert.ok(response, 'Workspace reload must navigate to a new document');
      assert.equal(response.status(), 200, 'Workspace reload must return a usable document');
      await page.waitForFunction(() => document.querySelector('#crm-live')?.dataset.mounted === 'true'
        && document.querySelector('#crm-live-content')?.getAttribute('aria-busy') === 'false');
    };
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
      await waitForSettledCard(result.deal);
      return result;
    };
    try {
      const bootstrapResponsePromise = page.waitForResponse((response) => response.url() === bootstrapURL);
      await page.goto(fixture.base + fixture.workspace);
      const bootstrapResponse = await bootstrapResponsePromise;
      assert.equal(bootstrapResponse.status(), 200);
      assert.match(bootstrapResponse.headers()['content-type'], /application\/javascript/);
      assert.equal(await bootstrapResponse.text(), bootstrapBundle);
      assert.equal(await page.evaluate(() => window.bootstrap?.Tooltip?.VERSION), '5.2.3');
      phase = 'create';
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
      expectedDealId = dealId;
      phase = 'commercial edit';
      let edit = page.locator('[data-command-form="edit"]');
      await edit.waitFor();
      await edit.locator('[name="work_subject"]').fill('Browser synthetic installation');
      await edit.locator('[name="specialization"]').fill('hammam');
      await edit.locator('[name="fulfillment_kind"]').selectOption('construction');
      await edit.locator('[name="object_ref"]').selectOption('planned');
      await edit.locator('[name="planned_object_name"]').fill('Browser planned synthetic hammam');
      await edit.locator('[name="planned_object_address"]').fill('Browser planned synthetic address');
      await edit.locator('[name="amount"]').fill('0.00');
      assert.equal(await edit.locator('[name="currency"]').getAttribute('type'), 'hidden');
      assert.equal(await edit.locator('[name="currency"]').inputValue(), 'RUB');
      await edit.locator('[name="amount_basis"]').selectOption('indicative');
      await edit.locator('[name="expected_decision_at"]').fill('2026-11-02T12:30');
      const planned = await save(edit);
      assert.equal(planned.deal.object.kind, 'planned');
      assert.equal(planned.deal.amount, '0.00');
      phase = 'totals on an empty page';
      await page.goto(fixture.base + fixture.workspace + '&page=999');
      await page.waitForFunction(() => document.querySelector('#crm-live-content')?.getAttribute('aria-busy') === 'false');
      const totalLane = page.locator(`.crm-live-lane[data-stage-id="${planned.deal.stage_id}"]`);
      assert.equal(await totalLane.locator('.crm-live-lane-count').textContent(), '1');
      assert.equal(await totalLane.locator('.crm-live-stage-total').textContent(), '0.00 ₽');
      assert.equal(await totalLane.locator('.crm-live-lane-empty').textContent(), 'На этой странице нет сделок этого этапа');
      phase = 'reload planned object';
      await page.goto(fixture.base + fixture.workspace);
      await reloadWorkspace();
      await open();
      edit = page.locator('[data-command-form="edit"]');
      assert.equal(await edit.locator('[name="amount"]').inputValue(), '0.00');
      assert.equal(await edit.locator('[name="planned_object_name"]').inputValue(), 'Browser planned synthetic hammam');
      assert.equal(await edit.locator('[name="expected_decision_at"]').inputValue(), '2026-11-02T12:30');
      phase = 'existing object edit';
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
      phase = 'drag and settle';
      const dragResponse = page.waitForResponse((response) => response.url().includes('/crm/workspace/commands/') && response.request().method() === 'POST');
      await card.dragTo(target);
      const movedResponse = await dragResponse;
      assert.equal(movedResponse.status(), 200);
      const moved = await movedResponse.json();
      assert.equal(moved.deal.id, dealId);
      assert.equal(moved.deal.stage_id, fixture.stageId);
      await waitForSettledCard(moved.deal);
      phase = 'reload after drag';
      await reloadWorkspace();
      const data = await json(fixture.data);
      const persisted = data.deals.find((deal) => deal.id === dealId);
      assert.equal(persisted.stage_id, fixture.stageId);
      assert.equal(persisted.amount, null);
      phase = 'open after drag';
      await open();
      assert.equal(await page.locator('[data-command-form="edit"] [name="amount"]').inputValue(), '');
      assert.equal(await page.locator('[data-command-form="edit"] [name="object_ref"]').inputValue(), 'pool:' + fixture.poolId);
      phase = 'mobile dark';
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
    } catch (error) {
      // Synthetic-only bounded diagnostics: no cookies, session, source bodies,
      // response bodies or arbitrary document HTML are collected.
      let dom;
      try {
        dom = await page.evaluate((dealId) => ({
          mounted: document.querySelector('#crm-live')?.dataset.mounted || null,
          busy: document.querySelector('#crm-live-content')?.getAttribute('aria-busy') || null,
          cards: Array.from(document.querySelectorAll('.crm-live-card')).slice(0, 8).map((card) => ({
            id: card.dataset.dealId, stage: card.closest('.crm-live-lane')?.dataset.stageId || null,
            pending: card.classList.contains('is-pending'),
          })),
          expectedCard: Array.from(document.querySelectorAll('.crm-live-card')).some((card) => card.dataset.dealId === String(dealId)),
          expectedTitle: Array.from(document.querySelectorAll('.crm-live-card-open')).some((node) => node.textContent === 'Browser synthetic card'),
          bootstrap: (() => {
            try {
              const data = JSON.parse(document.querySelector('#crm-live-bootstrap')?.textContent || '{}');
              return {pipeline: data.selected_pipeline_id, page: data.page, attention: data.attention,
                expectedCard: (data.deals || []).some((deal) => deal.id === dealId),
                cards: (data.deals || []).slice(0, 8).map((deal) => ({id: deal.id, stage: deal.stage_id, pipeline: deal.pipeline_id}))};
            } catch (_) { return {invalid: true}; }
          })(),
          operations: Array.from(document.querySelectorAll('#crm-live-operations .crm-live-operation')).slice(0, 5).map((node) => node.className),
          dialogOpen: Boolean(document.querySelector('#crm-live-dialog')?.open),
          notice: document.querySelector('#crm-live-notice')?.textContent?.slice(0, 200) || '',
          empty: document.querySelector('.crm-live-empty h2')?.textContent?.slice(0, 120) || '',
        }), expectedDealId);
      } catch (diagnosticError) {
        dom = {unavailable: String(diagnosticError.message).slice(0, 180)};
      }
      console.error('CRM browser lifecycle diagnostics: ' + JSON.stringify({phase, location: safeLocation(page.url()), documents, assets,
        failedRequests, pageerrors: errors.slice(-5).map((message) => message.slice(0, 200)), dom}));
      throw error;
    }
    await context.close();
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error.stack || String(error)); process.exitCode = 1; });
