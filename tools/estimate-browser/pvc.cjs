const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 for(const width of [390,1365]){
  const page=await browser.newPage({viewport:{width,height:900}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8795/estimates/');
  const price=page.locator('[data-row="piping"][data-field="price"]');
  await price.waitFor();
  const check=(ok,msg)=>{if(!ok)throw Error(msg);};
  check(Number(await price.inputValue())===38400,'equipment price');
  check(Number(await page.locator('[data-row="pvc_embedded"][data-field="price"]').inputValue())===32000,'embedded price');
  const response=page.waitForResponse(r=>r.request().postDataJSON()?.action==='validate');
  await price.fill('');await price.blur();
  const data=await(await response).json();
  const row=data.snapshot.variants.economy.blocks.find(b=>b.id==='equipment').rows.find(r=>r.id==='piping');
  check(row.price===null && row.unit_price_manual,'clear stays manual');
  await page.locator('[data-variant="standard"]').click();
  check(Number(await price.inputValue())===38400,'other quote unchanged');
  await page.locator('[data-variant="economy"]').click();
  check(await price.inputValue()==='','manual clear persists');
  check(!errors.length,errors.join('\n'));console.log(`${width}: two PVC totals, manual clear, quote isolation PASS`);
  await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
