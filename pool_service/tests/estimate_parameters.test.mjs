import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../static/pool_service/estimates/calculator.js', import.meta.url), 'utf8');
function fn(name) { return source.match(new RegExp(`function ${name}\\([^]*?\\n}`))[0]; }
test('multi-source controls serialize all checked values and restore legacy scalar', () => {
  const boxes = ['exchanger', 'electric', 'heatpump'].map(value => ({value, checked:false}));
  const context = vm.createContext({form:{querySelectorAll:()=>boxes}, FormData:class {
    constructor() { this.values = boxes.filter(x=>x.checked).map(x=>['heating_types',x.value]); }
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
