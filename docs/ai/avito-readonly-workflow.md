# Protected read-only Avito API audit

The dedicated Avito read-only API audit reads the already deployed operator without redeploying Passenger, refreshing static files, applying migrations, changing products/feed/settings, buying services or sending notifications. Existing saved Avito credentials remain on the authorized Service2 host.

It runs after successful main Test and deploy main completion, hourly at minute17 UTC (the same minute in Asia/Barnaul), and by workflow_dispatch with exact current-main confirmation. Scheduled Actions may be delayed; a configured workflow is not proof a schedule has run. Manual runs must target main. No new secrets, tokens, permissions outside read-only GitHub metadata or environment protection changes are introduced.

Every read requires current GitHub main, its latest matching completed successful production CI run, existing pinned SSH keys and the existing protected updater's preflight. The host additionally checks canonical checkout layout, private mutable directories, unchanged runtime code, its exact deployed HEAD, verified repository origin and current origin/main under the canonical ../tmp/service2-deploy.lock. Dirty/stale/untrusted/busy states block. There is no checkout, reset, fetch, cleanup, application restart or deployment fallback in this workflow.

The operator validates existing account/credential/owner scope and reads current Autoload source twice. It enumerates listings, reads the latest complete nonduplicated report, and joins the current fixed shop XML/stock source. Unknown bindings, denied accounts, provider failures, repeated report rows and physical warehouse stock stay explicit. A successful transport with a partial receipt is not a complete account audit.

SSH keys and raw hosting stdout/stderr exist only in private runner temporary files removed on exit. The existing strict receipt renderer emits fixed bounded metadata only; there are no new artifacts or raw provider exports. Logs must not be unmasked to reconstruct business values affected by GitHub secret masking.

After a shop XML correction, a report started before that correction does not prove subsequent Avito processing. Compare the report start time with the shop applied receipt. The shop's read-only category/TNVED diagnostics distinguish actual tags from schema/classification verification and do not apply corrections. The plugin supports generic rule parameters and attribute mapping; missing literal TNVED code is not a capability diagnosis.

Validation covers stale/failed/in-flight/fork/foreign/manual CI gates and executable host-boundary failures (dirty code, stale HEAD/remote, foreign origin, symlink/busy shared lock, unsafe directory modes). Reuse existing exact-tree CI, independent review for the SSH/credentials boundary, protected main deploy and health before the first live audit. CI/deploy and external-read results are reported separately.
