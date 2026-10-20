// Run server.py with ESTIMATE_BROWSER_CATALOG=preliminary (synthetic prices only).
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(ok,msg)=>{if(!ok)throw Error(msg);};
 for(const width of [320,390,1365]){
  const page=await browser.newPage({viewport:{width,height:900},hasTouch:width<500}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8782/estimates/');
  await page.waitForSelector('td:has([data-row-check="pump"]) .pe-help-warning');
  for(const variant of ['economy','standard','premium']){
   await page.locator(`[data-variant="${variant}"]`).click();
   for(const kind of ['pump','filter']){
    const row=page.locator(`[data-row-check="${kind}"]`).locator('..');
    check((await row.textContent()).includes('Synthetic unknown'),'actual candidate selected');
    const trigger=row.locator('.pe-help-warning');
    await trigger.scrollIntoViewIfNeeded();
    if(width<500)await trigger.tap();else await trigger.click();
    const panel=page.locator('#pe-help-popover');await panel.waitFor({state:'visible'});
    const text=await panel.textContent();
    check(text.includes('производительность модели'),'specific missing capacity');
    check(text.includes('совместимость')||text.includes('Совместимость'),'no engineering approval');
    check(text.includes('shop.aqualine22.ru/product-90'),'catalogue source retained');
    await page.keyboard.press('Escape');
   }
  }
  await page.screenshot({path:`/tmp/estimate-preliminary-${width}.png`});
  check(errors.length===0,errors.join('\n'));console.log(`${width}: all 3 quotes, visible Verify, missing facts and source PASS`);
  await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
