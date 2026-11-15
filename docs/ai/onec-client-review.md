# Неразрушающий импорт клиентов 1С

Локальная реализация от базы `667d33cd6b6b172445566e891d36c0fde117713e`.
Миграция: `0172_clientimportcandidate_review_state_and_more`.

## Поведение

Full и incremental используют один staging и `apply_candidate`. Новый клиент
создаётся после повторной проверки identity/dedupe под блокировкой организации.
Ref_Key и однозначный ИНН компании остаются основаниями для идентификации;
одного общего телефона или похожего имени недостаточно. Совпадение физлица ИП
по телефону/ФИО останавливает создание для ручной проверки.

Для существующей карточки пустое входящее значение ничего не стирает, пустое
локальное поле заполняется, эквивалентное значение сохраняет прежнее отображение.
Различающиеся непустые значения становятся предложениями. Тип клиента и
юридическая форма требуют подтверждения даже при пустой форме; несовместимая
структура имени ждёт подтверждения типа. Это относится и к связанному физлицу ИП.

Дополнительные контакты сохраняются при исчезновении из источника. Новые
контакты существующей карточки проходят review, кроме заполнения пустого набора
или представления уже принятого основного контакта. Подписи существующих
контактов заполняются только при пустом локальном значении, иначе предлагаются.
Источники, source_reference, phone_type и ручные признаки контактов не заменяются.
При принятии нового основного телефона/email прежний основной сохраняется как
дополнительный. Импорт не изменяет CallPrivateNumber или правила owner/peer calls.

Смена типа может удалить только явно показанные автоматические связи ИП того же
источника. Само физлицо сохраняется. Совместимые ручные связи при уточнении
юридической формы сохраняются; несовместимые связи блокируют переход между
компанией и физлицом. Предложение создания физлица фиксирует весь применяемый
набор данных. Межорганизационные связи блокируются до формирования snapshot.

Full refresh ограничен `_seen_refs` текущего завершённого scan. Неувиденные
источники помечаются недоступными, их pending предложения устаревают; сохранённые
карточки и контакты остаются. Явно неполные buyer/contact ответы не применяются.

## Review и конкурентность

Страница `/clients/onec/proposals/` связана с существующим разделом импорта.
Используются существующие owner/admin права организации, активный actor и
проверка доступа компании; сервис решения повторно проверяет их под блокировкой.
GET ничего не применяет. POST/CSRF принимает id, decision и token, а не имя поля
или новое значение. Список экранируется и разбит на страницы.

ClientImportCandidate остаётся staging. Отдельная ClientImportProposal хранит
исходный client, source snapshot, значение до/после, версию, actor и время решения.
Принятые и отклонённые решения не изменяются. Merge не перепривязывает их target.
Уникальность `(candidate, client, field, source_version)` защищена БД.

Версии ведутся для каждого поля/контакта отдельно, включая исчезновение значения.
Повторное наблюдение и изменения других полей не возвращают отклонённое предложение.
Для контактов сравниваются kind и нормализованное значение: изменение подписи или
регистра email не воскрешает отклонённый контакт. Подписи имеют отдельный цикл.

Устаревший source binding, local snapshot или token блокирует принятие.
HTTP возвращает 409 и доступное сравнение. Явный POST «Обновить сравнение» может
обновить незавершённый snapshot только для той же актуальной версии источника;
после этого нужен новый просмотр и отдельное принятие.

Общий внешний lock — Organization; существующие связанные Client блокируются
по PK, затем source/profile/contact/review rows. Сеть находится вне DB locks.
Ручной CRM editor и Django admin используют optimistic revision и те же org/client
locks. Старые формы без revision отклоняются. Нет запросов в 1С при просмотре review.

## Локальная проверка

На финальной реализации выполнены **149 тестов на MySQL 8.0.46**, без пропусков:
scalar/profile/IP/contacts, identity, tenant isolation, CSRF, revoked access,
source/local drift, merge/rebind, replay, manual editor/admin и существующие privacy
регрессии. Пять TransactionTestCase проверяют реальные параллельные MySQL операции:
manual edit/import, import/stale editor, два accept, source update/accept,
два import. Эти тесты явно пропускаются на SQLite.

В тот же прогон включён Chromium: desktop 1366×900 и mobile 390×844, принятие,
отклонение, stale → refresh → accept, отсутствие горизонтального переполнения,
реальное сохранение формы редактора. Browser тест блокирует внешние HTTP запросы.
Все данные синтетические. `manage.py check`, migration drift и `git diff --check`
проверены отдельно. Прежние промежуточные прогоны не заменяют финальный MySQL gate.

Временный MySQL запускался отдельно, только на loopback:

```sh
docker run --detach --name service2-onec-test-mysql \
  --publish 127.0.0.1:33316:3306 \
  --env MYSQL_ALLOW_EMPTY_PASSWORD=yes --env MYSQL_DATABASE=service2_test_sandbox \
  mysql:8.0 --default-authentication-plugin=mysql_native_password
```

Test settings при `SERVICE2_ONEC_MYSQL_TEST=1` используют только `127.0.0.1:33316`
и `test_service2_onec_review`, не production DB environment. Для браузера нужны
Playwright и `/usr/bin/chromium`.

```sh
SERVICE2_ONEC_MYSQL_TEST=1 ONEC_REVIEW_BROWSER_TEST=1 python manage.py test \
  pool_service.tests.test_client_import_review \
  pool_service.tests.test_client_import_review_concurrency \
  pool_service.tests.test_client_crm_import pool_service.tests.test_client_crm_sync \
  pool_service.tests.test_crm_editor_review_regressions \
  pool_service.tests.test_crm_ui_workflow pool_service.tests.test_crm_phone_input \
  pool_service.tests.test_client_merge pool_service.tests.test_owner_call_privacy \
  pool_service.tests.test_call_privacy_enforcement \
  pool_service.tests.test_client_import_review_browser \
  --settings=service_site.test_settings --noinput --keepdb
python manage.py check --settings=service_site.test_settings
python manage.py makemigrations --check --dry-run --settings=service_site.test_settings
```

## Граница проверки

Production, реальные данные 1С/МегаФон и магазин не использовались. CI, push, PR и
deploy не запускались. Миграция применена только к синтетическим тестовым БД.
Нагрузку полного scan на рабочем объёме и актуальное состояние production здесь
не подтверждали. До любого применения обязательны отдельная проверка текущей
системы, бэкапа, миграции и штатных gates. Этот результат не разрешает выкладку.
