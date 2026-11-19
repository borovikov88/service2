/* Real local Django requests and Chromium; all customer data are synthetic. */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  let input = ''; for await (const chunk of process.stdin) input += chunk;
  const f = JSON.parse(input);
  assert.match(f.base, /^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const browser = await chromium.launch({headless: true});
  let phase = 'setup';
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, hasTouch: true, serviceWorkers: 'block'});
    await context.addCookies([{name: f.sessionCookieName, value: f.sessionCookie, url: f.base}]);
    const bootstrap = readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'), 'utf8');
    await context.route('**/*', route => {
      if (route.request().url() === 'https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js') {
        return route.fulfill({contentType: 'application/javascript', body: bootstrap});
      }
      return new URL(route.request().url()).origin === f.base ? route.continue() : route.abort();
    });
    const page = await context.newPage(), errors = [], writes = [];
    page.setDefaultTimeout(10000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (request.method() === 'POST') writes.push(new URL(request.url()).pathname); });
    const dialog = page.locator('#lead-dialog');
    const card = id => page.locator(`.lead-row[data-lead-id="${id}"]`);
    const button = name => dialog.getByRole('button', {name, exact: true});
    const noOverflow = async () => {
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Page must fit the mobile viewport');
      const size = await dialog.evaluate(node => ({width: node.clientWidth, scroll: node.scrollWidth,
        overflowing: [...node.querySelectorAll('*')].filter(child => child.getBoundingClientRect().right > node.getBoundingClientRect().right + 1)
          .slice(0, 6).map(child => ({tag: child.tagName, className: child.className, width: child.getBoundingClientRect().width}))}));
      assert.ok(size.scroll <= size.width + 1, 'Completion dialog must not overflow horizontally: ' + JSON.stringify(size));
    };
    const fillCustomer = async (form, cancelled = false) => {
      await form.getByRole('combobox', {name: 'Контакт', exact: true}).selectOption(f.companyOnly ? 'none' : 'new');
      if (!f.companyOnly) {
        await form.getByLabel('Имя контакта', {exact: true}).fill(cancelled ? 'Synthetic cancelled person' : f.personName);
        await form.getByLabel('Телефон', {exact: true}).fill('+79990005566');
        await form.getByLabel('Email', {exact: true}).fill('completion-person@example.test');
        await form.getByRole('combobox', {name: 'Категория контакта', exact: true}).selectOption('landlord');
      }
      await form.getByRole('combobox', {name: 'Компания', exact: true}).selectOption('new');
      await form.getByLabel('Название компании', {exact: true}).fill(cancelled ? 'Synthetic cancelled company' : f.companyName);
      await form.getByLabel('Телефон компании', {exact: true}).fill('+79990007788');
      await form.getByLabel('Email компании', {exact: true}).fill('completion-company@example.test');
    };
    const openCompletion = async () => {
      await button('Завершить лид').click();
      await button('Сохранить контакт/компанию').waitFor();
      await button('Завершить без создания').waitFor();
      assert.equal(await dialog.locator('.lead-completion-options [aria-pressed="true"]').count(), 0, 'Finishing a lead must require an explicit outcome choice');
      assert.equal(await dialog.locator('form').count(), 0, 'The result chooser must not assume a deal or create records');
      if (!f.companyOnly) await button('Создать сделку').waitFor();
      else assert.equal(await button('Создать сделку').count(), 0, 'Deal creation must not be offered without permission');
      await button('Сохранить контакт/компанию').click();
      await button('Сохранить и завершить лид').waitFor();
      const form = dialog.locator('form[data-completion-form="customer"]');
      assert.equal(await form.locator('.crm-customer-editor').count(), 1, 'Completion must use the shared customer editor');
      assert.equal(await form.getByLabel('Название сделки', {exact: true}).count(), 0);
      assert.equal(await form.getByLabel('Воронка', {exact: true}).count(), 0);
      assert.equal(await form.getByLabel('Сохранить открытый следующий шаг', {exact: true}).count(), 0);
      return form;
    };
    phase = 'choose and cancel';
    await page.goto(f.base + f.path);
    await card(f.leadId).locator('button').first().click();
    let form = await openCompletion();
    await fillCustomer(form, true);
    assert.equal(writes.length, 0, 'Choosing a result and editing fields must not save');
    await button('Отмена').click();
    await button('Завершить лид').waitFor();
    assert.equal(writes.length, 0, 'Cancelling must not create customers or close the lead');

    phase = 'mobile customer form';
    await page.setViewportSize({width: 390, height: 844});
    await page.emulateMedia({colorScheme: 'dark'});
    form = await openCompletion();
    await form.getByRole('combobox', {name: 'Компания', exact: true}).selectOption('new');
    assert.equal(await form.getByLabel('Название компании', {exact: true}).inputValue(), '', 'Cancelled input must not become saved customer data');
    await fillCustomer(form);
    if (!f.companyOnly) {
      phase = 'switch outcome without losing customer draft';
      await button('К вариантам завершения').click();
      await button('Создать сделку, контакт и компанию').click();
      const dealForm = dialog.locator('[data-completion-panel="deal_contact_company"] form');
      await dealForm.getByLabel('Имя контакта', {exact: true}).waitFor({state: 'visible'});
      assert.equal(await dealForm.getByLabel('Имя контакта', {exact: true}).inputValue(), f.personName);
      assert.equal(await dealForm.getByLabel('Название компании', {exact: true}).inputValue(), f.companyName);
      assert.equal(await dealForm.getByRole('combobox', {name: 'Категория контакта', exact: true}).inputValue(), 'landlord');
      await button('К вариантам завершения').click();
      await button('Сохранить контакт/компанию').click();
      await form.waitFor();
      assert.equal(await form.getByLabel('Имя контакта', {exact: true}).inputValue(), f.personName);
      assert.equal(await form.getByLabel('Название компании', {exact: true}).inputValue(), f.companyName);
      assert.equal(await form.getByRole('combobox', {name: 'Категория контакта', exact: true}).inputValue(), 'landlord');
      assert.equal(writes.length, 0, 'Switching completion modes must preserve the draft without saving');
    }
    await noOverflow();
    if (process.env.CRM_SYNTHETIC_SCREENSHOTS) await page.screenshot({path: process.env.CRM_SYNTHETIC_SCREENSHOTS + '/crm-customer-completion-mobile' + (f.companyOnly ? '-company' : '-both') + '.png', fullPage: true});
    assert.equal(writes.length, 0);
    phase = 'save without deal';
    const pending = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === f.command);
    await button('Сохранить и завершить лид').tap();
    const response = await pending;
    assert.equal(response.status(), 200, await response.text());
    const saved = await response.json(), posted = response.request().postDataJSON();
    assert.equal(posted.command_type, 'lead.complete_customer');
    assert.equal(posted.changes.customer.company.name, f.companyName);
    assert.equal(posted.changes.customer.contact?.name || null, f.companyOnly ? null : f.personName);
    assert.equal(Object.hasOwn(posted.changes, 'deals'), false);
    assert.ok(posted.changes.expected_task_version, 'Closing the open task must carry its version');
    assert.equal(saved.lead.status, 'closed');
    assert.equal(saved.lead.result, 'customer_saved');
    assert.equal(saved.lead.company.name, f.companyName);
    assert.equal(saved.lead.contact?.name || null, f.companyOnly ? null : f.personName);
    assert.equal(writes.length, 1, 'One explicit confirmation must make exactly one command');

    phase = 'saved result and reload';
    await dialog.waitFor({state: 'hidden'});
    assert.ok(!new URL(page.url()).searchParams.has('lead'));
    await page.goto(f.base + f.path + '&lead=' + f.leadId);
    await dialog.waitFor({state: 'visible'});
    await dialog.getByRole('link', {name: f.companyName, exact: true}).first().waitFor();
    assert.equal(await button('Завершить лид').count(), 0, 'A completed lead cannot be completed again');
    assert.equal(await button('Сохранить контакт/компанию').count(), 0);
    const summary = dialog.locator('.lead-completion-result');
    await summary.waitFor();
    assert.equal((await summary.innerText()).trim(), f.companyOnly ? 'Компания сохранена. Сделка не создавалась.' : 'Контакт и компания сохранены. Сделка не создавалась.');
    await noOverflow();
    const records = [saved.lead.company, ...(!f.companyOnly ? [saved.lead.contact] : [])];
    for (const record of records) {
      const link = dialog.getByRole('link', {name: record.name, exact: true}).first();
      assert.equal(await link.getAttribute('href'), record.url);
      const opened = page.waitForResponse(res => res.request().isNavigationRequest() && new URL(res.url()).pathname === new URL(record.url, f.base).pathname);
      await link.click();
      assert.equal((await opened).status(), 200, 'Saved record links must open a real contact/company card');
      await page.goto(f.base + f.path + '&lead=' + f.leadId);
      await dialog.getByRole('link', {name: f.companyName, exact: true}).first().waitFor();
    }

    phase = 'archived lead guard';
    await page.goto(f.base + f.path + '&lead=' + f.archivedId);
    await dialog.getByRole('heading', {name: 'Synthetic archived completion request', exact: true}).waitFor();
    assert.equal(await button('Завершить лид').count(), 0, 'Deleted leads must require restoration before customer-only completion');
    assert.equal(await button('Сохранить контакт/компанию').count(), 0);
    assert.equal(writes.length, 1);
    assert.deepEqual(errors, []);
    console.log('Lead customer completion: chooser, cancel without writes, shared editor, 390px, saved linked records, no deal, closed/archive guards passed');
  } catch (error) {
    console.error('Customer completion browser phase:', phase);
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
