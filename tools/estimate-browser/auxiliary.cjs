// Synthetic catalogue only; run server.py with ESTIMATE_BROWSER_CATALOG=auxiliary.
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(value,message)=>{if(!value)throw Error(message);};
 for(const width of [320,390,1365]){
  const page=await browser.newPage({viewport:{width,height:900},hasTouch:width<500}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8794/estimates/');
  await page.locator('[data-row-check="treatment"]').waitFor({state:'attached'});
  for(const [tier,name] of [['economy','Хлоратор-полуавтомат'],['standard','PH/RX'],['premium','Free Cl']]){
   await page.locator(`[data-variant="${tier}"]`).click();
   check((await page.locator('[data-row-check="treatment"]').locator('..').textContent()).includes(name),'correct dosing '+tier);
   check((await page.locator('[data-row-check="control_panel"]').locator('..').textContent()).includes('Панель управления фильтрацией'),'separate panel');
   check(await page.locator('[data-row="level_inlet"]').count()===0,'no separate extra inlet');
   check(await page.locator('#pe-level-inlet-binding-label').isVisible(),'binding control visible');
  }
  const select=page.locator('#pe-level-inlet-binding');
  const response=page.waitForResponse(r=>r.request().postDataJSON()?.action==='level_inlet_binding');
  await select.selectOption('');
  const json=await (await response).json();
  check(json.snapshot.variants.premium.level_inlet_link_manual,'explicit unlink saved');
  check(json.snapshot.variants.premium.level_inlet_row_id==='','empty binding retained');
  await page.screenshot({path:`/tmp/estimate-auxiliary-${width}.png`});
  check(!errors.length,errors.join('\n'));console.log(`${width}: three dosing modes, separate panel, shared-inlet UI and explicit binding PASS`);
  await page.close();
 }
 await browser.close();
})().catch(error=>{console.error(error);process.exit(1)});
