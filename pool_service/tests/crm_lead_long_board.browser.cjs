const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const {readFileSync}=require('node:fs');
(async()=>{
 let input='';for await(const chunk of process.stdin)input+=chunk;const f=JSON.parse(input);
 assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
 const browser=await chromium.launch({headless:true});
 try{
 const context=await browser.newContext({viewport:{width:1440,height:800},hasTouch:true,serviceWorkers:'block'});
 await context.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
 const bootstrap=readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'),'utf8');
 await context.route('**/*',r=>r.request().url()==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js'?r.fulfill({status:200,contentType:'application/javascript',body:bootstrap}):new URL(r.request().url()).origin===f.base?r.continue():r.abort());
 const page=await context.newPage();const errors=[];let writes=0;page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST')writes++;});
 await page.goto(f.base+f.workspace);
 const cards=page.locator('[data-column=unparsed] .lead-row');await cards.last().waitFor();assert.equal(await cards.count(),18);
 assert.equal(await page.locator('.lead-card-move').count(),0);
 const zone=page.locator('.lead-conversion-drop');
 for(const index of [0,1,2,10,17,17]){
   const card=cards.nth(index);await card.scrollIntoViewIfNeeded();const b=await card.boundingBox();
   await page.mouse.move(b.x+b.width/2,b.y+30);await page.mouse.down();await page.mouse.move(b.x+b.width/2+15,b.y+40,{steps:5});
   await zone.waitFor({state:'visible'});const z=await zone.boundingBox();assert.ok(z.y>=0&&z.y+z.height<=800);
   await page.mouse.move(z.x+z.width/2,z.y+z.height/2,{steps:10});await page.mouse.up();
   await page.getByLabel('Название сделки',{exact:true}).waitFor();assert.equal(writes,0);
   await page.locator('#lead-close').click();await page.locator('#lead-dialog').waitFor({state:'hidden'});
 }
 // Escape cancellation must not leave a visible target or write anything.
 await cards.first().scrollIntoViewIfNeeded();let b=await cards.first().boundingBox();await page.mouse.move(b.x+80,b.y+30);await page.mouse.down();await page.mouse.move(b.x+100,b.y+45,{steps:5});await zone.waitFor({state:'visible'});await page.keyboard.press('Escape');await page.mouse.up();await zone.waitFor({state:'hidden'});
 const deleted=page.locator(`[data-lead-id="${f.deletedId}"]`);await deleted.scrollIntoViewIfNeeded();await deleted.locator('button').first().click();
 await page.getByRole('button',{name:'Создать сделку',exact:true}).click();await page.getByText('При подтверждении лид будет восстановлен и конвертирован. История удаления сохранится.').waitFor();await page.locator('#lead-close').click();
 await page.setViewportSize({width:390,height:844});await page.evaluate(()=>document.documentElement.setAttribute('data-theme','dark'));
 await cards.last().scrollIntoViewIfNeeded();await cards.last().locator('button').first().tap();await page.getByRole('button',{name:'Создать сделку',exact:true}).tap();await page.getByLabel('Потребность',{exact:true}).waitFor();
 await page.screenshot({path:'/tmp/crm-kanban-mobile.png',fullPage:true});await page.locator('#lead-close').click();
 await page.setViewportSize({width:1440,height:800});await page.evaluate(()=>document.documentElement.setAttribute('data-theme','light'));await cards.last().scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/crm-kanban-desktop.png',fullPage:true});
 assert.equal(writes,0);assert.deepEqual(errors,[]);console.log('Long board: 18 cards, first 3 + lower + repeat, real drag/drop, Escape, deleted, mobile/dark and no cancellation writes PASS');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
