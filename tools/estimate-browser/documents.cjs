const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const check=(v,m)=>{if(!v)throw Error(m)},page=await browser.newPage({viewport:{width:1365,height:950}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:8818/estimates/');
 await page.getByRole('button',{name:'Документы для клиента',exact:true}).click();
 const form=page.locator('#pe-documents-form');await form.waitFor({state:'visible'});
 check(!await form.locator('[name=variants][value=premium]').isChecked(),'premium not selected by default');
 await form.locator('[name=client_name]').fill('Synthetic customer');await form.locator('[name=object_address]').fill('Synthetic address');
 await form.locator('[name=technical_reviewed]').check();await form.locator('[type=submit]').click();
 const links=page.locator('#pe-document-links a');await links.first().waitFor();check(await links.count()===2,'two separate customer documents');
 for(const type of ['estimate','equipment']){
  const href=await links.nth(type==='estimate'?0:1).getAttribute('href'),documentPage=await browser.newPage();
  await documentPage.goto('http://127.0.0.1:8818'+href);await documentPage.locator('#document-print:enabled').waitFor();
  const text=await documentPage.locator('.document').innerText();
  check(!text.includes('PREMIUM-SECRET')&&!text.includes('INTERNAL-'),'no unselected tier or internal notes');
  check(text.includes('Synthetic customer')&&text.includes('Synthetic address'),'client data');
  await documentPage.emulateMedia({media:'print'});
  check(await documentPage.locator('.document-toolbar').isHidden(),'controls hidden in print');
  await documentPage.pdf({path:`/workspace/client-${type}-preview.pdf`,printBackground:true,preferCSSPageSize:true});
  if(type==='estimate'){check(await documentPage.locator('.quote-table th:nth-child(3)').first().evaluate(el=>el.clientWidth)>=35,'unit column has room');await documentPage.emulateMedia({media:'screen'});await documentPage.screenshot({path:'/workspace/client-estimate-preview.png',fullPage:true});}
  if(type==='equipment'){
   const url=new URL(documentPage.url());url.searchParams.set('variants','economy,standard,premium');await documentPage.goto(url.href);await documentPage.locator('#document-print:enabled').waitFor({state:'attached'});await documentPage.pdf({path:'/workspace/client-equipment-three-preview.pdf',printBackground:true,preferCSSPageSize:true});
   check(await documentPage.locator('.comparison-table thead th').count()===4,'three comparison columns');
  }
  await documentPage.close();
 }
 await page.setViewportSize({width:390,height:844});
 check(await form.evaluate(el=>el.getBoundingClientRect().width)<=390,'mobile dialog fits');
 check(await form.locator('[type=submit]').isVisible(),'mobile export action accessible');
 check(!errors.length,errors.join('\n'));console.log('PASS: selected variants, separate docs, client details, no internal notes, print PDF, mobile dialog');
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
