# Read-only OData preview валовой прибыли 1С Fresh

Команда `onec_odata_profit_preview` читает записи регистра
`AccumulationRegister_Продажи_RecordType` только методом HTTP GET и выводит
агрегаты в JSON. Она не создаёт import batch, строки валовой прибыли, версии
месяцев и вообще не выполняет записей в БД или 1С.

## Настройка

Все параметры задаются только через environment:

- `ONEC_ODATA_BASE_URL` — URL без credentials/query/fragment, заканчивающийся
  `/odata/standard.odata/`;
- `ONEC_ODATA_USERNAME` и `ONEC_ODATA_PASSWORD` — либо оба заданы, либо оба пусты;
- `ONEC_ODATA_ORGANIZATION_GUIDS` — allowlist GUID через запятую;
- `ONEC_ODATA_TIMEOUT_SECONDS` — timeout одного GET;
- `ONEC_ODATA_MAX_PAGES` — предел страниц;
- `ONEC_ODATA_MAX_ROWS` — общий предел строк preview.

Реальные endpoint и credentials нельзя добавлять в Git.

## Запуск

```bash
python manage.py onec_odata_profit_preview \
  --start-month 2026-05 \
  --end-month 2026-05 \
  --organization-guid 00000000-0000-0000-0000-000000000001
```

`--organization-guid` можно повторять; каждый GUID обязан входить в настроенный
allowlist. Диапазон месяцев обязателен и включителен.

## Гарантии preview

- только GET, redirects запрещены;
- каждая строка повторно проверяется по периоду и organization allowlist;
- деньги и количество разбираются как `Decimal`, без JSON float;
- identity строки — `Recorder + LineNumber`;
- нулевой GUID контрагента означает «без контрагента»;
- nextLink остаётся на исходных scheme/host/port и внутри OData base path;
- pagination и общий объём строк ограничены, pagination защищена от циклов;
- stdout содержит только агрегаты, не endpoint, credentials или строки регистра.
