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
