// Real browser actions: the pointer remains over the trigger under an overlapping
// popover. Its next hit-test after Escape must not reopen the dismissed help.
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(ok,message)=>{if(!ok)throw Error(message);};
 for(const width of [320,390,1180]){
  const page=await browser.newPage({viewport:{width,height:757}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.ESTIMATE_BROWSER_URL||'http://127.0.0.1:8785/estimates/');
  const button=page.getByRole('button',{name:'Подсказка: Подбор нагревателя',exact:true});
  const panel=page.getByRole('region',{name:'Подробности подсказки'});
  await button.press('Enter');
  check(await panel.isVisible(),'Enter opens heater help');
  const box=await button.boundingBox(),x=box.x+box.width/2,y=box.y+box.height/2;
  await page.mouse.move(x,y);
  await button.press('Escape');
  check(!await panel.isVisible(),'Escape closes immediately');
  await page.mouse.move(x,y); // Browser resamples the same stationary pointer.
  check(!await panel.isVisible(),'stationary pointer must not reopen after Escape');
  check(await button.getAttribute('aria-expanded')==='false','collapsed aria state');
  await page.mouse.move(1,1);
  await button.hover();
  check(await panel.isVisible(),'new pointer visit opens help');
  await button.press('Escape');
  await page.mouse.move(1,1);
  await page.locator('h1').click();
  await button.focus();
  check(await panel.isVisible(),'new focus visit opens help');
  await button.press('Enter');
  await button.press('Enter');
  check(!await panel.isVisible(),'repeat Enter closes pinned help');
  await page.mouse.move(x,y);
  check(!await panel.isVisible(),'repeat activation dismissal also persists');
  await button.press('Enter');
  check(await panel.isVisible(),'explicit activation reopens dismissed help');
  await page.getByRole('button',{name:'Закрыть подсказку',exact:true}).click();
  check(!await panel.isVisible(),'dismiss button closes');
  check(errors.length===0,errors.join('\n'));
  console.log(`${width}: heater Enter/Escape, pointer resampling, new hover/focus, repeat Enter, dismiss PASS`);
  await page.close();
 }
 await browser.close();
})().catch(error=>{console.error(error);process.exit(1)});
