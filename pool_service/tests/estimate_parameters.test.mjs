import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../static/pool_service/estimates/calculator.js', import.meta.url), 'utf8');
function fn(name) { return source.match(new RegExp(`function ${name}\\([^]*?\\n}`))[0]; }
test('multi-source controls serialize all checked values and restore legacy scalar', () => {
  const boxes = ['exchanger', 'electric', 'heatpump'].map(value => ({value, checked:false}));
  const context = vm.createContext({form:{querySelectorAll:()=>boxes,elements:{height_mode:{value:'legacy'},bowl_height:{value:''},tech_insulation_wall_height_mode:{value:'manual'}}}, FormData:class {
    constructor() { this.values = boxes.filter(x=>x.checked).map(x=>['heating_types',x.value]); }
    delete() {}
    getAll() { return this.values.map(x=>x[1]); }
    [Symbol.iterator]() { return this.values[Symbol.iterator](); }
  }});
  vm.runInContext(fn('inputs') + '\n' + fn('setHeating'), context);
  vm.runInContext('setHeating({heating:"electric"})', context);
  assert.equal(vm.runInContext('inputs().heating',context),'electric');
  vm.runInContext('setHeating({heating_types:["electric","heatpump","exchanger"]})', context);
  assert.equal(vm.runInContext('inputs().heating_types.length',context),3);
  vm.runInContext('setHeating({heating_types:[]})', context);
  assert.equal(vm.runInContext('inputs().heating',context),'none');
});
test('parameter input marks estimates stale and reveals heating fields without organization control', () => {
  let callback;
  const fields = {hidden:true}, result = {textContent:''};
  const context = vm.createContext({form:{addEventListener:(_,cb)=>callback=cb, querySelector:()=>({checked:true,disabled:true})}, parametersDirty:false,
    changed:()=>{}, status:()=>{}, snapshot:null, inputs:()=>({heating_types:['electric','exchanger']}),
    $:selector=>selector === '#pe-heating-fields' ? fields : result});
  vm.runInContext(fn('heatingFields') + '\n' + source.match(/form.addEventListener\('input', event => \{[^]*?\n\}\);/)[0],context);
  callback({target:{}});
  assert.equal(context.parametersDirty,true);
  assert.equal(fields.hidden,false);
  assert.match(result.textContent,/Пересчитайте/);
});

test('quantity source labels distinguish computed, manual zero and unknown', () => {
  const context = vm.createContext({});
  vm.runInContext(fn('quantityMode'), context);
  assert.equal(vm.runInContext('quantityMode({auto_quantity:true, quantity:"2"})',context),'авто');
  assert.equal(vm.runInContext('quantityMode({auto_quantity:false, quantity:"0"})',context),'вручную');
  assert.equal(vm.runInContext('quantityMode({auto_quantity:false, quantity:null})',context),'не задано');
});

test('server-linked exclusions refresh structure but quantity edits preserve the editor', () => {
  const context = vm.createContext({});
  vm.runInContext(fn('rowLayout'), context);
  const before = {blocks:[{id:'equipment',excluded:false,rows:[{id:'heater',excluded:false,quantity:'1'},{id:'pump',excluded:false,quantity:'1'}]}]};
  context.before=before;
  context.quantity=structuredClone(before);context.quantity.blocks[0].rows[1].quantity='2';
  assert.equal(vm.runInContext('rowLayout(before)===rowLayout(quantity)',context),true);
  context.excluded=structuredClone(before);context.excluded.blocks[0].rows[1].excluded=true;
  assert.equal(vm.runInContext('rowLayout(before)===rowLayout(excluded)',context),false);
});

test('loading an old version clears technology dimensions and schemes from the newer form', () => {
  const controls = {tech_sand:{value:'on'},tech_sand_thickness:{value:'0.2'}};
  const fallback = {value:''};
  const context = vm.createContext({form:{querySelectorAll:()=>Object.values(controls), elements:{light_color:{value:'rgb'},drain_scheme:{value:'empty_only'},lighting:{value:'yes'},light_count:{value:'2'},construction_scheme:{value:''},height_mode:{value:''},freeboard:{value:''},namedItem:key=>controls[key]||fallback}},
    snapshot:{inputs:{}}, setHeating:()=>{}, heatingFields:()=>{}, parametersDirty:true});
  vm.runInContext(fn('fillParameters')+';fillParameters()',context);
  assert.equal(controls.tech_sand.value,'');
  assert.equal(controls.tech_sand_thickness.value,'');
});

test('automatic wall insulation height follows H while manual height stays fixed', () => {
  const elements={height_mode:{value:'legacy'},tech_insulation_wall_height_mode:{value:'auto'},bowl_height:{value:'1.7'},tech_insulation_wall_height:{value:''}};
  const context=vm.createContext({form:{elements},FormData:class {delete(){} getAll(){return [];} [Symbol.iterator](){return [][Symbol.iterator]();}}});
  vm.runInContext(fn('inputs'),context);vm.runInContext('inputs()',context);
  assert.equal(elements.tech_insulation_wall_height.value,'1.6');
  elements.tech_insulation_wall_height_mode.value='manual'; elements.bowl_height.value='2';
  vm.runInContext('inputs()',context);assert.equal(elements.tech_insulation_wall_height.value,'1.6');
});

test('freeboard derives H, while legacy height is serialized without freeboard', () => {
  const elements={height_mode:{value:'freeboard'},depth_start:{value:'1.5'},freeboard:{value:'0.15'},bowl_height:{value:''},tech_insulation_wall_height_mode:{value:'manual'}};
  let removed=false;
  const context=vm.createContext({form:{elements},FormData:class {delete(key){removed=key==='freeboard';} getAll(){return [];} [Symbol.iterator](){return [][Symbol.iterator]();}}});
  vm.runInContext(fn('inputs')+';inputs()',context);assert.equal(elements.bowl_height.value,'1.65');assert.equal(removed,false);
  elements.height_mode.value='legacy';elements.bowl_height.value='1.7';
  vm.runInContext('inputs()',context);assert.equal(elements.bowl_height.value,'1.7');assert.equal(removed,true);assert.equal(elements.freeboard.value,'0.2');
  elements.depth_start.value='1.6';vm.runInContext('inputs()',context);assert.equal(elements.bowl_height.value,'1.7');assert.equal(elements.freeboard.value,'0.1');
});
