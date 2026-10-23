const form=document.querySelector('#document-choices'),button=document.querySelector('#document-print');
form?.addEventListener('submit',event=>{event.preventDefault();const selected=new FormData(form).getAll('variant');if(!selected.length)return alert('Выберите хотя бы один вариант.');const url=new URL(location.href);url.searchParams.set('variants',selected.join(','));location.assign(url);});
if(button&&!button.disabled){
 button.disabled=true;
 const images=[...document.images].map(img=>new Promise(resolve=>{
  const failed=()=>{img.hidden=true;const text=img.parentElement.querySelector('span');if(text)text.hidden=false;resolve();};
  if(img.complete){if(!img.naturalWidth)failed();else resolve();return;}
  img.addEventListener('load',resolve,{once:true});img.addEventListener('error',failed,{once:true});
  setTimeout(()=>{if(!img.complete)failed();else resolve();},8000);
 }));
 Promise.all([document.fonts?.ready,...images]).finally(()=>button.disabled=false);
 button.addEventListener('click',()=>window.print());
}
