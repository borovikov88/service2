const assert=require('node:assert/strict');
const {chromium}=require('playwright');const {readFileSync}=require('node:fs');
(async()=>{
 const f=JSON.parse(readFileSync(0,'utf8'));assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
 const browser=await chromium.launch({headless:true});
 try{
 const context=await browser.newContext({viewport:{width:1440,height:900},serviceWorkers:'block',reducedMotion:'reduce'});
 await context.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
 await context.route('**/*',r=>r.request().url()==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js'?r.fulfill({contentType:'application/javascript',body:readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'),'utf8')}):new URL(r.request().url()).origin===f.base?r.continue():r.abort());
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));let writes=0;page.on('request',r=>{if(r.method()==='POST')writes++;});
 const url=`${f.base}/crm/workspace/?org_id=${f.org}&pipeline=${f.pipeline}`;
 await page.goto(url);const lane=id=>page.locator(`.crm-live-lane[data-stage-id="${id}"]`);
 const ready=()=>page.waitForFunction(()=>document.querySelector('#crm-live-content').getAttribute('aria-busy')==='false');
 const all=()=>page.locator('.crm-live-card');
 const expand=async target=>{while(await target.locator('[data-action=column-more]').count()){await target.locator('[data-action=column-more]').click();await ready();}};
 assert.equal(await all().count(),100);await expand(lane(f.source));await expand(lane(f.other));assert.equal(await all().count(),274);
 assert.equal(new Set(await all().evaluateAll(ns=>ns.map(n=>n.dataset.dealId))).size,274);
 const card=page.locator(`.crm-live-card[data-deal-id="${f.deal}"]`);
 await card.scrollIntoViewIfNeeded();let b=await card.boundingBox(),t=await lane(f.target).boundingBox();
 await page.mouse.move(b.x+50,b.y+30);await page.mouse.down();await page.mouse.move(b.x+70,b.y+40,{steps:5});await page.keyboard.press('Escape');await page.mouse.up();assert.equal(writes,0);
 const saved=page.waitForResponse(r=>r.url().includes('/commands/')&&r.request().method()==='POST');
 const refreshed=page.waitForResponse(r=>r.url().includes('/workspace/data/'));
 await card.dragTo(lane(f.target),{targetPosition:{x:40,y:b.y+b.height/2-t.y}});assert.equal((await saved).status(),200);await refreshed;await ready();
 await lane(f.target).locator(`.crm-live-card[data-deal-id="${f.deal}"]`).waitFor();assert.equal(await all().count(),101,'Own move restarts all cursors');
 // Cursor becomes invalid after another supported writer, without a fake snapshot.
 const external=await context.newPage();await external.goto(url);
 // Use actual protected command and current revision from the existing data route.
 const changed=await external.evaluate(async()=>{
  const boot=JSON.parse(document.querySelector('#crm-live-bootstrap').textContent),deal=boot.deals.find(d=>d.stage_id===boot.pipelines.find(p=>p.id===boot.selected_pipeline_id).stages[0].id);
  const pipeline=boot.pipelines.find(p=>p.id===boot.selected_pipeline_id);
  const response=await fetch(boot.urls.command,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':document.querySelector('#crm-live [name=csrfmiddlewaretoken]').value},body:JSON.stringify({command_type:'deal.transition',operation_key:crypto.randomUUID(),subject_id:deal.id,expected_revision:deal.revision,changes:{stage_id:pipeline.stages[1].id,reason:''}})});
  return response.status;
 });assert.equal(changed,200);await external.close();
 await lane(f.source).locator('[data-action=column-more]').click();await page.getByText('Доска изменилась. Загружаем актуальные первые порции; продолжите загрузку заново.',{exact:true}).waitFor();await ready();assert.equal(await all().count(),101);
 // A late portion is ignored immediately when search changes.
 await page.route('**/workspace/data/**',async route=>{if(new URL(route.request().url()).searchParams.has('cursor'))await new Promise(r=>setTimeout(r,600));await route.continue().catch(()=>{});});
 await lane(f.source).locator('[data-action=column-more]').click();await page.locator('#crm-live-search').fill('Column synthetic 209');
 await page.waitForFunction(()=>document.querySelectorAll('.crm-live-card').length===1);await page.waitForTimeout(700);assert.equal(await all().count(),1);await page.unrouteAll({behavior:'wait'});
 await page.locator('#crm-live-search').fill('');await page.waitForFunction(()=>document.querySelectorAll('.crm-live-card').length===101);
 await page.locator('[data-view=table]').click();await ready();assert.equal(await all().count(),50);await page.locator('[data-action=page-next]').click();await ready();assert.equal(new URL(page.url()).searchParams.get('page'),'2');
 await page.locator('[data-view=kanban]').click();await ready();assert.equal(await all().count(),101);
 await page.evaluate(u=>history.pushState({},'',u+'&q=absent'),url);await page.goBack();await ready();assert.equal(await all().count(),101);await page.goForward();await page.waitForFunction(()=>document.querySelectorAll('.crm-live-card').length===0);
 const leadURL=`${f.base}/crm/workspace/leads/?org_id=${f.org}`;await page.goto(leadURL);
 const leadLane=key=>page.locator(`.lead-column[data-column="${key}"]`),leadRows=()=>page.locator('.lead-row');
 const leadReady=()=>page.waitForFunction(()=>document.querySelector('#lead-list').getAttribute('aria-busy')==='false');
 assert.equal(await leadRows().count(),100);
 for(const key of ['unparsed','working'])while(await leadLane(key).locator('[data-action=column-more]').count()){await leadLane(key).locator('[data-action=column-more]').click();await leadReady();}
 assert.equal(await leadRows().count(),274);assert.equal(new Set(await leadRows().evaluateAll(ns=>ns.map(n=>n.dataset.leadId))).size,274);
 const lead=page.locator(`.lead-row[data-lead-id="${f.lead}"]`),empty=leadLane('history');await lead.scrollIntoViewIfNeeded();b=await lead.boundingBox();t=await empty.boundingBox();
 const before=writes;
 for(let i=0;i<2;i++){await lead.dragTo(empty,{targetPosition:{x:35,y:b.y+b.height/2-t.y}});await page.locator('#lead-dialog form').waitFor();await page.locator('#lead-close').click();await page.locator('#lead-dialog').waitFor({state:'hidden'});assert.equal(writes,before);}
 await lead.dragTo(empty,{targetPosition:{x:35,y:b.y+b.height/2-t.y}});const leadSaved=page.waitForResponse(r=>r.url().includes('/leads/commands/')&&r.request().method()==='POST');await page.locator('#lead-dialog form button[type=submit]').click();assert.equal((await leadSaved).status(),200);await page.getByText('Изменения сохранены.',{exact:true}).waitFor();await leadReady();await page.locator('#lead-close').click();await page.locator('#lead-dialog').waitFor({state:'hidden'});await empty.locator(`.lead-row[data-lead-id="${f.lead}"]`).waitFor();assert.equal(await leadRows().count(),101);
 for(const theme of ['light','dark']){
 await page.setViewportSize({width:390,height:844});await page.evaluate(t=>document.documentElement.setAttribute('data-theme',t),theme);
 await leadLane('unparsed').locator('[data-action=column-more]').click();await leadReady();
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
 if(process.env.CRM_SYNTHETIC_SCREENSHOTS){await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:`${process.env.CRM_SYNTHETIC_SCREENSHOTS}/cursor-leads-${theme}.png`});}
 }
 await page.locator('#lead-search').fill('Synthetic lead new 209');await page.waitForFunction(()=>document.querySelectorAll('.lead-row').length===1);
 await page.evaluate(u=>history.pushState({},'',u+'&q=absent'),leadURL);await page.goBack();await leadReady();assert.equal(await leadRows().count(),1);await page.goForward();await page.waitForFunction(()=>document.querySelectorAll('.lead-row').length===0);
 await page.goto(url);await ready();await lane(f.source).locator('[data-action=column-more]').click();await ready();assert.equal(await all().count(),151);
 assert.deepEqual(errors,[]);console.log('Both boards >200/two columns, cursor reset, delayed response, lower empty drop, cancel/repeat, table and history, mobile/theme PASS');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
