# 1C Diagnostic: безопасный поиск продаж по номенклатуре

## Причина

После успешного OAuth-подключения ChatGPT может увидеть регистр `AccumulationRegister_Продажи_RecordType`, но универсальный `read_1c_rows` намеренно не читает `Catalog_Номенклатура`: у справочника нет `Организация_Key`. Поэтому запросы вроде «сколько песка продано в августе?» не могут безопасно разрешить `Номенклатура_Key` по названию и ChatGPT пробует другие приложения, в том числе Finance MCP, где нет полноценного live-справочника 1С и количественных движений.

Живой `$metadata` подтверждает:
- `Catalog_Номенклатура`: `Ref_Key`, `Code`, `Description`, `DeletionMark` и другие поля;
- `AccumulationRegister_Продажи_RecordType`: `Period`, `Номенклатура_Key`, `Организация_Key`, `Количество`, `Сумма` и другие поля.
В реальных августовских движениях встречаются отрицательные `Количество`/`Сумма`, поэтому итог должен быть нетто-суммой движений, а не количеством строк или суммой только положительных записей.

## Область релиза

### 1. Права Diagnostic

Привести интерактивную авторизацию Diagnostic к принятой управленческой политике Service2:
- разрешить активному `superuser`, если запрошена именно настроенная target organization;
- разрешить явные роли target organization: `owner`, `admin`, `accountant`;
- не разрешать `manager`, `service`, `installer` и пользователя без доступа;
- `ONEC_ODATA_TARGET_ORGANIZATION_ID` по-прежнему обязателен и проверяется fail-closed;
- OAuth principal organization scope по-прежнему обязателен, его нельзя обходить через superuser/admin.

### 2. Новый high-level MCP tool

Добавить отдельный read-only инструмент `get_1c_nomenclature_sales` для типовых вопросов о количестве продаж товара за период.

Вход:
- `query`: строка поиска номенклатуры, 1..100 символов;
- `start_date`: дата `YYYY-MM-DD`, включительно;
- `end_date`: дата `YYYY-MM-DD`, включительно.

Поведение:
1. Сервер сам ищет кандидатов только в `Catalog_Номенклатура` по `Description` и `Code` (при необходимости также `НаименованиеПолное`), возвращая/используя только минимальные безопасные поля: `Ref_Key`, `Code`, `Description` и признак удаления.
2. Поиск не должен превращаться в общий произвольный reader справочников. Запрещены caller-provided entity names, fields, raw OData filters/URLs и произвольные `$select`.
3. Удалённая номенклатура (`DeletionMark=true`) не используется.
4. Продажи читаются только из фиксированного `AccumulationRegister_Продажи_RecordType` и обязательно фильтруются одновременно по:
   - configured allowed `Организация_Key`;
   - `Period >= start_date 00:00:00`;
   - `Period < day_after(end_date) 00:00:00`;
   - найденным `Номенклатура_Key`.
5. Результат считается сервером полностью по безопасной пагинации. Не использовать лимит 50 строк универсального reader как будто это полный месячный итог.
6. Если пагинация, byte-limit или иной safety-limit не позволяют доказать полноту, не возвращать частичный итог как точный: fail closed либо явно `complete=false` без утверждения точного количества.
7. Количество и сумма агрегируются как нетто по всем движениям, включая отрицательные значения. Нулевые движения остаются математически нейтральными.
8. При нескольких подходящих товарах вернуть разрез по каждому кандидату и общий итог только когда семантика результата однозначна. Ответ должен позволять ChatGPT сообщить пользователю, какие позиции были учтены. Ограничить число кандидатов (например, 20) и fail/mark ambiguous при обрезанном поиске.
9. Сохраняются HTTPS-only, same-origin next links, redirect denial, OData credentials only server-side, per-page byte limits и read-only GET semantics.
10. Аудит MCP не должен сохранять строку пользовательского поиска, фильтр-значения, токены или 1C credentials. Допустимы имя tool, result, duration, response bytes и target organization metadata как сейчас.

Рекомендуемый output:
- `kind`;
- `query`, либо лучше безопасный echo только если политика аудита/ответа это допускает;
- `start_date`, `end_date`;
- `matches`: `Ref_Key`, `Code`, `Description`, `quantity_net`, `amount_net`, `row_count`;
- `quantity_net`, `amount_net`, `row_count` общего итога, если он однозначен;
- `complete: true/false`;
- `organization_scope_enforced: true`;
- `source: live_1c_odata`.

## Критерии готовности

- Старые три Diagnostic tools продолжают работать и сохраняют прежние ограничения.
- `tools/list` и ChatGPT anonymous discovery показывают новый OAuth-only read-only tool.
- Tool call без Bearer token возвращает OAuth challenge, как остальные Diagnostic tools.
- С `owner`, `admin`, `accountant` и target-scoped `superuser` authorization разрешается; manager/outsider/other-org остаются запрещены.
- Есть тесты на escaping строки поиска (включая `'`), запрет raw OData controls, deleted items, org-scope, date boundary, отрицательные движения, multiple matches, pagination completeness и safety limits.
- Нет миграций и нет записей в 1С.
- Targeted Diagnostic tests, полный Django suite, `manage.py check` и `makemigrations --check --dry-run` зелёные.
- Для этого изменения прав доступа и live 1C access обязателен независимый Codex review фактического final diff.
