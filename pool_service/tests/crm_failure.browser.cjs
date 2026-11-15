const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');
(async () => {
  let input=''; for await(const chunk of process.stdin) input+=chunk;
  const f=JSON.parse(input); assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const browser=await chromium.launch({headless:true}); let phase='setup';
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000},serviceWorkers:'block'});
    await context.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
    const bootstrap=readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'));
    await context.route('**/*',route=>route.request().url()==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js'
      ?route.fulfill({contentType:'application/javascript',body:bootstrap}):new URL(route.request().url()).origin===f.base?route.continue():route.abort());
    const page=await context.newPage(), errors=[], writes=[]; page.setDefaultTimeout(14000);
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{if(request.method()==='POST')writes.push(request.postDataJSON());});
    const leadDialog=page.locator('#lead-dialog'),dealDialog=page.locator('#crm-live-dialog');
    const leadCard=id=>page.locator(`.lead-row[data-lead-id="${id}"]`);
    const dealCard=id=>page.locator(`.crm-live-card[data-deal-id="${id}"]`);
    const dock=()=>page.locator('.crm-drag-actions'),target=()=>page.locator('.crm-failure-drop');
    async function startDrag(card){
      await card.scrollIntoViewIfNeeded(); const b=await card.boundingBox();
      await page.mouse.move(b.x+b.width-18,b.y+22);await page.mouse.down();
      await page.mouse.move(b.x+b.width-45,b.y+48,{steps:6});
      await target().waitFor({state:'visible'});
      if(process.env.CRM_SYNTHETIC_SCREENSHOTS)await page.screenshot({path:process.env.CRM_SYNTHETIC_SCREENSHOTS+'/failure-drag-'+(await page.locator('#crm-live').count()?'deal':'lead')+'.png'});
      const bounds=await target().boundingBox();
      assert.ok(bounds.y>500 && bounds.y+bounds.height<=1000,'Failure target is fixed inside the viewport');
    }
    async function drop(){const b=await target().boundingBox();await page.mouse.move(b.x+b.width/2,b.y+b.height/2,{steps:10});await page.mouse.up();}
    async function confirm(form,kind){
      const result=page.waitForResponse(r=>r.request().method()==='POST'&&r.request().postDataJSON()?.command_type===kind);
      await form.locator('button[type=submit]').click();const response=await result;
      assert.equal(response.status(),200,await response.text());return response.json();
    }
    async function mobileFits(dialog){
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Page fits mobile');
      assert.ok(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1),'Dialog fits mobile');
    }
    phase='lead actual drag, permission-specific dock and cancellation';
    await page.goto(f.base+f.leads);await leadCard(f.leadIds[0]).waitFor();
    assert.equal(await page.locator('.lead-column').count(),4);
    await startDrag(leadCard(f.leadIds[0]));
    assert.equal(await page.locator('.lead-conversion-drop').isVisible(),false,'Unavailable conversion target stays hidden');
    await drop();await leadDialog.getByLabel('Причина провала',{exact:true}).waitFor();
    assert.equal(writes.length,0);await leadDialog.getByRole('button',{name:'Отмена',exact:true}).click();
    assert.equal(writes.length,0);assert.equal(await leadDialog.isVisible(),false);
    await startDrag(leadCard(f.leadIds[0]));await page.keyboard.press('Escape');await page.mouse.up();
    assert.equal(await dock().isVisible(),false);assert.equal(writes.length,0);
    phase='lead confirmation and persistence';
    await startDrag(leadCard(f.leadIds[0]));await drop();
    await leadDialog.getByLabel('Причина провала',{exact:true}).fill('Synthetic browser refusal');
    await confirm(leadDialog.locator('form'),'lead.disposition');
    await page.locator(`[data-column=failed] [data-lead-id="${f.leadIds[0]}"]`).waitFor();
    await page.locator('#lead-close').click();await page.reload();
    await page.locator(`[data-column=failed] [data-lead-id="${f.leadIds[0]}"]`).waitFor();
    phase='deal drag cancellation, historical lane, foreign card';
    await page.goto(f.base+f.deals);await dealCard(f.dealIds[0]).waitFor();
    assert.equal(await dealCard(f.readonlyId).getAttribute('draggable'),'false');
    await page.locator(`[data-stage-id="${f.failedStage}"] .crm-live-card[data-deal-id="${f.oldId}"]`).waitFor();
    await startDrag(dealCard(f.dealIds[0]));await drop();
    await dealDialog.getByLabel('Причина провала',{exact:true}).waitFor();
    const before=writes.length;await dealDialog.getByRole('button',{name:'Отмена',exact:true}).click();
    assert.equal(writes.length,before);assert.equal(await dealDialog.isVisible(),false);
    phase='deal confirmation and reload';
    await startDrag(dealCard(f.dealIds[0]));await drop();
    await dealDialog.getByLabel('Причина провала',{exact:true}).fill('Synthetic browser loss');
    await confirm(dealDialog.locator('form'),'deal.close_negative');
    await page.locator(`[data-stage-id="${f.failedStage}"] .crm-live-card[data-deal-id="${f.dealIds[0]}"]`).waitFor();
    await page.locator('#crm-live-dialog [data-action=close]').first().click();await page.reload();
    await page.locator(`[data-stage-id="${f.failedStage}"] .crm-live-card[data-deal-id="${f.dealIds[0]}"]`).waitFor();
    phase='ordinary stage movement still works';
    const transfer=await page.evaluateHandle(()=>new DataTransfer());
    await dealCard(f.dealIds[1]).dispatchEvent('dragstart',{dataTransfer:transfer});
    const response=page.waitForResponse(r=>r.request().postDataJSON()?.command_type==='deal.transition');
    await page.locator(`[data-stage-id="${f.technicalStage}"]`).dispatchEvent('drop',{dataTransfer:transfer});
    assert.equal((await response).status(),200);
    await page.locator(`[data-stage-id="${f.technicalStage}"] .crm-live-card[data-deal-id="${f.dealIds[1]}"]`).waitFor();
    phase='mobile and dark card action, no drag required';
    await page.setViewportSize({width:390,height:844});
    await page.goto(f.base+f.deals+'&deal='+f.dealIds[1]);
    await page.evaluate(()=>document.documentElement.dataset.theme='dark');
    await dealDialog.getByRole('tab',{name:'Изменить',exact:true}).click();
    await dealDialog.locator('summary').filter({hasText:'Сделка провалена / отменена'}).click();
    const closeForm=dealDialog.locator('form[data-command-form=close_negative]');
    await closeForm.getByLabel('Причина',{exact:true}).fill('Synthetic mobile deal loss');
    await mobileFits(dealDialog);await confirm(closeForm,'deal.close_negative');
    await page.locator(`[data-stage-id="${f.failedStage}"] .crm-live-card[data-deal-id="${f.dealIds[1]}"]`).waitFor();
    await page.goto(f.base+f.leads+'&lead='+f.leadIds[1]);
    await page.evaluate(()=>document.documentElement.dataset.theme='dark');
    await leadDialog.locator('summary').filter({hasText:'Изменить этап'}).click();
    await leadDialog.getByRole('button',{name:'Лид провален',exact:true}).click();
    await leadDialog.getByLabel('Причина провала',{exact:true}).fill('Synthetic mobile lead refusal');
    await mobileFits(leadDialog);if(process.env.CRM_SYNTHETIC_SCREENSHOTS)await page.screenshot({path:process.env.CRM_SYNTHETIC_SCREENSHOTS+'/failure-mobile.png'});await confirm(leadDialog.locator('form'),'lead.disposition');
    await page.locator(`[data-column=failed] [data-lead-id="${f.leadIds[1]}"]`).waitFor();
    assert.equal(writes.filter(p=>p.command_type==='deal.close_negative').length,2);
    assert.equal(writes.filter(p=>p.command_type==='lead.disposition').length,2);
    assert.deepEqual(errors,[]);console.log('Failure targets: real mouse drag, cancel, Escape, permissions, reason, history, reload, normal move, mobile/dark passed.');
  }catch(error){throw new Error(phase+': '+error.stack);}finally{await browser.close();}
})().catch(error=>{console.error(error.stack);process.exit(1);});
