export const RULES_VERSION = '1.0 · 07.10.2026';
export const PARAMETERS = [
  {id:'iron', name:'Железо общее', unit:'мг/л', max:1000, hint:'Ориентир для удаления: выше 0,3 мг/л', threshold:0.3},
  {id:'hardness', name:'Общая жёсткость', unit:'°Ж', max:100, hint:'Умягчение для защиты от накипи: выше 3 °Ж', threshold:3},
  {id:'manganese', name:'Марганец', unit:'мг/л', max:100, hint:'Ориентир для удаления: выше 0,1 мг/л', threshold:0.1},
  {id:'ph', name:'Кислотность pH', unit:'pH', min:0,max:14, hint:'Влияет на окисление железа и выбор загрузки'},
  {id:'turbidity', name:'Мутность', unit:'NTU / ЕМФ', max:10000, hint:'Не мг/л по каолину — эти единицы нельзя подставлять'},
  {id:'tds', name:'Минерализация', unit:'мг/л', max:100000, hint:'Сухой остаток / TDS, не электропроводность'},
  {id:'nitrate', name:'Нитраты NO₃⁻', unit:'мг/л', max:10000, hint:'Если указано NO₃–N, выберите пересчёт ниже'},
  {id:'oxidation', name:'Окисляемость', unit:'мг O₂/л', max:1000, hint:'Перманганатная: помогает оценить органику'},
];
export const CORE = ['iron','hardness','manganese','ph','turbidity'];
export const PRESETS = {
  iron: {source:'borehole',people:4,taps:2,residence:'permanent',drain:'yes',pump:'',pressure:'',drinking:true,smell:'no',bio:'unknown',chlorine:false,nitrateUnit:'ion',iron:'2,4',hardness:'5,5',manganese:'0,18',ph:'7,2',turbidity:'2,8',tds:'520',nitrate:'12',oxidation:'2,5'},
  hard: {source:'mains',people:3,taps:2,residence:'permanent',drain:'yes',pump:'',pressure:'',drinking:true,smell:'no',bio:'negative',chlorine:true,nitrateUnit:'ion',iron:'0,08',hardness:'6',manganese:'0,02',ph:'7,4',turbidity:'0,4',tds:'480',nitrate:'8',oxidation:'1,5'},
  complex: {source:'well',people:5,taps:3,residence:'seasonal',drain:'no',pump:'1',pressure:'1,8',drinking:true,smell:'yes',bio:'positive',chlorine:false,nitrateUnit:'ion',iron:'8',hardness:'10',manganese:'0,6',ph:'6,1',turbidity:'12',tds:'1400',nitrate:'65',oxidation:'8'},
};
export function parseNumber(raw, min=0, max=Infinity) {
  if (raw === null || raw === undefined || String(raw).trim() === '') return {value:null};
  const text=String(raw).trim().replace(',', '.');
  if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(text)) return {value:null,error:'Введите число без знаков и единиц'};
  const value=Number(text);
  if (!Number.isFinite(value) || value<min || value>max) return {value:null,error:`Допустимо от ${min} до ${max}`};
  return {value};
}
export function validate(raw) {
  const values={},errors={};
  for (const p of PARAMETERS) {
    const item=parseNumber(raw[p.id],p.min??0,p.max);
    values[p.id]=item.value;
    if(item.error)errors[p.id]=item.error;
  }
  for (const [id,min,max] of [['people',1,20],['taps',1,10],['pump',0.01,100],['pressure',0.01,20]]) {
    const item=parseNumber(raw[id],min,max);
    values[id]=item.value;
    if(item.error)errors[id]=item.error;
    if(['people','taps'].includes(id) && (item.value===null || !Number.isInteger(item.value))) errors[id]='Введите целое число в указанном диапазоне';
  }
  return {values,errors};
}
export function evaluate(raw) {
  const {values:v,errors}=validate(raw);
  if(Object.keys(errors).length) return {errors};
  const nitrate=v.nitrate===null?null:v.nitrate*(raw.nitrateUnit==='nitrogen'?62/14:1);
  const known=CORE.filter(id=>v[id]!==null), missing=CORE.filter(id=>v[id]===null);
  const stages=[],warnings=[],findings=[];
  const warn=(id,title,text,level='notice')=>warnings.push({id,title,text,level});
  const stage=(id,title,description,tag='Рекомендуем')=>stages.push({id,title,description,tag});
  const n=value=>new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(value);
  const ironHigh=v.iron!==null&&v.iron>0.3, mnHigh=v.manganese!==null&&v.manganese>0.1;
  const hardnessHigh=v.hardness!==null&&v.hardness>3;
  const lowPH=v.ph!==null&&v.ph<6.8, unusualPH=v.ph!==null&&(v.ph<6||v.ph>9);
  const organic=v.oxidation!==null&&v.oxidation>5;
  const complex=(v.iron!==null&&v.iron>5)||(v.manganese!==null&&v.manganese>0.5)||organic||unusualPH;
  stage('mechanical','Механическая очистка',v.turbidity!==null&&v.turbidity>5?'При высокой мутности рассмотрите промывной фильтр или осветление. Один картридж может быстро забиваться.':'На вводе задерживает песок и взвесь, защищает последующие ступени. Тонкость фильтрации выбирают по размеру частиц.','Базовая ступень');
  if(lowPH&&(ironHigh||mnHigh))stage('ph','Коррекция pH','Кислая вода может затруднять окисление. Проверить щёлочность и согласовать коррекцию pH до обезжелезивания.','Проверить со специалистом');
  if(ironHigh||mnHigh||raw.smell==='yes'){
    stage('aeration',complex?'Окисление: подобрать метод':'Аэрация / окисление',complex?'Нужен индивидуальный выбор между аэрацией и реагентным окислением с учётом органики, pH и формы железа.':'Предварительный вариант — контакт с воздухом перед фильтром. Метод и время контакта зависят от pH, формы железа и запаха.','Предварительный вариант');
    stage('iron',ironHigh&&mnHigh?'Обезжелезивание и марганец':mnHigh?'Удаление марганца':ironHigh?'Обезжелезивание':'Фильтр после окисления','Удаляет продукты окисления. Каталитическую загрузку подбирают по анализу; аэрация сама по себе не заменяет фильтрацию.');
  }
  if(raw.chlorine===true)stage('carbon','Угольная фильтрация','Для остаточного хлора и связанного с ним вкуса. После удаления хлора особенно важны гигиена системы и своевременная замена загрузки.');
  if(hardnessHigh)stage('softener','Умягчение','Ионообменная ступень снижает соли жёсткости и накипь. Нужны соль, регенерация и отвод стоков. При железе ставится после его удаления.');
  if(organic)stage('organic','Доочистка от органики','Рассмотреть сорбцию и, при необходимости, другие методы. Высокая окисляемость не позволяет автоматически выбрать обычный уголь.','Индивидуальный подбор');
  if(raw.bio==='positive')stage('disinfection','Обеззараживание','Выявить источник загрязнения, обработать водозабор и разводку. Рассмотреть УФ после подготовки воды; эффективность зависит от прозрачности и UVT.','По проекту специалиста');
  else if(raw.source!=='mains'&&raw.bio==='unknown')stage('uv','Проверка обеззараживания','Сначала микробиологический анализ. По его результатам рассмотреть УФ после фильтров — не назначаем лампу автоматически.','После анализа');
  if(raw.drinking && ((nitrate!==null&&nitrate>45)||(v.tds!==null&&v.tds>1000)))stage('ro','Отдельная питьевая ступень','Рассмотреть обратный осмос на кухне. Выбрать систему с подтверждённым снижением нужных примесей и проверить воду после монтажа.','Предварительный вариант');
  if(missing.length)warn('missing',known.length?'Для точного подбора не хватает показателей':'Начните с анализа воды',`Нужны: ${missing.map(id=>PARAMETERS.find(p=>p.id===id).name.toLowerCase()).join(', ')}. Неизвестные значения не считаются нулевыми. Схема по имеющимся данным предварительная.`);
  if(v.ph===null&&(ironHigh||mnHigh))warn('unknown-ph','Без pH нельзя подтвердить обезжелезивание','Нужно знать pH, щёлочность и форму железа, прежде чем выбирать способ окисления и загрузку.');
  if(mnHigh)warn('manganese','Марганец требует отдельной проверки','Обычной аэрации может быть недостаточно. Проверить pH, контактное время и допустимые условия выбранной загрузки.');
  if(raw.smell==='yes')warn('smell','Запах нужно установить по анализу','Запах тухлых яиц — повод проверить сероводород и бактерии. По одному запаху нельзя установить состав воды.');
  if(complex||lowPH)warn('complex','Нужен индивидуальный подбор','Сложный состав или низкий pH: специалист должен проверить щёлочность, органику, аммоний, форму железа и условия работы загрузки. Не покупайте комплект по этой схеме.', 'important');
  if(v.turbidity!==null&&v.turbidity>5)warn('turbid','Сначала снизить мутность','Проверить взвесь, дебит и осветление воды. Мутность мешает работе фильтров и УФ-обеззараживанию.');
  if(raw.bio==='positive')warn('bacteria','Обнаружено микробиологическое загрязнение','До устранения причины и контрольного анализа используйте для питья и приготовления пищи воду из проверенного безопасного источника. Установка фильтра сама по себе проблему не подтверждает решённой.','critical');
  if(nitrate!==null&&nitrate>45)warn('nitrate','Повышены нитраты','Выше 45 мг/л по NO₃⁻: нужна отдельная оценка питьевой воды. Уголь, обычный умягчитель, аэрация и кипячение нитраты не решают. Для питья и готовки используйте проверенный безопасный источник до подтверждения очистки.','critical');
  if(raw.nitrateUnit==='nitrogen'&&v.nitrate!==null)findings.push({title:'Нитраты пересчитаны',text:`${n(v.nitrate)} мг/л NO₃–N × 62/14 = ${n(nitrate)} мг/л NO₃⁻.`});
  if(v.tds!==null&&v.tds>1000)warn('salinity','Высокая минерализация','Умягчитель не снижает общий солевой состав. Нужен расширенный анализ; опреснение всего дома рассматривается отдельно.');
  if(v.tds===0&&[v.iron,v.manganese,v.hardness,nitrate].some(x=>x!==null&&x>0))warn('inconsistent','Проверьте минерализацию и единицы','Нулевой сухой остаток противоречит другим указанным примесям. Возможно, перепутаны единицы или перенесено не то значение.','important');
  if(raw.drinking&&raw.bio==='unknown')warn('bio-unknown','Питьевое качество пока не подтверждено','Добавьте микробиологию. Прозрачная вода и низкое железо не доказывают её безопасность.');
  if(raw.drinking&&(v.nitrate===null||v.tds===null))warn('drinking-analysis','Нужен расширенный питьевой анализ','Для питья нужны как минимум нитраты, минерализация и микробиология; лаборатория определит дополнительные показатели по источнику. Калькулятор не проверяет все возможные загрязнения.');
  if(raw.drain!=='yes'&&stages.some(s=>['iron','softener','mechanical'].includes(s.id)))warn('drain',raw.drain==='no'?'Нужно решить отвод промывных стоков':'Проверьте возможность отвода стоков','Промывные колонны и умягчитель требуют канализации или согласованного отвода. Совместимость солевых стоков с септиком проверяют отдельно.',raw.drain==='no'?'important':'notice');
  const peak=v.taps*8*60/1000;
  const daily=v.people*150/1000;
  if(v.pump!==null&&v.pump<peak)warn('pump','Подача насоса ниже бытового ориентира',`Указано ${n(v.pump)} м³/ч, ориентир дома ${n(peak)} м³/ч. Проверить реальную подачу под давлением и запас для промывки.`, 'important');
  if(v.pressure!==null&&v.pressure<2)warn('pressure','Небольшой запас давления','Указано менее 2 бар. Проверить давление при расходе, потери на ступенях и условия регенерации клапанов.','important');
  if(raw.residence==='seasonal')warn('season','Для сезонного дома нужен уход','Предусмотреть промывку после простоя, защиту от замерзания и консервацию по инструкции оборудования.');
  if(v.taps>=3)warn('peak','Несколько точек одновременно','Душ, ванна и полив могут давать разный расход. Реальные пиковые нагрузки нужно уточнить отдельно.');
  if(ironHigh)findings.push({title:'Железо выше ориентира',text:`${n(v.iron)} мг/л при ориентире 0,3 мг/л. Обезжелезивание защищает сантехнику от рыжего налёта.`});
  if(mnHigh)findings.push({title:'Марганец выше ориентира',text:`${n(v.manganese)} мг/л при ориентире 0,1 мг/л. Нужна загрузка с подтверждённым удалением марганца.`});
  if(hardnessHigh)findings.push({title:'Возможна накипь',text:`${n(v.hardness)} °Ж: предлагаем умягчение для бытового комфорта. Порог 3 °Ж — инженерный ориентир, не санитарный норматив.`});
  if(!ironHigh&&!mnHigh&&!hardnessHigh&&known.length===CORE.length)findings.push({title:'Дополнительные колонны не обоснованы',text:'По введённым железу, марганцу и жёсткости обезжелезивание и умягчение не добавлены. Другие примеси оценивают отдельно.'});
  warnings.sort((a,b)=>({critical:0,important:1,notice:2}[a.level]-{critical:0,important:1,notice:2}[b.level]));
  const status=warnings.some(w=>w.level==='critical')?'critical':warnings.some(w=>w.level==='important')?'specialist':missing.length?'incomplete':'preliminary';
  return {errors:{},values:v,nitrate,stages,warnings,findings,known:known.length,missing,status,peak,daily};
}
