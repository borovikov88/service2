import test from 'node:test';import assert from 'node:assert/strict';
import {sections,moveViewRow} from '../static/pool_service/estimates/layout.js';
const row=(id,kind='work',extra={})=>({id,kind,excluded:false,amount:'100',...extra});
const fixture=()=>({blocks:[{id:'construction',excluded:false,rows:[row('bowl')]},{id:'embedded',excluded:false,rows:[row('install'),row('hidden','work',{excluded:true}),row('inlet','inlet')]},{id:'equipment',excluded:false,rows:[row('commission'),row('piping','other')]},{id:'finish',excluded:false,rows:[row('finish'),row('liner','other')]}]});
test('two stages have exactly two then four tables and unified works',()=>{
 const v=fixture(),s=sections(v);assert.deepEqual(s.map(x=>x.blocks.length),[2,4]);
 assert.deepEqual(s[1].blocks[3].rows.map(r=>r.id),['install','hidden','commission','finish']);
 assert.deepEqual(s[1].blocks[2].rows.map(r=>r.id),['liner','piping']);
});
test('move across sources within unified work table preserves hidden slots and source data',()=>{
 const v=fixture(),before=structuredClone(v.blocks);
 assert.ok(moveViewRow(v,'installation_work','install',1));
 assert.deepEqual(sections(v)[1].blocks[3].rows.map(r=>r.id),['commission','hidden','install','finish']);
 assert.deepEqual(v.blocks,before);assert.equal(moveViewRow(v,'materials','install',1),false);
 const copy=JSON.parse(JSON.stringify(v));assert.deepEqual(sections(copy),sections(v));
});
