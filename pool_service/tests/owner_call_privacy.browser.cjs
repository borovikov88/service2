const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const {readFileSync}=require('node:fs');
(async()=>{
 let input='';for await(const chunk of process.stdin)input+=chunk;const f=JSON.parse(input);
 assert.match(f.base,/^http:\/\/(127\.0\.0\.1|localhost):\d+$/);
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:390,height:844},serviceWorkers:'block'});
  await context.addCookies([{name:f.cookieName,value:f.ownerCookie,url:f.base}]);
  await context.route('**/*',route=>{
   const url=route.request().url();
   if(url==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js')return route.fulfill({contentType:'application/javascript',body:readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'),'utf8')});
   if(url.includes('bootstrap@5.2.3/dist/css/bootstrap.min.css'))return route.fulfill({contentType:'text/css',body:readFileSync(require.resolve('bootstrap/dist/css/bootstrap.min.css'),'utf8')});
   return new URL(url).origin===f.base?route.continue():route.abort();
  });
  const page=await context.newPage();page.setDefaultTimeout(10000);
  await page.goto(f.base+f.card+'?tab=contacts');
  const form=page.locator('[data-owner-personal-peer]');
  assert.match(await form.locator('[name=privacy_revision]').inputValue(),/^[a-f0-9]{64}$/);
  assert.equal(await form.locator('[name=enabled]').inputValue(),'1');
  let result=await Promise.all([page.waitForNavigation(),form.getByRole('button').click()]);assert.equal(result[0].status(),200);
  await page.reload();assert.equal(await form.locator('[name=enabled]').inputValue(),'0');
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  result=await Promise.all([page.waitForNavigation(),form.getByRole('button').click()]);assert.equal(result[0].status(),200);
  await page.reload();assert.equal(await form.locator('[name=enabled]').inputValue(),'1');
  await context.clearCookies();await context.addCookies([{name:f.cookieName,value:f.staffCookie,url:f.base}]);
  await page.goto(f.base+f.card+'?tab=contacts');assert.equal(await page.locator('[data-owner-personal-peer]').count(),0);
  console.log('Owner-only personal-peer form/reload/remove/mobile and staff non-disclosure PASS');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
