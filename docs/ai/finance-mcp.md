# Защищённый read-only финансовый MCP

## Назначение и источник цифр

`POST /mcp/finance` — удалённый stateless Streamable HTTP MCP для финансового
советника. Он **не** обращается к 1С и не имеет собственных финансовых формул.
Для каждой организации он вызывает только публичный read-only контракт
`pool_service.finance_imports.management_finance`, который уже используют
финансовые страницы Service2:

`активные подтверждённые выгрузки → management_finance → finance_advisor → MCP`.

Свод нескольких разрешённых организаций выполняет серверный
`pool_service.services.finance_advisor`: он объединяет уже рассчитанные
организационные результаты и не читает сырой 1С, mappings или строки импорта.
Пустое/неполное значение остаётся `null`; деньги сериализуются как строки
Decimal (`"103540.19"`).

## Публикуемые инструменты

Только эти пять инструментов видны после OAuth-авторизации:

1. `list_finance_organizations`
2. `get_finance_data_status`
3. `get_monthly_finance`
4. `get_cashflow_breakdown`
5. `get_profit_breakdown`

У каждого указан `readOnlyHint=true`. В MCP отсутствуют инструменты импорта,
подтверждения, изменения active version, CashFlowArticleMapping, финансовых
строк, пользователей, прав, файлов, SQL или shell. `tools/list` не зависит от
входного prompt и не публикует скрытых/административных методов.

## OAuth 2.1

Финансовый endpoint выключен по умолчанию. После включения он требует
`Authorization: Bearer …` для **каждого** MCP POST, включая `initialize` и
`tools/list`; неавторизованный POST отвечает `401` с Bearer challenge и ссылкой
на Protected Resource Metadata.

| Роль | URI |
|---|---|
| MCP HTTPS endpoint | `https://<host>/mcp/finance` |
| Canonical OAuth resource | `https://<host>/mcp/finance` |
| Protected-resource metadata | `https://<host>/.well-known/oauth-protected-resource/mcp/finance` |
| Authorization-server metadata | `https://<host>/.well-known/oauth-authorization-server` |
| Authorization endpoint | `https://<host>/oauth/finance/authorize` |
| Token endpoint | `https://<host>/oauth/finance/token` |

MCP endpoint и canonical OAuth resource намеренно совпадают полностью.

Реализован только безопасный заранее зарегистрированный **public client**:

- authorization code + обязательный PKCE `S256`;
- обязательные непустой `state`, точный `resource` и точный HTTPS
  `redirect_uri` из серверного allowlist;
- access token — непрозрачный и короткоживущий (по умолчанию 10 минут);
- при `offline_access` выдаётся refresh token; он ротируется при каждом обмене;
- повторное использование старого refresh token отзывает весь grant и его
  access tokens;
- в БД есть только SHA-256 hashes кодов/tokens, не их значения;
- токены никогда не передаются в URL, Git, CI, application logs или audit.

Metadata объявляет `finance.read`, `offline_access`,
`authorization_code`, `refresh_token`, `S256` и
`client_id_metadata_document_supported=true`. Dynamic Client Registration
намеренно не включён. Вместо произвольного локального `client_id` поддержан
только доверенный ChatGPT Client ID Metadata Document (CIMD):
`https://chatgpt.com/oauth/client.json`.

Это public client: Service2 объявляет только пересечение безопасного метода
аутентификации token endpoint — `none` — и всё равно требует PKCE `S256`.
Ни private key, ни JWKS, ни client secret в Service2 для этой интеграции не
создаются. `client_id` не является секретом, но его нельзя заменять похожим
URL или произвольной строкой.

## Включение и pre-registration

До проверки deployment оставить в production `.env`:

```dotenv
ADVISOR_FINANCE_MCP_ENABLED=false
```

После deployment и до включения заполнить **несекретные** параметры:

```dotenv
ADVISOR_FINANCE_MCP_RESOURCE_URL=https://<public-host>/mcp/finance
ADVISOR_FINANCE_MCP_AUTH_ISSUER=https://<public-host>
ADVISOR_FINANCE_MCP_ALLOWED_ORIGINS=https://chatgpt.com
ADVISOR_FINANCE_MCP_ACCESS_TOKEN_TTL_SECONDS=600
ADVISOR_FINANCE_MCP_REFRESH_TOKEN_TTL_SECONDS=2592000
ADVISOR_FINANCE_MCP_AUTHORIZATION_CODE_TTL_SECONDS=300
```

Каждое соединение с финансовым MCP проверяет `Origin` **до** Bearer token и
JSON body. Отсутствующий `Origin` разрешён для server-side транспорта ChatGPT;
если он передан, он должен в точности входить в
`ADVISOR_FINANCE_MCP_ALLOWED_ORIGINS` (по умолчанию `https://chatgpt.com`).
Иной или некорректный Origin получает `403`; endpoint не добавляет широких CORS
заголовков.

Перед provisioning не придумывайте callback или client ID. Команда получает
только allowlisted CIMD по HTTPS **один раз**, не следует redirect, ограничивает
ответ 16 KiB, проверяет JSON, точный `client_id`, поддержку public
authorization-code flow и то, что callback опубликован в документе. В БД
сохраняются лишь fingerprint документа, время проверки и точный callback;
runtime OAuth не делает сетевых запросов и не разыменовывает `client_id` из
запроса.

На момент подготовки документа ChatGPT публикует callback
`https://chatgpt.com/connector_platform_oauth_redirect`. Если trusted document
изменится, provisioning безопасно завершится ошибкой до создания доступа — не
подставляйте новый URI вручную без проверки обновлённого документа и версии
интеграции.

Затем оператор заранее создаёт отдельную identity и scope одной командой.
Указанный `--actor-id` обязателен: Service2 проверяет, что этот действующий
owner/admin/accountant имеет право управлять финансами **во всех** перечисленных
организациях. Значения примера не являются production данными:

```bash
../venv/bin/python -B manage.py manage_finance_mcp_access provision \
  --client-id 'https://chatgpt.com/oauth/client.json' \
  --client-name 'ChatGPT finance adviser' \
  --principal-subject 'chatgpt:service2-finance:v1' \
  --principal-name 'Финансовый советник ChatGPT' \
  --redirect-uri 'https://chatgpt.com/connector_platform_oauth_redirect' \
  --organization-id <allowed-org-id> \
  --actor-id <owner-or-admin-user-id> \
  --confirm 'https://chatgpt.com/oauth/client.json'
```

Команда не создаёт token и не печатает секрет. Один principal намеренно
создаётся только для одного client: повторное использование subject отклоняется,
чтобы новый scope не расширил доступ уже подключённому client.

После этого установить `ADVISOR_FINANCE_MCP_ENABLED=true`, выполнить обычный
deployment и добавить в ChatGPT custom MCP app endpoint
`https://<public-host>/mcp/finance`. Выбрать OAuth и пройти owner login
Service2; ChatGPT использует зарегистрированный CIMD `client_id`, поэтому не
нужно создавать или вводить отдельный секрет. Для `offline_access` ChatGPT
запрашивает scope через metadata; если он не нужен, access будет просто
переавторизован после истечения срока.

## Scope, фильтры и аудит

Organization scope — уникальная server-side связь principal–organization.
Если в запросе указан хотя бы один ID вне scope, **весь** tool call отклоняется
без частичного результата. Непереданный `organization_ids` означает весь
разрешённый scope. Сводный период ограничен 24 месяцами; детальные ДДС/ВП
ограничены `page_size ≤ 100` и пагинацией. `department` и `object` честно
возвращают `unavailable`, когда соответствующего разреза нет в активных строках.

Каждый tools/call создаёт `FinanceMcpAuditEvent` с principal, tool, временем,
организациями, периодом, group_by, результатом, длительностью и размером ответа.
Audit не хранит token или финансовый payload. Доступ к финансам через MCP не
создаёт/изменяет CashFlowRow, imports, mappings или active versions.

## Отзыв доступа

Оператор отзывает один grant или весь public client явной подтверждаемой
командой. Отзыв немедленно помечает текущие access/refresh tokens отозванными;
следующий MCP POST получит `401`.

```bash
# Отключить весь ChatGPT client и все его grants/tokens.
../venv/bin/python -B manage.py manage_finance_mcp_access revoke \
  --client-id 'https://chatgpt.com/oauth/client.json' \
  --actor-id <owner-or-admin-user-id> \
  --reason 'Owner requested access revocation' \
  --confirm 'https://chatgpt.com/oauth/client.json'

# Или отозвать ровно одну выданную авторизацию.
../venv/bin/python -B manage.py manage_finance_mcp_access revoke \
  --grant-id <grant-id> \
  --actor-id <owner-or-admin-user-id> \
  --reason 'Replace adviser authorization' \
  --confirm '<grant-id>'
```

Не заменяйте это удалением данных руками: command проверяет финансовые права
`--actor-id` на всём scope цели, сохраняет audit trail и отзывает все
производные token records согласованно.
