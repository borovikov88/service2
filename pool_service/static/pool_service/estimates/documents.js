// Document choices are version metadata; quote rows and prices are not edited here.
const fields=['client_name','client_address','object_address','proposal_number','proposal_date','equipment_warranty','installation_warranty','delivery_terms','payment_terms','validity','included','not_included','manager','conditions'];
const terms=fields.slice(5);
export function documentValues(form){
 const data=new FormData(form),values=Object.fromEntries(fields.map(k=>[k,String(data.get(k)||'')]));
 return {...values,variants:data.getAll('variants'),types:data.getAll('types'),technical_reviewed:data.has('technical_reviewed')};
}
export function installDocuments(root,context){
 const dialog=root.querySelector('#pe-documents-dialog'),form=root.querySelector('#pe-documents-form');
 const errors=root.querySelector('#pe-document-errors'),links=root.querySelector('#pe-document-links');
 if(!dialog||!form)return;
 let defaults=JSON.parse(root.querySelector('#pe-document-defaults')?.textContent||'{}'),pending=false;
 const message=text=>{errors.textContent=text;};
 root.querySelector('#pe-documents').addEventListener('click',()=>{
  const current=context.current();
  if(!current.snapshot)return context.error('Сначала создайте расчёт.');
  if(current.busy||current.parametersDirty)return context.error('Дождитесь расчёта и пересчитайте изменённые параметры.');
  if([...root.querySelectorAll('#pe-blocks input')].some(el=>!el.reportValidity()))return;
  const saved=current.snapshot.documents||{},values={...defaults,...saved};
  for(const key of fields)form.elements[key].value=values[key]||'';
  if(!form.elements.proposal_date.value){const d=new Date();d.setMinutes(d.getMinutes()-d.getTimezoneOffset());form.elements.proposal_date.value=d.toISOString().slice(0,10);}
  for(const el of form.querySelectorAll('[name=variants]'))el.checked=(saved.variants||['economy','standard']).includes(el.value);
  for(const el of form.querySelectorAll('[name=types]'))el.checked=(saved.types||['estimate','equipment']).includes(el.value);
  // Review is deliberate for this export, never inherited from an older version.
  form.elements.technical_reviewed.checked=false;
  const detail=form.querySelector('details');detail.open=!values.equipment_warranty||!values.installation_warranty;
  links.replaceChildren();message('');dialog.showModal();
 });
 form.addEventListener('input',()=>links.replaceChildren());
 form.addEventListener('invalid',()=>{form.querySelector('details').open=true;},true);
 form.addEventListener('submit',async event=>{
  event.preventDefault();if(pending)return;
  const values=documentValues(form);links.replaceChildren();message('');
  if(!values.variants.length||!values.types.length)return message('Выберите хотя бы один вариант и один документ.');
  const current=context.current();
  if(current.busy||current.parametersDirty)return message('Дождитесь расчёта и пересчитайте параметры.');
  pending=true;form.querySelector('[type=submit]').disabled=true;
  try{
   const candidate={...current.snapshot,documents:values};
   const check=await context.api({action:'document_check',snapshot:candidate});
   if(check.errors.length){message(check.errors.join('\n'));return;}
   const saved=await context.save(candidate);
   for(const type of values.types){
    const a=document.createElement('a');a.className='pe-btn';a.target='_blank';a.rel='noopener';
    a.href=`${location.pathname}${encodeURIComponent(saved.id)}/versions/${saved.loaded_version}/documents/?document=${type}&variants=${values.variants.join(',')}`;
    a.textContent=type==='estimate'?'Открыть смету · печать / PDF':'Открыть сравнение оборудования · печать / PDF';links.append(a);
   }
   const note=document.createElement('p');note.textContent='Откройте документ, проверьте предпросмотр и выберите «Печать / сохранить PDF». Каждый документ сохраняется отдельным файлом.';links.append(note);
  }catch(error){message(error.message);}finally{pending=false;form.querySelector('[type=submit]').disabled=false;}
 });
 root.querySelector('#pe-document-defaults-save')?.addEventListener('click',async()=>{
  if(pending)return;pending=true;
  try{const values=documentValues(form),data=await context.api({action:'document_defaults',values:Object.fromEntries(terms.map(k=>[k,values[k]]))});defaults=data.values;message('Общие условия сохранены. Данные клиента в них не включены.');}
  catch(error){message(error.message);}finally{pending=false;}
 });
 dialog.addEventListener('cancel',event=>{if(pending)event.preventDefault();});
}
