/* Actual browser, real local Django commands; all fixture data are synthetic. */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');
const {verifyBoardDragScroll} = require('./crm_board_drag_scroll_case.cjs');
(async () => {
  let input=''; for await (const chunk of process.stdin) input+=chunk;
  const f=JSON.parse(input);
  assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const bootstrap=readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'),'utf8');
  const browser=await chromium.launch({headless:true});
  try {
    // Show all six queues for native drag and archive recovery.
    const context=await browser.newContext({viewport:{width:1760,height:1000},hasTouch:true,serviceWorkers:'block'});
    await context.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
    await context.route('**/*',route=>{
      const url=route.request().url();
      if(url==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js') return route.fulfill({status:200,contentType:'application/javascript',body:bootstrap});
      return new URL(url).origin===f.base ? route.continue() : route.abort();
    });
    const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    const card=()=>page.locator(`[data-lead-id="${f.leadId}"]`);
    const commandResults=[];
    page.on('response',async response=>{if(new URL(response.url()).pathname===f.command&&response.request().method()==='POST'){
      try{const data=await response.json();commandResults.push({status:response.status(),code:data.code,error:data.error,lead:data.lead&&{status:data.lead.status,result:data.lead.result,revision:data.lead.revision}});}catch(_){}
    }});
    const inColumn=async key=>{try{await page.waitForFunction(({key,id})=>document.querySelector('#lead-list')?.getAttribute('aria-busy')==='false'&&document.querySelector(`[data-column="${key}"] [data-lead-id="${id}"]`)&&document.querySelector('#lead-operations')?.children.length===0,{key,id:f.leadId});}
      catch(error){throw new Error(error.message+'\nSynthetic command results: '+JSON.stringify(commandResults)+'\nSynthetic navigation: '+JSON.stringify({url:page.url(),title:await page.title(),text:(await page.locator('body').innerText()).slice(0,1800),errors}));}};
    const confirm=async(next=false)=>{
      await page.locator('#lead-dialog button[type=submit]').waitFor();
      const reason=page.getByLabel(/^(Причина перемещения|Почему это спам)$/);
      if (await reason.count()) await reason.fill('Synthetic explicit manager decision');
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
    await page.emulateMedia({colorScheme:'dark'});
    const search=page.locator('#lead-search');
    await search.focus();
    const focus=await search.evaluate(node=>({
      outline:getComputedStyle(node).outlineStyle,
      radius:parseFloat(getComputedStyle(node.parentElement).borderRadius),
      ring:getComputedStyle(node.parentElement).boxShadow,
    }));
    assert.equal(focus.outline,'none');
    assert.ok(focus.radius>0 && focus.ring!=='none');
    const owner=page.locator('#lead-owner');
    const sizes = await owner.evaluate(control => {
      const css = control.getAttribute('style'), original = control.value;
      const option = new Option('Длинное имя сотрудника для выбора ответственного', 'synthetic-long', false, true);
      control.add(option);
      control.style.width = '9rem';
      const bounds = control.getBoundingClientRect();
      const style = getComputedStyle(control), arrow = getComputedStyle(control, '::picker-icon');
      const measured = {height: bounds.height, line: parseFloat(style.lineHeight),
        right: parseFloat(arrow.right), top: parseFloat(arrow.top), padding: parseFloat(style.paddingRight)};
      option.remove();
      control.value = original;
      if (css === null) control.removeAttribute('style'); else control.setAttribute('style', css);
      return measured;
    });
    assert.ok(sizes.height < sizes.line * 2 + 6, 'Long selected label should occupy a single line');
    assert.ok(sizes.right >= 0 && sizes.padding > sizes.right + 10, 'Reserve space for the arrow on the right');
    assert.ok(Math.abs(sizes.top - sizes.height / 2) <= 2, 'Center the arrow vertically');
    await owner.focus();
    await page.keyboard.press('Space');
    await page.waitForFunction(()=>document.querySelector('#lead-owner').matches(':open'));
    const pickerRadius=control=>control.evaluate(node=>getComputedStyle(node,'::picker(select)').borderRadius);
    const ownerRadius=await pickerRadius(owner);
    assert.ok(parseFloat(ownerRadius)>0);
    if(process.env.CRM_SYNTHETIC_SCREENSHOTS){
      await page.screenshot({path:process.env.CRM_SYNTHETIC_SCREENSHOTS+'/crm-leads-owner-picker-dark.png'});
    }
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await page.waitForURL(url=>url.searchParams.get('owner')==='me');
    await owner.selectOption('all');
    await inColumn('unparsed');
    const columnHeadings=()=>page.locator('.lead-column h2').evaluateAll(nodes=>nodes.map(node=>node.getAttribute('aria-label')));
    assert.deepEqual(await columnHeadings(),['Не разобраны · 1','В работе · 0','Завершённые · 0','Лид провален · 0']);
    await page.getByRole('button',{name:'Спам и удалённые',exact:true}).click();
    assert.deepEqual(await columnHeadings(),['Не разобраны · 1','В работе · 0','Завершённые · 0','Лид провален · 0','Спам · 0','Удалённые · 0']);
    await verifyBoardDragScroll(page, card(), '#lead-list');
    assert.deepEqual(commandResults, []);
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
    const transfer=await page.evaluateHandle(()=>new DataTransfer());
    await card().dispatchEvent('dragstart',{dataTransfer:transfer});
    const bar=page.locator('.crm-drag-actions');
    const spam=bar.locator('.lead-spam-drop');
    await spam.waitFor();
    assert.deepEqual(await bar.locator('.crm-drag-target:visible').allTextContents(),['Завершить лид','Лид провален','Спам']);
    assert.ok((await bar.boundingBox()).height<=60);
    await spam.dispatchEvent('dragover',{dataTransfer:transfer});
    if(process.env.CRM_SYNTHETIC_SCREENSHOTS){
      await page.screenshot({path:process.env.CRM_SYNTHETIC_SCREENSHOTS+'/crm-leads-drag-actions-dark.png'});
    }
    await spam.dispatchEvent('drop',{dataTransfer:transfer});
    const reason=page.getByLabel('Почему это спам',{exact:true});
    await reason.waitFor();
    assert.ok(await reason.evaluate(node=>node.required));
    await confirm();await inColumn('spam');await close();
    await page.reload();await inColumn('spam');await noModal();
    // Keyboard/touch movement lives in the opened card, not an ellipsis menu.
    assert.equal(await card().locator('summary').count(),0);
    await card().locator('button').first().focus();await page.keyboard.press('Enter');
    await page.locator('.lead-detail-moves summary').click();
    await page.locator('.lead-detail-moves').getByRole('button',{name:'Восстановить',exact:true}).click();
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
    await page.getByRole('button',{name:'Завершить без создания',exact:true}).click();
    await page.getByText('Новые записи не создаются. История и уже связанные контакты, компании и сделки сохранятся.').waitFor();
    assert.equal(await page.getByLabel('Причина перемещения',{exact:true}).count(),0);
    await page.getByLabel('Комментарий к завершению',{exact:true}).fill('Synthetic explicit completion');
    await confirm();await inColumn('history');await noModal();assert.deepEqual(errors,[]);
    await page.goto(f.base+f.contacts);
    assert.equal(await pickerRadius(page.locator('#contact-category')),ownerRadius);
    console.log('Lead kanban: real drag, immutable replay, reload, spam/trash restore, live search, keyboard, 390px and dark passed');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

