import test from 'node:test';import assert from 'node:assert/strict';
import {addRow,emptyRow} from '../static/pool_service/estimates/state.js';
const fixture=()=>({variants:Object.fromEntries(['economy','standard','premium'].map(key=>[key,{blocks:[{id:'options',excluded:true,reason:'old scope',rows:[{...emptyRow('old'),price:'777'}]}]}]))});
test('new row one/all is explicit, clones data and restores only its own row',()=>{
 const original=fixture(),row={...emptyRow('new'),name:'Материал',price:'100',quantity:'2'};
 const one=addRow(original,'economy','options',row);
 assert.equal(one.variants.economy.blocks[0].rows.length,2);
 assert.equal(one.variants.standard.blocks[0].rows.length,1);
 assert.equal(one.variants.economy.blocks[0].excluded,false);
 assert.equal(one.variants.economy.blocks[0].rows[0].excluded,true);
 assert.equal(original.variants.economy.blocks[0].rows.length,1);
 const all=addRow(original,'economy','options',row,true);
 for(const v of Object.values(all.variants)){assert.equal(v.blocks[0].rows.length,2);assert.equal(v.blocks[0].rows[1].price,'100');}
 all.variants.economy.blocks[0].rows[1].specs.flow='20';
 assert.equal(all.variants.premium.blocks[0].rows[1].specs.flow,null);
 assert.equal(row.specs.flow,null);
});
test('capacity or identity conflicts reject the entire all-quote operation',()=>{
 const s=fixture();s.variants.premium.blocks[0].rows=Array.from({length:100},(_,i)=>emptyRow('r'+i));
 const before=structuredClone(s);
 assert.throws(()=>addRow(s,'economy','options',emptyRow('new'),true));assert.deepEqual(s,before);
 assert.throws(()=>addRow(fixture(),'economy','options',emptyRow('old'),true));
});
