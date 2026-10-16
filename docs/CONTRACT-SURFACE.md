# Contract surface: book-communication-platform

This page documents the API, event, CLI, storage and configuration surface that
`book-communication-platform` owns or consumes. The machine-readable list is `interfaces` in
[`contract.json`](../contract.json); `scripts/check.py` fails when an interface
listed there is missing from this page.

- Platform: `communication` (contract `book-platform.contract.v1`, API `v1`)
- Status: `scaffolded`; source status: `current-implementation-in-solo-empire`
- Current implementation: `solo-empire` at `infra/services/notifier`
- Depends on: `identity`, `workflow`, `observability`

Interfaces marked `current-in-solo-empire` are served by the `solo-empire`
control plane today. This repository does not serve them yet; they are the
compatibility surface a migration must preserve. The `source` column is a path
in the implementing repository.

## Interfaces

| ID | Kind | Direction | Interface | Contract | Auth | Source | Status |
|---|---|---|---|---|---|---|---|
| `communication.workflow.notifier-cycle` | workflow | provides | notifier check cycle (check_interval seconds; --once, --dry-run, --config) | - | - | `infra/services/notifier/cmd/main.go` | current-in-solo-empire |
| `communication.table.notification-log` | table | provides | notification_log delivery state (delivered_at, delivery_channel, delivery_attempts) | - | - | `infra/services/notifier/internal/db/db.go` | current-in-solo-empire |
| `communication.table.notifications` | table | provides | notifications inbox rows (task_id, channel, message, sent_at, is_read) | - | - | `infra/services/notifier/internal/db/db.go` | current-in-solo-empire |
| `communication.config.channels` | config | provides | channel switches: line, slack (webhook), email (SMTP), console | - | - | `infra/services/notifier/configs/config.yaml` | current-in-solo-empire |
| `communication.consumes.tasks` | table | consumes | tasks (overdue, due-soon, high-priority) read by the notifier | - | - | `infra/services/notifier/internal/db/db.go` | current-in-solo-empire |

## Behaviour notes

Derived from the current source; re-check the source before changing a
consumer.

- The notifier is a polling Go worker, not an HTTP service. Each cycle reads
  overdue, due-soon (default 24 h) and high-priority tasks plus pending
  engagement digests, queues a notification and records delivery per channel.
- Delivery is recorded only when an external channel confirms it; a cycle with
  no confirmed channel marks the item failed so it can be retried.
- Channel credentials come from environment references in
  `configs/config.yaml` (`LINE_NOTIFY_TOKEN`, `SLACK_WEBHOOK_URL`,
  `EMAIL_USER`, `EMAIL_PASS`); values are never stored in this repository.
- The platform owns delivery state, not the business events (tasks,
  engagement digests) that cause a message.

## Migration gates

- provider adapter mock
- retry idempotency
- opt-out enforcement

Gate evidence for all three (BOO-150, owner decision accepted 2026-10-16: policy + consumer contract):
[`docs/gates/BOO-150-communication-delivery-gate.md`](gates/BOO-150-communication-delivery-gate.md),
with synthetic provider fixtures in `fixtures/gates/communication-delivery.json`, a
reference policy module (`reference/communication_policy.py`, not wired to any
runtime) and the accepted caller contract in
`contracts/consumer/communication-delivery.consumer.v1.json`.

## Out of scope

No production provider integration, credential, database writer or customer
payload lives in this repository. Record parity, privacy and rollback evidence
in the parent platform registry before any cutover.
