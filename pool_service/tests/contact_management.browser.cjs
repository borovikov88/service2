const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {chromium}=require('playwright');
(async()=>{
 let input='';for await(const part of process.stdin)input+=part;
 const f=JSON.parse(input);assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
 const browser=await chromium.launch({headless:true});
 try {
  const context=await browser.newContext({viewport:{width:1440,height:1000},serviceWorkers:'block'});
  await context.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
  await context.route('**/*',route=>{
   const url=route.request().url();
   if(url==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js')return route.fulfill({contentType:'application/javascript',body:readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'),'utf8')});
   if(url.includes('bootstrap@5.2.3/dist/css/bootstrap.min.css'))return route.fulfill({contentType:'text/css',body:readFileSync(require.resolve('bootstrap/dist/css/bootstrap.min.css'),'utf8')});
   return new URL(url).origin===f.base?route.continue():route.abort();
  });
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(12000);
  page.on('response',async response=>{if(response.status()>=400 && new URL(response.url()).origin===f.base) console.error('Synthetic UI response',new URL(response.url()).pathname,response.status(),(await response.text()).slice(0,1000));});
  await page.goto(`${f.base}/clients/${f.a}/?tab=contacts`);
  await page.locator('.contact-phone-edit summary').click();
  let form=page.locator('.contact-phone-edit form');
  await form.locator('[name=phone]').fill('+12025550103');await form.locator('[name=phone_type]').selectOption('work');
  await form.locator('[name=label]').fill('Synthetic office');
  try {await Promise.all([page.waitForNavigation(),form.getByRole('button',{name:'Сохранить номер'}).click()]);}
  catch(error) {throw new Error(error.message + '\n' + JSON.stringify({errors,form:await form.innerText()}));}
  assert.match(await page.locator('[data-phone-manager]').innerText(),/Synthetic office/);
  assert.equal(await page.locator('[data-phone-manager] a[href="tel:+12025550101"]').count(),0);
  await page.locator('.contact-phone-edit summary').click();
  page.once('dialog',dialog=>dialog.accept());
  await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'Удалить номер',exact:true}).click()]);
  assert.match(await page.locator('[data-phone-manager]').innerText(),/Телефон не указан/);
  form=page.locator('.contact-phone-add form');await form.getByLabel('Номер телефона').fill('+12025550104');
  await Promise.all([page.waitForNavigation(),form.getByRole('button').click()]);
  await page.goto(`${f.base}/clients/`);
  await page.locator(`[data-merge-select="${f.a}"]`).check();await page.locator(`[data-merge-select="${f.b}"]`).check();
  assert.match(await page.locator('[data-contact-selected-count]').innerText(),/2/);
  await page.locator('[data-contact-merge]').click();await page.locator('[data-merge-confirm]:enabled').waitFor();
  await page.locator(`input[name=merge_target][value="${f.b}"]`).check();await page.locator('[data-merge-confirm]:enabled').waitFor();
  assert.equal(await page.locator('input[name=merge_target]:checked').inputValue(),String(f.b));
  await page.getByRole('button',{name:'Отмена',exact:true}).click();assert.equal(await page.locator('[data-merge-select]:checked').count(),2);
  await page.locator('[data-contact-merge]').click();await page.locator('[data-merge-confirm]:enabled').waitFor();
  assert.match(await page.locator('.contact-merge-result').innerText(),/2025550102/);
  if(process.env.CONTACTS_SCREENSHOT) await page.screenshot({path:process.env.CONTACTS_SCREENSHOT.replace('.png','-merge.png')});
  await Promise.all([page.waitForNavigation(),page.locator('[data-merge-confirm]').click()]);
  assert.ok(new URL(page.url()).pathname.endsWith(`/clients/${f.a}/`));
  assert.equal(await page.locator('[data-phone-manager] a[href^="tel:"]').count(),2);
  await page.reload();assert.equal(await page.locator('[data-phone-manager] a[href^="tel:"]').count(),2);
  if(process.env.CONTACTS_SCREENSHOT) await page.screenshot({path:process.env.CONTACTS_SCREENSHOT.replace('.png','-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.goto(`${f.base}/clients/?clients_tab=people`);
  await page.locator('#contacts-pane.active').waitFor();
  await page.locator(`[data-merge-select="${f.p}"]`).check();await page.locator(`[data-merge-select="${f.q}"]`).check();
  await page.locator('[data-contact-merge]').click();await page.locator('[data-merge-confirm]:enabled').waitFor();
  assert.ok(await page.evaluate(()=>document.querySelector('dialog').getBoundingClientRect().width<=innerWidth));
  await Promise.all([page.waitForNavigation(),page.locator('[data-merge-confirm]').click()]);
  assert.ok(new URL(page.url()).pathname.endsWith(`/clients/${f.p}/`));
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Contact editor fits mobile');
  assert.deepEqual(errors,[]);
  if(process.env.CONTACTS_SCREENSHOT) await page.screenshot({path:process.env.CONTACTS_SCREENSHOT,fullPage:true});
  console.log('Phone edit/delete/empty/add, merge preview/change survivor/cancel/commit/reload, mobile merge PASS');
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
