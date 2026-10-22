const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(x,m)=>{if(!x)throw Error(m)};
 for(const width of [320,390,1365]){
  const page=await browser.newPage({viewport:{width,height:950}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8807/estimates/');await page.locator('[data-drag-row=pump]').waitFor();
  check(await page.locator('.pe-block').evaluateAll(es=>es.every(e=>e.querySelector('tbody tr'))),'only populated blocks rendered');
  check(await page.locator('[data-add-view=extra_materials]').count()===0,'empty additional materials hidden');
  for(const [id,expected] of [['pvc_embedded','48000'],['piping','57600']])check((await page.locator(`[data-row=${id}][data-field=price]`).inputValue()).replace(/\s/g,'')===expected,'new PVC price '+id);
  const materialId=await page.locator('[data-estimate-stage=stage1] .pe-block').nth(1).locator('[data-drag-row]').first().getAttribute('data-drag-row');
  await page.locator('[data-scope=construction]').click();await page.locator('[data-scope-choice=one]').click();await page.waitForTimeout(350);
  check(await page.locator('[data-estimate-stage=stage1]').count()===0,'whole empty stage removed');
  check(await page.locator('.pe-stage>h2').count()===0,'both captions hidden');
  check(!(await page.locator('#pe-blocks').textContent()).includes('В этом блоке пока нет'),'no empty placeholders');
  await page.locator('#pe-excluded>summary').click();await page.locator(`[data-restore-row=${materialId}]`).click();await page.locator('[data-scope-choice=one]').click();await page.waitForTimeout(350);
  check(await page.locator('[data-estimate-stage=stage1] .pe-block').count()===1,'only restored block returns');
  check(await page.locator('.pe-stage>h2').count()===2,'captions restored');
  async function draft(view,name,price){
   await page.locator('#pe-add-view').selectOption(view);await page.locator('[data-add-selected]').click();
   const f=page.locator('#pe-row-form');await f.locator('[name=name]').fill(name);await f.locator('[name=unit]').fill('шт.');await f.locator('[name=quantity]').fill('2');await f.locator('[name=price]').fill(price);await f.getByRole('button',{name:'Продолжить',exact:true}).click();
   await page.locator('#pe-add-row-dialog').waitFor({state:'visible'});
  }
  await draft('extra_materials','Материал только эконом','1000');
  await page.locator('[data-close=pe-add-row-dialog]').click();check(await page.locator('#pe-row-dialog').isVisible(),'cancel keeps draft');
  await page.locator('#pe-row-form').getByRole('button',{name:'Продолжить',exact:true}).click();await page.locator('[data-add-row-choice=one]').click();await page.waitForTimeout(400);
  check(await page.locator('.pe-row-name').filter({hasText:'Материал только эконом'}).count()===1,'one material added');
  await draft('extra_materials','Материал все варианты','500');await page.locator('[data-add-row-choice=all]').click();await page.waitForTimeout(400);
  await draft('extra_work','Работа все варианты','700');await page.locator('[data-add-row-choice=all]').click();await page.waitForTimeout(400);
  for(const tier of ['economy','standard','premium']){
   await page.locator(`[data-variant=${tier}]`).click();
   check(await page.locator('.pe-row-name').filter({hasText:'Материал все варианты'}).count()===1,'all material '+tier);
   check(await page.locator('.pe-row-name').filter({hasText:'Работа все варианты'}).count()===1,'all work '+tier);
   check(await page.locator('.pe-row-name').filter({hasText:'Материал только эконом'}).count()===(tier==='economy'?1:0),'scope preserved '+tier);
   check(await page.locator('.pe-block').evaluateAll(es=>es.every(e=>e.querySelector('tbody tr'))),'no empty blocks '+tier);
  }
  check(!errors.length,errors.join('\n'));await page.screenshot({path:`/workspace/empty-blocks-${width}.png`,fullPage:true});console.log(JSON.stringify({width,passed:true}));await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
