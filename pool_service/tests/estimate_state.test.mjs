import test from 'node:test';
import assert from 'node:assert/strict';
import {setExcluded, emptyRow, automaticQuantity, quantityRequired} from '../static/pool_service/estimates/state.js';
import {availabilityStatus} from '../static/pool_service/estimates/availability.js';
import {renderCatalogCard} from '../static/pool_service/estimates/catalog-card.js';

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

test('unlinking an automatic exchanger accessory leaves unknown quantity, never NaN', () => {
  const s = sample();
  s.geometry = {};
  const row = {...emptyRow('accessory'), basis:'exchanger_quantity', heating_parent:'pump'};
  assert.equal(automaticQuantity(s, 'economy', row), '1');
  row.heating_parent = '';
  assert.equal(automaticQuantity(s, 'economy', row), null);
  assert.equal(JSON.stringify({quantity:automaticQuantity(s, 'economy', row)}), '{"quantity":null}');
});

test('automatic unknown quantities can submit, explicit manual zero remains valid', () => {
  const row = {...emptyRow('accessory'), basis:'exchanger_quantity',auto_quantity:true,quantity:null};
  assert.equal(quantityRequired(row),false);
  row.auto_quantity=false;
  assert.equal(quantityRequired(row),true);
  row.quantity='0';
  assert.equal(row.quantity,'0');
  assert.equal(quantityRequired(emptyRow('custom')),true);
});

test('row order swaps visible slots only, preserves identity, hidden rows and other quotes', async () => {
  const {moveRow} = await import('../static/pool_service/estimates/state.js');
  const s=sample(), b=s.variants.economy.blocks[0];
  b.rows.push({...emptyRow('heater'), heating_parent:'parent', quantity:'2', price:'123.45'});
  const [pump,hidden,heater]=b.rows, others=JSON.stringify(s.variants.standard);
  assert.equal(moveRow(s,'economy','equipment','heater',-1),true);
  assert.deepEqual(b.rows,[heater,hidden,pump]);
  assert.equal(b.rows[0],heater);
  assert.equal(JSON.stringify(s.variants.standard),others);
  for(const args of [['heater',-1],['pump',1],['filter',1],['heater',2],['missing',1]])
    assert.equal(moveRow(s,'economy','equipment',...args),false);
  assert.equal(moveRow(s,'economy','other','heater',1),false);
  assert.equal(moveRow(s,'economy','equipment','heater',1),true);
  assert.deepEqual(b.rows,[pump,hidden,heater]);
  b.excluded=true;assert.equal(moveRow(s,'economy','equipment','heater',-1),false);
});

test('filter loading quantity remains server-owned including unknown mass',()=>{
 const s=sample();
 assert.equal(automaticQuantity(s,'economy',{basis:'filter_load',quantity:'3.000'}),'3.000');
 assert.equal(automaticQuantity(s,'economy',{basis:'filter_load',quantity:null}),null);
 assert.equal(quantityRequired({...emptyRow('media'),kind:'filter_media',basis:'filter_load',auto_quantity:true}),false);
});

test('gravel bags remain server-owned and an individual exclusion stays protected', () => {
 const s=sample(), row={...emptyRow('gravel'),kind:'filter_gravel',basis:'filter_load',quantity:'8.000',auto_quantity:true};
 s.variants.economy.blocks[0].rows.push(row);
 assert.equal(automaticQuantity(s,'economy',row),'8.000');
 assert.equal(automaticQuantity(s,'economy',{...row,quantity:null}),null);
 assert.equal(quantityRequired(row),false);
 setExcluded(s,'economy','equipment',row.id,true);
 assert.equal(row.selection_locked,true);
});

test('filter media cards distinguish bag prices, per-kg prices and exact bag/fraction facts', () => {
 for (const kind of ['filter_media','filter_gravel']) {
  const p={id:'11',name:'Загрузка',kind,price:'150',filter_media:{bag_kg:'25',fraction_min:'1',fraction_max:'2',price_unit:'bag'}};
  const render=product=>renderCatalogCard(product,{money:String,thumbnail:()=>''});
  assert.match(render(p),/150 ₽ \/ меш\./);
  assert.match(render(p),/25 кг\/меш\./);
  assert.match(render(p),/1–2 мм/);
  assert.match(render({...p,filter_media:{...p.filter_media,price_unit:'kg'}}),/150 ₽ \/ кг/);
 }
});

// Public catalogue availability state; included in the existing CI state suite.

const product = (state, extra = {}) => ({available: ['own_stock', 'supplier_stock'].includes(state), availability: {state, orderable: true, ...extra}});
test('stock badges distinguish warehouse, supplier, backorder and unknown', () => {
  assert.deepEqual(availabilityStatus(product('own_stock')), {tone: 'confirmed', label: 'У нас', detail: 'У нас'});
  assert.equal(availabilityStatus(product('supplier_stock')).label, 'У поставщика');
  assert.equal(availabilityStatus(product('preorder')).label, 'Под заказ');
  assert.equal(availabilityStatus(product('unknown')).label, 'Уточнить наличие');
  assert.equal(availabilityStatus({available: true}).tone, 'pending');
});
test('conflicts and unconfirmed facts cannot receive a green badge', () => {
  for (const state of ['own_stock', 'supplier_stock', 'unavailable']) {
    const badge = availabilityStatus(product(state, {issue: 'Конфликт', label: 'Публичный статус'}));
    assert.equal(badge.tone, 'pending');
    assert.equal(badge.label, 'Уточнить наличие');
    assert.match(badge.detail, /Публичный статус · Конфликт/);
  }
  assert.equal(availabilityStatus({...product('own_stock'), available: false}).tone, 'pending');
});
test('explicit absence and non-orderable products are red without mutating selection facts', () => {
  for (const p of [product('unavailable'), product('own_stock', {orderable: false}), product('preorder', {orderable: false})]) {
    const original = structuredClone(p);
    assert.equal(availabilityStatus(p).tone, 'unavailable');
    assert.deepEqual(p, original);
  }
});


test('filter valve quantity stays server-owned and individual exclusion locks manual choice', () => {
 const s=sample();
 const row={...emptyRow('valve'),kind:'filter_valve',basis:'filter_valve',quantity:'2.000',auto_quantity:true};
 s.variants.economy.blocks[0].rows.push(row);
 assert.equal(automaticQuantity(s,'economy',row),'2.000');
 assert.equal(automaticQuantity(s,'economy',{...row,quantity:null}),null);
 assert.equal(quantityRequired(row),false);
 setExcluded(s,'economy',s.variants.economy.blocks[0].id,row.id,true);
 assert.equal(row.excluded,true);
 assert.equal(row.selection_locked,true);
 setExcluded(s,'economy',s.variants.economy.blocks[0].id,row.id,false);
 assert.equal(row.excluded,false);
 assert.equal(row.selection_locked,true);
});

test('compact catalogue keeps manual choice, full help and one safe product link', () => {
 const html=renderCatalogCard({id:'9',name:'Насос <Test>',brand:'A & B',sku:'A-9',price:'100',
  url:'https://shop.aqualine22.ru/item.html',kind:'pump',nominal_flow:'7',head:'4',power:'230',
  warning:'Проверить <совместимость>',available:false,availability:{state:'preorder',orderable:true}},
  {money:String,thumbnail:()=>''});
 assert.match(html,/Насос &lt;Test&gt;/);
 assert.match(html,/A &amp; B/);
 assert.match(html,/href="https:\/\/shop\.aqualine22\.ru\/item.html"/);
 assert.equal((html.match(/<a /g)||[]).length,1);
 assert.match(html,/арт\. A-9/);
 assert.match(html,/7 м³\/ч по каталогу при 4 м/);
 assert.match(html,/230 В/);
 assert.match(html,/data-help-label="●">Наличие: Под заказ/);
 assert.match(html,/data-help-label="!">Проверить &lt;совместимость&gt;/);
 assert.match(html,/data-catalog-product="9">Выбрать/);
 assert.doesNotMatch(html,/disabled|>Карточка товара ↗</);
});

test('compact catalogue preserves material units and code without allowing an unsafe link', () => {
 const html=renderCatalogCard({id:'10',name:'Лайнер',sku:'',price:'200',url:'javascript:alert(1)',
  kind:'liner',roll_area:'42',liner:{price_unit:'roll'},power:'unknown'},
  {money:String,thumbnail:()=>''});
 assert.match(html,/код 10/);
 assert.match(html,/200 ₽ \/ рулон/);
 assert.match(html,/42 м²\/рулон; в смете за м²/);
 assert.match(html,/pe-catalog-no-image/);
 assert.doesNotMatch(html,/<a |javascript:|undefined|> В</);
});
