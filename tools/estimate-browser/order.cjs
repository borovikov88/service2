const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(ok,msg)=>{if(!ok)throw Error(msg);};
 for(const width of [390,1365]){
  const page=await browser.newPage({viewport:{width,height:900},hasTouch:width<500}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8779/estimates/');
  const down=()=>page.locator('[data-move-row="pump"][data-direction="1"]');
  await down().waitFor();
  const rows=()=>page.locator('table:has([data-move-row="pump"]) tbody tr .pe-row-name').allTextContents();
  const before=await rows();
  const quantity=page.locator('[data-row="pump"][data-field="quantity"]');
  await quantity.fill('-1');await down().click();
  check(await quantity.inputValue()==='-1','invalid edit retained');
  check(JSON.stringify(await rows())===JSON.stringify(before),'invalid edit blocks reorder');
  await quantity.fill('1');await quantity.blur();
  const price=page.locator('[data-row="pump"][data-field="price"]');
  await price.fill('12.345');await down().click();
  check(await price.inputValue()==='12.345','invalid price precision retained');
  check(JSON.stringify(await rows())===JSON.stringify(before),'invalid price blocks reorder');
  await price.fill('12.34');await price.blur();
  await page.waitForResponse(r=>r.url().includes('/estimates/api')&&r.request().postDataJSON()?.action==='validate');
  await page.waitForFunction(()=>document.querySelector('[data-amount="pump"]')?.textContent==='12,3');
  const total=await page.locator('[data-variant="economy"] .pe-total').textContent();
  await down().scrollIntoViewIfNeeded();
  if(width<500)await down().tap();else {await down().focus();await page.keyboard.press('Enter');}
  const moved=await rows();check(moved[1]===before[0]&&moved[0]===before[1],'swap visible rows');
  await page.waitForResponse(r=>r.url().includes('/estimates/api')&&r.request().postDataJSON()?.action==='validate');
  check(JSON.stringify(await rows())===JSON.stringify(moved),'validation keeps order');
  check(await page.locator('[data-variant="economy"] .pe-total').textContent()===total,'total unchanged');
  await page.locator('[data-variant="standard"]').click();
  check((await rows())[0]===before[0],'other quote unaffected');
  await page.locator('[data-variant="economy"]').click();check(JSON.stringify(await rows())===JSON.stringify(moved),'return keeps order');
  await page.locator('[data-move-row="pump"][data-direction="-1"]').click();
  check(JSON.stringify(await rows())===JSON.stringify(before),'reverse restores order');
  check(!errors.length,errors.join('\n'));console.log(`${width}: touch/keyboard reorder, server validation, totals, quote isolation PASS`);
  await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
