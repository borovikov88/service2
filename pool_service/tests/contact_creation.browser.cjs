/* Synthetic local fixture only; all remote requests except pinned local Bootstrap are blocked. */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const f = JSON.parse(input);
  assert.match(f.base, /^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const browser = await chromium.launch({headless: true});
  let phase = 'directory';
  let releaseLeadCandidates;
  const leadCandidatesReady = new Promise(resolve => { releaseLeadCandidates = resolve; });
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1080}, serviceWorkers: 'block'});
    await context.addCookies([{name: f.sessionCookieName, value: f.sessionCookie, url: f.base}]);
    const bootstrap = readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'), 'utf8');
    const directoryResponses = [];
    await context.route('**/*', async route => {
      if (route.request().url() === 'https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js') return route.fulfill({contentType: 'application/javascript', body: bootstrap});
      const url = new URL(route.request().url());
      if (url.origin === f.base && url.pathname === f.responsiblesPath && url.searchParams.get('lead_id') === String(f.leadId)) {
        // Reproduce a slow candidate response without timing-dependent sleeps.
        await leadCandidatesReady;
        return route.continue();
      }
      if (route.request().method() === 'POST' && new URL(route.request().url()).pathname === f.createPath) {
        // Read the local result before the application follows its success URL.
        // Navigation can release Chromium's response body immediately afterward.
        const response = await route.fetch();
        directoryResponses.push({status: response.status(), body: await response.json()});
        return route.fulfill({response});
      }
      return new URL(route.request().url()).origin === f.base ? route.continue() : route.abort();
    });
    const page = await context.newPage(), errors = [], writes = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      const path = new URL(request.url()).pathname;
      if (request.method() === 'POST' && [f.createPath, f.dealCommandPath, f.leadCommandPath].includes(path)) writes.push(path);
    });
    page.on('dialog', dialog => dialog.accept());
    const noOverflow = async dialog => {
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Page must not overflow horizontally');
      assert.ok(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth + 1), 'Dialog must not overflow horizontally');
    };
    const fillCustomer = async (scope, prefix, phoneSuffix) => {
      await scope.getByRole('combobox', {name: 'Контакт', exact: true}).selectOption('new');
      await scope.getByLabel('Имя контакта', {exact: true}).fill(`Synthetic ${prefix} person`);
      await scope.getByLabel('Телефон', {exact: true}).fill('+7999000' + phoneSuffix);
      await scope.getByLabel('Email', {exact: true}).fill(prefix.toLowerCase() + '@example.test');
      await scope.getByRole('combobox', {name: 'Категория контакта', exact: true}).selectOption('landlord');
      await scope.getByRole('combobox', {name: 'Компания', exact: true}).selectOption('new');
      await scope.getByLabel('Название компании', {exact: true}).fill(`Synthetic ${prefix} company`);
      await scope.getByLabel('Телефон компании', {exact: true}).fill('+7999001' + phoneSuffix);
      await scope.getByLabel('Email компании', {exact: true}).fill(prefix.toLowerCase() + '-company@example.test');
    };
    const labels = scope => scope.locator('.crm-customer-editor .customer-field-label').allTextContents();
    const responseFor = path => page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === path);
    const waitForResponsible = async form => {
      const responsible = form.locator('[name="responsible_id"]');
      await responsible.locator('option[value="' + f.actorId + '"]').waitFor({state: 'attached'});
      assert.equal(await responsible.inputValue(), String(f.actorId), 'The current manager must be selected after candidates load');
    };
    const saveDirectory = async dialog => {
      const responseIndex = directoryResponses.length;
      // Saving reloads the current directory URL. Waiting for that URL alone
      // resolves against the old document and can race the next navigation.
      const [saved] = await Promise.all([
        responseFor(f.createPath),
        page.waitForEvent('load'),
        dialog.getByRole('button', {name: 'Сохранить', exact: true}).click(),
      ]);
      const captured = directoryResponses[responseIndex];
      assert.equal(saved.status(), 200, JSON.stringify(captured?.body));
      assert.equal(captured.status, 200);
      const result = captured.body;
      assert.equal(new URL(page.url()).pathname, f.directory);
      return result;
    };

    await page.goto(f.base + f.directory);
    await page.getByRole('link', {name: 'Создать контакт', exact: true}).click();
    let directoryDialog = page.locator('.contact-create-dialog');
    await directoryDialog.waitFor({state: 'visible'});
    assert.equal(new URL(page.url()).pathname, f.directory, 'Directory creation must open in its dialog');
    await directoryDialog.getByLabel('Имя контакта', {exact: true}).fill('Synthetic cancelled directory person');
    await directoryDialog.getByRole('button', {name: 'Отмена', exact: true}).click();
    await directoryDialog.waitFor({state: 'detached'});
    assert.equal(writes.length, 0, 'Cancelling directory creation must not save');
    await page.getByRole('link', {name: 'Создать контакт', exact: true}).click();
    directoryDialog = page.locator('.contact-create-dialog');
    await directoryDialog.waitFor({state: 'visible'});
    assert.equal(await directoryDialog.getByLabel('Имя контакта', {exact: true}).inputValue(), '');
    await fillCustomer(directoryDialog, 'Directory', '1101');
    const sharedLabels = await labels(directoryDialog);
    await noOverflow(directoryDialog);
    if (process.env.CRM_SYNTHETIC_SCREENSHOTS) await page.screenshot({path: process.env.CRM_SYNTHETIC_SCREENSHOTS + '/contact-directory-unified.png'});
    const directoryResult = await saveDirectory(directoryDialog);
    assert.equal(directoryResult.contact.name, 'Synthetic Directory person');
    assert.equal(directoryResult.company.name, 'Synthetic Directory company');
    assert.deepEqual(directoryResult.contact.categories, ['landlord']);
    assert.equal(writes.length, 1);

    phase = 'directory-existing-match';
    await page.getByRole('link', {name: 'Создать контакт', exact: true}).click();
    directoryDialog = page.locator('.contact-create-dialog');
    await directoryDialog.getByLabel('Имя контакта', {exact: true}).fill('Synthetic Directory person');
    await directoryDialog.locator('.customer-matches .customer-result', {hasText: 'Synthetic Directory person'}).click();
    assert.equal(await directoryDialog.getByRole('combobox', {name: 'Контакт', exact: true}).inputValue(), 'existing');
    assert.equal(await directoryDialog.getByLabel('Найти контакт', {exact: true}).inputValue(), 'Synthetic Directory person');
    await directoryDialog.getByRole('button', {name: 'Отмена', exact: true}).click();
    await directoryDialog.waitFor({state: 'detached'});
    assert.equal(writes.length, 1, 'Selecting an existing match must remain a draft until saving');

    phase = 'company-only';
    await page.getByRole('link', {name: 'Создать компанию', exact: true}).click();
    directoryDialog = page.locator('.contact-create-dialog');
    await directoryDialog.waitFor({state: 'visible'});
    assert.equal(await directoryDialog.getByRole('combobox', {name: 'Контакт', exact: true}).inputValue(), 'none');
    assert.equal(await directoryDialog.getByLabel('Имя контакта', {exact: true}).isVisible(), false);
    await directoryDialog.getByLabel('Название компании', {exact: true}).fill('Synthetic directory company only');
    const companyResult = await saveDirectory(directoryDialog);
    assert.equal(companyResult.contact, null, 'A company must not require a fictitious person');
    assert.equal(companyResult.company.name, 'Synthetic directory company only');
    assert.equal(writes.length, 2);

    phase = 'deal';
    await page.goto(f.base + f.deals);
    await page.locator('#crm-live-create').click();
    let dealForm = page.locator('[data-command-form="create"]');
    await dealForm.getByRole('button', {name: 'Создать контакт или компанию', exact: true}).click();
    await dealForm.getByLabel('Имя контакта', {exact: true}).fill('Synthetic cancelled deal person');
    await page.locator('#crm-live-dialog').getByRole('button', {name: 'Закрыть карточку', exact: true}).click();
    assert.equal(writes.length, 2, 'Closing a deal draft must not create its person');
    await page.locator('#crm-live-create').click();
    dealForm = page.locator('[data-command-form="create"]');
    await dealForm.locator('[name="title"]').fill('Synthetic unified deal');
    await dealForm.getByRole('button', {name: 'Создать контакт или компанию', exact: true}).click();
    assert.equal(await dealForm.getByLabel('Имя контакта', {exact: true}).inputValue(), '');
    await dealForm.getByLabel('Имя контакта', {exact: true}).fill('Synthetic Directory person');
    await dealForm.locator('[name="next_title"]').fill('Synthetic unified next action');
    await dealForm.locator('[name="due_at"]').fill('2026-12-01T11:00');
    await waitForResponsible(dealForm);
    const duplicateResponse = responseFor(f.dealCommandPath);
    await dealForm.getByRole('button', {name: 'Создать сделку', exact: true}).click();
    const rejected = await duplicateResponse;
    assert.equal(rejected.status(), 400, await rejected.text());
    await page.locator('#crm-live-dialog-operations .is-failed').waitFor();
    assert.equal(await dealForm.locator('[name="client_search"]').isDisabled(), true, 'Hidden legacy client cannot be re-enabled after a failed save');
    assert.equal(await dealForm.getByLabel('Название компании', {exact: true}).isDisabled(), true, 'Inactive company fields must remain disabled after a failed save');
    assert.equal(await dealForm.getByLabel('Найти контакт', {exact: true}).isDisabled(), true, 'The hidden existing-contact search must stay disabled in new mode');
    await page.locator('#crm-live-dialog-operations').getByRole('button', {name: 'Понятно', exact: true}).click();
    assert.equal(writes.length, 3);
    await fillCustomer(dealForm, 'Deal', '1102');
    assert.deepEqual(await labels(dealForm), sharedLabels, 'Directory and deal must present the same identity fields');
    await dealForm.locator('[name="next_title"]').fill('Synthetic unified next action');
    await dealForm.locator('[name="due_at"]').fill('2026-12-01T11:00');
    assert.equal(writes.length, 3);
    const dealResponse = responseFor(f.dealCommandPath);
    await dealForm.getByRole('button', {name: 'Создать сделку', exact: true}).click();
    const savedDeal = await dealResponse;
    assert.equal(savedDeal.status(), 200, await savedDeal.text());
    const dealResult = await savedDeal.json();
    assert.equal(dealResult.deal.contact.name, 'Synthetic Deal person');
    assert.equal(dealResult.deal.company.name, 'Synthetic Deal company');
    assert.ok(savedDeal.request().postDataJSON().changes.customer);
    assert.equal(writes.length, 4);

    phase = 'lead-mobile';
    await page.setViewportSize({width: 390, height: 844});
    await page.emulateMedia({colorScheme: 'dark'});
    await page.goto(f.base + f.leads + '&lead=' + f.leadId);
    const leadDialog = page.locator('#lead-dialog');
    await leadDialog.getByRole('button', {name: 'Создать сделку', exact: true}).click();
    const leadForm = leadDialog.locator('form');
    await fillCustomer(leadForm, 'Lead', '1103');
    assert.deepEqual(await labels(leadForm), sharedLabels, 'Lead conversion must use the same identity fields');
    assert.equal(await leadForm.getByLabel('Сохранить открытый следующий шаг', {exact: true}).isChecked(), true);
    await noOverflow(leadDialog);
    assert.equal(writes.length, 4);
    if (process.env.CRM_SYNTHETIC_SCREENSHOTS) await page.screenshot({path: process.env.CRM_SYNTHETIC_SCREENSHOTS + '/contact-lead-mobile-unified.png'});
    const responsible = leadForm.locator('[name="responsible_id"]');
    assert.equal(await responsible.inputValue(), '');
    assert.equal(await responsible.evaluate(node => node.validity.valueMissing), true);
    await leadForm.getByRole('button', {name: 'Подтвердить создание сделки', exact: true}).click();
    assert.equal(writes.length, 4, 'A pending required responsible must not submit a partial conversion');
    releaseLeadCandidates();
    await waitForResponsible(leadForm);
    const leadResponse = responseFor(f.leadCommandPath);
    await leadForm.getByRole('button', {name: 'Подтвердить создание сделки', exact: true}).click();
    const converted = await leadResponse;
    assert.equal(converted.status(), 200);
    const destination = new URL(f.deals, f.base);
    await page.waitForURL(url => url.pathname === destination.pathname &&
      url.searchParams.has('deal') && url.searchParams.get('org_id') === String(f.orgId));
    const convertedDeal = page.locator('#crm-live-dialog');
    await convertedDeal.locator('.crm-live-detail-client').getByText('Synthetic Lead company', {exact: true}).waitFor();
    await convertedDeal.locator('.crm-live-detail-contact').getByText('Synthetic Lead person', {exact: true}).waitFor();
    await page.locator('#crm-live-content[aria-busy="false"]').waitFor();
    assert.equal(writes.length, 5);
    assert.deepEqual(errors, []);
  } catch (error) {
    console.error('Unified editor browser phase: ' + phase);
    throw error;
  } finally { releaseLeadCandidates(); await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
