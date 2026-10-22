const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(v,m)=>{if(!v)throw Error(m)};
 for(const width of [320,390,1365]){
  const page=await browser.newPage({viewport:{width,height:950}}),errors=[];let state;
  page.on('pageerror',e=>errors.push(e.message));page.on('response',async r=>{if(r.url().includes('/estimates/api')){try{const d=await r.json();if(d.snapshot)state=d.snapshot;}catch{}}});
  const film=()=>state.variants.economy.blocks.flatMap(b=>b.rows).find(r=>r.id==='finish_material');
  const corner=()=>state.variants.economy.blocks.flatMap(b=>b.rows).find(r=>r.id==='liner_corner_2m');
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8814/estimates/');
  await page.locator('[data-drag-row=liner_corner_2m]').waitFor();
  check(corner().catalog.url.includes('0050032'),'corner linked to exact product');
  check(corner().catalog.image.endsWith('corner.jpg')&&corner().price==='900.00','corner image and price');
  await page.locator('[data-shop=finish_material]').click();await page.locator('[data-catalog-product="11"]').waitFor();
  check(await page.locator('#pe-catalog-brand').inputValue()==='','all liner brands default');
  check(await page.locator('[data-catalog-product]').count()===4,'all liners including unknown sizes');
  await page.locator('[data-catalog-product="11"]').click();await page.waitForTimeout(400);
  check(film().catalog.id==='11'&&Number(film().catalog.roll_area)===41.58,'generic liner converts');
  check(film().price_conversion_note.includes(`Справочно: ${Math.ceil(Number(film().quantity)/41.58)} целых рулонов`),'whole rolls only in reference: '+film().price_conversion_note);
  check(Number(film().amount)!==20000,'no full roll surcharge');
  await page.locator('[data-field=quantity][data-row=finish_material]').fill('100');await page.locator('[data-field=quantity][data-row=finish_material]').blur();await page.waitForTimeout(800);
  check(film().price_conversion_note.includes('Справочно: 3 целых рулонов'),'reference roll count follows quantity');
  await page.locator('[data-shop=finish_material]').click();await page.locator('[data-catalog-product="13"]').click();await page.waitForTimeout(400);
  check(film().catalog.id==='13'&&film().price===null&&film().amount===null,'unknown size is selectable without false price');
  const price=page.locator('[data-field=price][data-row=finish_material]');await price.fill('42');await price.blur();await page.waitForTimeout(800);
  check(film().unit_price_manual&&Number(film().amount)===4200,'manual confirmed m2 price accepted');
  await page.getByRole('button',{name:'Пересчитать параметры',exact:true}).click();await page.waitForTimeout(600);
  check(film().catalog.id==='13'&&Number(film().price)===42,'manual unknown liner survives recalculation');
  check(!errors.length,errors.join('\n'));console.log(JSON.stringify({width,passed:true}));await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
