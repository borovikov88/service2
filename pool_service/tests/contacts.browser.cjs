const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {chromium}=require('playwright');
(async()=>{
 let input='';for await(const part of process.stdin)input+=part;
 const f=JSON.parse(input);assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000},serviceWorkers:'block'});
  await context.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
  await context.route('**/*',route=>{
   const url=route.request().url();
   if(url==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js')return route.fulfill({contentType:'application/javascript',body:readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'),'utf8')});
   if(url.includes('bootstrap@5.2.3/dist/css/bootstrap.min.css'))return route.fulfill({contentType:'text/css',body:readFileSync(require.resolve('bootstrap/dist/css/bootstrap.min.css'),'utf8')});
   return new URL(url).origin===f.base?route.continue():route.abort();
  });
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  page.setDefaultTimeout(10000);
  await page.goto(f.base+f.list);
  assert.deepEqual(await page.locator('#clients-tab button').allTextContents(),['Компании','Поставщики','Физ лица']);
  assert.equal(await page.locator('[data-client-row=companies]:visible').count(),50);
  await page.locator('[data-contact-pager=companies]').getByRole('button',{name:'Далее'}).click();
  assert.equal(await page.locator('[data-client-row=companies]:visible').count(),3);
  await page.reload();assert.equal(await page.locator('[data-client-row=companies]:visible').count(),3);
  await page.locator('[data-client-search=companies]').fill('Linked Person');
  await page.waitForFunction(()=>document.querySelector('[data-client-search-count=companies]').textContent==='Найдено: 1');
  assert.equal(await page.locator('[data-client-row=companies]:visible').count(),1);
  await page.locator('#suppliers-tab').click();await page.locator('#suppliers-pane.active').waitFor();
  assert.ok((await page.locator('#suppliers-pane').textContent()).includes('Поставщики ещё не отмечены'));
  await page.goto(f.base+f.card);
  await Promise.all([page.waitForURL(url=>url.searchParams.get('tab')==='overview'),page.locator('[data-supplier-form] button').click()]);
  await page.locator('[data-supplier-badge]').waitFor();
  await page.goto(f.base+f.list+'?clients_tab=suppliers');await page.locator('#suppliers-pane.active').waitFor();
  assert.equal(await page.locator('[data-client-row=suppliers]:visible').count(),1);
  await page.locator('#supplier-search').fill('No match');
  await page.waitForFunction(()=>document.querySelector('[data-client-search-count=suppliers]').textContent==='Найдено: 0');
  await page.setViewportSize({width:390,height:844});await page.emulateMedia({colorScheme:'dark'});
  await page.reload();await page.locator('#suppliers-pane.active').waitFor();
  assert.equal(await page.locator('[data-client-row=suppliers]:visible').count(),0);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Mobile page must not overflow');
  await page.goto(f.base+f.card);await page.locator('[data-supplier-form] button').click();
  await page.waitForFunction(()=>!document.querySelector('[data-supplier-badge]'));
  assert.deepEqual(errors,[]);console.log('Contacts real UI: role add/remove, linked-person search, 50-row pagination, reload, tabs, mobile/dark PASS');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
