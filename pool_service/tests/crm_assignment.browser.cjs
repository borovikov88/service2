/* Chromium against a real isolated Django server. No production recipients. */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');
(async () => {
  let input=''; for await (const chunk of process.stdin) input+=chunk;
  const f=JSON.parse(input); assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const browser=await chromium.launch({headless:true}); let phase='setup';
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000},serviceWorkers:'block'});
    await context.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
    const bootstrap=readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'));
    await context.route('**/*',route=>route.request().url()==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js'
      ?route.fulfill({contentType:'application/javascript',body:bootstrap}):new URL(route.request().url()).origin===f.base?route.continue():route.abort());
    const errors=[],writes=[];
    const observe=p=>{p.setDefaultTimeout(15000);
      p.on('pageerror',error=>errors.push(error.message));
      p.on('request',request=>{if(request.method()==='POST')writes.push(request);});};
    let page=await context.newPage();observe(page);
    let leadDialog=page.locator('#lead-dialog'), dealDialog=page.locator('#crm-live-dialog');
    const choose=async(form,id)=>{const field=form.locator('[name="responsible_id"]');await field.locator('option[value="'+id+'"]').waitFor({state:'attached'});await field.selectOption(String(id));return field;};
    const submit=async(form,kind)=>{
      const response=page.waitForResponse(r=>r.request().method()==='POST'&&r.request().postDataJSON()?.command_type===kind);
      await form.locator('button[type="submit"]').click(); const r=await response;
      assert.equal(r.status(),200,await r.text());return r.json();
    };
    const noOverflow=async(dialog)=>{
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      assert.ok(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1),'Assignment drawer must fit mobile viewport');
    };
    phase='conversion responsible selector and cancel';
    await page.goto(f.base+f.leads+'&lead='+f.ownLead);
    await leadDialog.getByRole('button',{name:'Создать сделку',exact:true}).click();
    const conversion=leadDialog.locator('.lead-conversion-form');
    await choose(conversion,f.recipients[0]); const conversionWrites=writes.length;
    await conversion.getByRole('button',{name:'Отмена',exact:true}).click();
    await leadDialog.locator('.lead-summary').waitFor(); assert.equal(writes.length,conversionWrites);
    await page.locator('#lead-close').click();
    phase='lead creation';
    await page.locator('#lead-create').click(); const leadCreate=leadDialog.locator('form').first();
    await leadCreate.getByLabel('Название',{exact:true}).fill('Synthetic assigned lead');
    await leadCreate.getByLabel('Что требуется / контекст',{exact:true}).fill('Synthetic assignment request');
    await leadCreate.getByLabel('Следующий шаг',{exact:true}).fill('Synthetic assigned lead action');
    await leadCreate.locator('input[type="datetime-local"]').last().fill('2026-12-01T12:00');
    await choose(leadCreate,f.recipients[0]); const createdLead=await submit(leadCreate,'lead.create');
    assert.equal(createdLead.lead.assignee.id,f.recipients[0]);
    await leadDialog.locator('.lead-summary').filter({hasText:f.names[0]}).waitFor();
    phase='lead transfer cancellation';
    await leadDialog.getByRole('button',{name:'Сменить ответственного',exact:true}).click();
    let leadAssign=leadDialog.locator('[data-assignment-form]');await choose(leadAssign,f.recipients[1]);
    let before=writes.length;await leadAssign.getByRole('button',{name:'Отмена',exact:true}).click();
    await leadDialog.locator('.lead-summary').filter({hasText:f.names[0]}).waitFor();assert.equal(writes.length,before);
    phase='lead transfer';
    await leadDialog.getByRole('button',{name:'Сменить ответственного',exact:true}).click();leadAssign=leadDialog.locator('[data-assignment-form]');
    await choose(leadAssign,f.recipients[1]);
    assert.equal(await leadAssign.locator('[name="transfer_next_action"]').isChecked(),true);
    await leadAssign.locator('[name="transfer_next_action"]').uncheck();
    const changedLead=await submit(leadAssign,'lead.assign');assert.equal(changedLead.lead.assignee.id,f.recipients[1]);
    await page.reload();await leadDialog.locator('.lead-summary').filter({hasText:f.names[1]}).waitFor();
    await page.setViewportSize({width:390,height:844});await page.evaluate(()=>document.documentElement.setAttribute('data-bs-theme','dark'));
    await leadDialog.getByRole('button',{name:'Сменить ответственного',exact:true}).click();await choose(leadDialog.locator('[data-assignment-form]'),f.recipients[0]);await noOverflow(leadDialog);
    await page.setViewportSize({width:1440,height:1000});
    phase='deal creation';
    await page.goto(f.base+f.deals);await page.locator('#crm-live-create').click();const dealCreate=dealDialog.locator('[data-command-form="create"]');
    await dealCreate.locator('[name="title"]').fill('Synthetic assigned deal');
    await dealCreate.locator('[name="client_search"]').fill(f.clientName);await page.locator('.crm-live-client-results button').filter({hasText:f.clientName}).click();
    await dealCreate.locator('[name="next_title"]').fill('Synthetic assigned deal action');await dealCreate.locator('[name="due_at"]').fill('2026-12-01T13:00');
    await choose(dealCreate,f.recipients[0]);const createdDeal=await submit(dealCreate,'deal.create');assert.equal(createdDeal.deal.manager.id,f.recipients[0]);
    await dealDialog.locator('.crm-live-detail-meta').filter({hasText:f.names[0]}).waitFor();
    phase='deal transfer cancellation';
    await dealDialog.getByRole('button',{name:'Сменить ответственного',exact:true}).click();let dealAssign=dealDialog.locator('[data-command-form="assign"]');
    await choose(dealAssign,f.recipients[1]);before=writes.length;await dealAssign.getByRole('button',{name:'Отмена',exact:true}).click();
    // Cancel reloads the detail asynchronously; do not reopen the old form
    // while its still-visible summary belongs to the previous render.
    await page.waitForLoadState('networkidle');
    await dealDialog.locator('.crm-live-detail-meta').filter({hasText:f.names[0]}).waitFor();assert.equal(writes.length,before);
    phase='deal transfer';
    await dealDialog.getByRole('button',{name:'Сменить ответственного',exact:true}).click();dealAssign=dealDialog.locator('[data-command-form="assign"]');await choose(dealAssign,f.recipients[1]);
    assert.equal(await dealAssign.locator('[name="transfer_next_action"]').isChecked(),true);
    const changedDeal=await submit(dealAssign,'deal.assign');assert.equal(changedDeal.deal.manager.id,f.recipients[1]);
    await page.reload();await dealDialog.locator('.crm-live-detail-meta').filter({hasText:f.names[1]}).waitFor();
    await page.setViewportSize({width:390,height:844});await page.evaluate(()=>document.documentElement.setAttribute('data-bs-theme','dark'));
    await dealDialog.getByRole('button',{name:'Сменить ответственного',exact:true}).click();await choose(dealDialog.locator('[data-command-form="assign"]'),f.recipients[0]);await noOverflow(dealDialog);
    assert.equal(writes.filter(r=>r.postDataJSON()?.command_type==='lead.assign').length,1);
    assert.equal(writes.filter(r=>r.postDataJSON()?.command_type==='deal.assign').length,1);
    assert.equal(writes.find(r=>r.postDataJSON()?.command_type==='lead.assign').postDataJSON().changes.transfer_next_action,false);
    assert.equal(writes.find(r=>r.postDataJSON()?.command_type==='deal.assign').postDataJSON().changes.transfer_next_action,true);
    phase='new responsible works on transferred task';
    // A late response from the previous actor must not satisfy the recipient's
    // detail wait. Use a fresh page while keeping the same local network policy.
    await page.close();
    await context.clearCookies();await context.addCookies([{name:f.sessionCookieName,value:f.recipientCookie,url:f.base}]);
    page=await context.newPage();observe(page);await page.setViewportSize({width:390,height:844});
    leadDialog=page.locator('#lead-dialog');dealDialog=page.locator('#crm-live-dialog');
    const recipientDetail=page.waitForResponse(r=>new URL(r.url()).pathname===f.detailPrefix+createdDeal.deal.id+'/');
    await page.goto(f.base+f.deals+'&deal='+createdDeal.deal.id);
    const recipientResponse=await recipientDetail;
    const recipientHeaders=await recipientResponse.request().allHeaders();
    assert.ok((recipientHeaders.cookie||'').split('; ').includes(f.sessionCookieName+'='+f.recipientCookie),
      'Recipient detail must originate from the recipient session');
    assert.equal(recipientResponse.status(),200,'The new responsible must be able to read the assigned deal');
    const recipientDeal=(await recipientResponse.json()).deal;
    assert.deepEqual({manager:recipientDeal.manager?.id, can_next_action:recipientDeal.can_next_action,
      restricted:Boolean(recipientDeal.next_action?.restricted),waiting:Boolean(recipientDeal.next_action?.waiting),archived:recipientDeal.is_archived},
      {manager:f.recipients[1],can_next_action:true,restricted:false,waiting:false,archived:false});
    await page.waitForLoadState('networkidle'); // File SQLite: finish unrelated candidate GET transactions first.
    assert.deepEqual((await dealDialog.locator('.crm-live-form-error').allTextContents()).filter(Boolean),[],
      'Recipient detail must render successfully before editing the transferred task');
    await dealDialog.getByRole('tab',{name:'Дела',exact:true}).click();
    const nextForm=dealDialog.locator('[data-command-form="next_action"]');await nextForm.locator('[name="next_title"]').fill('Synthetic recipient accepted next action');
    await submit(nextForm,'deal.next_action');
    phase='lead without task transfer supports safe basic edit';
    await page.goto(f.base+f.leads+'&lead='+createdLead.lead.id);await page.waitForLoadState('networkidle');
    await leadDialog.getByRole('button',{name:'Редактировать',exact:true}).click();
    const basic=leadDialog.locator('.lead-editor form');assert.equal(await basic.locator('[name="responsible_id"]').count(),0);
    await basic.getByLabel('Что требуется / контекст',{exact:true}).fill('Synthetic recipient updated basic need');
    const editedLead=await submit(basic,'lead.edit');assert.equal(editedLead.lead.need,'Synthetic recipient updated basic need');
    const basicPayload=writes.filter(r=>r.postDataJSON()?.command_type==='lead.edit').at(-1).postDataJSON().changes;
    assert.deepEqual(Object.keys(basicPayload).sort(),['need']);
    assert.equal(editedLead.lead.title,createdLead.lead.title);
    assert.deepEqual(errors,[]);console.log('PASS lead/deal create, transfer, cancel, conversion selector, explicit next-task transfer, recipient editing, mobile, persisted ownership');
  } catch(error) {throw new Error('Phase: '+phase+'; '+error.stack);} finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
