const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const {readFileSync}=require('node:fs');
(async()=>{
 let input='';for await(const chunk of process.stdin)input+=chunk;const f=JSON.parse(input);
 assert.match(f.base,/^http:\/\/(127\.0\.0\.1|localhost):\d+$/);
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:390,height:844},serviceWorkers:'block'});
  await context.addCookies([{name:f.cookieName,value:f.cookie,url:f.base}]);
  await context.route('**/*',route=>{
   const url=route.request().url();
   if(url==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js')return route.fulfill({contentType:'application/javascript',body:readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'),'utf8')});
   if(url.includes('bootstrap@5.2.3/dist/css/bootstrap.min.css'))return route.fulfill({contentType:'text/css',body:readFileSync(require.resolve('bootstrap/dist/css/bootstrap.min.css'),'utf8')});
   return new URL(url).origin===f.base?route.continue():route.abort();
  });
  const page=await context.newPage();page.setDefaultTimeout(10000);
  await page.goto(f.base+f.url);
  for(const [key,value] of Object.entries(f.refs))await page.locator(`[data-preview-form] [name=${key}]`).fill(value);
  await Promise.all([page.waitForNavigation(),page.locator('[data-preview-form] button').click()]);
  const fill=async()=>{const form=page.locator('[data-allocation-form]');await form.locator('[name=month]').fill('2026-01');await form.locator('[name=quantity]').fill('0.5');await form.locator('[name=amount]').fill('60');await form.locator('[name=contract_confirm]').check();await form.locator('[name=service_confirm]').check();await form.locator('[name=reason]').fill('Synthetic explicit month');if(await form.locator('[name=reconcile]').count())await form.locator('[name=reconcile]').check();};
  await fill();
  let result=await Promise.all([page.waitForNavigation(),page.locator('[data-allocation-form] button:not([type=button])').click()]);
  assert.equal(result[0].status(),409);assert.match(await page.locator('[role=alert]').first().innerText(),/source_changed/);
  await fill();
  const data=await page.locator('[data-allocation-form]').evaluate(form=>Array.from(new FormData(form).entries()));
  result=await Promise.all([page.waitForNavigation(),page.locator('[data-allocation-form] button:not([type=button])').click()]);assert.equal(result[0].status(),200);
  assert.match(await page.locator('[role=status]').innerText(),/сохранено/);
  // Retry the identical submitted request, as after a lost response: no second version.
  const replay=await page.evaluate(async({url,data})=>{const r=await fetch(url,{method:'POST',body:new URLSearchParams(data)});return r.status;},{url:f.url,data});assert.equal(replay,200);
  await page.reload();assert.match(await page.locator('[data-service-bindings]').innerText(),/manually_allocated/);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.setViewportSize({width:1280,height:900});await page.reload();
  assert.equal(await page.locator('[data-service-bindings] details').count(),1);
  for(const [key,value] of Object.entries(f.refs))await page.locator(`[data-preview-form] [name=${key}]`).fill(value);
  await Promise.all([page.waitForNavigation(),page.locator('[data-preview-form] button').click()]);
  assert.equal(await page.locator('[name=reconcile]').count(),1);await fill();
  result=await Promise.all([page.waitForNavigation(),page.locator('[data-allocation-form] button:not([type=button])').click()]);assert.equal(result[0].status(),200);
  assert.equal(await page.locator('[data-service-bindings] details').count(),2);
  console.log('Fresh source change, explicit resubmit, replay, reload, mobile and desktop PASS');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
