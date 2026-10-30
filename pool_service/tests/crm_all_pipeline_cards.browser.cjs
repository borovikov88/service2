const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');
(async () => {
  let input=''; for await (const part of process.stdin) input+=part;
  const f=JSON.parse(input); assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const browser=await chromium.launch({headless:true});
  try {
    const c=await browser.newContext({serviceWorkers:'block',hasTouch:true});
    await c.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
    await c.route('**/*',r=>r.request().url()==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js'
      ? r.fulfill({contentType:'application/javascript',body:readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'),'utf8')})
      : new URL(r.request().url()).origin===f.base?r.continue():r.abort());
    const page=await c.newPage(); const errors=[];page.on("pageerror",e=>errors.push(e.message));
    for (const width of [1440,390]) for (const row of f.rows) {
      await page.setViewportSize({width,height:900});
      await page.goto(`${f.base}/crm/workspace/?org_id=${f.org}&pipeline=${row.pipeline}${width===390?'&view=table':''}`);
      const card=page.locator(`.crm-live-card[data-deal-id="${row.id}"]`);
      await card.waitFor();
      assert.match(await card.textContent(),/1\u00a0234\u00a0567,00 ₽/);
      await card.locator('[data-action="open"]').click();
      const copy=page.locator('#crm-live-dialog-caption .crm-live-copy-link');await copy.waitFor();
      const bounds=await copy.boundingBox();assert.ok(bounds.x>=0&&bounds.x+bounds.width<=width&&bounds.y>=0&&bounds.y<900);
      assert.equal(await copy.textContent(),'Скопировать ссылку');
      const form=page.locator('[data-command-form="transition"]');
      assert.equal(await form.locator(`option[value="${row.target}"]`).evaluate(n=>n.disabled),false);
      assert.equal(await form.locator(`option[value="${row.final}"]`).evaluate(n=>n.disabled),false);
      let commands=0; const count=r=>{if(r.url().includes('/commands/')&&r.method()==='POST')commands++;};page.on('request',count);
      // Keyboard form path opens explicit confirmation, never sends immediately.
      await form.locator('[name="stage_id"]').selectOption(String(width===1440?row.final:row.target));
      if(width===1440){
        await form.locator('button[type="submit"]').focus();await page.keyboard.press('Enter');
        const confirm=page.locator('[data-command-form="transition-confirm"]');await confirm.waitFor();
        assert.equal(commands,0);assert.match(await confirm.textContent(),/Исполнение и оплата не подтверждаются/);
        await page.keyboard.press('Escape');assert.equal(commands,0);
        // Drag to the same final stage must show the same confirmation gate.
        const dt=await page.evaluateHandle(()=>new DataTransfer());await card.dispatchEvent('dragstart',{dataTransfer:dt});
        await page.locator(`.crm-live-lane[data-stage-id="${row.final}"]`).dispatchEvent('drop',{dataTransfer:dt});await card.dispatchEvent('dragend',{dataTransfer:dt});await confirm.waitFor();
        await confirm.locator('[name="confirm_manual_completion"]').check();
        const response=page.waitForResponse(r=>r.url().includes('/commands/')&&r.request().method()==='POST');
        await confirm.locator('button[type="submit"]').click();const r=await response;assert.equal(r.status(),200);const body=await r.json();
        assert.equal(body.deal.stage_id,row.final);assert.equal(body.deal.outcome,'open');assert.equal(body.deal.manual_completion,true);assert.equal(body.deal.amount,'1234567.00');
        await page.getByText('CRM завершена вручную.',{exact:false}).waitFor();
      }else{
        // On mobile the already persisted manual state remains explicit after reload.
        assert.match(await page.locator('#crm-live-dialog-content').textContent(),/CRM завершена вручную/);
        await form.locator('[name="reason"]').fill('Synthetic explicit mobile return');
        let response=page.waitForResponse(r=>r.url().includes('/commands/')&&r.request().method()==='POST');await form.locator('button[type="submit"]').tap();assert.equal((await response).status(),200);
        await page.waitForFunction(id=>document.querySelector('[data-command-form="transition"] [name="stage_id"]')?.value===String(id),row.target);
        await form.locator('[name="stage_id"]').selectOption(String(row.final));await form.locator('button[type="submit"]').tap();
        const confirm=page.locator('[data-command-form="transition-confirm"]');await confirm.waitFor();const beforeConfirm=commands;
        await confirm.locator('button[type="submit"]').tap();assert.equal(commands,beforeConfirm,'Unchecked confirmation must never write');
        await confirm.locator('[name="confirm_manual_completion"]').check();response=page.waitForResponse(r=>r.url().includes('/commands/')&&r.request().method()==='POST');await confirm.locator('button[type="submit"]').tap();assert.equal((await response).status(),200);
        await page.getByText('CRM завершена вручную.',{exact:false}).waitFor();
      }
      if(process.env.CRM_SYNTHETIC_SCREENSHOTS && row===f.rows[0]){await page.emulateMedia({colorScheme:width===390?'dark':'light'});await page.screenshot({path:process.env.CRM_SYNTHETIC_SCREENSHOTS+'/manual-stage-'+width+'.png'});}
      page.off('request',count);
    }
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
