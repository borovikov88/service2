(async function () {
  const report = [];
  const assert = (ok, message) => {if (!ok) throw Error(message);};
  const wait = (ms=320) => new Promise(resolve => setTimeout(resolve, ms));
  async function test(name, fn) {try {await fn(); report.push({name, ok:true});} catch(e) {report.push({name, ok:false, error:String(e.stack || e)});}}
  function field(name='client', n=1, {required=false, value='', multiple=false}={}) {
    const form = document.createElement('form');
    form.noValidate = true;
    const label = document.createElement('label');
    const select = document.createElement('select');
    select.name=name; select.id='source-'+document.forms.length;
    label.htmlFor=select.id; label.textContent='Client label';
    select.className='form-select'; select.required=required; select.multiple=multiple;
    select.add(new Option('All', ''));
    for(let i=1;i<=n;i++) select.add(new Option('Client '+i, String(i)));
    select.value=value;
    const submit=document.createElement('button');submit.type='submit';submit.textContent='Save';
    form.append(label, select, submit);document.body.append(form);
    window.initLargeChoiceSearch(form);
    const root=select.nextElementSibling;
    return {form,label,select,root, input:root?.querySelector('input'),list:root?.querySelector('[role=listbox]'),clear:root?.querySelector('.input-group button'),submit};
  }
  const type = async (p,value) => {p.input.value=value;p.input.dispatchEvent(new Event('input',{bubbles:true}));await wait();};
  const rows=p=>Array.from(p.list.children);
  const key=(p,key)=>p.input.dispatchEvent(new KeyboardEvent('keydown',{key,bubbles:true,cancelable:true}));

  await test('client field becomes an empty search and keeps original name/id/choices', async()=>{
    const p=field('client',40);
    assert(p.input && p.input.name==='', 'unnamed presentation input');
    assert(p.select.name==='client' && p.select.options.length===41,'server choices unchanged');
    assert(getComputedStyle(p.select).display==='none','native dropdown hidden');
    p.input.focus();await wait();
    assert(p.list.hidden && rows(p).length===0,'no initial suggestions');
    assert(p.input.getAttribute('aria-label')==='Client label','accessible name');
    assert(p.input.getAttribute('aria-controls')===p.list.id,'list linkage');
  });
  await test('one/two meaningful characters and punctuation never show a list', async()=>{
    const p=field();
    for(const q of ['', 'C','Cl',' - ',' C-l ']){await type(p,q);assert(p.list.hidden && rows(p).length===0,'too-short query '+q);}
  });
  await test('search is min3, bounded to 20 and clears old suggestions',async()=>{
    const p=field('client',45);
    await type(p,'Cli');assert(rows(p).length===20,'max20');
    assert(p.root.querySelector('[role=status]').textContent.includes('20'),'refine hint');
    await type(p,'Cl');assert(rows(p).length===0 && p.list.hidden,'shortening cancels');
  });
  await test('case, hyphens, spaces and Russian e/yo normalize',async()=>{
    const p=field();p.select.options[1].label='\u0421\u0422\u0420\u041e\u0419\u2013\u0418\u041d\u0412\u0415\u0421\u0422 \u0421\u0435\u043c\u0451\u043d';await wait();
    for(const q of ['\u0421\u0442\u0440\u043e\u0439 \u0438\u043d\u0432\u0435\u0441\u0442','\u0441\u0442\u0440\u043e\u0439-\u0438\u043d\u0432\u0435\u0441\u0442','\u0441\u0435\u043c\u0435\u043d']) {await type(p,q);assert(rows(p).length===1,'normalized '+q);}
  });
  await test('initial chosen value survives and label focuses the search',async()=>{
    const p=field('pool',2,{value:'2'});
    assert(p.input.value==='Client 2','saved selection visible');
    p.label.click();assert(document.activeElement===p.input,'label focuses search');
    assert(p.list.hidden,'saved selection does not open list');
  });
  await test('typing clears ID silently; choosing emits original change once',async()=>{
    const p=field('client',2,{value:'2'});let changes=0;p.select.addEventListener('change',()=>changes++);
    await type(p,'Client 1');assert(p.select.value==='' && changes===0,'do not auto-submit while typing');
    rows(p)[0].click();assert(p.select.value==='1' && changes===1,'one change on original');
    assert(new FormData(p.form).get('client')==='1','original field submitted');
  });
  await test('typed-but-not-selected input blocks even novalidate forms',async()=>{
    const p=field();await type(p,'Missing');
    const event=new Event('submit',{bubbles:true,cancelable:true});p.form.dispatchEvent(event);
    assert(event.defaultPrevented,'invalid selection blocked');assert(p.input.validationMessage,'message visible');
  });
  await test('clear chooses original empty option and optional form can submit',async()=>{
    const p=field('client',2,{value:'2'});let changes=0;p.select.addEventListener('change',()=>changes++);
    p.clear.click();assert(p.select.value==='' && changes===1 && p.input.value==='','clear');
    const event=new Event('submit',{bubbles:true,cancelable:true});p.form.dispatchEvent(event);
    assert(!event.defaultPrevented && p.input.validity.valid,'optional blank valid');
    assert(new FormData(p.form).get('client')==='','empty option preserved');
  });
  await test('required selection remains invalid and focuses visible input',async()=>{
    const p=field('client',1,{required:true});
    assert(!p.form.checkValidity(),'native required maintained');
    assert(document.activeElement===p.input,'invalid select forwarded');
    await type(p,'Cli');rows(p)[0].click();assert(p.form.checkValidity(),'chosen value valid');
  });
  await test('keyboard can choose with arrows and Enter, Escape does not close parent modal',async()=>{
    const p=field('client',3);await type(p,'Cli');key(p,'ArrowUp');
    assert(p.input.getAttribute('aria-activedescendant').endsWith('-2'),'last row active');
    key(p,'Enter');assert(p.select.value==='3','Enter chooses last');
    await type(p,'Cli');let bubbled=0;p.form.addEventListener('keydown',()=>bubbled++);
    key(p,'Escape');assert(p.list.hidden && !bubbled,'Escape locally contained');
  });
  await test('dependent choices update after DOM replacement and old option cannot be chosen',async()=>{
    const p=field();await type(p,'Cli');const old=rows(p)[0];
    p.select.replaceChildren(new Option('All',''),new Option('Updated object','9'));await wait();
    old.click();assert(p.select.value!=='1','detached option rejected');
    await type(p,'Updated');assert(rows(p).length===1,'new option searchable');
    rows(p)[0].click();assert(p.select.value==='9','dependent value chosen');
  });
  await test('disabled/hidden options and disabled groups are never offered',async()=>{
    const p=field('client',4);p.select.options[1].disabled=true;p.select.options[2].hidden=true;p.select.options[3].classList.add('d-none');
    const group=document.createElement('optgroup');group.disabled=true;group.append(new Option('Client grouped','5'));p.select.append(group);await wait();
    await type(p,'Client');assert(rows(p).length===1 && rows(p)[0].textContent==='Client 4','restricted options excluded');
  });
  await test('changing native select externally updates the visible value',async()=>{
    const p=field('pool',3);p.select.value='2';p.select.dispatchEvent(new Event('change',{bubbles:true}));
    assert(p.input.value==='Client 2','external change reflected');
  });
  await test('reset restores the original chosen option and no suggestions',async()=>{
    const p=field('client',2);p.select.options[2].defaultSelected=true;p.form.reset();await wait();
    await type(p,'Client 1');rows(p)[0].click();p.form.reset();await wait();
    assert(p.select.value==='2' && p.input.value==='Client 2' && p.list.hidden,'form reset');
  });
  await test('disabled field does not submit and reenables without losing selection',async()=>{
    const p=field('client',2,{value:'2'});p.select.disabled=true;await wait();
    assert(p.input.disabled && p.clear.disabled && !new FormData(p.form).has('client'),'disabled preserved');
    p.select.disabled=false;await wait();assert(!p.input.disabled && p.input.value==='Client 2','reenabled');
  });
  await test('empty/small status lists and hidden client fields are not rewritten',async()=>{
    for(const n of [0,3,20]){const p=field('direction',n);assert(!p.input,'small lists native');}
    const f=document.createElement('form'),s=document.createElement('select');s.name='client';s.hidden=true;s.add(new Option('Client','1'));f.append(s);document.body.append(f);window.initLargeChoiceSearch(f);
    assert(!f.querySelector('[data-large-choice-search]'),'hidden client untouched');
  });
  await test('large unrelated single list is searched, not only client/pool names',async()=>{
    const p=field('supplier',23);assert(p.input,'large supplier list enhanced');await type(p,'Cli');assert(rows(p).length===20,'shared cap');
  });
  await test('new modal controls initialize automatically and initialization is idempotent',async()=>{
    const modal=document.createElement('div');modal.innerHTML='<select name="pool"><option value="">All</option><option value="8">Modal object</option></select>';
    document.body.append(modal);await wait();assert(modal.querySelector('input'),'dynamic init');
    window.initLargeChoiceSearch(modal);window.initLargeChoiceSearch(modal);
    assert(modal.querySelectorAll('[data-large-choice-search]').length===1,'no duplicate init');
  });
  await test('a hidden field initializes when revealed, and enhanced fields can be hidden again',async()=>{
    const f=document.createElement('form'),s=document.createElement('select');s.name='client';s.hidden=true;s.add(new Option('Client','1'));f.append(s);document.body.append(f);await wait();
    s.hidden=false;await wait();const root=f.querySelector('[data-large-choice-search]');assert(root,'revealed initialization');
    s.hidden=true;await wait();assert(root.hidden && root.querySelector('input').disabled,'hide remains effective');
  });
  await test('malicious labels are text, not HTML',async()=>{
    const p=field();p.select.options[1].label='<img src=x onerror=window.injected=true>';await wait();await type(p,'img');
    assert(rows(p).length===1 && !p.list.querySelector('img') && !window.injected,'text only');
  });
  await test('clicking outside cancels the debounce and cannot reopen suggestions later',async()=>{
    const p=field();p.input.value='Client';p.input.dispatchEvent(new Event('input',{bubbles:true}));document.body.click();await wait();
    assert(p.list.hidden,'late render canceled');
  });
  await test('only one field shows suggestions at a time',async()=>{
    const a=field(),b=field();await type(a,'Client');await type(b,'Client');
    assert(a.list.hidden && !b.list.hidden,'previous picker closed');
  });
  await test('large custom multi-select keeps checked/locked values and starts blank',async()=>{
    const root=document.createElement('div');root.setAttribute('data-multi-select','');root.className='multi-select';
    root.innerHTML='<button type="button" data-multi-select-trigger><span class="multi-select__value"></span></button><div data-multi-select-list></div>';
    const list=root.lastElementChild;
    for(let i=0;i<25;i++){const label=document.createElement('label');label.className='multi-select__option';const check=document.createElement('input');check.type='checkbox';check.name='participants';check.value=String(i);check.checked=i===0;check.disabled=i===0;label.append(check,document.createTextNode('Participant '+i));list.append(label);}
    document.body.append(root);await wait();const input=root.querySelector('[data-participant-search]');assert(input,'large general multi initialized');
    let visible=()=>Array.from(list.querySelectorAll('.multi-select__option')).filter(x=>!x.hidden);assert(visible().length===1,'only locked checked shown');
    input.value='Par';input.dispatchEvent(new Event('input',{bubbles:true}));assert(visible().length===21,'20 suggestions + selected');assert(list.querySelector('input[type=checkbox]').checked && list.querySelector('input[type=checkbox]').disabled,'locked untouched');
  });

  await test('native large multiple-choice starts without a list and retains existing selections',async()=>{
    const p=field('customers',24,{multiple:true});
    p.select.options[2].selected=true;p.select.options[3].selected=true;p.select.dispatchEvent(new Event('change',{bubbles:true}));
    assert(p.input && p.list.hidden && rows(p).length===0,'multiple search without initial catalog');
    assert(p.root.querySelector('[data-chosen-options]').children.length===3,'selected values visible including original blank');
    await type(p,'Client');assert(new FormData(p.form).getAll('customers').join(',')===',2,3','typing preserves confirmed selections');
  });
  await test('native multiselect toggle preserves name, option identities and repeated form values',async()=>{
    const p=field('customers',24,{multiple:true});p.select.options[0].selected=false;
    const option=p.select.options[4];await type(p,'Client 4');rows(p)[0].click();
    assert(option.selected && p.select.options[4]===option,'original option toggled');
    assert(new FormData(p.form).getAll('customers').join(',')==='4','native array payload');
    await type(p,'Client 4');rows(p)[0].click();assert(!option.selected,'second selection removes');
  });
  await test('native multiselect clear and chips do not remove locked selections',async()=>{
    const p=field('customers',24,{multiple:true});p.select.options[0].selected=false;
    p.select.options[1].selected=true;p.select.options[1].disabled=true;p.select.options[2].selected=true;p.select.dispatchEvent(new Event('change',{bubbles:true}));
    const chips=p.root.querySelector('[data-chosen-options]').children;assert(chips[0].disabled && !chips[1].disabled,'locked chip');
    chips[1].click();assert(!p.select.options[2].selected && p.select.options[1].selected,'remove only unlocked');
    p.select.options[3].selected=true;p.clear.click();assert(!p.select.options[3].selected && p.select.options[1].selected,'clear preserves disabled choices');
  });
  await test('native required multiselect remains invalid until an actual selection',async()=>{
    const p=field('customers',24,{multiple:true,required:true});p.select.selectedIndex=-1;p.select.dispatchEvent(new Event('change',{bubbles:true}));
    assert(!p.form.checkValidity(),'required not lost');await type(p,'Client 7');rows(p)[0].click();assert(p.form.checkValidity(),'selected required multiple');
  });
  await test('native multiselect reset updates visible chips to initial selection',async()=>{
    const p=field('customers',24,{multiple:true});p.select.options[0].defaultSelected=false;p.select.options[8].defaultSelected=true;
    p.form.reset();await wait();assert(p.root.querySelector('[data-chosen-options]').textContent.includes('Client 8'),'reset original selection shown');
    await type(p,'Client 9');rows(p)[0].click();p.form.reset();await wait();
    assert(new FormData(p.form).getAll('customers').join(',')==='8','reset original payload');
  });
  await test('separate formsets preserve prefixed client and pool field names',async()=>{
    const a=field('form-0-client',3),b=field('form-1-pool',3);
    await type(a,'Client 1');rows(a)[0].click();await type(b,'Client 2');rows(b)[0].click();
    assert(new FormData(a.form).get('form-0-client')==='1' && new FormData(b.form).get('form-1-pool')==='2','separate formsets');
  });

  await test('required multiselect checks all selected values, not just the first empty option',async()=>{
    const p=field('customers',24,{multiple:true,required:true});p.select.options[4].selected=true;p.select.dispatchEvent(new Event('change',{bubbles:true}));
    assert(p.form.checkValidity(),'nonempty later selection is valid');
  });
  document.documentElement.dataset.testReport=btoa(unescape(encodeURIComponent(JSON.stringify(report))));
})();
