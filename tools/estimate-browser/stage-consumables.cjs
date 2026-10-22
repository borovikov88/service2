const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(x,m)=>{if(!x)throw Error(m)};
 for(const width of [320,390,1365]){
  const page=await browser.newPage({viewport:{width,height:950}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8806/estimates/');await page.locator('[data-drag-row=pump]').waitFor();
  check(await page.locator('.pe-stage>h2').count()===2,'initial captions');
  const header=page.locator('.pe-stage>h2').first();
  check(Number(await header.evaluate(e=>parseFloat(getComputedStyle(e).paddingLeft)))>=20,'header inset');
  check((await page.locator('[data-row=finish_consumables][data-field=price]').inputValue()).replace(/\s/g,'')==='26000','71.6m2 allowance rounded to26000');
  const materialId=await page.locator('section.pe-block').filter({has:page.locator('[data-add-view=bowl_materials]')}).locator('[data-drag-row]').first().getAttribute('data-drag-row');
  for(const id of ['bowl_work','bowl_materials']){
   await page.locator(`[data-exclude-view=${id}]`).click();await page.locator('[data-scope-choice=one]').click();await page.waitForTimeout(350);
  }
  check(await page.locator('.pe-stage>h2').count()===0,'manual exclusion hides BOTH captions');
  check(await page.locator('[data-drag-row=finish_consumables]').count()===1,'stage2 allowance preserved');
  await page.locator('#pe-excluded>summary').click();
  await page.locator(`[data-restore-row=${materialId}]`).click();await page.locator('[data-scope-choice=one]').click();await page.waitForTimeout(350);
  check(await page.locator('.pe-stage>h2').count()===2,'one restored row returns BOTH captions');
  await page.locator('[data-scope=construction]').click();await page.locator('[data-scope-choice=one]').click();await page.waitForTimeout(350);
  check(await page.locator('.pe-stage>h2').count()===0,'checkbox excludes both captions');
  await page.locator('[data-variant=standard]').click();check(await page.locator('.pe-stage>h2').count()===2,'other quote preserved');
  await page.locator('[data-variant=economy]').click();await page.locator('[data-scope=construction]').click();await page.locator('[data-scope-choice=one]').click();await page.waitForTimeout(350);
  check(await page.locator('.pe-stage>h2').count()===2,'checkbox restoration returns captions');
  check(!errors.length,errors.join('\n'));await page.screenshot({path:`/workspace/stage-consumables-${width}.png`,fullPage:true});
  console.log(JSON.stringify({width,passed:true}));await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
