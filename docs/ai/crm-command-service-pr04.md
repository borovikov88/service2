# PR04: внутренние команды коммерческой CRM

`pool_service.services.crm_commercial_commands.execute_commercial_command` —
единственная предоставленная точка входа нового application writer. Форма,
канбан, bulk, HTTP/API и фоновые adapters в этом пакете не реализованы и не
переключены на него. Их будущие изменения должны вызывать этот сервис. Legacy
writer продолжает работать. Intake, backfill, контакты, договоры, исполнение,
финансы, отправка клиентам и закрытие не входят в PR04. D01–D12 сохранены;
Q03 не доопределён, спорные права Q06 не выданы, Q13 отложен.

## Команды и доступ

Все команды принимают actor, organization, operation_key, command_type,
subject_id, expected_revision и changes. Только существующие собственные
неархивные карточки; lead.assignee/deal.manager должен совпадать с текущим actor.
Свободную или чужую карточку нельзя взять, переназначить либо изменить через
этот пакет. Необходимы одновременно свежий активный User, membership этой
организации, отсутствие inactive/dismissed Employee, crm_v2_read,
crm_v2_write, доступ чтения и **точный explicit grant** из таблицы.
Отсутствующий Employee сохраняет принятый контракт аккаунта PR03.

| Команда | Поля changes | Explicit capability |
| --- | --- | --- |
| lead.edit | title, need | edit_own_leads |
| lead.transition | status; reason и follow_up_at по правилам ниже | transition_own_leads |
| deal.edit | title | edit_own_deals |
| deal.transition | stage_id; reason для возврата | transition_own_deals |

Новые четыре CrmAccess boolean имеют default=False и editable=False.
Bootstrap их не создаёт и не выдаёт. Выдача возможна только через отдельный
доверенный setup; нового public/admin endpoint нет. owner/admin/superuser,
флаги и D06 не дают ни одного write grant. D06 даёт менеджеру только командное
чтение своей организации; другим ролям дополнительно нужен view_team_deals.
`can_write_commercial` — лишь актуальная подсказка доступности какого-либо
own grant, а не разрешение конкретного действия/карточки. Сам сервис повторяет
проверку под locks. Q06 перед будущей выдачей edit-other/reassign/маржи/командных
полей остаётся отдельным решением.

Не принимаются client, assignment, pipeline, суммы/бюджеты/валюта, archive,
outcome, даты исполнения/оплаты/закрытия, source и прочие поля. Неизвестная
сумма остаётся NULL. Создание/конвертация/квалификация лида и их результаты
отложены. Редактирование лида с завершённым разбором и сделки с outcome != open
запрещено. title обязателен, максимум 255 символов; need ограничен 10000.

## Единые правила переходов

Рабочие lead status: working, qualifying, waiting, deferred. Возврат в new и
closed недоступны. Назад по порядку new→working→qualifying→waiting→deferred
требуется непустая причина. Для waiting/deferred каждый переход требует свою
причину и timezone-aware контрольную дату; старые reason/date не подставляются
молча. При выходе из ожидания дата очищается. Дедлайны/число попыток Q10 не
придумываются.

| Код pipeline | Разрешённые подготовительные stage codes в порядке перехода |
| --- | --- |
| construction | need → technical → proposal |
| service | request → assessment → proposal |
| retail | need → selection → proposal |
| tender | review → assessment → preparation |

И текущий, и целевой этап должны быть в этой таблице, цель — в текущей pipeline
и организации. Для нового перехода pipeline и целевой stage должны быть active.
Исторический отключённый текущий этап остаётся и допускает уход на разрешённую
активную цель. Переход на тот же этап отклоняется. Порядок определяется кодами
таблицы, а не пользовательским position/name. Название «Успешно завершено» не
меняет outcome. Остальные этапы явно отвергаются с причиной о ещё отсутствующем
контракте/исполнении/оплате/закрытии; retail/разовая финализация не реализована.
Назад требуется reason. Произвольные и неизвестные pipeline codes не получают
автоматического правила.

## Транзакция, канонизация и конкуренция

operation_key: строго lowercase ASCII `[a-z0-9][a-z0-9_-]*`, максимум 100,
без lower/trim aliases. subject_id — positive int, expected_revision —
nonnegative int; bool не принимается за int. Поля ограничены таблицей,
строки trim, дата приводится к UTC ISO, optional reason/date имеют явные
канонические defaults. SHA256 считается по JSON со стабильным порядком ключей,
включая actor, organization, command_type, subject_id, expected_revision, changes.
Другая revision/субъект/действие/автор с тем же ключом — другой payload.
Свободный payload не хранится в command/event.

В atomic: Organization FOR UPDATE → User → membership rows (pk) → Employee
rows (pk) → CrmAccess → subject → текущий Client, если есть → operation key;
для перехода сделки дополнительно текущие pipeline/stage и целевой stage (pk).
Обычные supported CommercialModel.save тоже начинают с Organization mutex
(для Stage через immutable pipeline); затем сохраняют принятые row/FK locks и
full_clean. Недопустимый reparent сначала блокирует затронутые org по pk, затем
отклоняется. Это устраняет инверсию settings row→org относительно command.

**Tradeoff PR04: все коммерческие команды и supported settings saves одной
организации сериализованы.** Параллельное выполнение разных карточек внутри
org не обещается. Mutex закрывает и отсутствующую строку operation_key без
попытки блокировать несуществующую строку/полагаться на MySQL gap locks. Разные
org независимы. Физическая уникальность org/key остаётся вторым guard.

Сервис использует свежие locking reads прав, держит их до commit. Изменение
flag, persisted User, существующих membership/Employee/grant строк до получения
соответствующего lock видно команде после ожидания; изменение после её locks
ожидает завершения. Поэтому завершившаяся до команды/replay revocation не
обходится stale Python instance или сохранённым результатом. Supported settings
save следует тому же org mutex. Тесты удерживают незакоммиченные мутации и
проверяют оба порядка command/revoke отдельными соединениями.

Текущий субъект, ownership и scope проверяются и для replay. Точный key/hash
возвращает сохранённые структурные IDs/revision до проверки expected_revision:
это повтор уже выполненной операции. Другой payload даёт CommercialConflict.
Для нового ключа stale revision тоже даёт CommercialConflict. Успех атомарно
сохраняет изменение, revision+1, CrmCommand, CrmEvent и command.result. Ошибка
либо внешний rollback удаляет все эти результаты. Команда без фактического edit
отвергается. Неуспешный ключ не резервируется; после исправления возможен retry.
Уведомления/внешние побочные эффекты не требуются и не добавлены.

## Журнал и границы ORM

Event.safe_diff содержит revision, stage IDs, whitelist lead status и bool
markers title_changed/need_changed; содержимое title/need не копируется.
Отдельный bounded Event.reason (до 500) сохраняет **явно введённое пользователем
основание команды**, включая возврат назад. Он не предназначен для сообщений,
расшифровок или копирования финансового источника. Event доступен только через
будущий scoped reader с актуальным доступом к субъекту; прямое чтение модели
само по себе не является авторизацией. Отдельного timeline/API reader здесь нет.
Command.result по-прежнему только IDs/revision. Event неизменяем через ORM.

Сохранён limited ORM contract PR03: unsafe update/bulk запрещены; User FK
PROTECT; historical retained assignee не требует нового назначения; source-client
merge guard до PR13 сохранён. Прямые validated instance saves — доверенная
низкоуровневая граница для setup/истории, а не обход разрешений приложения для
будущих adapters. Raw SQL, save_base(raw=True), private Django methods и
исторические migration models остаются trusted bypass. Долговечный DB-level
межтабличный invariant при самостоятельных legacy перепривязках не обещается;
команда блокирует/проверяет текущий Client и не ремонтирует его молча.

0152 только добавляет четыре false capability поля и пустой Event.reason;
не выдаёт grants, не меняет flags/legacy и не конвертирует данные. MySQL DDL
rollback не обещается. Reverse теряет причины/новые grants и не является
штатным production rollback; при OFF сохранять additive schema.

## Проверки и следующий пакет

`test_crm_commercial_commands` проверяет вход/права/ownership/tenant FK,
идемпотентность, возврат, запрет закрытия, snapshot сохранности и rollback.
TransactionTestCase использует отдельные thread connections и перекрытие
транзакций для same revision/key, revocation, flag и stage settings. На SQLite
весь concurrency класс честно skipped; только isolated MySQL 8.4 workflow
проверяет row-lock поведение. Workflow сохраняет все прежние finance/Avito/CRM
suites и добавляет commands. PR03 migration test восстанавливает graph leaf;
новая проверка 0151→leaf сохраняет read grant/строки, не выдавая own grants.

Локальная целевая проверка: 87 tests, OK (7 concurrency skips на SQLite);
check и makemigrations --check --dry-run — PASS. MySQL и полный CI ещё не
проверены локально. Фактические head/results фиксируются в отчёте выполнения. Полный CI и
независимый review точного head обязательны перед принятием; локальная SQLite
их не заменяет. Следующий PR05: происхождение контактов и очередь неоднозначной
идентификации, с сохранением текущего capture; реализация в PR04 не начата.
