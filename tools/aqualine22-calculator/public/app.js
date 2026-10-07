import {PARAMETERS, CORE, PRESETS, RULES_VERSION, evaluate} from './engine.js';
const $=id=>document.getElementById(id), form=$('water-form');
const iconNames={mechanical:'Механика',ph:'pH',aeration:'Окисление',iron:'Fe / Mn',softener:'Умягчение',carbon:'Уголь',organic:'Органика',disinfection:'УФ / защита',uv:'Проверка УФ',ro:'Питьевой кран'};
let lastResult=null,lastRaw=null,dirty=false,isDemo=false;
const format=(number,digits=2)=>new Intl.NumberFormat('ru-RU',{maximumFractionDigits:digits}).format(number);
function el(tag,className,text){const n=document.createElement(tag);if(className)n.className=className;if(text!==undefined)n.textContent=text;return n;}
for(const p of PARAMETERS){
  const label=el('label','field');label.htmlFor=p.id;
  label.append(el('span','',p.name));
  const shell=el('div','input-shell'),input=el('input');input.id=p.id;input.name=p.id;input.type='text';input.inputMode='decimal';input.placeholder='Не знаю';input.autocomplete='off';input.setAttribute('aria-describedby',`${p.id}-hint ${p.id}-error`);
  shell.append(input,el('span','',p.unit));const hint=el('small','',p.hint);hint.id=`${p.id}-hint`;const error=el('span','field-error');error.id=`${p.id}-error`;label.append(shell,hint,error);
  (CORE.includes(p.id)?$('analysis-fields'):$('additional-fields')).append(label);
}
function readForm(){const raw=Object.fromEntries(new FormData(form));raw.drinking=$('drinking').checked;raw.chlorine=$('chlorine').checked;return raw;}
function clearErrors(){form.querySelectorAll('[aria-invalid]').forEach(x=>x.removeAttribute('aria-invalid'));form.querySelectorAll('.field-error').forEach(x=>x.textContent='');$('form-errors').hidden=true;}
function markDirty(){if(!lastResult)return;dirty=true;$('updated-note').hidden=false;$('download').disabled=true;$('download').style.opacity='.5';$('announce').textContent='Параметры изменены. Обновите подбор.';}
form.addEventListener('input',markDirty);form.addEventListener('change',markDirty);
function render(result){
  $('empty-result').hidden=true;$('populated-result').hidden=false;$('updated-note').hidden=true;$('download').disabled=false;$('download').style.opacity='';
  const labels={critical:['Нужен специалист','Сначала решите вопросы качества воды'],specialist:['Нужен специалист','Схема требует индивидуальной проверки'],incomplete:['Нужен анализ','Уточним состав — подберём систему'],preliminary:['Предварительная схема','Ваша схема водоочистки']};
  const [pill,title]=labels[result.status];$('result-pill').textContent=pill;$('result-pill').className=`pill ${result.status==='critical'?'critical':result.status==='specialist'?'important':''}`;$('result-title').textContent=title;
  $('result-subtitle').textContent=isDemo?'Демонстрационный анализ. Это пример работы калькулятора, а не данные вашей воды.':result.known===0?'По неизвестному составу нельзя назначить оборудование. Ниже — базовая подготовка и необходимые проверки.':'Последовательность по введённым данным. Перед покупкой подтвердите схему по полному анализу.';
  const coverage=$('coverage');coverage.replaceChildren();for(let i=0;i<CORE.length;i++)coverage.append(el('span',`coverage-dot ${i<result.known?'filled':''}`));coverage.append(el('span','',`${result.known} из ${CORE.length} основных показателей`));
  const scheme=$('scheme');scheme.replaceChildren();const mainsStages=result.stages.filter(s=>s.id!=='ro');
  mainsStages.forEach((s,i)=>{if(i)scheme.append(el('span','scheme-arrow','→'));const item=el('div','scheme-item');item.append(el('div','scheme-symbol',s.id==='ph'?'pH':s.id==='iron'?'Fe':s.id==='uv'||s.id==='disinfection'?'UV':`${i+1}`),el('span','',iconNames[s.id]));scheme.append(item);});
  const stages=$('stages');stages.replaceChildren();result.stages.forEach((s,i)=>{const row=el('div','stage');row.dataset.stage=s.id;const body=el('div','stage-body'),heading=el('div','stage-heading');heading.append(el('h4','',s.title),el('span','stage-tag',s.tag));body.append(heading,el('p','',s.description));row.append(el('span','stage-index',s.id==='ro'?'↳':`${i+1}`),body);stages.append(row);});
  const findings=$('findings');findings.replaceChildren();findings.hidden=!result.findings.length;for(const f of result.findings){const row=el('div','finding');row.append(el('h4','',f.title),el('p','',f.text));findings.append(row);}
  $('peak').textContent=`${format(result.peak)} м³/ч`;$('daily').textContent=`${format(result.daily)} м³`;
  const warnings=$('warnings');warnings.replaceChildren();for(const w of result.warnings){const row=el('div',`warning ${w.level}`);row.dataset.warning=w.id;row.append(el('h4','',w.title),el('p','',w.text));warnings.append(row);}
  $('announce').textContent=`${title}. Ступеней: ${result.stages.length}. Предупреждений: ${result.warnings.length}.`;
}
function calculate(scroll=true){clearErrors();const raw=readForm(),result=evaluate(raw);if(Object.keys(result.errors).length){for(const [id,message]of Object.entries(result.errors)){$(id).setAttribute('aria-invalid','true');$(`${id}-error`).textContent=message;const detail=$(id).closest('details');if(detail)detail.open=true;}$('form-errors').textContent='Проверьте выделенные поля. Используйте точку или запятую для дробных чисел.';$('form-errors').hidden=false;$(Object.keys(result.errors)[0]).focus();return false;}lastResult=result;lastRaw=raw;dirty=false;render(result);if(scroll){$('result-title').focus({preventScroll:true});if(window.innerWidth<741)$('result-card').scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});}return true;}
form.addEventListener('submit',event=>{event.preventDefault();calculate();});
function reset(){form.reset();clearErrors();lastResult=null;lastRaw=null;dirty=false;isDemo=false;$('demo-note').hidden=true;$('empty-result').hidden=false;$('populated-result').hidden=true;$('result-pill').textContent='Предварительная схема';$('result-pill').className='pill';document.querySelectorAll('[data-preset]').forEach(x=>x.classList.remove('active'));$('announce').textContent='Данные и результат очищены.';}
$('reset-form').addEventListener('click',reset);$('clear-demo').addEventListener('click',reset);
document.querySelectorAll('[data-preset]').forEach(button=>button.addEventListener('click',()=>{reset();const data=PRESETS[button.dataset.preset];for(const [key,value]of Object.entries(data)){if(typeof value==='boolean')$(key).checked=value;else $(key).value=String(value);}isDemo=true;$('demo-note').hidden=false;button.classList.add('active');calculate(false);}));
$('download').addEventListener('click',()=>{
  if(!lastResult||dirty)return;
  const lines=['АКВАЛАЙН — ПРЕДВАРИТЕЛЬНАЯ СХЕМА ВОДООЧИСТКИ',`Правила: ${RULES_VERSION}`,`Дата: ${new Date().toLocaleString('ru-RU')}`,isDemo?'ДЕМОНСТРАЦИОННЫЙ АНАЛИЗ — не данные воды пользователя':'Данные пользователя','', 'ИСХОДНЫЙ АНАЛИЗ'];
  for(const p of PARAMETERS)lines.push(`${p.name}: ${lastRaw[p.id]||'неизвестно'} ${p.id==='nitrate'&&lastRaw.nitrateUnit==='nitrogen'?'мг/л NO₃–N':p.unit}`);
  lines.push(`Нитраты по NO₃⁻: ${lastResult.nitrate===null?'неизвестно':format(lastResult.nitrate)+' мг/л'}`,'','ПАРАМЕТРЫ ДОМА',`Источник: ${{borehole:'скважина',well:'колодец',mains:'водопровод'}[lastRaw.source]}`,`Жителей: ${lastRaw.people}; точек одновременно: ${lastRaw.taps}`,`Проживание: ${lastRaw.residence==='permanent'?'постоянное':'сезонное'}`,`Отвод стоков: ${{yes:'есть',no:'нет',unknown:'уточнить'}[lastRaw.drain]}`,`Подача насоса: ${lastRaw.pump||'неизвестно'} м³/ч; давление: ${lastRaw.pressure||'неизвестно'} бар`,`Запах тухлых яиц: ${{yes:'есть',no:'нет',unknown:'неизвестно'}[lastRaw.smell]}`,`Микробиология: ${{positive:'загрязнение',negative:'нарушений не выявлено',unknown:'не проверена'}[lastRaw.bio]}`,`Остаточный хлор / его запах: ${lastRaw.chlorine?'да':'не отмечено'}`,`Питьевая задача: ${lastRaw.drinking?'да':'нет'}`,'','СТУПЕНИ');
  lastResult.stages.forEach((s,i)=>lines.push(`${s.id==='ro'?'Отдельная питьевая ветка':i+1}. ${s.title} [${s.tag}]\n${s.description}`));
  lines.push('','ОБЪЯСНЕНИЕ');lastResult.findings.forEach(f=>lines.push(`${f.title}: ${f.text}`));
  lines.push('','БЫТОВОЙ ОРИЕНТИР',`${format(lastResult.peak)} м³/ч; ${format(lastResult.daily)} м³/сутки. Формулы: точки × 8 л/мин × 60 / 1000; жители × 150 л/сутки / 1000. Не включает полив, бассейн, большие ванны. Не является подбором размера фильтра.`,'','ПРЕДУПРЕЖДЕНИЯ');
  lastResult.warnings.forEach(w=>lines.push(`${w.title}: ${w.text}`));
  lines.push('','Это предварительная схема, не проект и не заключение о питьевом качестве. Размеры, загрузки и режимы подтверждает специалист по полному анализу. После установки нужен контрольный анализ воды.');
  const blob=new Blob(['\ufeff',lines.join('\n\n')],{type:'text/plain;charset=utf-8'}),url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download='aqualine-water-scheme.txt';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);$('announce').textContent='Схема и исходные данные скачаны.';
});
