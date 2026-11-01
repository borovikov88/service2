const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const fixture = JSON.parse(input);
  assert.match(fixture.base, /^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({
      viewport: {width: 1440, height: 1000}, serviceWorkers: 'block',
    });
    await context.addCookies([{
      name: fixture.sessionCookieName, value: fixture.sessionCookie, url: fixture.base,
    }]);
    await context.route('**/*', route => {
      const url = route.request().url();
      if (url === 'https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js') {
        return route.fulfill({contentType: 'application/javascript',
          body: readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'), 'utf8')});
      }
      if (url.includes('bootstrap@5.2.3/dist/css/bootstrap.min.css')) {
        return route.fulfill({contentType: 'text/css',
          body: readFileSync(require.resolve('bootstrap/dist/css/bootstrap.min.css'), 'utf8')});
      }
      return new URL(url).origin === fixture.base ? route.continue() : route.abort();
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.setDefaultTimeout(10000);
    const servicesUrl = fixture.base + fixture.url + '?account=' + fixture.servicesId;
    let response = await page.goto(servicesUrl);
    assert.equal(response.status(), 200);
    await page.locator('[data-avito-owner]').waitFor();
    assert.equal(await page.locator('[data-owner-kpi]').count(), 5);
    assert.ok((await page.locator('[data-owner-kpi="views"]').innerText()).includes('—'));
    assert.ok((await page.locator('[data-owner-kpi="contacts"]').innerText()).includes('6'));
    for (const name of ['views', 'contacts', 'cost']) {
      const svg = page.locator('svg[data-owner-chart="' + name + '"]');
      await svg.waitFor();
      assert.equal(await svg.getAttribute('role'), 'img');
      assert.ok((await svg.getAttribute('aria-label') || '').length > 0);
    }
    const data = await page.locator('#avito-owner-chart-data').textContent();
    assert.equal(JSON.parse(data).views[2], null);
    assert.equal(JSON.parse(data).views[4], 0);
    const paths = page.locator('svg[data-owner-chart="views"] polyline');
    assert.ok(await paths.count() >= 2, 'Unknown day must split a line into separate segments');
    const cost = page.locator('svg[data-owner-chart="cost"]');
    const initialCost = await cost.innerHTML();
    for (const key of ['cost_contact', 'spending', 'cost_view']) {
      const button = page.locator('[data-owner-cost="' + key + '"]');
      await button.click();
      assert.equal(await button.getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator('[data-owner-cost][aria-pressed="true"]').count(), 1);
      if (key === 'cost_contact') assert.notEqual(await cost.innerHTML(), initialCost);
    }
    await page.locator('[data-owner-cost="spending"]').focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('[data-owner-cost="spending"]').getAttribute('aria-pressed'), 'true');

    await page.locator('#avito-listings-details > summary').click();
    await page.locator('[data-avito-search]').fill('Synthetic Water');
    await page.waitForFunction(() => {
      return [...document.querySelectorAll('[data-avito-item]')]
        .filter(row => !row.hidden && getComputedStyle(row).display !== 'none').length === 1;
    });
    assert.equal(await page.locator('[data-avito-item]:visible').count(), 1);
    assert.ok(new URL(page.url()).searchParams.get('q').includes('Synthetic Water'));

    await Promise.all([
      page.waitForURL(url => url.searchParams.get('owner_period') === '7'),
      page.locator('[data-owner-period="7"]').click(),
    ]);
    assert.equal(new URL(page.url()).searchParams.get('account'), String(fixture.servicesId));
    await page.locator('.avito-owner-custom > summary').click();
    await page.locator('[name="owner_from"]').fill('2026-01-02');
    await page.locator('[name="owner_to"]').fill('2026-01-06');
    const customForm = page.locator('[name="owner_from"]').locator('xpath=ancestor::form');
    await Promise.all([
      page.waitForURL(url => url.searchParams.get('owner_from') === '2026-01-02'
        && url.searchParams.get('owner_to') === '2026-01-06'),
      customForm.locator('button[type="submit"]').click(),
    ]);
    assert.equal(new URL(page.url()).searchParams.get('owner_period'), 'custom');

    for (const colorScheme of ['dark', 'light']) {
      await page.emulateMedia({colorScheme});
      await page.setViewportSize({width: 390, height: 844});
      await page.reload();
      await page.locator('[data-avito-owner]').waitFor();
      assert.equal(await page.locator('html').getAttribute('data-theme'), colorScheme);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
        'Owner dashboard must fit the mobile viewport');
      assert.equal(await page.locator('[data-owner-kpi]').count(), 5);
      await page.locator('[data-owner-cost="cost_contact"]').click();
      assert.equal(await page.locator('[data-owner-cost="cost_contact"]').getAttribute('aria-pressed'), 'true');
    }

    await page.setViewportSize({width: 1440, height: 1000});
    response = await page.goto(fixture.base + fixture.url + '?account=' + fixture.goodsId);
    assert.equal(response.status(), 200);
    await page.locator('[data-avito-owner]').waitFor();
    assert.ok((await page.locator('[data-owner-kpi="views"]').innerText()).includes('—'));
    assert.ok((await page.locator('[data-owner-kpi="spending"]').innerText()).includes('—'));
    const goodsData = JSON.parse(await page.locator('#avito-owner-chart-data').textContent());
    assert.ok(goodsData.views.every(value => value === null));
    assert.equal(await page.locator('a[href="/avito/1/services-feed/"]').count(), 0);
    assert.equal(await page.locator('svg[data-owner-chart="views"] polyline').count(), 0);
    await page.setViewportSize({width: 390, height: 844});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
      'Goods empty state must also fit mobile');
    assert.deepEqual(errors, []);
    console.log('Avito owner dashboard Chromium: charts, gaps, cost controls, periods, account separation, mobile and themes PASS');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
