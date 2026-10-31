const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const {readFileSync}=require('node:fs');
(async()=>{
 let raw='';for await(const chunk of process.stdin)raw+=chunk;const f=JSON.parse(raw);
 assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
 const browser=await chromium.launch({headless:true});
 try {
 const context=await browser.newContext({viewport:{width:1440,height:950},serviceWorkers:'block'});
 const cookie=value=>({name:f.sessionCookieName,value,url:f.base});
 await context.addCookies([cookie(f.senderCookie)]);
 const bootstrap=readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'),'utf8');
 await context.route('**/*',r=>r.request().url()==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js'?r.fulfill({status:200,contentType:'application/javascript',body:bootstrap}):new URL(r.request().url()).origin===f.base?r.continue():r.abort());
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const preview=async()=>{
   await page.goto(f.base+f.path);await page.getByRole('heading',{name:'Передача сервисной заявки',exact:true}).waitFor();
   await page.locator(`[name=task_id][value="${f.task}"]`).check();
   await page.locator('[name=document_note]').fill('Synthetic checked documents');
   await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'Проверить пакет',exact:true}).click()]);
   await page.locator('[data-handoff-preview]').waitFor();
 };
 await preview();await page.locator('[data-handoff-preview]').getByRole('link',{name:'Отмена',exact:true}).click();
 assert.equal(await page.getByRole('button',{name:'Принять пакет',exact:true}).count(),0);
 await preview();const form=page.locator('[data-handoff-preview] form');
 await form.locator('[name=evidence]').fill('Synthetic sender confirmation');await form.locator('[name=confirm]').check();
 const submit=form.getByRole('button',{name:'Передать пакет',exact:true});await submit.focus();
 await Promise.all([page.waitForNavigation(),page.keyboard.press('Enter')]);
 assert.match(await page.locator('[data-handoff]').innerText(),/Ожидает приёмки/);
 assert.equal(await page.getByRole('button',{name:'Принять пакет',exact:true}).count(),0);
 await page.screenshot({path:'/tmp/crm-handoff-desktop.png',fullPage:true});
 await context.clearCookies();await context.addCookies([cookie(f.recipientCookie)]);await page.reload();
 await page.getByRole('button',{name:'Принять пакет',exact:true}).waitFor();
 await page.setViewportSize({width:390,height:844});await page.evaluate(()=>document.documentElement.setAttribute('data-theme','dark'));
 await page.locator('[name=evidence]').fill('Synthetic recipient checked agreed scope');
 await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'Принять пакет',exact:true}).click()]);
 await page.evaluate(()=>document.documentElement.setAttribute('data-theme','dark'));
 assert.match(await page.locator('[data-handoff]').innerText(),/Пакет принят/);
 await page.getByText('История передачи',{exact:true}).click();
 assert.match(await page.locator('[data-handoff]').innerText(),/Synthetic recipient checked agreed scope/);
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
 await page.screenshot({path:'/tmp/crm-handoff-mobile-dark.png',fullPage:true});
 await page.reload();assert.equal(await page.getByRole('button',{name:'Принять пакет',exact:true}).count(),0);
 assert.deepEqual(errors,[]);console.log('handoff cancel/keyboard submit/distinct recipient/mobile/reload PASS');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
