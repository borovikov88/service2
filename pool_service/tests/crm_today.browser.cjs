/* Real local-only acceptance of the shared Today filter in both workspace views. */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const fixture = JSON.parse(input);
  assert.match(fixture.base, /^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const browser = await chromium.launch({headless: true,
    ...(process.env.CRM_TEST_CHROMIUM_PATH ? {executablePath: process.env.CRM_TEST_CHROMIUM_PATH} : {})});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, timezoneId: 'America/Los_Angeles', serviceWorkers: 'block'});
    await context.addCookies([{name: fixture.sessionCookieName, value: fixture.sessionCookie, url: fixture.base}]);
    assert.equal(require('bootstrap/package.json').version, '5.2.3');
    const bundle = readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'), 'utf8');
    await context.route('**/*', route => {
      const url = route.request().url();
      if (url === 'https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js') {
        return route.fulfill({status: 200, contentType: 'application/javascript', body: bundle});
      }
      return new URL(url).origin === fixture.base ? route.continue() : route.abort();
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const settled = () => page.waitForFunction(() => document.querySelector('#crm-live')?.dataset.mounted === 'true'
      && document.querySelector('#crm-live-content')?.getAttribute('aria-busy') === 'false');
    const assertToday = async () => {
      await settled();
      assert.equal(await page.locator('[data-attention="today"]').getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator('.crm-live-card').count(), 2);
      assert.match(await page.locator('#crm-live-result-status').innerText(), /по фильтру: 2/);
      assert.match(await page.locator('#crm-live-content').innerText(), /Контроль ожидания/);
      assert.doesNotMatch(await page.locator('#crm-live-content').innerText(), /Synthetic tomorrow/);
      assert.equal(new URL(page.url()).searchParams.get('attention'), 'today');
    };
    await page.goto(fixture.base + fixture.workspace);
    await settled();
    assert.equal(await page.locator('.crm-live-card').count(), 3);
    await page.locator('[data-attention="today"]').click();
    await assertToday();
    await page.reload();
    await assertToday();
    // Build a same-document history entry, then exercise the real popstate path.
    await page.evaluate(() => {
      const url = new URL(location.href); url.searchParams.set('attention', 'all');
      history.pushState({}, '', url);
      dispatchEvent(new PopStateEvent('popstate'));
    });
    await settled();
    assert.equal(await page.locator('.crm-live-card').count(), 3);
    await page.goBack();
    await assertToday();
    await page.locator('[data-view="table"]').click();
    await assertToday();
    await page.setViewportSize({width: 390, height: 844});
    await page.emulateMedia({colorScheme: 'dark'});
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
    await page.reload();
    await assertToday();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    await page.locator('[data-attention="all"]').click();
    await settled();
    assert.equal(await page.locator('.crm-live-card').count(), 3);
    await page.locator('[data-attention="today"]').focus();
    await page.keyboard.press('Enter');
    await assertToday();
    assert.deepEqual(errors, []);
    console.log('Today desktop/table/mobile/dark/keyboard/reload: PASS');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
