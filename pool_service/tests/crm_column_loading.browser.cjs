const assert=require('node:assert/strict');
const {chromium}=require('playwright');
(async()=>{
 const f=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
 const browser=await chromium.launch({headless:true});
 try{
 const context=await browser.newContext({viewport:{width:1440,height:900}});
 await context.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const url=`${f.base}/crm/workspace/?org_id=${f.org}&pipeline=${f.pipeline}`;
 await page.goto(url);const lane=id=>page.locator(`.crm-live-lane[data-stage-id="${id}"]`);
 const cards=()=>page.locator('.crm-live-card');
 const ready=()=>page.waitForFunction(()=>document.querySelector('#crm-live-content').getAttribute('aria-busy')==='false');
 assert.equal(await cards().count(),50);assert.equal(await page.locator('.crm-live-pagination').isVisible(),false);
 await lane(f.source).getByRole('button',{name:'Показать ещё 50',exact:true}).click();await ready();assert.equal(await cards().count(),100);
 await lane(f.source).getByRole('button',{name:'Показать ещё 50',exact:true}).click();await ready();assert.equal(await cards().count(),107);
 assert.equal(await lane(f.source).locator('[data-action=column-more]').count(),0);
 const ids=await cards().evaluateAll(nodes=>nodes.map(n=>n.dataset.dealId));assert.equal(new Set(ids).size,107);
 // An actual command after expansion refreshes the entire bounded board, not append stale offsets.
 const card=page.locator(`.crm-live-card[data-deal-id="${f.deal}"]`);
 await card.scrollIntoViewIfNeeded();const b=await card.boundingBox(),t=await lane(f.target).boundingBox();
 const response=page.waitForResponse(r=>r.url().includes('/commands/')&&r.request().method()==='POST');
 const refreshed=page.waitForResponse(r=>r.url().includes('/workspace/data/'));
 await card.dragTo(lane(f.target),{targetPosition:{x:40,y:b.y+b.height/2-t.y}});
 assert.equal((await response).status(),200);await refreshed;await ready();
 await lane(f.target).locator(`.crm-live-card[data-deal-id="${f.deal}"]`).waitFor();assert.equal(await cards().count(),107);
 await page.locator('#crm-live-search').fill('Column synthetic 106');await page.waitForFunction(()=>document.querySelectorAll('.crm-live-card').length===1);
 await page.locator('#crm-live-search').fill('');await page.waitForFunction(()=>document.querySelectorAll('.crm-live-card').length===51);
 // Interrupt stale search response; it must not replace the newer filter.
 await page.route('**/workspace/data/**',async route=>{if(new URL(route.request().url()).searchParams.get('q')==='Column synthetic 99')await new Promise(r=>setTimeout(r,700));await route.continue().catch(()=>{});});
 await page.locator('#crm-live-search').fill('Column synthetic 99');await page.waitForTimeout(400);
 await page.locator('#crm-live-search').fill('Column synthetic 106');await page.waitForFunction(()=>document.querySelectorAll('.crm-live-card').length===1&&document.querySelector('.crm-live-card').textContent.includes('Column synthetic 106'));
 await page.waitForTimeout(800);assert.ok((await cards().innerText()).includes('Column synthetic 106'));await page.unrouteAll({behavior:'wait'});
 await page.locator('#crm-live-search').fill('');await page.waitForFunction(()=>document.querySelectorAll('.crm-live-card').length===51);
 await page.locator('[data-view=table]').click();await page.locator('.crm-live-table').waitFor();await ready();assert.equal(await cards().count(),50);
 await page.locator('[data-action=page-next]').click();await ready();assert.equal(new URL(page.url()).searchParams.get('page'),'2');
 await page.locator('[data-view=kanban]').click();await ready();assert.equal(await cards().count(),51);assert.equal(new URL(page.url()).searchParams.has('page'),false);
 for(const theme of ['light','dark']){
 await page.setViewportSize({width:390,height:844});await page.evaluate(t=>document.documentElement.setAttribute('data-theme',t),theme);
 await lane(f.source).getByRole('button',{name:'Показать ещё 50',exact:true}).click();await ready();
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
 if(process.env.CRM_SYNTHETIC_SCREENSHOTS){await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:`${process.env.CRM_SYNTHETIC_SCREENSHOTS}/columns-mobile-${theme}.png`});}
 }
 await page.reload();await ready();assert.equal(await cards().count(),51,'Reload starts bounded prefixes');
 assert.deepEqual(errors,[]);console.log('Column expansion/move/no duplicate/filter/interruption/table/mobile/light-dark PASS');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
