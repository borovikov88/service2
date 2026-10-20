import test from 'node:test';
import assert from 'node:assert/strict';
import {setExcluded, emptyRow} from '../static/pool_service/estimates/state.js';

function sample() {
  return {variants: Object.fromEntries(['economy', 'standard', 'premium'].map(key => [key, {blocks: [{id: 'equipment', excluded: false, reason: '', rows: [emptyRow('pump'), {...emptyRow('filter'), excluded: true, price: '120'}]}]}]))};
}
test('restoring a block preserves earlier individual exclusions and prices', () => {
  const s = sample();
  setExcluded(s, 'economy', 'equipment', null, true);
  setExcluded(s, 'economy', 'equipment', null, false);
  assert.equal(s.variants.economy.blocks[0].rows[1].excluded, true);
  assert.equal(s.variants.economy.blocks[0].rows[1].price, '120');
  assert.equal(s.variants.economy.blocks[0].rows.length, 2);
});
test('default scope edits only one variant, explicit all updates three', () => {
  const s = sample();
  setExcluded(s, 'economy', 'equipment', 'pump', true);
  assert.equal(s.variants.standard.blocks[0].rows[0].excluded, false);
  setExcluded(s, 'economy', 'equipment', 'pump', true, true);
  assert.ok(Object.values(s.variants).every(v => v.blocks[0].rows[0].excluded));
});
test('restore one row from an excluded block does not restore siblings', () => {
  const s = sample();
  s.variants.economy.blocks[0].rows[1].excluded = false;
  setExcluded(s, 'economy', 'equipment', null, true);
  setExcluded(s, 'economy', 'equipment', 'pump', false);
  const b = s.variants.economy.blocks[0];
  assert.equal(b.excluded, false);
  assert.equal(b.rows[0].excluded, false);
  assert.equal(b.rows[1].excluded, true);
});
test('all-quote actions match embedded roles across different materials, without restoring old sets', () => {
  const s = sample();
  for (const [key,v] of Object.entries(s.variants)) {
    v.embedded_profile = key;
    v.blocks[0].id='embedded';
    v.blocks[0].rows=[{...emptyRow('skimmer_'+key),embedded_role:'skimmer',embedded_profile:key}, {...emptyRow('old_'+key),embedded_role:'skimmer',embedded_profile:'old',excluded:true}];
  }
  setExcluded(s,'economy','embedded','skimmer_economy',true,true);
  assert.ok(Object.values(s.variants).every(v=>v.blocks[0].rows[0].excluded));
  setExcluded(s,'economy','embedded','skimmer_economy',false,true);
  assert.ok(Object.values(s.variants).every(v=>!v.blocks[0].rows[0].excluded && v.blocks[0].rows[1].excluded));
});

import {isWork, samePrice, workPriceTargets, setWorkPrice} from '../static/pool_service/estimates/state.js';
function workSample() {
  const s = sample();
  for (const v of Object.values(s.variants)) v.blocks[0].rows = [{...emptyRow('labor'), kind:'work', work_code:'install', price:'10', quantity:'3'}, {...emptyRow('material'), price:'20'}];
  return s;
}
test('work rate one/all propagates price only, including excluded counterparts', () => {
  const s=workSample(), source=s.variants.economy.blocks[0].rows[0];
  s.variants.premium.blocks[0].excluded=true;
  s.variants.standard.blocks[0].rows[0].quantity='9';
  setWorkPrice(s,'economy','equipment',source,'12');
  assert.equal(s.variants.standard.blocks[0].rows[0].price,'10');
  setWorkPrice(s,'economy','equipment',source,'0',true);
  assert.ok(Object.values(s.variants).every(v=>v.blocks[0].rows[0].price==='0' && v.blocks[0].rows[1].price==='20'));
  assert.equal(s.variants.standard.blocks[0].rows[0].quantity,'9');
  assert.equal(s.variants.premium.blocks[0].excluded,true);
  setWorkPrice(s,'economy','equipment',source,null,true);
  assert.ok(Object.values(s.variants).every(v=>v.blocks[0].rows[0].price===null));
});
test('different units, finishes, materials and ambiguous duplicates are not overwritten', () => {
  const s=workSample(), source=s.variants.economy.blocks[0].rows[0];
  s.variants.standard.blocks[0].rows[0].unit='рейс';
  s.variants.premium.blocks[0].rows[0].finish_type='tile';
  assert.equal(workPriceTargets(s,'economy','equipment',source).length,1);
  assert.equal(isWork(s.variants.economy.blocks[0].rows[1]),false);
  assert.equal(samePrice('10','10.00'),true);assert.equal(samePrice(null,'0'),false);
  const copy=structuredClone(source);copy.id='other';
  s.variants.standard.blocks[0].rows=[copy,{...copy,id:'duplicate'}];
  assert.equal(workPriceTargets(s,'economy','equipment',source).length,1);
});
