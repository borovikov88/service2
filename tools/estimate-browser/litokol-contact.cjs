const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(v,m)=>{if(!v)throw Error(m)};
 for(const width of [320,390,1365]){
  const page=await browser.newPage({viewport:{width,height:950}}),errors=[];let state;
  page.on('pageerror',e=>errors.push(e.message));page.on('response',async r=>{if(r.url().includes('/estimates/api')){try{const d=await r.json();if(d.snapshot)state=d.snapshot;}catch{}}});
  const find=(id,tier='standard')=>state.variants[tier].blocks.flatMap(b=>b.rows).find(r=>r.id===id);
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8815/estimates/');
  await page.locator('[data-drag-row=work_contact_layer]').waitFor();
  const rows=await page.locator('table:has([data-drag-row=work_contact_layer]) [data-drag-row]').evaluateAll(els=>els.map(e=>e.dataset.dragRow));
  const wanted=['work_primer','work_contact_layer','work_plaster','embedded_work','finish_work','work_underlay','equipment_work','work_finish_transport','work_finish_travel'];
  const included=rows.filter(id=>wanted.includes(id));check(included.join()===wanted.join(),'screen work order: '+included.join());
  await page.locator('[data-variant=standard]').click();
  check(find('finish_geotextile').catalog.url.includes('geotekstil-aquaviva-300-gm2-50-m.html'),'preferred geotextile linked');
  check(find('finish_geotextile').price==='1.00','geotextile roll price normalized to m2');
  check(find('tech_plaster_material').catalog.id==='3','CR30 linked');check(find('tech_plaster_latex').catalog.id==='4','kg latex selected before cheap litres');
  await page.locator('[data-shop=tech_plaster_material]').click();await page.locator('[data-catalog-product="3"]').waitFor();
  check(await page.locator('#pe-catalog-kind').inputValue()==='plaster_component','Litokol catalogue category');
  check(await page.locator('#pe-catalog-query').inputValue()==='CR30','exact CR30 query');
  await page.locator('#pe-catalog-dialog [data-close=pe-catalog-dialog]').click();
  await page.locator('[data-shop=finish_geotextile]').click();await page.locator('[data-catalog-product="1"]').waitFor();
  check(await page.locator('#pe-catalog-query').inputValue()==='','old CR30 query does not hide geotextile');
  await page.locator('#pe-catalog-dialog [data-close=pe-catalog-dialog]').click();
  check(find('work_contact_layer').quantity===find('work_plaster').quantity,'contact quantity matches plaster');
  const qty=page.locator('[data-field=quantity][data-row=work_plaster]');await qty.fill('100');await qty.blur();await page.waitForTimeout(800);
  check(find('work_contact_layer').quantity==='100.000'&&find('work_contact_layer').amount==='25000.00','dependent work area and250rate');
  await page.getByRole('button',{name:'Пересчитать параметры',exact:true}).click();await page.waitForTimeout(650);
  check(find('work_contact_layer').quantity==='100.000','manual plaster quantity survives recalc and propagates');
  check(!errors.length,errors.join('\n'));console.log(JSON.stringify({width,passed:true}));await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
