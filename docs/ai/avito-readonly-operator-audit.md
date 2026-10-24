# Protected services release audit

The existing protected audit command and scheduled/post-deploy workflow now read
only Service2 connection4 / Avito446312402. Revision15 means the requested
published revision; the current draft revision and history row PK are separate
fields. No browser session, new service, credentials or paid API is required.

The fixed command is:
manage.py audit_avito_readonly --connection-id 4 --account-id 446312402 --revision 15

Other connections/accounts/revisions fail before token/provider access. Optional
item IDs are intersected with the bound services draft before requests; the
default is empty. The scoped reader never fetches Shop XML, physical stock or
legacy goods IDs. Legacy multi-account collection remains an internal
compatibility function, not an operational command/workflow path.

The protected GitHub workflow requires current main, successful exact-head
production CI, pinned SSH, a canonical private checkout, clean deployed source
and the existing deployment lock. Its normal workflow_run trigger supplies
fresh read-only evidence after deployment without dispatch tooling. The command
does not publish a file, create photo assets, register webhooks, change monitor
settings or create notifications. Existing account4 token-cache and request
throttle mechanisms retain their ordinary behavior.

Only bounded schema-projected receipts leave the host. They include the exact
fixed services scope, active/report coverage, safe publication/history metadata,
an optional immutable-file HTTP byte comparison, current webhook ingestion and
owner-notification evidence, and a decoded existing photograph shared with the
published snapshot. URLs, feed capability UUIDs, hashes, image bytes, actors,
listing text, message bodies, callback identifiers and secrets are withheld.
The receipt exposes only the authenticated owner page /avito/4/services-feed/;
the concrete immutable URL is shown there to the authorized owner.

One raw item-detail GET uses the existing authenticated bounded transport and
the first owned published service ID. Only fixed lower/capitalized-key presence,
type and array/object count for title/description/images/photos are projected.
Another-user/not-found/unknown status cannot confirm a content boundary. A
requested detail for the same item reuses that response. This measures the
specific endpoint response; field presence is never actual content verification
or proof of another endpoint's capabilities.

Database/account/organization/owner/key bindings are rechecked after all reads.
Drift discards all connection evidence. Helper failures remain independent.
The strict standalone renderer accepts one framed receipt and validates it
again, rejects extra/foreign connections, and never echoes raw SSH stderr.

An immutable HTTP byte match proves Service2 served that version, not that
Avito fetched it or applied its text/photos. Report errors and unavailable data
remain distinct from zero. Registration, authenticated ingress, message
persistence, owner notification and push delivery are separate facts.


## Separately approved draft-photo operation

A different fixed command run_avito_services_release_operation is invoked only
after normal deployment health and setup when the main push contains the exact
marker [avito-services-release-v15-20261024]. Its generation is
services-v15-20261024-r1. Ordinary later deployments and the read-only workflow
do not call this operation. The protected command verifies the exact deployed
SHA, account/actor/key binding and its durable generation lease.

This first operation checks the existing publication and approved photo, then
uses the signed Passenger endpoint for a real upload, private preview, cover
order save and exact restoration of the original draft content. Its two editor
saves create history and advance the draft revision; they do not publish the
file or change the current published contents/revision. It does not prove
browser clicks, public availability of the new asset, or actual Avito content.

The pending operation identity is saved before POST. Timeout or a repeated
invocation only reconciles the durable endpoint receipt; it never sends another
POST or creates a new token blindly. Unknown remains unknown until a matching
current-bound durable result exists. Shared deployment locking and private
stdout/stderr handling match the read-only operator. A separate strict parser
projects fixed positive/negative facts; workflow success alone is not proof
that the photo operation completed.
