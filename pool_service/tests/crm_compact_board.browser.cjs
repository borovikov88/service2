const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {chromium}=require('playwright');
(async()=>{
 let input='';for await(const c of process.stdin)input+=c;const f=JSON.parse(input);
 assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
 const browser=await chromium.launch({headless:true});
 try{
 const context=await browser.newContext({viewport:{width:1440,height:850},hasTouch:true,serviceWorkers:'block'});
 await context.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
 await context.route('**/*',r=>r.request().url()==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js'?r.fulfill({contentType:'application/javascript',body:readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'),'utf8')}):new URL(r.request().url()).origin===f.base?r.continue():r.abort());
 const page=await context.newPage(),errors=[];let writes=0;
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST')writes++;});
 const url=`${f.base}/crm/workspace/?org_id=${f.org}&pipeline=${f.pipeline}`;
 await page.goto(url);await page.locator('.crm-live-card').last().waitFor();
 assert.equal(await page.locator('.crm-live-card').count(),12);
 assert.equal(await page.locator('.crm-live-card-filters').count(),0);
 assert.equal(await page.locator('.crm-live-heading #crm-live-pipelines').count(),1);
 const bounds=await Promise.all(['.crm-live-heading h1','#crm-live-pipelines','#crm-live-create'].map(s=>page.locator(s).boundingBox()));
 assert.ok(bounds[0].x+bounds[0].width<bounds[1].x&&bounds[1].x+bounds[1].width<=bounds[2].x);
 assert.equal(await page.locator('.crm-live-pagination').isVisible(),false);
 const card=()=>page.locator(`.crm-live-card[data-deal-id="${f.deal}"]`),lane=id=>page.locator(`[data-stage-id="${id}"].crm-live-lane`);
 for(const width of [1440,390])for(const theme of ['light','dark']){
   await page.setViewportSize({width,height:850});await page.evaluate(t=>document.documentElement.setAttribute('data-theme',t),theme);
   const heights=await Promise.all([lane(f.source),lane(f.target)].map(n=>n.evaluate(e=>e.getBoundingClientRect().height)));
   assert.ok(Math.abs(heights[0]-heights[1])<2,'CRM lanes must have equal full drop height');
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
   if(process.env.CRM_SYNTHETIC_SCREENSHOTS){await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:`${process.env.CRM_SYNTHETIC_SCREENSHOTS}/compact-${width}-${theme}.png`});}
 }
 await page.setViewportSize({width:1440,height:850});
 await card().scrollIntoViewIfNeeded();let b=await card().boundingBox();
 await page.mouse.move(b.x+60,b.y+30);await page.mouse.down();await page.mouse.move(b.x+85,b.y+45,{steps:5});await page.keyboard.press('Escape');await page.mouse.up();assert.equal(writes,0);
 await card().scrollIntoViewIfNeeded();b=await card().boundingBox();const target=await lane(f.target).boundingBox();
 assert.ok(b.y+ b.height/2-target.y>1000,'Exercise lower blank area');
 const refreshed=page.waitForResponse(r=>r.url().includes('/crm/workspace/data/')&&r.request().method()==='GET');
 const response=page.waitForResponse(r=>r.url().includes('/commands/')&&r.request().method()==='POST');
 await card().dragTo(lane(f.target),{targetPosition:{x:40,y:b.y+b.height/2-target.y}});
 assert.equal((await response).status(),200);await refreshed;await page.waitForFunction(()=>document.querySelector('#crm-live-content').getAttribute('aria-busy')==='false');await lane(f.target).locator(`.crm-live-card[data-deal-id="${f.deal}"]`).waitFor();assert.equal(writes,1);
 const dragAcross=async target=>{
   await card().scrollIntoViewIfNeeded();const c=await card().boundingBox(),t=await target.boundingBox();
   const y=c.y+c.height/2;assert.ok(y>70&&y<850,'Drag stays below sticky header');
   await page.mouse.move(c.x+c.width/2,y);await page.mouse.down();await page.mouse.move(c.x+c.width/2+15,y+3,{steps:5});
   await page.mouse.move(t.x+6,y,{steps:12});await page.mouse.up();
 };
 await dragAcross(lane(f.target));assert.equal(writes,1,'Same stage drop must be a no-op');
 // Backward move keeps the existing reason/confirmation gate; cancel is read-only.
 await dragAcross(lane(f.source));await page.locator('[data-command-form=transition-confirm]').waitFor();await page.keyboard.press('Escape');assert.equal(writes,1);
 await page.locator('#crm-live-search').fill('Synthetic compact 11');await page.waitForFunction(()=>document.querySelectorAll('.crm-live-card').length===1);
 await page.locator('#crm-live-owner').selectOption('me');await page.waitForURL(u=>u.searchParams.get('owner')==='me');
 await page.locator('[data-view=table]').click();await page.locator('.crm-live-table').waitFor();
 await page.reload();await page.locator('.crm-live-table').waitFor();assert.equal(await page.locator('#crm-live-search').inputValue(),'Synthetic compact 11');
 await page.locator('#crm-live-pipelines button').last().click();await page.waitForFunction(()=>document.querySelector('#crm-live-content').getAttribute('aria-busy')==='false');await page.locator('#crm-live-search').fill('');
 await page.goto(url+'&specialization=absent');await page.locator('#crm-live-linked-filters').waitFor();await page.getByRole('button',{name:'Убрать условия',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.crm-live-card').length===12);assert.equal(new URL(page.url()).searchParams.has('specialization'),false);
 // Browser history invokes popstate with removed controls absent.
 await page.evaluate(u=>history.pushState({},'',u),url+'&specialization=absent&owner=me');
 await page.goBack();await page.waitForFunction(()=>document.querySelector('#crm-live-content').getAttribute('aria-busy')==='false'&&document.querySelectorAll('.crm-live-card').length===12);
 await page.goForward();await page.waitForFunction(()=>!document.querySelector('#crm-live-linked-filters').hidden&&document.querySelector('#crm-live-content').getAttribute('aria-busy')==='false'&&document.querySelectorAll('.crm-live-card').length===0);
 await page.getByRole('button',{name:'Убрать условия',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.crm-live-card').length===12);assert.equal(new URL(page.url()).searchParams.get('owner'),'me');
 assert.deepEqual(errors,[]);console.log('Compact header, filters/navigation, lower blank drop, cancel/repeat, desktop/mobile/light/dark PASS');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
