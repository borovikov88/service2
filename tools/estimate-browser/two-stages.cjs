const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(x,m)=>{if(!x)throw Error(m)};
 for(const width of [320,390,1365]){
  const page=await browser.newPage({viewport:{width,height:900}}),errors=[];let generated;
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/estimates/api/**',async route=>{
   if(route.request().method()==='POST' && route.request().postDataJSON().action==='generate'){
    const response=await route.fetch(),data=await response.json();generated=data.snapshot;
    const v=data.snapshot.variants;data.snapshot.variants={economy:v.economy,premium:v.premium,standard:v.standard};
    await route.fulfill({response,json:data});
   }else await route.continue();
  });
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8802/estimates/');
  await page.locator('[data-drag-row=pump]').waitFor();
  check(Boolean(generated),'controlled generated response');
  check(await page.locator('.pe-stage').count()===2,'two stages');
  check(await page.locator('.pe-stage').nth(0).locator('.pe-block').count()===2,'two bowl blocks');
  check(await page.locator('.pe-stage').nth(1).locator('.pe-block').count()===5,'five populated installation blocks');
  check(JSON.stringify(await page.locator('[data-variant]').evaluateAll(es=>es.map(e=>e.dataset.variant)))===JSON.stringify(['economy','standard','premium']),'stable card order after shuffled JSON');
  check(await page.locator('#pe-level-inlet-binding').count()===0,'no level model picker');
  const work=page.locator('table:has([data-drag-row=embedded_work])');
  check(await work.locator('[data-drag-row=equipment_work]').count()===1,'commissioning same table');
  check(await work.locator('[data-drag-row=finish_work]').count()===1,'finish same table');
  const extra=page.locator('section.pe-block').filter({has:page.locator('[data-add-view=extra_work]')});
  check(await extra.locator('[data-drag-row=work_accommodation]').count()===1,'accommodation in additional works');
  check(await page.locator('#pe-add-view option[value=extra_materials]').count()===1,'additional materials available');
  const before=await work.locator('tbody tr').evaluateAll(es=>es.map(e=>e.dataset.orderRow));
  const total=await page.locator('[data-variant=economy] .pe-total').textContent();
  const handle=work.locator('[data-drag-row=embedded_work]');
  await handle.press('ArrowDown');await page.waitForTimeout(400);
  const after=await work.locator('tbody tr').evaluateAll(es=>es.map(e=>e.dataset.orderRow));check(JSON.stringify(before)!==JSON.stringify(after),'unified work order changes across original sources');
  await page.getByRole('button',{name:'Пересчитать параметры',exact:true}).click();await page.waitForTimeout(600);
  check(JSON.stringify(await work.locator('tbody tr').evaluateAll(es=>es.map(e=>e.dataset.orderRow)))===JSON.stringify(after),'view order survives recalc');
  check(await page.locator('[data-variant=economy] .pe-total').textContent()===total,'totals unchanged after order');
  const materials=page.locator('table:has([data-drag-row=liner_corner_2m])');check(await materials.locator('[data-drag-row=piping]').count()===1,'piping moved to materials');
  check(await materials.locator('[data-row=liner_corner_2m][data-field=quantity]').inputValue()==='12','8x4 corner12pieces');
  await page.locator('[data-exclude-view=installation_work]').click();await page.locator('[data-scope-choice=one]').click();await page.waitForTimeout(400);
  check(await page.locator('[data-drag-row=embedded_work]').count()===0,'one unified work table excluded');
  check(await page.locator('[data-drag-row=pump]').count()===1,'equipment retained');
  await page.locator('[data-variant=standard]').click();check(await page.locator('[data-drag-row=embedded_work]').count()===1,'other quote retained');
  check(!errors.length,errors.join('\n'));
  await page.screenshot({path:`/workspace/two-stages-${width}.png`,fullPage:true});
  console.log(JSON.stringify({width,passed:true}));await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
