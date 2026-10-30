const {test}=require('node:test');const assert=require('node:assert/strict');
const {intakeStatusText}=require('../static/pool_service/crm_live/leads.js');
test('run error is not a successful empty queue',()=>{
 const text=intakeStatusText({enabled:true,last_attempt_at:'synthetic',last_attempt_result:'error',attempt_age_seconds:10800},x=>x);
 assert.match(text,/180 мин/);assert.match(text,/ошибка обработки/);assert.match(text,/успешного прохода неизвестно/);assert.match(text,/оценка недоступна/);
 assert.doesNotMatch(text,/очередь пуста/);
});
test('no recorded attempt stays unknown',()=>assert.match(intakeStatusText({enabled:false},x=>x),/выключен.*ещё не зафиксирована/));
