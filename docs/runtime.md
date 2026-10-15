# Communication runtime pilot

Status: implemented and locally verified; deployment artifacts staged only.
Correlation: `SHARED-INFRA-2026-10-15`. No production activation or provider
acceptance is claimed. The legacy contract's scaffolded migration marker stays
unchanged because its guard requires activation evidence.

## Installation and lifecycle

Python >=3.12, PostgreSQL >=16. Build/review wheels for this repository and
`platform-backend-core==0.1.0`; the parent release bundles all transitive wheels
in `wheelhouse/` for the Dockerfile. Nothing is published to PyPI. Local editing
uses `pip install -e . --no-deps` after installing the reviewed core and runtime
dependencies. `Dockerfile` and `railway.toml` are reference release assets;
no container build or Railway deployment has been executed here.

- `python -m platform_communication migrate`: explicit migration with DDL-role `DATABASE_URL`.
- `python -m platform_communication serve`: separate nonowner, NOSUPERUSER NOBYPASSRLS
  runtime `DATABASE_URL`; `PORT` defaults to 8080. Maximum API concurrency 100,
  pool 8, SQL timeout 10 seconds, HTTP boundary 30 seconds.
- Configure `JWT_ISSUER`, `JWT_AUDIENCE`, and exactly one `JWT_PUBLIC_KEY`
  (public PEM) or `JWT_JWKS_URL` (trusted HTTPS). Credentials never come from
  files or token-supplied URLs. Use the shared-core workspace RS256 profile.
- `GET /healthz`, `/readyz`, `/metrics` use shared redacted diagnostics.
  Readiness checks exact applied migration versions/checksums, table RLS, effective
  schema USAGE/history SELECT and every required table operation individually;
  missing schema returns generic 503. Setup and serving are separate commands.
- Python interface `platform_communication.app:create_app(database, verifier)` and
  `platform_communication.migrations.MIGRATIONS` support explicit dependency injection.

The DDL role owns the schema. Grant runtime schema USAGE, SELECT on
`platform_schema_migrations`, and only needed table permissions below. Do not
grant ownership, CREATE, TRUNCATE, schema-history writes or migration execution.
Use a dedicated database per service; no other service's tables are imported.
Runtime role and backup/restore provisioning remain operator release work.
All workspace authority comes from JWT `ws`, actor from JWT `sub`; body/header
workspace fields never select tenant context. API validation errors are generic.

## Verification and release gate

Run original `bash scripts/check.sh`, then `PLATFORM_TEST_DSN=<explicit disposable
local admin DSN> python -m pytest -q`. Without that DSN the PostgreSQL cases
visibly skip and provide no durability evidence. Runtime tests use unique
throwaway databases and NOSUPERUSER NOBYPASSRLS roles, real JWT signatures,
concurrent independent connections, and HTTP requests. Tests never call live
providers. Local verification uses Python 3.14/PostgreSQL 18; the 3.12/16 floor
is specified but was not executed. One upstream Starlette/httpx deprecation
warning remains. Production load, backup restore, credential rotation, ingress
policy and full provider acceptance belong to integration/release gates.

Rollback stops this optional runtime and restores consumers to the previous
adapter. Do not downgrade applied SQL or overwrite a live database. Restore
only to a new isolated database and verify synthetic workspace reads before
cutover. Coordinate retention and backup encryption with the owning operator;
no scheduled deletion or backup credential access is implemented here.

## Outbox API and workers

Grant runtime SELECT, INSERT, UPDATE on `message_outbox`; SELECT on migration
history. `POST /v1/messages` (`messages:write`, required `Idempotency-Key`)
accepts channel `email|sms|push|telegram`, bounded channel-valid recipient,
body 1..4096 characters, template `plain|html` (HTML only for email), and
`max_attempts` 1..10 (default 5). Response 202 means durably queued.
Changed canonical payload for a key returns 409. Queue capacity is 10,000
queued/leased/sending/uncertain rows per workspace, serialized transactionally;
full capacity returns 429, while exact replays still succeed. No caller URL
selects a provider. HTML content is escaped, plain text is sent without
provider markup parsing. Email uses a fixed Notification subject.

`GET /v1/messages` / `GET /v1/messages/{uuid}` require `messages:read`.
Page limit 1..100, optional after UUID, ordered by UUID; concurrent appends
are not snapshot pagination. Responses include only the scoped message,
status/attempts/times, never provider credentials or internal lease tokens.

`python -m platform_communication worker` requires explicit operator-injected
`COMMUNICATION_WORKSPACE_ID` and runtime `DATABASE_URL`. It processes only that
workspace; one worker process is sequential, independently replicable and
handles SIGTERM. Library `DeliveryWorker(OutboxStore(database), adapters)` /
`run_one(workspace)` supports an external trusted scheduler. No global
cross-tenant table reader or caller-selected worker workspace is exposed.
Bound the process/replica count to the database pool budget. Default lease is
30 seconds. Claim uses SKIP LOCKED; persisted sending precedes provider I/O;
finish requires matching unexpired lease token. Expired presend leases recover;
expired sending becomes `uncertain`, because the send may already have happened.

Delivery attempts are bounded with exponential delay capped at 3600 seconds.
A known 429 rejection retries; 4xx permanent rejection becomes dead; redirects,
5xx, malformed success, response >64 KiB, timeout or worker crash after marking
sending become uncertain. Unknown exceptions are redacted and uncertain.
Uncertain rows never automatically resend; reconcile with provider logs before
an operator-controlled new send. There is no exactly-once external guarantee,
no automatic replay of ambiguous sends, and no manual retry API that could
silently duplicate effects. A successful provider response means accepted by
provider, not delivered to a recipient. Delivery callbacks are not implemented.

`app:create_app(..., adapters={...})` supports injected test/provider adapters.
API readiness needs at least one configured adapter plus current schema/RLS;
posting to a disabled channel returns 503. Built-in adapters read only these
explicit injected variables; absent/incomplete config disables the channel:

| Channel | Variables | Fixed provider API |
|---|---|---|
| email | RESEND_API_KEY, RESEND_FROM | api.resend.com/emails |
| sms | TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM | api.twilio.com |
| push | FCM_PROJECT_ID, FCM_ACCESS_TOKEN | fcm.googleapis.com |
| telegram | TELEGRAM_BOT_TOKEN | api.telegram.org |

FCM token is already scoped and externally refreshed; the adapter never
looks up service-account files or mints tokens. Stable Resend Idempotency-Key
is `message/<UUID>` on every attempt; other adapters have no assumed provider
dedupe. No retries follow ambiguous provider acceptance even for Resend.
Provider HTTP uses fixed HTTPS endpoints, no redirects or environment proxies,
5-second I/O timeout plus elapsed-response deadline checked per chunk, bounded
response, no payload/credential exception text. A single outstanding socket
operation can finish after the elapsed deadline, within its own I/O timeout.

Protocol references checked 2026-10-15:
[Resend](https://resend.com/docs/api-reference/emails/send-email),
[Twilio](https://www.twilio.com/docs/messaging/api/message-resource),
[FCM](https://firebase.google.com/docs/cloud-messaging/send/v1-api),
[Telegram](https://core.telegram.org/bots/api#sendmessage).
No real external messages, delivery credentials or customer data were used.
Configure retention for terminal message content before production; the pilot
preserves records for reconciliation and has no automatic destructive purge.

## Local verification record — 2026-10-15

- Initial runtime suite was RED during collection because the new service
  package/store did not exist. This is missing-interface evidence, not an
  assertion-level behavioral failure.
- An assertion-level regression test removed applied migration history and
  observed the developer readiness endpoint incorrectly return 200; after
  adding exact checksum/version checks it returns 503. The same history
  checks are present in all three services.
- Final full suite: `57 passed`, no database skips, one upstream warning.
- Original `bash scripts/check.sh`: passed, 38 legacy unit tests.
- Isolated wheel build and import: passed. No wheel publication, container
  build, live provider operation, push or deployment was performed.
- Additional assertion-level RED/GREEN evidence: disabled Resend send raised
  TypeError before its generic disabled guard; elapsed-response deadline test
  accepted a response after the deadline before the elapsed check. Both now
  raise the generic DeliveryError. A new queue-capacity test first failed on
  missing QueueFull, then passed after durable serialized capacity admission.
- Real PostgreSQL cases cover concurrent create dedupe, distinct concurrent
  claims, presend lease recovery, stale/expired completion denial, ambiguous
  sending quarantine, bounded retry/dead-letter, persisted-before-send state,
  cross-workspace reads and RLS WITH CHECK writes. Provider wire tests use
  only injected HTTP transports and synthetic fixtures.

## Independent review correction — 2026-10-15

Round 1 reproduced 2 assertion-level failures before fixes in this service.
Readiness now checks effective `public` schema USAGE, history SELECT, and
separate SELECT/INSERT/UPDATE privileges for writable tables, plus DELETE for
feature flags. It does not interpret comma-separated privilege lists as an
all-required check (PostgreSQL treats those as any privilege). Each required
privilege is revoked independently in a disposable runtime role: ready changes
200 to 503, then returns to 200 after restoration. All audit forbidden
UPDATE/DELETE/TRUNCATE checks remain; each is also granted independently in
regression tests and correctly makes security not ready.

Full PostgreSQL-backed suite after correction: `62 passed`, no database
skips, one unchanged upstream Starlette/httpx warning. Original offline check
also passed. Migration 0001 SQL/checksum is unchanged.
