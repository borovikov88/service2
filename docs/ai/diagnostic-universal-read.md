# Universal read-only 1C Diagnostic

## Goal

Turn the Diagnostic MCP from a set of one-off tools into a small reusable read-only investigation surface for the owner/accountant. ChatGPT should be able to answer new operational questions by composing metadata search + safe row queries, without adding a new server action for every business question.

Examples that must become possible without another release:

- find a nomenclature item and investigate why its cost is negative by following its register movements, recorder/document references and related source items;
- find a contractor and determine which heater/nomenclature was sold to that contractor;
- inspect a document or document tabular part by a known Ref_Key;
- resolve batches of GUID references back to catalog rows.

## Separation from Service2 application

Service2 Diagnostic and the normal Service2 application are separate contracts:

- Diagnostic is an owner/accountant investigation surface over live 1C and remains read-only.
- Normal Service2 imports must read and validate every 1C relationship required by their own business logic; they must not call Diagnostic or depend on Diagnostic tools at runtime.
- Diagnostic may be used during development or incident investigation to understand live 1C structures and movements, but that knowledge must be implemented in the Service2 importer itself when it becomes production logic.
- Future employee-facing AI access should be built on Service2 data and Service2 server-side permissions. Expanded Diagnostic access is not the employee AI gateway and must not be broadened for that purpose.

## Access model

Diagnostic access is two-factor at the application-policy level:

- the active Service2 user must be explicitly listed in `ADVISOR_ONEC_DIAGNOSTIC_MCP_ALLOWED_USER_IDS`;
- the same user must still have a target-organization management role allowed for Diagnostic (`owner`, `admin`, or `accountant`), unless the explicitly allowlisted user is a superuser;
- inactive users, users outside the explicit allowlist, users for another organization, and technical/service identities not explicitly allowlisted are denied;
- an empty or invalid allowlist fails closed.


Keep authentication strong while relaxing read policy:

- OAuth Bearer remains mandatory for `tools/call`;
- target organization/principal scope remains mandatory;
- active superuser is allowed for the configured target organization;
- explicit target-org roles allowed: `owner`, `accountant` only;
- `admin`, `manager`, `service`, `installer`, outsiders and other-org users are denied;
- no 1C write/import actions are added;
- transport remains HTTPS GET-only, no redirects, same-origin pagination only, server-held credentials only;
- audit remains metadata-only and must not log row/filter values, tokens or credentials.

## Immediate bug fix

The current `get_1c_nomenclature_sales` fixed schema incorrectly requires `Количество` and `Сумма` as `Edm.Decimal`. Live 1C metadata publishes both as `Edm.Double` (and `Active` as `Edm.Boolean`). Make the fixed contract match the live published schema while preserving active-only, organization/date/nomenclature revalidation and complete bounded pagination.

Do not remove the specialized sales tool; it remains useful for exact complete aggregates.

## New reusable query tool

Add OAuth-only tool `query_1c_rows` (keep existing tools for compatibility).

Inputs:

- `entity_set`: published entity under existing business prefixes only;
- `fields`: scalar fields to return;
- `filters`: structured filters only;
- `limit`: default 100, hard maximum 500;
- optional `include_deleted`: default false;
- optional `include_inactive`: default false.

No caller-supplied URL, `$filter`, `$select`, `$expand`, next link, organization override, HTTP method or arbitrary OData fragment.

### Supported structured filters

Keep `eq`, `ne`, `gt`, `ge`, `lt`, `le` and add:

- `contains` for `Edm.String` only, safe OData literal escaping, non-empty query, max 300 chars;
- `startswith` for `Edm.String` only;
- `in` for primitive scalar values, list length 1..50, rendered as a server-built OR expression.

The server must validate every field/type/value from metadata before constructing OData.

### Entity policy

Published entities remain limited to existing business prefixes:

- `Catalog_`
- `Document_`
- `AccumulationRegister_`
- `InformationRegister_`
- `AccountingRegister_`

Keep primitive-only output and existing secret/direct-personal-identifier field denial.

Relax the organization rule:

1. If an entity has `Организация_Key: Edm.Guid`, automatically inject the configured allowed organization GUID clause and revalidate every returned row when that field is selected for transport. Return `organization_scope_enforced: true`.
2. If an entity has no `Организация_Key`, do not reject solely for that reason. Allow read-only access when the caller supplied at least one meaningful server-validated filter. Return `organization_scope_enforced: false`.
3. An unscoped non-catalog entity is not authorized merely because it has a key filter. For a published document tabular part, require a `Ref_Key` `eq`/`in` anchor, infer the longest published organization-scoped parent `Document_*`, verify every anchored parent document against the configured allowed organization(s), and revalidate every returned child `Ref_Key`. If that parent ownership cannot be proven, fail closed. Other unscoped non-catalog entities remain denied.
4. Catalog entities may be searched by `contains`/`startswith` on normal scalar text fields such as Description/Code/НаименованиеПолное.

### Automatic business hygiene

- If `DeletionMark: Edm.Boolean` exists and `include_deleted` is false, inject `DeletionMark eq false` and revalidate returned rows.
- If `Active: Edm.Boolean` exists and `include_inactive` is false, inject `Active eq true` and revalidate returned rows.
- Do not assume `Posted` means the same as `Active`; expose it as a readable scalar if requested.

### Limits and completeness

- hard result limit 500 rows per call;
- preserve bounded page bytes, page count, same-origin next-link checks and no redirects;
- fetch at most `limit + 1` rows logically so the response can say whether the result is truncated;
- return `complete: true|false` and `truncated: true|false` rather than presenting a limited slice as complete;
- never expose raw next links to the caller.

### Result shape

Return:

- `kind: onec_query_rows`
- `entity_set`
- `row_count`
- `rows`
- `limit`
- `complete`
- `truncated`
- `organization_scope_enforced`
- `deleted_rows_excluded`
- `inactive_rows_excluded`
- `source: live_1c_odata`

## Safer error visibility

The current MCP maps all `OneCDiagnosticError` failures to the same generic policy message, making diagnosis unnecessarily slow. For authenticated allowed users, include the fixed server error code (for example `SALES_SCHEMA_MISMATCH`, `FIELD_NOT_PUBLISHED`) in structured error output. Do not include raw URLs, filter values, response bodies, credentials or stack traces.

## Existing generic reader

Do not silently weaken `read_1c_rows` semantics in a way that breaks existing callers. Prefer adding `query_1c_rows` with the relaxed policy. Existing `read_1c_rows` may remain strict/organization-scoped for compatibility.

## Tests required

Add focused tests for:

- `get_1c_nomenclature_sales` accepts live `Edm.Double` quantity/sum contract and still requires `Active: Edm.Boolean`;
- owner/accountant allowed; admin/manager/service/installer/outsider/other-org denied; active target superuser allowed;
- OAuth principal organization scope still mandatory;
- catalog without `Организация_Key` can be searched with `contains` and apostrophes are escaped;
- organization-scoped register automatically injects/revalidates organization scope;
- unscoped document tabular part requires a parent `Ref_Key` anchor and server-side verification that every parent document belongs to an allowed organization; arbitrary `*_Key` anchors are insufficient;
- `contains`, `startswith`, `in` type validation and injection resistance;
- `DeletionMark=false` and `Active=true` automatic default filters plus explicit include flags;
- sensitive fields and non-primitive fields still denied;
- raw URL/entity fragments/OData controls/next links/write methods remain impossible through tool arguments;
- hard 500 row limit and `complete/truncated` behavior;
- safe error code visibility contains no raw values/secrets;
- anonymous `tools/list` exposes the new tool but `tools/call` remains OAuth-only;
- no migrations.

Run targeted Diagnostic tests, Django check, `makemigrations --check --dry-run`, then full test suite.

## Review/deployment

This changes permissions and live 1C read policy, so independent Codex review of the exact final GitHub head is mandatory before merge. Deployment uses the existing exact-head review gate and production workflow.
