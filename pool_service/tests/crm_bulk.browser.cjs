/* Actual browser + isolated Django: bulk writes keep single-card authorization. */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {chromium} = require('playwright');
(async () => {
  let input = ''; for await (const chunk of process.stdin) input += chunk;
  const f = JSON.parse(input); assert.match(f.base, /^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const browser = await chromium.launch({headless: true}); let phase = 'setup';
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, serviceWorkers: 'block'});
    await context.addCookies([{name: f.sessionCookieName, value: f.sessionCookie, url: f.base}]);
    const bootstrap = readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'));
    await context.route('**/*', route => route.request().url() === 'https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js'
      ? route.fulfill({contentType: 'application/javascript', body: bootstrap}) : new URL(route.request().url()).origin === f.base ? route.continue() : route.abort());
    const page = await context.newPage(), errors = [], writes = [], detailReads = []; page.setDefaultTimeout(15000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (request.method() === 'POST') writes.push(request);
      if (request.method() === 'GET' && /\/crm\/workspace\/(leads|deals)\/\d+\//.test(new URL(request.url()).pathname)) detailReads.push(request);
    });
    const bar = page.locator('.crm-bulk-bar'), modal = page.locator('.crm-bulk-dialog');
    const check = async ids => { for (const id of ids) await page.locator('input[data-bulk-id="' + id + '"]').check(); };
    const open = async label => { await bar.getByRole('button', {name: label, exact: true}).click(); await modal.getByRole('button', {name: /^Применить к /}).waitFor(); };
    const apply = async count => {
      await modal.getByRole('button', {name: /^Применить к /}).click();
      await modal.locator('.crm-bulk-status').filter({hasText: 'Сохранено: ' + count + '.'}).waitFor();
      await page.waitForFunction(() => !document.querySelector('#crm-live-content[aria-busy=true],#lead-list[aria-busy=true]'));
    };
    const close = () => modal.getByRole('button', {name: 'Закрыть', exact: true}).click();
    const session = async cookie => { await context.clearCookies(); await context.addCookies([{name: f.sessionCookieName, value: cookie, url: f.base}]); };
    phase = 'deal selection, select-all, clear and cancel';
    await page.goto(f.base + f.deals);
    assert.equal(await page.locator('#crm-live .crm-section-nav').count(), 0);
    await page.getByRole('button', {name: 'Список', exact: true}).click();
    await page.locator('.crm-live-table').waitFor();
    assert.equal(await page.getByRole('link', {name: 'Потребности объектов', exact: true}).isVisible(), true);
    await page.getByRole('button', {name: 'Канбан', exact: true}).click(); await page.locator('.crm-live-board').waitFor();
    await Promise.all([page.waitForNavigation({waitUntil: 'load'}), page.getByRole('button', {name: 'Обновить страницу', exact: true}).click()]);
    assert.equal(page.url(), f.base + f.deals);
    await check([f.dealIds[0]]);
    assert.equal(await page.locator('#crm-live-dialog').isVisible(), false);
    await bar.getByRole('button', {name: 'Выбрать все', exact: true}).click();
    assert.equal(await page.locator('input[data-bulk-id]:checked').count(), 3);
    await bar.getByRole('button', {name: 'Снять выделение', exact: true}).click();
    assert.equal(await bar.isVisible(), false);
    const readsBeforeStage = detailReads.length;
    await check(f.dealIds.slice(0, 2)); await open('Изменить стадию');
    assert.equal(detailReads.length, readsBeforeStage, 'Opening the stage form must use the existing board projection');
    await modal.locator('[name=stage]').selectOption(String(f.technicalStage));
    await modal.getByRole('button', {name: 'Отмена', exact: true}).click(); assert.equal(writes.length, 0);
    if (f.screenshotDir) await page.screenshot({path: join(f.screenshotDir, 'crm-bulk-desktop.png')});
    phase = 'bulk deal stage';
    await open('Изменить стадию'); await modal.locator('[name=stage]').selectOption(String(f.technicalStage)); await apply(2); await close();
    assert.equal(writes.length, 1, 'Two stage changes need one HTTP request');
    assert.equal(writes[0].postDataJSON().commands.length, 2);
    assert.equal(detailReads.length, readsBeforeStage, 'Saving the group must not reload each card');
    phase = 'deal assignment with lost response and same-key retry';
    await check(f.dealIds.slice(0, 2)); await open('Сменить ответственного');
    await modal.locator('[name=responsible_id]').selectOption(String(f.recipient));
    await modal.locator('[name=transfer]').check();
    let lost = false;
    const loseResponse = async route => {
      const request = route.request();
      if (request.method() === 'POST' && request.postDataJSON()?.command_type === 'deal.assign' && !lost) {
        lost = true; const response = await route.fetch(); assert.equal(response.status(), 200); await route.abort('failed');
      } else await route.continue();
    };
    await page.route('**/crm/workspace/commands/**', loseResponse);
    await modal.getByRole('button', {name: /^Применить к /}).click();
    await modal.locator('.crm-bulk-status').filter({hasText: 'Требует проверки: 1.'}).waitFor();
    await close(); await page.unroute('**/crm/workspace/commands/**', loseResponse);
    assert.equal(lost, true);
    await page.locator('#crm-live-operations').getByRole('button', {name: 'Проверить / повторить', exact: true}).click();
    await page.locator('#crm-live-operations .crm-live-operation').waitFor({state: 'hidden'});
    await page.waitForFunction(() => document.querySelector('#crm-live-content').getAttribute('aria-busy') === 'false');
    const attempted = writes.filter(request => request.postDataJSON()?.command_type === 'deal.assign');
    assert.equal(attempted.length, 2); assert.deepEqual(attempted[0].postDataJSON(), attempted[1].postDataJSON());
    await check([f.dealIds[1]]); await open('Сменить ответственного'); await modal.locator('[name=responsible_id]').selectOption(String(f.recipient)); await modal.locator('[name=transfer]').check(); await apply(1); await close();
    phase = 'deal failure and mobile';
    await session(f.recipientCookie); await page.goto(f.base + f.deals);
    await page.setViewportSize({width: 390, height: 844}); await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
    await check(f.dealIds.slice(0, 2)); await open('Провалить');
    assert.ok(await modal.evaluate(node => node.scrollWidth <= node.clientWidth + 1));
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    const beforeMissingReason = writes.length;
    assert.equal(await modal.locator('[name=reason]').getAttribute('maxlength'), '500');
    await modal.getByRole('button', {name: /^Применить к /}).click(); assert.equal(writes.length, beforeMissingReason);
    if (f.screenshotDir) await page.screenshot({path: join(f.screenshotDir, 'crm-bulk-mobile.png')});
    await modal.locator('[name=reason]').fill('Synthetic bulk refusal'); await apply(2); await close();
    phase = 'lead selection clears when filter changes';
    await session(f.sessionCookie); await page.setViewportSize({width: 1440, height: 1000}); await page.goto(f.base + f.leads); await check([f.leadIds[0]]);
    await page.locator('#lead-search').fill('bulk lead 1'); await page.waitForFunction(() => document.querySelector('#lead-list').getAttribute('aria-busy') === 'false' && document.querySelectorAll('input[data-bulk-id]').length === 1);
    assert.equal(await bar.isVisible(), false);
    await page.locator('#lead-search').fill(''); await page.waitForFunction(() => document.querySelector('#lead-list').getAttribute('aria-busy') === 'false' && document.querySelectorAll('input[data-bulk-id]').length === 3);
    phase = 'bulk lead stage with one conflicting card';
    const leadReadsBeforeStage = detailReads.length;
    await check(f.leadIds); await open('Изменить стадию'); await modal.locator('[name=stage]').selectOption('unparsed'); await modal.locator('[name=reason]').fill('Synthetic review');
    assert.equal(detailReads.length, leadReadsBeforeStage);
    const reject = async route => {
      const body = route.request().postDataJSON();
      if (route.request().method() === 'POST' && body?.commands?.some(command => command.subject_id === f.leadIds[2])) {
        for (const command of body.commands) if (command.subject_id === f.leadIds[2]) command.expected_revision++;
        const response = await route.fetch({postData: JSON.stringify(body)}); await route.fulfill({response});
      } else await route.continue();
    };
    await page.route('**/crm/workspace/leads/commands/**', reject); await apply(2);
    assert.match(await modal.locator('.crm-bulk-status').innerText(), /Не выполнено: 1/); await close();
    await page.unroute('**/crm/workspace/leads/commands/**', reject);
    await bar.getByRole('button', {name: 'Снять выделение', exact: true}).click();
    phase = 'bulk lead assignment';
    await check(f.leadIds.slice(0, 2)); await open('Сменить ответственного'); await modal.locator('[name=responsible_id]').selectOption(String(f.recipient)); await modal.locator('[name=transfer]').check(); await apply(2); await close();
    phase = 'recipient closes assigned leads';
    await session(f.recipientCookie); await page.goto(f.base + f.leads);
    await check(f.leadIds.slice(0, 2)); await open('Провалить'); await modal.locator('[name=reason]').fill('Synthetic bulk lead loss'); await apply(2); await close();
    assert.deepEqual(errors, []);
    process.stdout.write('CRM_BULK_BROWSER_OK\n');
  } catch (error) { console.error('phase=' + phase); throw error; }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
