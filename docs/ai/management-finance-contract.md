# Внутренний контракт управленческих финансов

Статус: read-only внутренний Python-контракт. Он не является MCP, HTTP API,
service-principal или token-интерфейсом и не запускает import, refresh,
confirm/cancel или изменение mapping.

## Общий источник ДДС

`pool_service.finance_imports.management_finance.management_cashflow_data()`
читает только `CashFlowRow` активных **подтверждённых** версий конкретной
организации и соответствующие `CashFlowArticleMapping`. Результаты остаются
`Decimal`; сохранённых агрегатов, кэша с финансовыми значениями и миграций нет.
Серверные view продолжают проверять организацию и capability до вызова сервиса.

Поддерживаемые типы потока: `operating`, `investing`, `financing`, `liquidity`,
`internal`, `unclassified`. `liquidity` уже распознаётся read-моделью, но не
добавляет и не меняет ни одной существующей mapping. Изменение Django
`FLOW_CHOICES` отложено, потому что это меняет migration state.

- Отсутствующая или неподтверждённая mapping всегда остаётся внешней
  `unclassified`; её flags ещё не считаются достоверными.
- Подтверждённые `is_internal_turnover=True` и `flow_type=internal` дают
  результирующий `internal`, поэтому внутреннее не попадает в operating или
  внешний поток.
- Подтверждённое `include_in_external_cashflow=False` без внутреннего признака
  остаётся отдельной `non_external` группой. Сумма не теряется и не выдаётся за
  internal/operating; она попадает в warning и read-only registry.
- Для внешнего потока соблюдается равенство:
  `external = operating + investing + financing + liquidity + external_unclassified`.
  `net_cash_flow` в read-модели всегда вычисляется как `receipts - payments`;
  платежи не инвертируются.

`mapping_review_registry` показывает только статьи активных подтверждённых
версий, для которых нужны решение/проверка (нет mapping, статус не подтверждён,
`unclassified`, liquidity или особое исключение из external). Это результат
чтения, а не очередь записей.

Реестр не применяет keyword-эвристику и не пытается угадать, что сохранённая
как `operating` статья — это депозит или иной liquidity-кандидат. Сейчас в нём
видны только явно сохранённые `liquidity` mappings и перечисленные выше
сигналы. Пока owner/accountant не утвердит отдельный список кандидатов, такой
список отсутствует (пустой), а решение остаётся задачей следующего этапа.

## Контрактные функции

Все функции принимают один уже авторизованный `organization`. Они не заменяют
server-side guard и не выбирают организацию пользователя через `.first()`.

```python
get_finance_data_status(organization, first_month=None, last_month=None,
                        *, filters=None, group_by=None)
get_monthly_finance(organization, month=None, *, start_month=None,
                    end_month=None, filters=None, group_by=None)
get_cashflow_breakdown(organization, first_month=None, last_month=None,
                       *, filters=None, group_by=None)
get_profit_breakdown(organization, first_month, last_month=None,
                     *, filters=None, group_by=None)
```

Периоды принимаются как дата первого дня месяца или строка `YYYY-MM`.
Неизвестные фильтры/группировки отклоняются `ValueError`; они не игнорируются.
Если даты в `get_cashflow_breakdown` опущены, его `period` выводится как
min→max активных подтверждённых месяцев; внутренние пропуски всё равно
возвращаются в `missing_months` и делают ответ неполным.
У `get_finance_data_status` такая же семантика применяется отдельно к каждому
источнику: при отсутствии границ coverage строится от его первого до последнего
активного подтверждённого месяца, поэтому пропуск между январём и мартом не
маскируется как полный статус. Верхний `period` берёт min→max доступных
источников.
`get_finance_data_status` возвращает coverage источников, суммы
неклассифицированного ДДС и summary активных аномалий себестоимости.
`get_cashflow_breakdown` поддерживает фильтры `article_names`, `flow_types`,
`management_categories`, `allocations` и реальные группировки `month`,
`flow_type`, `management_category`, `article`. Стабильное полное дерево всегда
находится в `breakdown`, а явно запрошенная материализованная группировка — в
`grouped`/`grouped_by`.
`get_profit_breakdown` поддерживает реальные фильтры и группировки `manager`,
`client`, `nomenclature`, `document` по полям активных строк. `department` и
`object` — также валидные запросы, но возвращают explicit unavailable breakdown
с `items=null` и фактической причиной, а не вымышленный split.
`get_monthly_finance` принимает один `month` или диапазон `start_month` /
`end_month` (один `month` — краткая форма), возвращает totals и строки `monthly`
с `revenue`, `cost`, `gross_profit`, `gross_margin`, начисленным ФОТ, а также
operating/investing/external/liquidity/financing/internal/non_external/
unclassified ДДС. Его явно валидируемые вложенные фильтры — `cashflow`,
`profit`; `profit_nomenclature` оставлен как совместимый короткий фильтр.

Каждый ответ имеет общий envelope:

- `as_of`, `period`, `organizations`, `consolidation`, `metadata`;
- `available`, `complete`, `missing_months`, `preliminary_months`, `warnings`;
- `active_batch_ids` и `active_versions`;
- применённые `filters` и `group_by`.

`get_monthly_finance.metadata` дополнительно даёт `data_through` и
`last_updated` по каждому из `cashflow`, `profit`, `payroll` источников.

Все денежные `Decimal` отдаются строками без потери точности. Отсутствующее
значение возвращается как `null`, а не как `"0.00"`. Если нет активной
подтверждённой версии, `complete=false`, даже при пустом диапазоне
`missing_months`.

`get_monthly_finance` даёт значения прибыли, начисленного ФОТ и управленческого
ДДС за один месяц или диапазон. Для ДДС основным значением являются external operating
receipts/payments/net; external, liquidity, financing, internal, non_external
и unclassified остаются отдельными полями.

`get_profit_breakdown` не изобретает разбивку по подразделениям или объектам:
у активных `OneCMonthlyProfit` нет таких измерений. Поэтому в `dimensions`
возвращается `available=false`, `value=null` и фактическая причина: нет
подтверждённых данных, активная версия пуста или в строках нет нужного поля.

## Отображение

Страница ДДС и overview используют одну каноническую агрегацию. На ДДС
основные карточки — внешний operating flow, точная desktop-таблица — по
месяцам, а каждая mobile-карточка раскрывает собственные
`тип → категория → статья` за этот месяц. График до шести статей читает те же
канонические месячные article buckets, сохраняет отдельный filter contract и не
изменяет эти итоги. Overview показывает
operating receipts/payments/net как основной ДДС, затем external/liquidity/
financing/internal/non_external и ссылку на предупреждение по
неклассифицированным статьям.

В этом первом UI-этапе экран ДДС даёт только период и selector статей графика.
Контрактные фильтры flow/category/article предназначены для будущего
внутреннего adapter-а и не выдаются за реализованные controls страницы;
организация по-прежнему задаётся авторизованным server-side context.
