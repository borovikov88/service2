const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');
(async()=>{
 let input='';for await(const chunk of process.stdin)input+=chunk;
 const f=JSON.parse(input);assert.match(f.base,/^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000},serviceWorkers:'block'});
  await context.addCookies([{name:f.sessionCookieName,value:f.sessionCookie,url:f.base}]);
  const bundle=readFileSync(require.resolve('bootstrap/dist/js/bootstrap.bundle.min.js'),'utf8');
  await context.route('**/*',route=>{
   const url=route.request().url();
   if(url==='https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js')return route.fulfill({status:200,contentType:'application/javascript',body:bundle});
   return new URL(url).origin===f.base?route.continue():route.abort();
  });
  const page=await context.newPage(),errors=[],posts=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(r.method()==='POST')posts.push(r);});
  await page.goto(f.base+f.workspace);
  async function choose(){
   await page.locator(`[data-lead-id="${f.leadId}"] button`).first().click();
   await page.getByText('Действия с лидом',{exact:true}).click();
   await page.locator('#lead-dialog label').filter({has:page.getByText('Состояние',{exact:true})}).locator('select').selectOption('closed');
   await page.locator('#lead-dialog label').filter({has:page.getByText('Результат завершения',{exact:true})}).locator('select').selectOption('duplicate');
   await page.getByLabel('Подтверждённая потребность / сделка',{exact:true}).fill('Synthetic continuation');
   await page.locator('.lead-picker-results').getByRole('button',{name:'Synthetic continuation target',exact:true}).click();
  }
  await choose();assert.equal(posts.length,0,'Selecting a candidate must not save');
  await page.keyboard.press('Escape');
  assert.equal(posts.length,0,'Cancelling must not save');
  await choose();
  assert.ok(await page.locator('#lead-dialog').evaluate(n=>n.scrollWidth<=n.clientWidth+1));
  if(process.env.CRM_SYNTHETIC_SCREENSHOTS)await page.screenshot({path:process.env.CRM_SYNTHETIC_SCREENSHOTS+'/continuation-desktop.png'});
  const saved=page.waitForResponse(r=>new URL(r.url()).pathname===f.command&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Сохранить состояние',exact:true}).click();
  assert.equal((await saved).status(),200);
  const related=page.locator('#lead-dialog').getByRole('button',{name:'Synthetic continuation target',exact:true});await related.waitFor();
  assert.equal(posts.length,1);
  await page.setViewportSize({width:390,height:844});await page.emulateMedia({colorScheme:'dark'});await page.evaluate(()=>document.documentElement.setAttribute('data-theme','dark'));
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  if(process.env.CRM_SYNTHETIC_SCREENSHOTS)await page.screenshot({path:process.env.CRM_SYNTHETIC_SCREENSHOTS+'/continuation-mobile.png'});
  await related.click();
  await page.waitForFunction(()=>document.querySelector('#lead-dialog')?.textContent.includes('Synthetic continuation target'));
  await page.reload();await page.locator('#lead-dialog[open]').waitFor();
  assert.match(await page.locator('#lead-dialog').innerText(),/Synthetic continuation target/);
  assert.deepEqual(errors,[]);console.log('PASS continuation explicit choice/cancel/save/linked navigation/reload/desktop/mobile');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
