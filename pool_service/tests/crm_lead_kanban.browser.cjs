/* Actual browser, real local Django commands; all fixture data are synthetic. */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');
(async () => {
  let input=''; for await (const chunk of process.stdin) input+=chunk;
  const f=JSON.parse(input);
  assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const bootstrap=readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'),'utf8');
  const browser=await chromium.launch({headless:true});
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000},hasTouch:true,serviceWorkers:'block'});
    await context.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
    await context.route('**/*',route=>{
      const url=route.request().url();
      if(url==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js') return route.fulfill({status:200,contentType:'application/javascript',body:bootstrap});
      return new URL(url).origin===f.base ? route.continue() : route.abort();
    });
    const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    const card=()=>page.locator(`[data-lead-id="${f.leadId}"]`);
    const inColumn=async key=>{await page.locator(`[data-column="${key}"] [data-lead-id="${f.leadId}"]`).waitFor();};
    const confirm=async(next=false)=>{
      await page.locator('#lead-dialog button[type=submit]').waitFor();
      if(await page.getByLabel('Причина перемещения',{exact:true}).count()) await page.getByLabel('Причина перемещения',{exact:true}).fill('Synthetic explicit manager decision');
      if(next){await page.getByLabel('Следующий шаг',{exact:true}).fill('Synthetic actual next step');await page.locator('#lead-dialog input[type=datetime-local]').fill('2026-11-20T12:00');}
      await page.locator('#lead-dialog button[type=submit]').click();
    };
    const close=async()=>{
      await page.locator('#lead-close').click();
      await page.locator('#lead-dialog').waitFor({state:'hidden'});
      await page.waitForURL(url=>!url.searchParams.has('lead'));
    };
    const noModal=async()=>assert.equal(await page.locator('#lead-dialog').evaluate(node=>node.open),false,'Reload must keep the closed card closed');
    await page.goto(f.base+f.workspace);await inColumn('unparsed');
    assert.deepEqual(await page.locator('.lead-column h2').allTextContents(),['Не разобраны · 1','В работе · 0','Завершённые · 0','Спам · 0','Удалённые · 0']);
    const sent=[];let lose=true;
    await context.route('**'+f.command+'*',async route=>{
      if(route.request().method()!=='POST')return route.continue();
      sent.push(route.request().postData());
      if(lose){lose=false;const response=await route.fetch();assert.equal(response.status(),200);return route.abort();}
      return route.continue();
    });
    await card().dragTo(page.locator('[data-column=working] h2'));await confirm(true);
    await page.locator('#lead-operations').getByRole('button',{name:'Проверить результат',exact:true}).waitFor();await close();
    await page.getByRole('button',{name:'Проверить результат',exact:true}).click();
    await inColumn('working');assert.equal(sent[0],sent[1]);
    await page.reload();await inColumn('working');await noModal();
    await card().dragTo(page.locator('[data-column=spam] h2'));await confirm();await inColumn('spam');await close();
    await page.reload();await inColumn('spam');await noModal();
    // Keyboard/touch movement lives in the opened card, not an ellipsis menu.
    assert.equal(await card().locator('summary').count(),0);
    await card().locator('button').first().focus();await page.keyboard.press('Enter');
    await page.locator('.lead-detail-moves summary').click();
    await page.locator('.lead-detail-moves').getByRole('button',{name:'Не разобраны',exact:true}).click();
    await confirm();await inColumn('unparsed');await close();
    await card().dragTo(page.locator('[data-column=working] h2'));await confirm(true);await inColumn('working');await close();
    await card().dragTo(page.locator('[data-column=deleted] h2'));await confirm();await inColumn('deleted');await close();
    await card().dragTo(page.locator('[data-column=unparsed] h2'));await confirm();await inColumn('unparsed');await close();
    await page.locator('#lead-search').fill('No matching inquiry');
    await page.waitForURL(url=>url.searchParams.get('q')==='No matching inquiry');
    await page.waitForFunction(()=>document.querySelector('#lead-count').textContent==='Найдено: 0');
    await page.locator('#lead-search').fill('Synthetic browser');await inColumn('unparsed');
    await page.setViewportSize({width:390,height:844});await page.emulateMedia({colorScheme:'dark'});
    await page.evaluate(()=>document.documentElement.setAttribute('data-theme','dark'));
    await card().locator('button').first().tap();await page.locator('.lead-detail-moves summary').tap();await page.locator('.lead-detail-moves').getByRole('button',{name:'В работе',exact:true}).tap();
    await page.getByLabel('Следующий шаг',{exact:true}).waitFor({state:'visible'});assert.ok(await page.getByLabel('Следующий шаг',{exact:true}).isVisible());await close();
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'390px page must not overflow outside the scrollable board');
    await page.reload();await inColumn('unparsed');await noModal();
    await page.setViewportSize({width:1440,height:1000});await card().dragTo(page.locator('[data-column=history] h2'));
    await page.getByText('Лид будет завершён без создания сделки. Открытый следующий шаг отменится; сделки, выполнение и оплата не изменятся.').waitFor();
    assert.equal(await page.getByLabel('Причина перемещения',{exact:true}).count(),0);await confirm();await inColumn('history');await close();assert.deepEqual(errors,[]);
    console.log('Lead kanban: real drag, immutable replay, reload, spam/trash restore, live search, keyboard, 390px and dark passed');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
