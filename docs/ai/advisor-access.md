# Канал доступа финансового советника

## Актуальный статус

Service2 реализует отдельный, выключенный по умолчанию, remote Streamable HTTP
MCP для **ChatGPT Developer Mode**: `POST /mcp/finance/`.

- Он читает только единый server-side финансовый контракт Service2 и не читает
  1С напрямую.
- Он не содержит собственных финансовых формул и не публикует методов записи.
- Доступ требует OAuth 2.1 authorization-code flow с PKCE `S256`; No
  Authentication, service token и отдельный GET advisor API не используются.
- Роли, organization scope, отзыв OAuth grant/client, непрозрачные hashed
  short-lived tokens и минимальный audit находятся на сервере Service2.
- Реализация остаётся выключенной до отдельного provisioning, deployment и
  production read-only smoke-check.

Единственным нормативным документом подключения, OAuth, scopes, endpoint,
отзыва доступа и published tools является [finance-mcp.md](finance-mcp.md).
Он описывает строгую pre-registration ChatGPT CIMD public client
`https://chatgpt.com/oauth/client.json`; произвольный `client_id`, service
token и устаревший advisor API использовать нельзя.

`/mcp/test/` остаётся отдельным нефинансовым техническим probe и не даёт доступ
к управленческим данным.
