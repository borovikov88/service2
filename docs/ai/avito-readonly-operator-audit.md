# Explicit hosting-operator Avito audit

The connected 1C Diagnostic MCP scope is unchanged. This audit uses the existing
protected production deployment identity and the encrypted Avito client credentials
on their Service2 host, for the owner's requested account diagnostics.

Acceptance: confirm account self matches each configured connection; join selected
item IDs through API autoload_item_id to the existing XML's exact Id; read the
complete stable last_successful v4 report; publish only bounded schema-projected
receipts; never change listings, prices, categories, user rights or schedules.

manage.py audit_avito_readonly --item-ids <comma-separated numeric IDs> is an
explicit privileged hosting-operator command. At most two active credentialed
Avito connections are read; an active owner/admin with channel-management capability
must exist in each organization. Bindings, encrypted-key fingerprints and account
self are checked again after the reads. Drift discards the connection result.
Only the official https://api.avito.ru root and the existing fixed, pinned shop
XML reader are used. Existing client token caching and database request throttling
continue; no workspace/auto-collector setting or notification is written.

The deployment workflow invokes it only on a main push whose commit message
contains [avito-audit], after the normal deployment and HTTPS health. It acquires
the existing ../tmp/service2-deploy.lock, verifies the deployed tested SHA and audit
source cleanliness, uses pinned SSH, and limits execution to 360 seconds.
Ordinary deployments do not read Avito through this command.
Raw SSH stdout/stderr remain private, are deleted, and are not artifacts or logs.
A separate pure-Python parser accepts exactly one framed receipt and projects its
full schema again; invalid transport/schema is a fixed unavailable marker.

The source receipt includes XML hash/count and bounded Laguna 2.0x1.25 candidates,
XML ID/price/Availability only. API details and the latest report have distinct
fields and timestamps; no_autoload_id and not_in_current_xml are not proof an ad
was manually created or should be deleted. A matching ID proves the ID match,
not that account autoload settings use this exact feed URL: that is explicitly
unverified. This is not a full inventory of all account listings.
Reports may be incomplete/denied; their absence never becomes a successful zero.
Critical messages are bounded and contact/credential text is redacted again.
No deletion, deactivation, promotion or shop database/profile edit is performed.
