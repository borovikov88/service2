const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(ok,msg)=>{if(!ok)throw Error(msg);};
 for(const width of [320,390,1365]){
  const page=await browser.newPage({viewport:{width,height:900},hasTouch:width<500}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8799/estimates/');
  const handle=page.locator('[data-drag-row="pump"]');await handle.waitFor();
  const table=page.locator('table:has([data-drag-row="pump"])');
  const ids=()=>table.locator('tbody tr').evaluateAll(rows=>rows.map(r=>r.dataset.orderRow));
  const before=await ids(),total=await page.locator('[data-variant="economy"] .pe-total').textContent();
  await handle.scrollIntoViewIfNeeded();
  check(await page.locator('[data-move-row]').count()===0,'old arrow buttons removed');
  const actions=table.locator('tbody tr').first().locator('.pe-actions-inline button');
  const boxes=await actions.evaluateAll(bs=>bs.map(b=>({y:b.getBoundingClientRect().y,h:b.getBoundingClientRect().height})));
  check(boxes.every(b=>Math.abs(b.y-boxes[0].y)<1),'actions share one line');
  check(await table.locator('[data-shop="pump"] svg').count()===1,'catalog icon');
  const heights=await table.locator('tbody tr').evaluateAll(rs=>rs.map(r=>r.getBoundingClientRect().height));
  const median=[...heights].sort((a,b)=>a-b)[Math.floor(heights.length/2)];
  check(median<=65,`compact median height ${median}`);
  const from=await handle.boundingBox(),to=await table.locator('tbody tr').nth(1).boundingBox();
  const x=from.x+from.width/2,y=from.y+from.height/2,end=to.y+to.height-3;
  if(width<500){
   const cdp=await page.context().newCDPSession(page);
   await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y}]});
   for(let i=1;i<=8;i++)await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x,y:y+(end-y)*i/8}]});
   await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  }else{
   await page.mouse.move(x,y);await page.mouse.down();await page.mouse.move(x,end,{steps:8});await page.mouse.up();
  }
  await page.waitForFunction(first=>document.querySelector('table:has([data-drag-row="pump"]) tbody tr')?.dataset.orderRow===first,before[1]);
  check((await ids())[1]===before[0],'drag swaps rows');
  await page.waitForTimeout(450);
  check(await page.locator('[data-variant="economy"] .pe-total').textContent()===total,'totals unchanged');
  await page.locator('[data-variant="standard"]').click();check(JSON.stringify(await ids())===JSON.stringify(before),'other quote untouched');
  await page.locator('[data-variant="economy"]').click();
  await handle.press('ArrowUp');check(JSON.stringify(await ids())===JSON.stringify(before),'keyboard restores order');
  if(width===1365){
   await handle.scrollIntoViewIfNeeded();const a=await handle.boundingBox(),b=await table.locator('tbody tr').nth(1).boundingBox();
   await page.mouse.move(a.x+10,a.y+10);await page.mouse.down();await page.mouse.move(a.x+10,b.y+b.height-3,{steps:5});await page.keyboard.press('Escape');await page.mouse.up();
   check(JSON.stringify(await ids())===JSON.stringify(before),'escape cancels');
   await page.setViewportSize({width,height:1600});
   const other=page.locator('[data-drag-row="pvc_embedded"]');
   await other.scrollIntoViewIfNeeded();await handle.scrollIntoViewIfNeeded();
   const start=await handle.boundingBox(),foreign=await other.boundingBox();
   check(foreign.y>=0 && foreign.y<1600,'other table visible');
   await page.mouse.move(start.x+10,start.y+10);await page.mouse.down();
   await page.mouse.move(foreign.x+10,foreign.y+10,{steps:8});await page.mouse.up();
   check(JSON.stringify(await ids())===JSON.stringify(before),'cross-table drag rejected');
  }
  await table.screenshot({path:`/tmp/estimate-compact-${width}.png`});
  check(!errors.length,errors.join('\n'));console.log(`${width}: drag/touch, keyboard, cancellation, compact ${median}px, unchanged totals PASS`);
  await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
