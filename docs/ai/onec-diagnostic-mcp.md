# 1C Diagnostic MCP

## Purpose

Expose a separate protected read-only MCP for controlled live diagnostics of the published 1C OData business surface. This MCP is not a finance source of truth and must not change Finance MCP calculations, confirmed snapshots, or import state.

Data path:

`1C OData -> Service2 onec_diagnostic policy gateway -> Diagnostic MCP -> ChatGPT`

## Access policy

- HTTP endpoint: `/mcp/1c`.
- OAuth scope: `onec.diagnostic.read`.
- Only an authenticated Service2 user explicitly listed in `ADVISOR_ONEC_DIAGNOSTIC_MCP_ALLOWED_USER_IDS` may authorize a Diagnostic MCP grant.
- That allowlisted user must also have `owner`, `admin`, or `accountant` access to `ONEC_ODATA_TARGET_ORGANIZATION_ID` (or be an explicitly allowlisted active superuser); `manager`, `service`, `installer`, other-org users, and all non-allowlisted users are denied.
- Finance MCP tokens must never authorize Diagnostic MCP calls; Diagnostic MCP tokens must never authorize Finance MCP calls.
- Reuse the already proven MCP OAuth client/principal/token storage only as transport infrastructure. Diagnostic authorization creates a separate grant and separate access/refresh token records with the Diagnostic resource and scope. Audience/resource and scope must be checked on every bearer authentication.
- Never accept credentials, bearer tokens or organization scope from MCP tool arguments.

## OAuth/resource isolation

The protected resource metadata for `/mcp/1c` must advertise a Diagnostic resource URL and the `onec.diagnostic.read` scope. Authorization and token processing may reuse shared OAuth primitives only if the resource and requested scope are explicit inputs and the existing Finance MCP behavior remains unchanged under regression tests.

A `finance.read` grant/token is invalid for the Diagnostic resource even when the principal is the same. A Diagnostic grant/token is invalid for `/mcp/finance`.

## MCP transport

Match the hardened behavior of Finance MCP:

- Streamable HTTP JSON-RPC only;
- supported protocol versions `2025-03-26` and `2025-06-18`;
- `POST` plus bounded `OPTIONS`; no GET tool execution;
- exact HTTPS Origin allowlist when Origin is present;
- Bearer authentication before JSON body parsing;
- `Content-Type: application/json`, `Accept: application/json`;
- request body maximum 64 KiB;
- `Cache-Control: no-store`, `Pragma: no-cache`, `X-Content-Type-Options: nosniff`;
- all tools advertise read-only/non-destructive/idempotent annotations.

Server name: `service2-onec-diagnostic-readonly`.

## Initial tools

### `list_1c_entities`

Search sanitized live `$metadata` through `describe_metadata`. Inputs: optional `query`, optional entity prefix, bounded `limit`.

### `get_1c_entity_schema`

Describe one allowed published business entity set through `get_entity_schema`. Must expose which scalar fields are denied as sensitive, without returning field values.

### `read_1c_rows`

Perform a bounded live GET through `read_entity_rows` only. Inputs are structured and must never contain a raw URL, raw `$filter`, raw `$select`, credentials, Basic auth, OData nextLink or organization GUID override.

Inputs:

- `entity_set`;
- `fields` (bounded list);
- `filters` as `{field, op, value}` objects;
- `top` within the gateway maximum.

The underlying gateway remains authoritative for HTTPS, GET-only, same-origin nextLink, target organization GUID filtering, sensitive-field denial, primitive-only fields, selective filters, page/row/field/filter limits and 2 MiB per page.

## Deliberate non-goals for this PR

- no writes or OData mutation methods;
- no arbitrary raw OData query tool;
- no raw HTTP/URL fetch tool;
- no unrestricted catalog dump;
- no import/apply/confirm operations;
- no finance calculations;
- no cost-tracing business logic yet;
- no UI/dashboard changes;
- no production deployment until independent review passes.

## Audit

Diagnostic tool calls need a dedicated persistent audit event. Store metadata only, never source payloads, returned field values, credentials or tokens. At minimum record:

- principal/grant;
- authorizing Service2 user when available;
- tool name;
- target organization id;
- entity set name;
- selected field names (or a bounded hash/list if needed), never values;
- result (`success`, `denied`, `error`);
- duration and response bytes;
- timestamp.

## Required tests

1. Diagnostic resource accepts only a valid `onec.diagnostic.read` token with matching audience/resource.
2. `finance.read` token is rejected by `/mcp/1c` and Diagnostic token is rejected by `/mcp/finance`.
3. Authorization is denied unless the Service2 user is explicitly allowlisted and has an allowed target-organization role (`owner`, `admin`, or `accountant`), with the explicitly allowlisted active-superuser exception described above.
4. Origin, content type, Accept, request byte limit, protocol version and JSON-RPC validation match Finance MCP hardening.
5. `tools/list` contains only read-only Diagnostic tools.
6. Tool arguments cannot inject raw `$filter`, URL, credentials, organization GUID or unsupported fields.
7. `read_1c_rows` delegates to `onec_diagnostic.read_entity_rows`; it does not implement a second security policy.
8. Audit records metadata only and never request filter values, response rows, credentials or tokens.
9. Existing Finance MCP regression tests stay green.

## Delivery order

This is a stacked change on top of `feat/onec-diagnostic-readonly-foundation` / PR #114. It must not be merged before that foundation has clean CI and an independent security/data-access review. After the foundation changes, rebase/update this branch onto the reviewed head before its own independent review.