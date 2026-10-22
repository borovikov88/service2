const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(x,m)=>{if(!x)throw Error(m)};
 for(const width of [320,390,1365]){
  const page=await browser.newPage({viewport:{width,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8804/estimates/');await page.locator('[data-drag-row=pump]').waitFor();
  check(await page.locator('[data-row^=drain_][data-field=quantity]').inputValue()==='1','one drain for empties-only48m3');
  await page.locator('[name=lighting]').selectOption('yes');await page.locator('[name=light_count]').fill('2');
  await page.getByRole('button',{name:'Пересчитать параметры',exact:true}).click();await page.locator('[data-drag-row=pool_light_0]').waitFor();
  const lamp=page.locator('tr:has([data-drag-row=pool_light_0])');check((await lamp.textContent()).includes('White'),'white initial light');check(!(await lamp.textContent()).includes('фонтан'),'no fountain candidate');
  check(await lamp.locator('.pe-row-detail a').count()===1,'SKU/ID linked to shop');check((await lamp.locator('.pe-row-detail a').getAttribute('href')).startsWith('https://shop.aqualine22.ru/'),'shop host');
  const equipment=page.locator('table:has([data-drag-row=pump])');check(await equipment.locator('[data-drag-row=light_transformer_0]').count()===1,'transformer in equipment');
  check(await page.locator('table:has([data-drag-row=embedded_work])').count()===1,'unified labor');
  await page.locator('[name=light_color]').selectOption('rgb');await page.getByRole('button',{name:'Пересчитать параметры',exact:true}).click();await page.locator('[data-drag-row=light_controller_0]').waitFor();
  check((await lamp.textContent()).includes('RGB'),'RGB light');check((await equipment.textContent()).includes('RC-01'),'RGB controller');
  const baseline=Number(await page.locator('[data-row=embedded_work][data-field=quantity]').inputValue());
  await page.locator('[data-row=pool_light_0][data-field=quantity]').fill('4');await page.locator('[data-row=pool_light_0][data-field=quantity]').press('Tab');await page.waitForTimeout(600);
  check(await page.locator('[data-row=light_niche_0][data-field=quantity]').inputValue()==='4','niche follows manual lamp count');
  check(await page.locator('[data-row=light_box_0][data-field=quantity]').inputValue()==='4','boxes follow manual lamp count');
  check(Number(await page.locator('[data-row=embedded_work][data-field=quantity]').inputValue())===baseline+4,'installation points follow lamps and boxes');
  const skim=page.locator('[data-row^=skimmer_][data-field=quantity]');await skim.fill('4');await skim.press('Tab');await page.waitForTimeout(600);
  check(Number(await page.locator('[data-row=embedded_work][data-field=quantity]').inputValue())===baseline+6,'installation follows manual skimmer count');
  for(const rid of ['pvc_embedded','piping']){
   const table=page.locator(`table:has([data-drag-row=${rid}])`);check(await table.locator('tbody tr').last().getAttribute('data-order-row')===rid,'PVC last '+rid);
  }
  await page.locator('[data-scope=construction]').click();await page.locator('[data-scope-choice=one]').click();await page.waitForTimeout(400);
  check(await page.locator('.pe-stage').first().locator('tbody tr').count()===0,'construction work and materials both excluded');check(await page.locator('[data-drag-row=pump]').count()===1,'equipment preserved');
  await page.locator('[data-variant=standard]').click();check(await page.locator('.pe-stage').first().locator('tbody tr').count()>0,'other quote preserved');
  await page.locator('[data-variant=economy]').click();await page.locator('[data-scope=construction]').click();await page.locator('[data-scope-choice=one]').click();await page.waitForTimeout(400);check(await page.locator('.pe-stage').first().locator('tbody tr').count()>0,'construction restored');
  await page.locator('[name=light_color]').selectOption('white');await page.getByRole('button',{name:'Пересчитать параметры',exact:true}).click();await page.waitForTimeout(500);
  check(await page.locator('[data-drag-row=light_controller_0]').count()===0,'white disables controller');check(await page.locator('#pe-excluded-content').textContent().then(s=>s.includes('RC-01')),'controller retained in exclusions');
  check(!errors.length,errors.join('\n'));await page.screenshot({path:`/workspace/lighting-links-${width}.png`,fullPage:true});console.log(JSON.stringify({width,passed:true}));await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
