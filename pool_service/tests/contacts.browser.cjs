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
  assert.equal(await page.locator('[data-client-row=companies]').count(),50);
  assert.equal(await page.locator('[data-client-row=companies]').filter({hasText:'Synthetic Company 52'}).count(),0);
  await page.locator('[data-contact-pager=companies]').getByRole('button',{name:'Далее'}).click();
  await page.waitForFunction(()=>document.getElementById('contacts-directory').getAttribute('aria-busy')==='false');
  assert.equal(await page.locator('[data-client-row=companies]').count(),3);
  await page.reload();assert.equal(await page.locator('[data-client-row=companies]').count(),3);
  await page.locator('[data-client-search=companies]').fill('Linked Person');
  await page.waitForFunction(()=>document.querySelector('[data-client-search-count=companies]').textContent==='Найдено: 1');
  assert.equal(await page.locator('[data-client-row=companies]:visible').count(),1);
  await page.goBack();
  await page.waitForFunction(()=>document.getElementById('contacts-directory').getAttribute('aria-busy')==='false' && document.querySelectorAll('[data-client-row=companies]').length===50);
  await page.goForward();
  await page.waitForFunction(()=>document.getElementById('contacts-directory').getAttribute('aria-busy')==='false' && document.querySelectorAll('[data-client-row=companies]').length===1);
  // Deliberately ignore abort for one old response: generation must still win.
  await page.evaluate(()=>{
    const original=window.fetch;window.__originalContactFetch=original;
    window.fetch=async (url,options)=>{
      if(new URL(url,location.href).searchParams.get('companies_q')==='Company 00'){
        const response=await original(url,{...options,signal:undefined});window.__oldContactStarted=true;
        await new Promise(resolve=>{window.__releaseOldContact=resolve;});window.__oldContactReleased=true;return response;
      }
      return original(url,options);
    };
  });
  await page.locator('[data-client-search=companies]').fill('Company 00');
  await page.waitForFunction(()=>window.__oldContactStarted===true);
  await page.locator('[data-client-search=companies]').fill('Company 52');
  await page.waitForFunction(()=>document.getElementById('contacts-directory').getAttribute('aria-busy')==='false' && document.querySelector('[data-client-search=companies]').value==='Company 52' && document.querySelectorAll('[data-client-row=companies]').length===1);
  await page.evaluate(()=>window.__releaseOldContact());
  await page.waitForFunction(()=>window.__oldContactReleased===true);
  assert.ok((await page.locator('[data-client-row=companies]').textContent()).includes('Synthetic Company 52'));
  await page.evaluate(()=>{window.fetch=window.__originalContactFetch;});
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
  await page.goto(f.base+f.personCard+'?tab=contacts');
  const phoneForm=page.locator('[data-person-phone-form]');
  assert.match(await phoneForm.locator('[name=contacts_revision]').inputValue(),/^[a-f0-9]{64}$/);
  await phoneForm.getByLabel('Номер телефона').fill('+7 900 111 22 33');
  await phoneForm.getByLabel('Тип телефона').selectOption('personal');
  let submitted=await Promise.all([page.waitForNavigation(),phoneForm.getByRole('button').click()]);
  assert.equal(submitted[0].status(),200,await page.locator('body').innerText());
  await page.reload(); assert.match(await page.locator('#client-contacts').innerText(), /Личный/);
  await phoneForm.getByLabel('Номер телефона').fill('+7 900 444 55 66');
  await phoneForm.getByLabel('Тип телефона').selectOption('work');
  submitted=await Promise.all([page.waitForNavigation(),phoneForm.getByRole('button').click()]);
  assert.equal(submitted[0].status(),200,await page.locator('body').innerText());
  await page.reload(); assert.equal(await page.locator('#client-contacts a[href^="tel:"]').count(),2);
  const personForm=page.locator('[data-person-user-form]'); await personForm.locator('select').selectOption(String(f.userId));
  submitted=await Promise.all([page.waitForNavigation(),personForm.getByRole('button').click()]);
  assert.equal(submitted[0].status(),200,await page.locator('body').innerText()); await page.reload();
  assert.equal(await page.locator('[data-person-user-form]').count(),0);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Contact forms fit mobile');
  assert.deepEqual(errors,[]);console.log('Contacts real UI: role add/remove, linked-person search, 50-row pagination, reload, tabs, mobile/dark PASS');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
