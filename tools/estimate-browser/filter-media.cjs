const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(v,m)=>{if(!v)throw Error(m)};
 for(const width of [320,390,1365]){
  const page=await browser.newPage({viewport:{width,height:950}}),errors=[];let state;
  page.on('pageerror',e=>errors.push(e.message));page.on('response',async r=>{if(r.url().includes('/estimates/api')){try{const d=await r.json();if(d.snapshot)state=d.snapshot;}catch{}}});
  const media=()=>state.variants.economy.blocks.flatMap(b=>b.rows).find(r=>r.kind==='filter_media');
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8811/estimates/');
  await page.locator('[data-drag-row=filter_media_filter]').waitFor();
  check(media().quantity==='3.000','quartz 75/25=3');
  await page.locator('[data-variant=premium]').click();
  check((await page.locator('tr:has([data-drag-row=filter_media_filter])').textContent()).includes('стеклянный'),'premium glass');
  await page.locator('[data-variant=economy]').click();
  await page.locator('[data-shop=filter_media_filter]').click();await page.locator('[data-catalog-product="4"]').waitFor();
  check(await page.locator('#pe-catalog-brand').inputValue()==='','all brands default');
  check(await page.locator('[data-catalog-product]').count()===3,'all quartz and glass options including other brand');
  await page.locator('[data-catalog-product="4"]').click();await page.waitForTimeout(400);
  check(media().quantity==='8.000'&&media().catalog.id==='4','manual catalog replacement recalculates bags');
  const filterQty=page.locator('[data-field=quantity][data-row=filter]');await filterQty.fill('2');await filterQty.blur();await page.waitForTimeout(800);
  check(media().quantity==='15.000','dependent media follows filter qty');
  await page.getByRole('button',{name:'Пересчитать параметры',exact:true}).click();await page.waitForTimeout(650);
  check(media().catalog.id==='4'&&media().quantity==='15.000','manual material survives recalculation');
  await page.locator('[data-exclude-row=filter_media_filter]').click();await page.locator('[data-scope-choice=one]').click();await page.waitForTimeout(650);
  await page.getByRole('button',{name:'Пересчитать параметры',exact:true}).click();await page.waitForTimeout(500);
  check(media().excluded,'exclusion survives recalculation');
  check(!errors.length,errors.join('\n'));console.log(JSON.stringify({width,passed:true}));await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
