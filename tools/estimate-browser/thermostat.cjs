const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});const check=(x,m)=>{if(!x)throw Error(m)};
 for(const width of [320,390,1365]){
  const page=await browser.newPage({viewport:{width,height:950}}),errors=[];let lastSnapshot;page.on('response',async r=>{if(r.url().includes('/estimates/api')){try{const d=await r.json();if(d.snapshot)lastSnapshot=d.snapshot;}catch{}}});page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8808/estimates/');await page.locator('[data-drag-row=control_panel]').waitFor();
  check((await page.locator('tr:has([data-drag-row=control_panel])').textContent()).includes('AVP-01'),'confirmed panel');
  await page.locator('[name=heating_types][value=exchanger]').check();await page.getByRole('button',{name:'Пересчитать параметры',exact:true}).click();await page.waitForTimeout(500);
  check(await page.locator('[data-drag-row^=thermostat_]').count()===0,'panel suppresses separate thermostat');
  check((await page.locator('#pe-excluded-content').textContent()).includes('Температурой управляет выбранная панель'),'specific exclusion reason');
  await page.locator('[data-exclude-row=control_panel]').click();await page.locator('[data-scope-choice=one]').click();await page.waitForTimeout(650);
  await page.getByRole('button',{name:'Пересчитать параметры',exact:true}).click();await page.waitForTimeout(500);
  check(await page.locator('[data-drag-row^=thermostat_]').count()===1,'restored thermostat after panel excluded');
  check((await page.locator('[data-row^=thermostat_][data-field=price]').inputValue()).replace(/\s/g,'')==='100','economy selected approved thermostat fixture price');
  check(await page.locator('table:has([data-drag-row=pump]) [data-drag-row^=thermostat_]').count()===1,'thermostat in equipment');
  await page.locator('#pe-excluded>summary').click();await page.locator('[data-restore-row=control_panel]').click();await page.locator('[data-scope-choice=one]').click();await page.waitForTimeout(650);
  check(await page.locator('[data-drag-row^=thermostat_]').count()===0,'panel restoration excludes thermostat');
  await page.locator('[data-restore-row^=thermostat_]').click();await page.locator('[data-scope-choice=one]').click();await page.waitForTimeout(650);
  check(await page.locator('[data-drag-row^=thermostat_]').count()===1,'explicit manual restoration respected');
  const manual=lastSnapshot?.variants.economy.blocks.flatMap(b=>b.rows).find(r=>r.kind==='thermostat');check(manual?.selection_note.includes('ручной термостат'),'duplicate manual warning');
  await page.locator('tr:has([data-drag-row^=thermostat_])').getByRole('button',{name:'Подсказка: Цифровой контроллер-термостат Aquaviva',exact:true}).hover();
  const hint=page.getByRole('region',{name:'Подробности подсказки'});await hint.waitFor({state:'visible'});check((await hint.textContent()).includes('ручной термостат'),'warning accessible in help');
  await page.locator('[data-variant=standard]').click();check(await page.locator('[data-drag-row^=thermostat_]').count()===0,'other quote unaffected');
  check(!errors.length,errors.join('\n'));console.log(JSON.stringify({width,passed:true}));await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
