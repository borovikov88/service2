const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-features=OverlayScrollbar']});
 const assert=(ok,msg)=>{if(!ok)throw Error(msg);};
 for(const width of [1180,390,320]){
  const context=await browser.newContext({viewport:{width,height:740},hasTouch:width<500});
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8774/estimates/');
  await page.waitForSelector('#pe-blocks .pe-help-button');
  await page.addStyleTag({content:'html{overflow-y:scroll!important;scrollbar-gutter:stable} body{min-height:3000px} ::-webkit-scrollbar{width:15px}'});
  const trigger=page.locator('[data-row="material_concrete_bowl"][data-field="price"]').locator('..').locator('.pe-help-button');
  await trigger.evaluate(el=>{el.style.cssText='position:fixed;right:0;top:100px;z-index:9999';});
  await trigger.click();
  const panel=page.locator('#pe-help-popover');await panel.waitFor({state:'visible'});
  async function bounds(label){
   const data=await panel.evaluate(el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,inner:innerWidth,client:document.documentElement.clientWidth,height:Math.min(innerHeight,document.documentElement.clientHeight),scrollHeight:el.scrollHeight,clientHeight:el.clientHeight};});
   assert(data.right<=data.client-8+0.5&&data.left>=7.5,`${label}: horizontal clipping ${JSON.stringify(data)}`);
   assert(data.bottom<=data.height-8+0.5&&data.top>=7.5,`${label}: vertical clipping ${JSON.stringify(data)}`);
   return data;
  }
  const normal=await bounds('normal '+width);
  assert(normal.inner>normal.client,'test must reserve a real layout scrollbar');
  await panel.evaluate(el=>{const text=document.createElement('div');text.textContent=('Длинная подсказка с техническими пояснениями. ').repeat(500);el.append(text);});
  await page.evaluate(()=>window.dispatchEvent(new Event('resize')));
  const long=await bounds('long '+width);assert(long.scrollHeight>long.clientHeight,'long help scrolls internally');
  await page.setViewportSize({width,height:240});await bounds('short height '+width);
  await page.screenshot({path:`/tmp/estimate-help-bounds-${width}.png`});
  assert(errors.length===0,errors.join('\n'));
  console.log(`width ${width}: inner=${normal.inner}, client=${normal.client}, panel.right=${normal.right}; long/short-height PASS; JS errors 0`);
  await context.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
