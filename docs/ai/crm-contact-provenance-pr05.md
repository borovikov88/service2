# PR05: происхождение контактов и уточнение идентичности

Продолжает принятые PR03/04 и существующие Client/Conversation/ClientContact.
D01–D12 сохраняются; Q06 не решается выдачей прав, Q13 отложен. Коммерческие
commands, CrmSourceLink, финансовые данные и внешние отправления не меняются.
Нового HTTP endpoint/UI нет: очередь и resolver доступны только как внутренний
серверный контракт. Следующий пакет — PR06 надёжного inbox; экран очереди требует
отдельного подключения к этой политике, а не собственных проверок в view.

## Identity и происхождение

`receive_message` остаётся входом capture. Новый неизвестный inbound создаёт одну
обычную Client с ClientCRMProfile.source=communications и is_preliminary=True,
даже без номера/ID автора. Повторы того же разговора используют его Client;
одинаковый Avito participant ID в рамках одного connection/account переиспользует
единственную действующую карточку. ID автора другого аккаунта не равен этой
идентичности. Неоднозначные provider phone/author IDs не выбираются по первому
совпадению. Отсутствие номера не блокирует сообщения.

Для нового provider phone используется отдельное `Conversation.provider_phone`:
его заполняет только явный participant_phone из входящего вызова. Старое
participant_phone могло быть синтезировано из Client.phone, поэтому оно само по
себе не доказывает provider origin, не заполняет новое поле и больше не участвует
в автоматическом capture matching. Это сознательная fail-closed граница legacy
matching; старые contacts и Client.phone продолжают работать в существующем
clients_by_phones. Их история не исправляется и не считается подтверждённой.

Новый provider phone может однозначно сопоставляться через прежние ClientContact
и legacy Client.phone. На новой карточке он может заполнить основной номер;
на существующей сохраняется дополнительный identity contact, но основной номер
не перезаписывается и ручное очищение не отменяется. Provider supplied — степень
`provider`, а не подтверждение владельца номера сотрудником.

Телефоны/email из body всегда являются suggestions: не пишутся в Client.phone,
Client.email или ClientContact и не участвуют в последующем capture/enrichment
matching. Локальный детерминированный разбор без AI отмечает foreman лишь в
короткой прямой конструкции «телефон/номер прораба: +номер» с ровно одним
телефоном. Отрицание, конкурирующее утверждение о владельце или промежуточный
текст оставляют роль unknown; другим номерам
роль не переносится. Неопределённая роль остаётся unknown. Имена сохраняют прежний
защищённый enrichment: явная ручная правка и более новое message name приоритетны.

`ContactObservation` хранит org, FK Conversation, snapshots connection PK и
external account ID, channel kind, external conversation ID, локальный message ID
для message-suggestion, typed origin/kind/value/role, confirmation и PROTECT автора
подтверждения. Event key — SHA256 структурного набора, а не текст сообщения.
Сырые сообщения, расшифровки, токены и credential payload здесь не хранятся.
Provider identity подтверждает лишь идентификатор источника. Только явное решение
resolver создаёт отдельное confirmed identity observation с автором; оно не
повышает доверие suggestions или preliminary Client автоматически.

Исторические sources/source_reference/profile.communication_data — прежние
сведения, не доказательство авторства. is_preliminary=False на legacy profiles
означает только прежний статус, не подтверждённую идентичность. Backfill нет.

## Неоднозначность и явное решение

При нескольких provider matches Conversation.client остаётся NULL, а сообщения
и их исходные события продолжают записываться. Client не создаётся. Единственная
ContactIdentityReview на Conversation хранит bounded candidate ID snapshots
(максимум 50; переполнение явно помечено), reason, revision и результат решения.
Один запрос matching читает максимум 51 результата. Очередь остаётся pending при
повторах, новых сообщениях или enrichment, даже если позднее matching изменился.
При копировании source-local очереди сохраняется исходный признак усечения.
Кандидаты заново фильтруются по текущей организации и merge-active состоянию,
удерживаются row locks до сохранения новой очереди. Drift исторических IDs не
откатывает новый inbound и не копирует foreign/deleted/merged hints: новая pending
очередь получает reason=candidate_drift и только актуальные безопасные IDs,
допуская 0/1 кандидата. Старая audit snapshot не переписывается. Пустая очередь
не разрешает выбрать произвольного клиента; при единственном оставшемся кандидате
всё равно требуется явный versioned resolver. Это fail-closed уточнение, а не
автоматическое связывание или создание дубля.
Другой чат того же provider author также получает свою source-local очередь и
не создаёт предварительный дубль, пока есть unresolved источник того же автора.
Приоритет существующей явной Conversation.client сохраняется.

`preview_identity_review` и `resolve_identity_review` используют одну текущую
серверную политику под locks connection → organization → conversation → права.
Нужны одновременно активный persisted User (технический superuser исключён),
свежая owner/admin membership (фактическое право управления карточкой клиента),
действующий Employee при его наличии, доступность организации, коммуникационные
view + take и assignment capability для чужого assignee, а также явный
CrmAccess.resolve_contact_identity. Новая capability default OFF, editable=False;
миграция, bootstrap и role signals никому её не выдают. Trusted setup после
отдельного решения о полномочиях может задать её конкретной существующей паре
organization/user через validated CrmAccess.save; командный CRM read или менеджерская
роль этой операции не разрешают. Такой setup в PR05 не выполняется.

Resolver принимает только source Conversation и одну записанную candidate ID,
expected_revision и lowercase ASCII operation_key (1–64 символа). Произвольный
клиент, cross-org или merged alias отвергаются: snapshots не являются правами.
Кандидаты, живой Client, текущая привязка и connection/account scope проверяются
заново. Preview возвращает лишь ID/revision/reason/truncation; source body,
контакты, сделки и связанные приватные звонки не выдаются. Телефонные звонки не
являются поддержанным источником resolver и не получают обход private-call policy.

Решение атомарно записывает только Conversation.client, confirmed observation и
revision/state/actor/result review. Строгий повтор того же actor/key/revision/client
возвращает прежний structural result после свежей проверки прав и live binding.
Другой запрос, устаревшая версия, ручная перепривязка, rebind аккаунта на том же
connection PK и foreign graph отвергаются без частичной записи. Повтор capture
не отменяет решение. После generic merge audit selected_client_id сохраняет старое
решение; старый replay не объявляется успехом для уже изменившейся live binding.
Это не операция объединения или переноса коммерческих связей.

Новые модели не имеют Client FK: generic client merge продолжает перемещать только
прежний Conversation.client, а historical candidate/selected ID snapshots не
переписываются bulk walker. Источники с CrmLead/CrmDeal по-прежнему блокируются
PR03 guard. На новых моделях запрещены queryset update/bulk/delete и instance
удаление; observations immutable, scope/candidates/reason и принятое решение
неизменяемы через validated ORM. Raw SQL, исторические models и Django internals —
доверенные пути вне контракта, как в PR03; межтабличного SQL tenant CHECK нет.

## Enrichment и миграция

Enrichment сохраняет внешний provider read вне транзакции и snapshot recheck
после него, включая provider_phone и account binding. Исторический unlinked
разговор не создаёт новую Client через enrich (`create=False`), но может связаться
с существующей по доказанному provider phone/identity. История проходит capture
по реальным incoming message IDs: остаются suggestions, исходящие игнорируются.
Агрегированный body не создаёт фиктивного event provenance. Обычный новый inbound
сохраняет create=True и preliminary capture. Это не массовый backfill.

0153 только добавляет три поля и две таблицы с unique/FK/CHECK; RunPython, grants,
intake и reclassification нет. FK авторов PROTECT. MySQL DDL имеет implicit commit
и возможные metadata locks: атомарный rollback всей миграции не обещается.
Откат совместимого кода сохраняет additive schema; reverse 0153 удаляет новые
таблицы/поля и теряет provenance/очередь, поэтому после первой записи требует
backup/fix-forward, а не штатного deployment rollback.

Целевые проверки: contact_identity (+ migrations), conversation_clients,
client_merge/search/card, PR03/04 foundation/commands/migrations, org/task/call privacy,
check и migration consistency. Пять race tests используют отдельные MySQL
connections (проверяется CONNECTION_ID): concurrent ambiguity/repeat,
unknown author across chats, конкурирующие решения, решение против replay и
uncommitted отзыв коммуникационного take-права перед новым решением/повтором.
Последний тест доказывает ожидание row lock и отказ после commit отзыва без
частичных изменений источника/решения.
SQLite их пропускает и не доказывает concurrency. Существующий MySQL8.4 workflow
дополнен этими suites без удаления старых; полный CI и независимое R/M review
должны фиксировать точный head, отдельно от локальной SQLite проверки.
