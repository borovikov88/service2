const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(ok,msg)=>{if(!ok)throw Error(msg);};
 for(const width of [320,390,1365]){
  const page=await browser.newPage({viewport:{width,height:900},hasTouch:width<500});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8777/estimates/');
  await page.waitForSelector('#pe-blocks .pe-help-button');
  const heater=page.locator('#pe-heating-title .pe-help-button');
  check(await heater.count()===1,'one general heater help at legend');
  check(await page.locator('label:has(input[type=checkbox]) .pe-help-button').count()===0,'no general help on last checkbox');
  await heater.click();check((await page.locator('#pe-help-popover').textContent()).includes('полную мощность'),'heater content');
  await page.keyboard.press('Escape');
  for(const variant of ['economy','standard','premium']){
   await page.locator(`[data-variant="${variant}"]`).click();
   check(await page.locator('#pe-editor-title .pe-help-button').count()===1,'editor help restored after render');
   const input=page.locator('[data-row="material_sand"][data-field="price"]');
   const button=input.locator('..').locator('.pe-help-corner');
   await button.scrollIntoViewIfNeeded();
   const geometry=await button.evaluate(el=>{
    const input=el.parentElement.querySelector('input'), ir=input.getBoundingClientRect(),br=el.getBoundingClientRect(),row=el.closest('tr');
    const before=row.getBoundingClientRect().height;el.style.display='none';const after=row.getBoundingClientRect().height;el.style.display='';
    const style=getComputedStyle(el),tap=getComputedStyle(el,'::before');
    return {before,after,gap:br.x-(parseFloat(tap.width)-br.width)/2-ir.right,border:style.borderTopWidth,padding:style.paddingTop,position:style.position,tap:parseFloat(tap.width)};
   });
   check(geometry.before===geometry.after,'corner never increases row height');
   check(geometry.gap>=0,'tap area clear of input/value/spinner');
   check(geometry.position==='absolute'&&geometry.border==='0px'&&geometry.padding==='0px'&&geometry.tap>=24,'compact accessible corner');
   await page.keyboard.press("Tab");await button.focus();check(await button.evaluate(el=>getComputedStyle(el).outlineStyle)!=='none','keyboard focus visible');
   await page.keyboard.press('Escape');
  }
  await page.locator('#pe-heating-title').scrollIntoViewIfNeeded();
  const tails=await page.locator('.pe-help-tail').evaluateAll(nodes=>nodes.filter(n=>n.getClientRects().length).every(n=>{const r=document.createRange();r.selectNodeContents(n);return r.getClientRects().length>0&&n.getBoundingClientRect().height<30;}));
  check(tails,'heading word and icon remain on same line');
  await page.screenshot({path:`/tmp/estimate-help-compact-${width}.png`});
  check(!errors.length,errors.join('\n'));console.log(`${width}: headings, 3 variants, corner geometry, tap gutter, focus PASS`);await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
