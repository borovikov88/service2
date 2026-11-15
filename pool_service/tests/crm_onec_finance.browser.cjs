/* Real local Django API and Chromium. Fixture contains only synthetic customers/orders. */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  let input = ''; for await (const chunk of process.stdin) input += chunk;
  const f = JSON.parse(input);
  assert.match(f.base, /^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const browser = await chromium.launch({headless:true});
  let phase = 'setup';
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000},hasTouch:true,serviceWorkers:'block'});
    await context.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
    const bootstrap = readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'));
    await context.route('**/*', route => {
      if (route.request().url() === 'https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js') return route.fulfill({contentType:'application/javascript',body:bootstrap});
      return new URL(route.request().url()).origin === f.base ? route.continue() : route.abort();
    });
    const page = await context.newPage(), errors = [], writes = [];
    page.setDefaultTimeout(15000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (request.method() === 'POST') writes.push(request); });
    const dialog = page.locator('#crm-live-dialog');
    let currentUrls;
    const finance = () => dialog.locator('.crm-onec-finance');
    const formatPercent = value => new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(Number(value))+'%';
    const noOverflow = async () => {
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1), 'Page must fit the viewport');
      assert.ok(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth+1), 'Deal drawer must fit the viewport');
    };
    const open = async row => {
      const url = new URL(f.workspace, f.base); url.searchParams.set('deal', row.dealId);
      const response = await page.goto(url.href);
      assert.equal(response.status(),200);
      await finance().waitFor();
      const data = JSON.parse(await page.locator('#crm-live-bootstrap').textContent()); currentUrls = data.urls; return data;
    };
    const choose = async (row, refreshNeeded = false) => {
      await finance().locator('.crm-onec-binding').filter({has:page.locator('summary',{hasText:'Привязать заказ'})}).locator('summary').click();
      const form = finance().locator('[data-command-form="bind_order"]');
      const beforeTyping = writes.length;
      const refresh = form.getByRole('button',{name:'Найти в 1С',exact:true});
      assert.equal(await refresh.isDisabled(),true,'Exact source search requires a non-empty number');
      const candidatePath = new URL(currentUrls.order_candidates,f.base).pathname;
      const cached = page.waitForResponse(response => response.request().method() === 'GET' && new URL(response.url()).pathname === candidatePath && new URL(response.url()).searchParams.get('q') === row.orderNumber);
      await form.getByRole('searchbox',{name:'Найти заказ клиента',exact:true}).fill(row.orderNumber);
      const cachedResponse = await cached;
      assert.equal(cachedResponse.status(),200);
      assert.equal(writes.length,beforeTyping,'Typing searches saved orders without POST or a live 1C read');
      if (refreshNeeded) {
        assert.equal((await cachedResponse.json()).orders.length,0,'Old unlinked order must start outside the saved order list');
        const refreshPath = new URL(currentUrls.order_refresh,f.base).pathname;
        const requested = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === refreshPath);
        await refresh.click();
        const response = await requested;
        assert.equal(response.status(),200,await response.text());
        assert.deepEqual(response.request().postDataJSON(),{deal_id:row.dealId,q:row.orderNumber});
        const found = (await response.json()).orders.find(order => order.number === row.orderNumber);
        assert.ok(found,'Explicit source lookup must return the requested old order'); row.orderId = found.id;
        assert.equal(writes.length,beforeTyping+1,'One explicit lookup must make one source-refresh POST');
        assert.equal(await form.locator('.crm-onec-order-selected').isVisible(),false,'Refreshing saved data must not select or bind an order automatically');
      }
      await form.locator('.crm-onec-order-option').filter({hasText:'Заказ № '+row.orderNumber}).click();
      await form.locator('.crm-onec-order-selected').waitFor();
      return form;
    };
    const save = async (form, command) => {
      const waiting = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === command);
      await form.locator('button[type=submit]').click();
      const response = await waiting;
      assert.equal(response.status(),200,await response.text());
      return {data:await response.json(),posted:response.request().postDataJSON()};
    };
    for (const [index,row] of f.cases.entries()) {
      phase = 'case '+index+' initial';
      await page.setViewportSize({width:1440,height:1000});
      const bootstrap = await open(row), command = new URL(bootstrap.urls.command,f.base).pathname;
      const before = writes.length;
      assert.equal(await finance().getAttribute('data-finance-state'),'unlinked');
      assert.equal(await finance().getByRole('progressbar').count(),0,'Unlinked must not display invented zero percent');
      let form = await choose(row,Boolean(row.requiresRefresh));
      const readWrites = row.requiresRefresh ? 1 : 0;
      assert.equal(await form.getByLabel('Постоянное обслуживание',{exact:true}).isChecked(),row.recurring,'Service defaults must preserve a continuing deal');
      assert.equal(writes.length,before+readWrites,'Selection must not bind an order');
      await form.getByRole('button',{name:'Отмена',exact:true}).click();
      await form.waitFor({state:'hidden'}); await finance().waitFor();
      assert.equal(writes.length,before+readWrites,'Cancel must not send any POST');
      await page.reload(); await finance().waitFor();
      assert.equal(await finance().getAttribute('data-finance-state'),'unlinked');

      phase = 'case '+index+' bind';
      form = await choose(row);
      await form.getByLabel('Подтверждаю связь выбранного заказа со сделкой',{exact:true}).check();
      const saved = await save(form,command);
      assert.equal(saved.posted.command_type,'deal.bind_order');
      assert.deepEqual(saved.posted.changes,{order_id:row.orderId,recurring:row.recurring});
      assert.equal(saved.data.deal.outcome,row.expectedOutcome);
      assert.equal(saved.data.deal.onec_finance.order.id,row.orderId);
      assert.equal(writes.length,before+readWrites+1);
      await page.reload(); await finance().locator('.crm-onec-order').waitFor();
      assert.ok((await finance().innerText()).includes(row.orderNumber));
      for (const [key,percent] of [['completion',row.completionPercent],['payment',row.paymentPercent]]) {
        const metric = finance().locator(`[data-finance-metric="${key}"]`);
        assert.equal(await metric.locator('.crm-onec-metric-heading strong').innerText(),formatPercent(percent));
        assert.equal(await metric.getByRole('progressbar').getAttribute('aria-valuenow'),String(Math.min(100,Number(percent))));
      }
      if (row.recurring) assert.ok((await finance().innerText()).includes('Постоянное обслуживание:'));
      if (row.expectedOutcome === 'won') assert.ok((await finance().innerText()).includes('Сделка завершена автоматически:'));
      await page.setViewportSize({width:390,height:844});
      await page.evaluate(()=>document.documentElement.dataset.theme='dark');
      await noOverflow();
      if(process.env.CRM_SYNTHETIC_SCREENSHOTS)await page.screenshot({path:process.env.CRM_SYNTHETIC_SCREENSHOTS+'/crm-onec-finance-'+index+'.png',fullPage:false});

      if (row.recurring) {
        phase = 'case '+index+' unbind';
        const part = finance().locator('.crm-onec-binding').filter({has:page.locator('summary',{hasText:'Отвязать заказ'})});
        await part.locator('summary').click();
        const unlink = part.locator('[data-command-form="unbind_order"]');
        await unlink.getByLabel('Причина отвязки',{exact:true}).fill('Synthetic corrected order selection');
        const removed = await save(unlink,command);
        assert.equal(removed.posted.command_type,'deal.unbind_order');
        assert.equal(removed.data.deal.onec_finance.order,null);
        await page.reload(); await finance().waitFor();
        assert.equal(await finance().getAttribute('data-finance-state'),'unlinked');
        assert.equal(writes.length,before+readWrites+2);
      }
    }
    assert.deepEqual(errors,[]);
    console.log('CRM 1C finance: order search/cancel/bind, recurring and one-off, progress, reload, mobile and unlink passed');
  } catch(error) { console.error('CRM 1C browser phase:',phase); throw error; }
  finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1});
