/* Minimal URL-state adapter for the actual server-directory JS; browser tests
   cover real DOM/network races, Django tests cover filtering and permissions. */
const vm=require('node:vm');
function load(source,address){
 const location=new URL(address);let active='companies',timerId=0;
 const timers=new Map(),requests=[];
 function element(dataset={}){const listeners=new Map();return{dataset,value:'',hidden:false,
  addEventListener(name,fn){listeners.set(name,fn);},fire(name){listeners.get(name)?.();},
  setAttribute(){},focus(){doc.activeElement=this;},setSelectionRange(){}};}
 const inputs=Object.fromEntries(['companies','people','suppliers'].map(group=>{
  const input=element({clientSearch:group});input.value=location.searchParams.get(group+'_q')||'';return[group,input];}));
 const tabs=Object.fromEntries(Object.keys(inputs).map(key=>[key,element()]));
 const root=element(),error=element(),retry=element();root.replaceWith=()=>{};
 root.querySelectorAll=selector=>selector==='[data-client-search]'?Object.values(inputs):[];
 root.querySelector=selector=>inputs[selector.match(/"(companies|people|suppliers)"/)[1]];
 const doc={readyState:'complete',activeElement:null,getElementById:id=>({
  'contacts-directory':root,'contacts-error':error,'contacts-retry':retry,
  'companies-tab':tabs.companies,'contacts-tab':tabs.people,'suppliers-tab':tabs.suppliers}[id]||null)};
 const history={replaceState(_s,_t,url){location.href=new URL(url,location).href;},pushState(_s,_t,url){location.href=new URL(url,location).href;}};
 const bootstrap={Tab:{getOrCreateInstance:button=>({show(){active=Object.keys(tabs).find(key=>tabs[key]===button);button.fire('shown.bs.tab');}})}};
 const context={document:doc,location,history,bootstrap,URL,AbortController,
  window:{addEventListener(){}},
  setTimeout(fn){timers.set(++timerId,fn);return timerId;},clearTimeout(id){timers.delete(id);},
  fetch:async url=>{requests.push(url);return{ok:true,redirected:false,text:async()=>''};},
  DOMParser:class{parseFromString(){return{getElementById:()=>root};}}};
 vm.runInNewContext(source,context,{timeout:1000});
 return{location,inputs,requests,get active(){return active;},
  tab(group){bootstrap.Tab.getOrCreateInstance(tabs[group]).show();},
  async search(group,value){inputs[group].value=value;doc.activeElement=inputs[group];inputs[group].fire('input');for(const fn of timers.values())fn();timers.clear();await new Promise(resolve=>setImmediate(resolve));}};
}
async function scenario(input){
 let page=load(input.source,input.url);
 for(const[action,group,value]of input.actions){
  if(action==='tab')page.tab(group);else if(action==='search')await page.search(group,value);
  else if(action==='reload')page=load(input.source,page.location.href);else throw Error('Unknown action');
 }
 return{active:page.active,url:page.location.href,values:Object.fromEntries(Object.entries(page.inputs).map(([k,v])=>[k,v.value])),requests:page.requests};
}
module.exports={scenario};
if(require.main===module){let raw='';process.stdin.on('data',s=>raw+=s);process.stdin.on('end',()=>scenario(JSON.parse(raw)).then(s=>console.log(JSON.stringify(s))).catch(e=>{console.error(e);process.exitCode=1;}));}
