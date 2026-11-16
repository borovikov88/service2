/* Real Chromium + isolated Django. Only synthetic contacts and conversations. */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {chromium} = require('playwright');
(async () => {
  let input = ''; for await (const chunk of process.stdin) input += chunk;
  const f = JSON.parse(input); assert.match(f.base, /^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const browser = await chromium.launch({headless:true}); let phase = 'deal';
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1080}, serviceWorkers:'block'});
    await context.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
    const bootstrap = readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'));
    await context.route('**/*', route => route.request().url() === 'https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js'
      ? route.fulfill({contentType:'application/javascript',body:bootstrap}) : new URL(route.request().url()).origin === f.base ? route.continue() : route.abort());
    const page = await context.newPage(), errors = [], writes = []; page.setDefaultTimeout(15000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {if(request.method()==='POST') writes.push(request.postDataJSON());});
    const responseFor = kind => page.waitForResponse(response => response.request().method()==='POST' && response.request().postDataJSON()?.command_type === kind);
    const assertSaved = async (kind, click) => {const pending=responseFor(kind); await click(); const response=await pending; assert.equal(response.status(),200,await response.text());};
    const noOverflow = async dialog => {assert.ok(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth+1), 'Card must fit viewport'); assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1));};
    await page.goto(f.base+f.dealUrl);
    let dialog = page.locator('#crm-live-dialog'), activity = dialog.locator('.crm-card-activity');
    await activity.getByText('Synthetic incoming:',{exact:false}).waitFor();
    await activity.getByText('Synthetic outgoing:',{exact:false}).waitFor();
    await activity.getByRole('heading',{name:'Пропущенный звонок',exact:true}).waitFor();
    await dialog.locator('.crm-card-money summary').click();
    const amount = dialog.locator('[data-command-form=amount]');
    await amount.getByLabel('Сумма сделки, ₽',{exact:true}).fill('125 000,50');
    await amount.locator('[name=amount_basis]').selectOption('agreed');
    await assertSaved('deal.edit',()=>amount.getByRole('button',{name:'Сохранить сумму',exact:true}).click());
    await dialog.locator('.crm-card-money-value').filter({hasText:'125\u00a0000,50'}).waitFor();
    await activity.getByText('Сумма: Не указано → 125\u00a0000,50',{exact:true}).waitFor();
    phase = 'comment with lost response';
    let drop = true;
    const loseResponse = async route => {
      if (drop && route.request().method()==='POST' && route.request().postDataJSON()?.command_type==='deal.comment') {drop=false; await route.fetch(); return route.abort();}
      return route.continue();
    };
    await page.route('**/crm/workspace/commands/**',loseResponse);
    await activity.getByLabel('Комментарий',{exact:true}).fill('Согласовали график. <script>plain text</script>');
    await activity.getByRole('button',{name:'Добавить комментарий',exact:true}).click();
    const retry=dialog.locator('#crm-live-dialog-operations').getByRole('button',{name:'Проверить / повторить',exact:true});
    await retry.waitFor(); await retry.click();
    await activity.locator('.crm-activity-body').filter({hasText:'Согласовали график. <script>plain text</script>'}).waitFor();
    const attempts=writes.filter(row=>row?.command_type==='deal.comment');
    assert.equal(attempts.length,2); assert.deepEqual(attempts[0],attempts[1]);
    await page.unroute('**/crm/workspace/commands/**',loseResponse);
    await noOverflow(dialog);
    if(f.screenshotDir) await page.screenshot({path:join(f.screenshotDir,'crm-card-deal-desktop.png')});
    await activity.getByRole('button',{name:'Комментарии',exact:true}).click();
    await activity.locator('.crm-activity-event').waitFor();
    assert.equal(await activity.locator('.crm-activity-event').count(),1);
    assert.equal(await activity.locator('script').count(),0);
    await page.reload(); await dialog.locator('.crm-card-money-value').filter({hasText:'125\u00a0000,50'}).waitFor();
    await activity.locator('.crm-activity-body').filter({hasText:'Согласовали график.'}).waitFor();
    phase='lead'; await page.goto(f.base+f.leadUrl);
    dialog=page.locator('#lead-dialog'); activity=dialog.locator('.crm-card-activity');
    await activity.getByText('Synthetic outgoing:',{exact:false}).waitFor();
    await dialog.locator('.crm-card-money summary').click();
    const budget=dialog.locator('.crm-card-money');
    await budget.getByLabel('Сумма / от, ₽',{exact:true}).fill('35 000,75');
    await assertSaved('lead.edit',()=>budget.getByRole('button',{name:'Сохранить сумму',exact:true}).click());
    await dialog.locator('.crm-card-money-value').filter({hasText:'35\u00a0000,75'}).waitFor();
    await activity.getByLabel('Комментарий',{exact:true}).fill('Перезвонить после согласования бюджета.');
    await assertSaved('lead.comment',()=>activity.getByRole('button',{name:'Добавить комментарий',exact:true}).click());
    await activity.locator('.crm-activity-body').filter({hasText:'Перезвонить после согласования бюджета.'}).waitFor();
    await page.reload(); await activity.locator('.crm-activity-body').filter({hasText:'Перезвонить после согласования бюджета.'}).waitFor();
    await noOverflow(dialog);
    if(f.screenshotDir) await page.screenshot({path:join(f.screenshotDir,'crm-card-lead-desktop.png')});
    phase='mobile dark'; await page.setViewportSize({width:390,height:844});
    await page.evaluate(()=>document.documentElement.setAttribute('data-theme','dark'));
    await activity.getByRole('button',{name:'Комментарии',exact:true}).click();
    await activity.locator('.crm-activity-body').filter({hasText:'Перезвонить после согласования бюджета.'}).waitFor();
    await activity.scrollIntoViewIfNeeded(); await noOverflow(dialog);
    if(f.screenshotDir) await page.screenshot({path:join(f.screenshotDir,'crm-card-lead-mobile.png')});
    assert.deepEqual(errors,[]); assert.equal(writes.filter(row=>row?.command_type==='lead.comment').length,1);
  } catch(error) {console.error('Card activity phase: '+phase);throw error;}
  finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
