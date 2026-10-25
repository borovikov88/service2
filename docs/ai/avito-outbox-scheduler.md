# Services outgoing replies and the minute fallback

Scope is connection 4 / account 446312402 only. New manager replies dispatch
after their save transaction commits. The existing 15-minute account monitor,
its managed cron block and owner notification quiet hours are independent.
XML readiness is not a prerequisite for a manager's reply.

The protected post-health deployment step runs
scripts/avito_outbox_cron.py setup with the exact current-main SHA.
It checks deployed HEAD and origin/main under the deployment shared lock,
configures the scoped initial queue cutoff, and reads DB-only readiness.
It never processes pending messages, performs a message POST, or makes the
historical queue eligible. A preserved owner opt-out remains disabled.

If the hosting crontab CLI exists, the separate managed Services outbox block
is installed with the existing private compare-before-write/readback manager.
The monitor and outbox installers serialize their writes with one common
private crontab mutation mutex. It preserves every other byte, refuses
unmanaged/global/modified outbox
entries, uses a private backup, and removes only its exact block for rollback.
No unrelated account-monitor job, environment setting or connection is changed.

Beget may expose CronTab only in its panel. A missing CLI returns
panel_setup_required and does not fail an otherwise healthy deployment.
It is not evidence that a panel job is absent or present. An authorized channel
administrator sees the exact shell-quoted runtime command at
`/communications/connections/4/edit/#avito-outbox-admin` (connection settings,
not a customer conversation). The view and command builder both enforce current
organization/channel-management access and the Services account binding. The
response is private and non-cacheable. Other accounts do not receive the panel.
Inspect existing Beget jobs first. Only if the Services fallback job is absent,
create one with that command and the five fields `* * * * *` (every minute).
Preserve existing jobs. Healthy periodic telemetry does not ask the owner to
create another job; it shows the command for inspection of the existing one.

The fixed tick launches only send_avito_outbox --connection-id 4 --limit 10 --periodic.
The sender enforces current account/organization binding, explicit manager
intent and cutoff; it does not silently release old pending rows.
One exact old recovery is a different protected, explicitly authorized action,
not part of setup, readiness or a periodic tick.

The wrapper has a Services-only mutex, shared deployment lock, clean scheduled
environment and a 120-second deadline/10-second TERM grace. The outer supervisor
has a 145-second process-group watchdog. An interrupted POST remains unknown;
neither timeout nor a busy tick permits an automatic duplicate send.

A private 0600 tick JSON under a 0700 directory contains only UTC timestamps,
state and exit code. The sender's separate periodic DB worker record, not an on-commit run,
this private file or setup, is authoritative for outgoing last-success/queue diagnostics. A completed
empty tick can prove the worker ran, not that a client received or read anything.
Provider IDs prove provider acceptance only. Public deployment output uses
fixed codes; actual hosting paths, full cron tables and message content remain
private. Panel registration and actual periodic execution require separate
evidence.

## Separate execution evidence

The UI and read-only receipt expose immediate and periodic execution separately.
A general historical last_success_at is never attributed to either mechanism.
Immediate sending is event-driven: a long idle period alone is not a stale-worker
warning. The three-minute freshness threshold applies to minute periodic ticks.

Each periodic invocation records a bounded last-four history: start, completion,
safe error code, result counters, and scoped pending/unknown/failed counts before
and after. Run tokens stay private. Late completion of an older invocation
cannot erase the newer invocation's error or running state. Empty before/after
queues plus zero accepted count demonstrate that a run needed no customer POST.

The --periodic flag labels execution intent; these records prove the process
ran, not how it was launched. Inspect the hosting scheduler separately, then
observe at least two successive natural minute ticks. Do not manually call a
sender to manufacture schedule proof, send a test reply, or release held rows.
An unavailable hosting CLI leaves Beget panel registration unknown.
