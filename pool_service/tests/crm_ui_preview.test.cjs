const test = require('node:test');
const assert = require('node:assert/strict');
const { readState, matches, stateSearch, normalize } = require('../static/pool_service/crm_preview/crm-preview.js');
const records = [
  { pipeline:'inbox', owner:'none', stage:'inbox:new', source:'site', status:'overdue', search:'Бассейн Берег Ёлки' },
  { pipeline:'inbox', owner:'anna', stage:'inbox:waiting', source:'phone', status:'missing', search:'Фильтр Сосны' },
  { pipeline:'construction', owner:'anna', stage:'construction:need', source:'site', status:'overdue', search:'Бассейн Берег' }
];
test('URL roundtrip preserves local navigation, unicode filters, theme and detail', () => {
  const state = readState('?section=construction&view=table&q=бассейн&owner=anna&stage=construction%3Aneed&source=site&attention=overdue&deal=demo-construction-1&theme=dark');
  assert.deepEqual(readState(stateSearch(state)), state);
  assert.equal(state.theme, 'dark');
  assert.equal(state.view, 'table');
});
test('unsafe and unsupported URL states fall back to bounded defaults', () => {
  const state = readState('?section=%3Cscript%3E&owner=admin&view=write&source=private&attention=won&q='+'a'.repeat(400));
  assert.equal(state.section,'inbox'); assert.equal(state.owner,'');
  assert.equal(state.view,'kanban'); assert.equal(state.q.length,200);
  assert.equal(state.attention,'all'); assert.equal(state.source,'');
});
test('local search handles Russian case, ё and whitespace', () => {
  assert.equal(normalize('  ЁЛКИ '),'елки');
  assert.deepEqual(records.filter(r => matches(r, readState('?q=елки'))),[records[0]]);
});
test('same predicate yields view parity and contextual attention counts', () => {
  for (const view of ['table','kanban']) {
    const state = readState(`?view=${view}&owner=anna&source=phone`);
    assert.deepEqual(records.filter(r => matches(r,state)),[records[1]]);
  }
  const state = readState('?attention=overdue');
  assert.equal(records.filter(r => matches(r,state)).length,1);
  assert.equal(records.filter(r => matches(r,state,false)).length,2);
  state.stage = 'inbox:waiting'; assert.equal(records.filter(r => matches(r,state)).length,0);
  state.attention = 'missing'; assert.deepEqual(records.filter(r => matches(r,state)),[records[1]]);
});
