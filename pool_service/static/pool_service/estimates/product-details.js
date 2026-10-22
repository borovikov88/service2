const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function shopUrl(value){
 try{const u=new URL(value);return u.protocol==='https:'&&u.hostname==='shop.aqualine22.ru'&&!u.username&&!u.password&&(!u.port||u.port==='443')?u.href:'';}catch{return '';}
}
export function productDetails(row){
 const origin=row.catalog,url=shopUrl(origin?.url);
 const brand=esc(row.brand),identity=origin?.sku?`арт. ${esc(origin.sku)}`:origin?.id?`ID ${esc(origin.id)}`:'';
 if(!origin)return [brand,esc(row.model)].filter(Boolean).join(' · ');
 return [brand,identity?(url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" class="pe-product-link">${identity}</a>`:identity):''].filter(Boolean).join(' · ');
}

export function productTitle(row){
 const name=String(row.name||''),model=String(row.model||'');
 return model&&!name.toLowerCase().includes(model.toLowerCase())?name+' · '+model:name;
}
