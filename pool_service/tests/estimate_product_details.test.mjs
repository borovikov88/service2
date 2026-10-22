import test from 'node:test';import assert from 'node:assert/strict';
import {productDetails,shopUrl} from '../static/pool_service/estimates/product-details.js';
import {setConstructionExcluded,constructionScope} from '../static/pool_service/estimates/state.js';

test('brand and catalogue SKU are linked safely without duplicated full model',()=>{
 const row={brand:'Aquaviva',model:'Full product model',catalog:{id:'12',sku:'RC-01',url:'https://shop.aqualine22.ru/item.html'}};
 const html=productDetails(row);assert.match(html,/Aquaviva/);assert.match(html,/>арт\. RC-01<\/a>/);assert.ok(!html.includes('Full product model'));assert.match(html,/rel="noopener noreferrer"/);
 for(const url of ['javascript:alert(1)','https://evil.test/item','https://shop.aqualine22.ru.evil.test/item','https://secret@shop.aqualine22.ru/item'])assert.equal(shopUrl(url),'');
 assert.ok(!productDetails({...row,brand:'<script>',catalog:{...row.catalog,sku:'<svg/onload=alert(1)>',url:'javascript:alert(1)'}}).includes('<svg'));
 assert.match(productDetails({brand:'Manual',model:'Model'}),/Manual · Model/);
});
test('construction checkbox changes labor and bowl materials atomically and preserves row exclusions',()=>{
 const make=()=>({blocks:[{id:'construction',excluded:false,rows:[{id:'a',excluded:true}]},{id:'construction_materials',excluded:false,rows:[{id:'b',excluded:false}]},{id:'equipment',excluded:false,rows:[]}]});
 const s={variants:{economy:make(),standard:make(),premium:make()}};
 setConstructionExcluded(s,'economy',true);assert.ok(constructionScope(s.variants.economy).every(b=>b.excluded));assert.ok(constructionScope(s.variants.standard).every(b=>!b.excluded));
 setConstructionExcluded(s,'economy',false,true);assert.ok(Object.values(s.variants).every(v=>constructionScope(v).every(b=>!b.excluded)));assert.ok(s.variants.economy.blocks[0].rows[0].excluded);assert.equal(s.variants.economy.blocks[2].excluded,false);
});
