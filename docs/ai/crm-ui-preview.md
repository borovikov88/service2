# CRM UI Preview 1

Синтетический просмотр CRM Аквалайн: входящие и четыре воронки из
`crm_commercial_bootstrap.PROPOSED_PIPELINES`, table/kanban и карточка. Это
read-only UI package, не staging и не новая бизнес-система. Все 21 запись,
имена, объекты, суммы и сроки вымышлены. Опорная дата — 26 октября 2026.
Нет сообщений, расшифровок, контактов, рабочих идентификаторов и интеграций.

## Воспроизведение

Без Django setup и базы, из корня repository:

```sh
python -c 'from pool_service.crm_ui_preview import export_preview; export_preview("/tmp/crm-ui-preview")'
```

Или безопасной management command:

```sh
python manage.py export_crm_ui_preview --output /tmp/crm-ui-preview --settings=service_site.test_settings
```

Выход: `index.html`, `assets/crm-preview.css`, `assets/crm-preview.js`.
Размещать только эти файлы на отдельном private static host. Production settings,
URLconf, grants, flags, migrations и deployment не меняются. Export не включает
чужие файлы; для публикации использовать новый пустой каталог. Предпочтителен
HTTP static preview; при `file://` поддержка history API зависит от браузера.

## Контракт переиспользования

`workspace.html` включает одинаковый `card.html` для обоих видов и `detail.html`
для каждой карточки. `standalone.html` — только изолированная export-оболочка,
без production base.html/context processors/forms. CSS/JS namespaced `acrm`;
нет Bootstrap/CDN/npm dependency, внешних assets, fetch, service worker и
аналитики. Presenter — plain dictionary с `pipelines`, `deals`, `owners`,
`sources`; строки autoescaped. Список этапов читается как AST literal из
bootstrap, его код не импортируется и модели не загружаются.

Будущий permission-scoped adapter должен выдавать только разрешённые записи и
поля, а также согласованные команды через существующие серверные сервисы.
Это отдельная работа: локальные фильтры и demo-разметка не являются контролем
доступа. Перед реальной подгрузкой заменить синтетические идентификаторы и
разрешить только scoped detail. Для больших наборов нужны серверные partials,
пагинация и отмена устаревшего поиска; preview намеренно ограничен 21 записью.
Адаптер обязан передавать один согласованный набор `deals`/`stage.deals`.

JS выполняет только поиск (300 ms debounce), фильтры, навигацию и открытие
native dialog. URL хранит section/view/q/owner/stage/source/attention/deal/theme;
ссылку можно перезагрузить или передать в рамках private host. Смена раздела
сбрасывает этап и сохраняет остальные фильтры. Счётчики рабочих представлений
учитывают запрос и фильтры, но не текущий attention; счётчики навигации показывают
полный синтетический набор раздела. Неизвестная сумма — «Не указана», без
агрегирования разных показателей. Просмотр не меняет назначение/этап.

На телефоне канбан становится вертикальным списком этапов, таблица — компактным
списком тех же карточек. Dialog занимает весь экран; Escape и кнопка закрытия
возвращают фокус, native dialog ограничивает Tab внутри открытой карточки.

## Проверки

```sh
python -m unittest pool_service.tests.test_crm_ui_preview
node --test pool_service/tests/crm_ui_preview.test.cjs
python manage.py check --settings=service_site.test_settings
```

Тесты проверяют детерминированность, отсутствие DB queries, autoescape,
одинаковый inventory обоих видов, URL roundtrip и общий фильтр. Browser QA
1440/390 px, обе темы, пустой результат, drawer, Escape/focus и network
проверяются отдельно владельцем публикации. Авторизация hosting не обеспечивается
HTML: private audience и запрет анонимного доступа проверяются на host.
