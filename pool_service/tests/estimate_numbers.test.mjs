import test from 'node:test';
import assert from 'node:assert/strict';
import {displayNumber,exactNumber,indivisible,bindNumber,rawValue,installNumbers,cleanNote} from '../static/pool_service/estimates/numbers.js';
test('display quantities and money without changing exact geometry or historical pieces',()=>{
 assert.equal(displayNumber('5.9136'),'5.9'); assert.equal(displayNumber('1259.2533'),'1259.3');
 assert.equal(displayNumber('113332.80'),'113332.8');
 for(const x of ['0.15','0.05','0.02'])assert.equal(displayNumber(x,{exact:true}),x);
 assert.equal(exactNumber('.6000'),'0.6');assert.equal(exactNumber('20.000000'),'20');
 assert.equal(displayNumber('1.25',{whole:true}),'1.25');assert.equal(displayNumber('2.000',{whole:true}),'2');
 for(const u of ['шт','шт.','компл.']) assert.ok(indivisible(u));
 for(const u of ['м²','т','кг'])assert.ok(!indivisible(u));
 assert.equal(cleanNote('наценка 20.000000%; площадь 4.850000'),'наценка 20%; площадь 4.85');
});
test('focus, blur and serialization preserve original strings until actual input',()=>{
 const listeners={}; const root={addEventListener:(name,fn)=>listeners[name]=fn}; installNumbers(root);
 const el={value:'',setCustomValidity:()=>{}};
 for(const original of ['5.9136000','1259.253301','0.000','1.250']){
  bindNumber(el,original); assert.equal(rawValue(el),original);
  listeners.focusin({target:el});assert.equal(rawValue(el),original);
  let stopped=false;listeners.change({target:el,stopImmediatePropagation:()=>stopped=true});assert.ok(stopped);
  listeners.focusout({target:el});assert.equal(rawValue(el),original);
 }
 bindNumber(el,'5.9136');listeners.focusin({target:el});el.value='6.2';listeners.input({target:el});listeners.focusout({target:el});assert.equal(rawValue(el),'6.2');
});

test('reusing a field or changing pieces to weight clears the old integer error',()=>{
 const el={value:'',error:'old',setCustomValidity(value){this.error=value;}};
 bindNumber(el,'1.5',{whole:false});assert.equal(el.error,'');
 bindNumber(el,'2',{whole:true});assert.equal(el.error,'');
});

test('whole grouped money preserves raw history and validates new edits',async()=>{
 const {money}=await import('../static/pool_service/estimates/numbers.js');
 assert.equal(money('1234456.49'),'1 234 456');assert.equal(money('1234456.5'),'1 234 457');
 const listeners={};installNumbers({addEventListener:(k,fn)=>listeners[k]=fn});
 const el={type:'number',value:'',error:'',setCustomValidity(v){this.error=v;}};
 bindNumber(el,'1234.56',{money:true});assert.equal(el.value,'1 235');assert.equal(rawValue(el),'1234.56');
 listeners.focusin({target:el});listeners.focusout({target:el});assert.equal(rawValue(el),'1234.56');
 listeners.focusin({target:el});el.value='2 345';listeners.input({target:el});assert.equal(rawValue(el),'2345');listeners.focusout({target:el});assert.equal(rawValue(el),'2345');
 for(const value of ['1 200','1\u00a0200','1\u202f200']){el.value=value;listeners.input({target:el});assert.equal(rawValue(el),'1200');assert.equal(el.error,'');}
 for(const value of ['-1','NaN','12.5','1000000001']){el.value=value;listeners.input({target:el});assert.ok(el.error);}
 bindNumber(el,'99',{money:true});assert.equal(rawValue(el),'99');assert.equal(el.error,'');
});
