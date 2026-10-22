import {isWork} from './state.js?v=11';
export const groups=[
 {id:'stage1',title:'Этап 1. Строительство чаши',blocks:[['bowl_work','Работы'],['bowl_materials','Материалы для чаши бассейна']]},
 {id:'stage2',title:'Этап 2. Монтаж оборудования и отделка',blocks:[['embedded','Закладные элементы'],['equipment','Оборудование'],['materials','Материалы'],['installation_work','Работы']]},
];
export function groupFor(block,row){
 if(isWork(row))return block.id==='construction'&&!/transport|travel|handling|delivery/.test(row.work_code||'')?'bowl_work':'installation_work';
 if(['construction','construction_materials'].includes(block.id))return 'bowl_materials';
 if(['light_transformer','light_controller'].includes(row.kind))return 'equipment';
 if(row.id==='piping')return 'materials';
 return ['embedded','equipment'].includes(block.id)?block.id:'materials';
}
export function sections(variant){
 const all=variant.blocks.flatMap(b=>b.rows.map(row=>({...row,source_block:b.id,source_excluded:b.excluded,group:groupFor(b,row)})));
 return groups.map(stage=>({...stage,blocks:stage.blocks.map(([id,title])=>{
  const order=variant.view_order?.[id]||[],rank=new Map(order.map((rid,i)=>[rid,i]));
  const rows=all.filter(r=>r.group===id).sort((a,b)=>Number(['pvc_embedded','piping'].includes(a.id))-Number(['pvc_embedded','piping'].includes(b.id)) || (rank.get(a.id)??rank.size)-(rank.get(b.id)??rank.size));
  return {id,title,rows,subtotal:rows.filter(r=>!r.source_excluded&&!r.excluded).reduce((s,r)=>s+Number(r.amount||0),0)};
 })}));
}
export function moveViewRow(variant,viewId,rowId,direction){
 const view=sections(variant).flatMap(s=>s.blocks).find(b=>b.id===viewId);if(!view)return false;
 const ids=view.rows.map(r=>r.id),visible=view.rows.map((r,i)=>({r,i})).filter(({r})=>!r.excluded&&!r.source_excluded);
 const from=visible.findIndex(({r})=>r.id===rowId),to=from+direction;
 if(![-1,1].includes(direction)||from<0||to<0||to>=visible.length)return false;
 if(['pvc_embedded','piping'].includes(rowId)||['pvc_embedded','piping'].includes(visible[to].r.id))return false;
 const a=visible[from].i,b=visible[to].i;[ids[a],ids[b]]=[ids[b],ids[a]];
 variant.view_order??={};variant.view_order[viewId]=ids;return true;
}
