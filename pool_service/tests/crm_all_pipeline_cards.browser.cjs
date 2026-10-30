const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');
(async () => {
  let input=''; for await (const part of process.stdin) input+=part;
  const f=JSON.parse(input); assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const browser=await chromium.launch({headless:true});
  try {
    const c=await browser.newContext({serviceWorkers:'block'});
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
      assert.equal(await form.locator(`option[value="${row.final}"]`).evaluate(n=>n.disabled),true, JSON.stringify({row, options:await form.locator("option").evaluateAll(nodes=>nodes.map(n=>({value:n.value,disabled:n.disabled,text:n.textContent})))}));
      // Playwright can programmatically select a native disabled option.
      // Verify both its DOM state above and the independent submit guard.
      await form.evaluate(f=>{f.dataset.trace="";f.addEventListener("submit",()=>f.dataset.trace+="submit;");f.addEventListener("click",e=>f.dataset.trace+="click:"+e.target.tagName+";");});
      let commands=0; const count=r=>{if(r.url().includes('/commands/')&&r.method()==='POST')commands++;};
      page.on('request',count);
      await form.locator('[name="stage_id"]').selectOption(String(row.final));
      await form.locator('button[type="submit"]').click();
      await form.locator('.crm-live-form-error:not([hidden])').waitFor();
      assert.equal(commands,0);page.off('request',count);
      if(width===1440){
        await form.locator('[name="stage_id"]').selectOption(String(row.target));
        let r;try{[r]=await Promise.all([page.waitForResponse(r=>r.url().includes('/commands/')&&r.request().method()==='POST',{timeout:5000}),form.locator('button[type="submit"]').click({timeout:4000})]);}catch(error){throw new Error(JSON.stringify({row,width,errors,error:error.message,form:await form.evaluate(f=>({trace:f.dataset.trace,values:Array.from(f.elements).map(n=>({name:n.name,value:n.value,disabled:n.disabled,valid:n.validity?.valid,message:n.validationMessage})),text:f.textContent}))}));}
        assert.equal(r.status(),200);const body=await r.json();
        assert.equal(body.deal.stage_id,row.target);assert.equal(body.deal.outcome,'open');
        assert.equal(body.deal.amount,'1234567.00');
        await page.waitForFunction(id=>document.querySelector('[data-command-form="transition"] [name="stage_id"]')?.value===String(id),row.target);
      }
    }
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
