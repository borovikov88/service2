const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(ok,msg)=>{if(!ok)throw Error(msg);};
 for(const width of [320,390,1365]){
  const page=await browser.newPage({viewport:{width,height:900}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8801/estimates/');
  await page.locator('[data-drag-row="pump"]').waitFor();
  check(await page.locator('.pe-stage>h2').count()===3,'materials plus two labor stages');
  check(await page.locator('[name=lighting]').isVisible(),'lighting independent of heating');
  const price=page.locator('[data-row="pump"][data-field="price"]');
  await price.fill('12345');await price.press('Tab');await page.waitForTimeout(500);
  check(await price.inputValue()==='12 345','whole grouped inline price');
  await page.locator('[data-edit="pump"]').click();
  check(await page.locator('#pe-row-form [name=price]').inputValue()==='12 345','first modal price');
  await page.locator('[data-close="pe-row-dialog"]').click();
  await page.locator('[data-edit="filter"]').click();
  await page.locator('#pe-row-form [name=price]').fill('999');
  await page.locator('#pe-row-form button[type=submit]').click();await page.waitForTimeout(500);
  check(await page.locator('[data-row="filter"][data-field="price"]').inputValue()==='999','second modal independent state');
  const warning=page.locator('.pe-row-warning-icon').first();
  check(await warning.textContent()==='!','warning icon only');
  check(await warning.evaluate(el=>el.closest('.pe-row-name')!==null),'warning beside name');
  await warning.click();check(await page.locator('#pe-help-popover').isVisible(),'warning details open');
  await warning.press('Escape');check(!await page.locator('#pe-help-popover').isVisible(),'Escape remains closed');
  await page.locator('[name=lighting]').selectOption('yes');
  await page.locator('[name=light_count]').fill('2');
  await page.getByRole('button',{name:'Пересчитать параметры',exact:true}).click();
  await page.locator('[data-drag-row=pool_light_0]').waitFor();
  check(await page.locator('[data-row=pool_light_0][data-field=quantity]').inputValue()==='2','lighting count');
  check(await page.locator('[data-drag-row=light_transformer_0]').count()===1,'one transformer row');
  check(!errors.length,errors.join('\n'));
  await page.screenshot({path:`/workspace/stages-lighting-${width}.png`,fullPage:true});
  console.log(JSON.stringify({width,passed:true}));await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
