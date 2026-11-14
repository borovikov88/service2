const assert=require('node:assert/strict');
const {chromium}=require('playwright');
(async()=>{
 let raw='';for await(const c of process.stdin)raw+=c;const f=JSON.parse(raw);
 assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
 const browser=await chromium.launch({headless:true});
 try{
 const context=await browser.newContext({viewport:{width:1440,height:950},serviceWorkers:'block'});
 await context.addCookies([{name:f.sessionCookieName,value:f.senderCookie,url:f.base}]);
 await context.route('**/*',r=>new URL(r.request().url()).origin===f.base?r.continue():r.abort());
 const page=await context.newPage();
 await page.goto(f.base+f.path);
 if(f.denied){assert.match(await page.locator('[data-construction]').innerText(),/Персональное право запуска не настроено/);assert.equal(await page.locator('button[name=action]').count(),0);return;}
 const preview=async()=>{
 await page.goto(f.base+f.path);
 await page.locator(`[name=task_id][value="${f.task}"]`).check();
 await page.locator('[name=recipient_id]').selectOption(String(f.recipient));
 await page.locator('[name=planned_start]').fill('2026-11-01');await page.locator('[name=planned_end]').fill('2026-11-08');
 await page.locator('[name=document_note]').fill('Synthetic checked construction documents');
 await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'Проверить запуск',exact:true}).click()]);
 await page.locator('[data-construction-preview]').waitFor();
 };
 await preview();await page.locator('[data-construction-preview]').getByRole('link',{name:'Отмена',exact:true}).click();
 assert.equal(await page.locator('[data-launch-record]').count(),0);
 await preview();await page.screenshot({path:'/tmp/crm-construction-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.evaluate(()=>document.documentElement.setAttribute('data-theme','dark'));
 let sent;page.on('request',r=>{if(r.method()==='POST'&&new URLSearchParams(r.postData()).get('action')==='launch')sent=r.postData();});
 const form=page.locator('[data-construction-preview] form');await form.locator('[name=evidence]').fill('Synthetic explicit first launch');await form.locator('[name=confirm]').check();
 await form.getByRole('button',{name:'Передать в работу',exact:true}).focus();
 await Promise.all([page.waitForNavigation(),page.keyboard.press('Enter')]);
 await page.locator('[data-launch-record]').waitFor();assert.ok(sent);
 const replay=await page.request.post(f.base+f.path,{data:sent,headers:{'Content-Type':'application/x-www-form-urlencoded'}});assert.equal(replay.status(),200);
 await page.getByText('История запуска',{exact:true}).click();
 await page.evaluate(()=>document.documentElement.setAttribute('data-theme','dark'));
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
 assert.equal(await page.locator('.quote-table td').nth(2).evaluate(el=>getComputedStyle(el).whiteSpace),'nowrap');
 await page.screenshot({path:'/tmp/crm-construction-mobile-dark.png',fullPage:true});
 await page.reload();await page.locator('[data-launch-record]').waitFor();
 assert.equal(await page.getByRole('button',{name:'Передать в работу',exact:true}).count(),0);
 console.log('construction cancel/keyboard/mobile/replay/history PASS');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
