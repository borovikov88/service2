const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  let input = ''; for await (const chunk of process.stdin) input += chunk;
  const f = JSON.parse(input);
  assert.match(f.base, /^http:\/\/(localhost|127\.0\.0\.1):\d+$/);
  const browser = await chromium.launch({headless: true, executablePath: process.env.CRM_ROLE_CHROMIUM || undefined});
  try {
    const context = await browser.newContext({viewport: {width: 390, height: 844}, serviceWorkers: 'block'});
    await context.addCookies([{name: f.cookieName, value: f.cookie, url: f.base}]);
    await context.route('**/*', r => new URL(r.request().url()).origin === f.base ? r.continue() : r.abort());
    const page = await context.newPage(); page.setDefaultTimeout(12000);
    await page.goto(f.base + f.url);
    const preview = async () => {
      await page.locator('[name=roles][value=service]').uncheck();
      await page.locator('[name=roles][value=manager]').check();
      await page.locator('[name=reviewed]').check();
      await Promise.all([page.waitForNavigation(), page.getByRole('button', {name: 'Проверить изменения'}).click()]);
      await page.getByRole('heading', {name: 'Проверьте результат'}).waitFor();
    };
    await preview();
    assert.ok(await page.locator('[name=call_visibility_ack]').isVisible());
    // Cancel must only navigate, never submit the signed proposal.
    await Promise.all([page.waitForNavigation(), page.getByRole('link', {name: 'Отменить', exact: true}).click()]);
    assert.ok(await page.locator('[name=roles][value=service]').isChecked());
    await preview();
    const before = page.url();
    await page.getByRole('button', {name: 'Подтвердить изменение прав'}).click();
    assert.equal(page.url(), before);
    assert.equal(await page.locator('[name=call_visibility_ack]').evaluate(e => e.validity.valueMissing), true);
    await page.locator('[name=call_visibility_ack]').check();
    const fields = await page.locator('form:has([name=confirmation])').evaluate(form => Array.from(new FormData(form).entries()));
    await Promise.all([page.waitForNavigation(), page.getByRole('button', {name: 'Подтвердить изменение прав'}).click()]);
    assert.ok(await page.locator('[name=roles][value=manager]').isChecked());
    // Lost-response replay must not create a second audit/change.
    const replay = await page.evaluate(async fields => (await fetch(location.href, {method: 'POST', body: new URLSearchParams(fields)})).status, fields);
    assert.equal(replay, 409);
    await page.reload();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    if (process.env.CRM_ROLE_SCREENSHOT) await page.screenshot({path: process.env.CRM_ROLE_SCREENSHOT, fullPage: true});
    await page.setViewportSize({width: 1280, height: 900}); await page.reload();
    assert.ok(await page.locator('[name=roles][value=manager]').isChecked());
    console.log('Preview, cancel, required acknowledgment, apply, stale replay, mobile and desktop PASS');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
