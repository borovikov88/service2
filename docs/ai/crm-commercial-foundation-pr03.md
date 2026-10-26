# PR03: выключенная основа коммерческой CRM

План до реализации: семь таблиц внутри `pool_service`, только расширяющая
миграция после `0150_crm_source_link`; организация, nullable существующий Client,
назначение через существующий User. Начальные настройки — предложения §7 ТЗ0.4,
а не новые решения владельца. D01–D12 сохраняются; Q06-права не выдаются,
Q13 отложен. Договоров и CrmExecution пока нет (PR18).

| Модель | Поля и ограничения |
| --- | --- |
| CrmPipeline | organization/code unique; название, порядок, active |
| CrmStage | pipeline/code unique; название, порядок, active; удаление через ORM запрещено, вместо него отключение |
| CrmLead | organization, client/assignee nullable; client/tender, потребность, канал/источник, даты, status/result/reason/follow_up, budget unknown/exact/range, currency, revision, archive |
| CrmDeal | organization/pipeline/stage, nullable client/manager, specialization/fulfillment_kind, nullable amount/currency/basis, outcome, data_quality, отдельные даты, revision, archive |
| CrmCommand | organization/operation_key unique, command_type, SHA256 payload_hash, actor, структурный result, created_at; исполнителя команд нет |
| CrmEvent | organization, ровно один lead/deal (DB XOR), nullable command/actor, occurred/recorded, структурный safe_diff; история неизменяема через предоставленный ORM |
| CrmAccess | organization/user unique; только view_team_deals=False; bootstrap не создаёт доступы |

DB FK используют PROTECT. DB CHECK различают неизвестную сумму (NULL) и ноль,
проверяют форму и неотрицательность бюджета, enum результата/качества, условия
ожидания лида. Состояния разбора и результаты квалификации/привязки/нецелевого/
спама/дубля/закрытия без сделки разделены; для последнего нужна причина. Exact budget хранится равными min/max. Outcome отделён от стадии
и архива; unknown допустим только с legacy_review. Никакой автоматический won,
подтверждение оплаты/завершения или исполнение из наличия этих полей не следует.

`code` и `operation_key` на разрешённых ORM записях — только lowercase ASCII:
начальный `[a-z0-9]`, далее `[a-z0-9_-]`. Строгий отказ для другого регистра,
Unicode и пробелов исключает различия SQLite/MySQL collation в поддерживаемом
контракте. Коды и command identity/hash/actor после создания неизменяемы.
Hash не заменяет проверку повторов/прав: она появится в PR04.

## Граница записи и чтения

Это ограниченный ORM write contract, **не DB-level межтабличный CHECK**.
`save()` вызывает full_clean в транзакции, блокирует текущую строку и прямые FK,
проверяет одинаковую организацию и принадлежность stage выбранной pipeline.
organization и stage.pipeline неизменяемы; update_fields отвергает отличающиеся
несохранённые поля. `QuerySet.update`, bulk_create и bulk_update запрещены на
всех новых моделях, включая их base manager. Stage и Event нельзя удалить через
предоставленный ORM. Event нельзя изменить; safe_diff допускает только
stage/pipeline/manager/assignee ID, revision и archive с before/after из
ID/bool/NULL. Command.result допускает только lead/deal/event ID и revision.
Свободные JSON payload, личные сообщения и расшифровки не принимаются.

Raw SQL, save_base(raw=True), внутренние методы Django и исторические migration
models — доверенные пути вне этого контракта. SQL unique/XOR/FK/локальные CHECK
защищают их частично; межтабличный tenant scope и содержимое JSON — нет.
Новых HTTP/API/admin/фоновых writers нет. PR04 обязан сохранить эту границу или
заменить её явным проверяемым command service; это ещё не обработчик переходов,
optimistic locking, retry engine или разрешение закрывать сделки.

Legacy Client/Employee/OrganizationAccess не получают triggers или запретов.
Новая связь с Client требует его непустую совпадающую организацию. Если legacy
Client позже перенесён/обезличен, deals_for_user исключает всю связанную карточку
до явного исправления ссылки; следующая save отказывает. Прямое чтение таблицы
не является проверкой доступа. Долговечный SQL tenant invariant при независимой
legacy reparent записи не обещается. Назначение через User проверяет активность,
свежую membership и Employee при новом назначении; неизменённое историческое
назначение остаётся, не блокируя увольнение, отзыв или ремонт карточки.

can_view_team_deals проверяет persisted account.is_active, текущую membership,
Employee и crm_v2_read при каждом вызове. Для manager D06 даёт командный просмотр
своей организации. Прочим ролям требуется явный CrmAccess read grant; superuser
без membership не обходит эту новую политику. Нет edit-other, reassign, private,
finance, close или других спорных grants. Queryset следует получать заново на
каждую операцию, не кэшировать после проверки прав. Связь не открывает private
источник/задачу и не заменяет отдельные финансовые/коммуникационные политики.

## Флаги и bootstrap

crm_v2_read, crm_v2_write, crm_intake_process, crm_materials, crm_accounting,
crm_close, tender_mail, tender_autoselect — organization booleans default OFF,
editable=False: существующие admin/автоматические ModelForm не становятся
переключателями. Read не включает другие флаги. can_write_commercial всегда
False: одного изменения флага недостаточно для создания второго writer.
Валидация рабочих комбинаций и включение конкретных процессов — последующие PR;
Q13 не включается. Legacy UI и запись продолжают прежний путь.

`bootstrap_crm_commercial --organization-id ID` явно создаёт только недостающие
4 воронки и 30 этапов §7 через get_or_create под lock организации. Повтор
сохраняет названия, порядок, active, flags, права и legacy строки. Автоматического
post_migrate/deploy bootstrap и backfill нет. Настройки не исполняют правила
переходов/закрытия.

Существующий Avito CrmSourceLink/0150 остаётся единственной моделью с этим
именем. Будущие lead/deal source-links должны расширить её отдельной миграцией
после проверки текущего контракта, не создавать одноимённый параллельный слой.

## Миграция и проверка

0151 только добавляет восемь полей default OFF и семь таблиц с индексами/FK/CHECK;
RunPython, copy/backfill, legacy UI и CrmItem изменений нет. MySQL DDL выполняет
неявные commit: atomic rollback всей миграции не обещается. ADD COLUMN на большой
Organization может потребовать metadata lock; длительность надо измерить на
стенде. Откат кода при OFF сохраняет additive schema. Reverse 0151 удаляет новые
таблицы/поля и **теряет их данные**, поэтому не является рабочим откатом после
первой v2-записи; нужны backup/совместимый код или fix-forward. Не запускать reverse
как штатный deployment rollback.

Локальный набор: test_crm_commercial_foundation, test_crm_commercial_migrations,
test_crm_org_scope, test_task_read_privacy; check и makemigrations --check.
Migration test стартует с0150, сохраняет legacy sentinel, проверяет физические
unique/XOR/FK/CHECK (в том числе NULL), повтор, reverse/forward на синтетических
данных. MySQL8.4 workflow явно включает эти CRM тесты и новые paths, сохраняя
все прежние finance/Avito suites. Фактический MySQL результат и полный CI надо
фиксировать по точному head; локальная SQLite их не заменяет.


## Совместимость объединения клиентов

Существующий generic merge сохраняется для клиентов без исходных коммерческих
ссылок, включая случай, когда только целевой клиент уже имеет CrmLead/CrmDeal:
целевые ссылки остаются неизменными. Если на исходного клиента ссылается любой
CrmLead/CrmDeal, merge_clients явно отказывает до перемещения legacy-связей,
контактов, профиля или диалогов. Проверка не фильтрует архив/организацию/active и
действует даже при OFF флагах. Для переноса таких ссылок нужен будущий отдельный
ограниченный коммерческий merge contract; успешный merge не оставляет
неперенесённые исходные коммерческие ссылки. Общий запрет unsafe bulk/update не
ослаблен. Это совместимость с legacy merge, а не новый commercial writer.
