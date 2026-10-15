# BOO-150 — communication delivery gate evidence

Status: evidence recorded; owner decision **proposed, not accepted**. Nothing
here changes a status, activates a runtime, or moves traffic.

## Next gate (verbatim)

> Add provider adapter fixtures, opt-out enforcement, retry idempotency, and a consumer contract.

Source: `solo-empire` `repository-catalog/registries/system-repositories.yaml:455-478`
(system `communications`, backend `platform-communication`, `integration_status: scaffold`).

## Contract section answered

`systems/architecture/solo-empire-api-contract-v1.md#signed-and-authenticated-webhook-routes`
(private `architecture-docs`, lines 136-143), plus the signed-webhook auth class at line 61.
The relevant row is `POST /api/webhook/line`: `x-line-signature` verified on the raw body,
`events[]` validated, deduplicated by the derived external event ID. This gate uses that row as
the only inbound path for LINE opt-out signals (`unfollow`), and the contract's "changing
idempotency key semantics or duplicate response requires a contract update" rule for retries.

## Deliverables in this repository

| Artifact | Path | Wired to runtime |
|---|---|---|
| Provider adapter fixtures, retry and opt-out cases (synthetic) | `fixtures/gates/communication-delivery.json` | no |
| Reference policy module (pure Python, stdlib only) | `reference/communication_policy.py` | **no** — nothing imports it at run time; it sends nothing and holds no credentials |
| Consumer contract for callers | `contracts/consumer/communication-delivery.consumer.v1.json` | no (`status: proposed`, `owner_acceptance: pending`) |
| Tests (run by `bash scripts/check.sh`) | `tests/test_delivery_gate.py` | — |

`main` has no adapter, store or worker code, so per the gate brief this adds a reference policy
module plus tests rather than testing runtime code. Open PR #1
(`codex/shared-infrastructure-20261015`, "Add durable workspace message delivery") would add a
runtime (`src/platform_communication/adapters.py` with Resend/Twilio/FCM/Telegram adapters, a
PostgreSQL `message_outbox` with `(workspace_id, event_key)` idempotency and lease/retry). This
branch does not touch any file that PR changes (`README.md`, `.gitignore`, `src/`, `runtime_tests/`,
`Dockerfile`, `pyproject.toml`, `railway.toml`, `docs/runtime.md`, `.github/workflows/shared-runtime.yml`),
so the doc is linked from `docs/CONTRACT-SURFACE.md` instead of the README. Neither PR #1's
adapters nor its outbox enforce opt-out.

## Inventory — what the current source does

The registry `source_paths` (`infra/scripts/utils/notifications_store.py`, `operations`) no longer
exist in `solo-empire` HEAD `63efeeb0`; they were removed in `5f034945` ("the hub is only the
registry and the clone folder"). Rows below cite the last version (`5f034945^`) and the server
that now implements the API, `bookchaowalit/platform-notifications-api` at `57dd097` (`main`).
`operations/` (last present at `daf06f31^`) contains runbooks only — no delivery, retry or
opt-out code.

| Concern | Current behaviour | Evidence (file:line) | Fixture / test |
|---|---|---|---|
| Queue idempotency | `POST /v1/notifications` with `dedupe_key` returns `outcome: existing` and the first entry; concurrent repeats keep the first (`ON CONFLICT (workspace_id, dedupe_key)`) | platform-notifications-api `src/solo_notifications/app.py:416-449` (conflict at 437); client `notifications_store.py:153` | `retry_idempotency.rules[queue-dedupe]`; `test_queue_is_idempotent_by_dedupe_key` |
| Dedupe key construction | `"k-" + sha256(json.dumps(parts, default=str, ensure_ascii=False))[:60]` | `notifications_store.py:59`; `dispatch.py:216-218` | `golden_dedupe_keys`; `test_golden_dedupe_keys`, `SourceDriftTests.test_dedupe_construction` |
| Dedupe key limits | 8..200 chars | `app.py:124` | consumer `queue.limits`; `test_queue_fields_and_limits` |
| Delivery recording | `delivered` (needs non-console channel) and `failed` each add an attempt; `console` adds none; else `422 console_is_not_delivery` | `app.py:161-166`, `452-475` | `test_console_is_never_a_delivery`, `test_delivery_outcomes_and_console_rule` |
| Retry ceiling | pending = `undelivered=true&max_attempts=3`; `_record` retries a lost result on 5xx up to 3 times; at-least-once | `dispatch.py:56`, `135-138`, `333-343` | `max_attempts: 3`; `test_max_attempts_agree`, `test_scenarios` |
| Live external provider | Telegram `sendMessage`, token never echoed, errors reduced to `telegram_http_<code>` | `dispatch.py:311-330` | `providers.telegram` (status `current`) |
| Local observation | console is a local log, never proof of delivery | `app.py:452-455`; `notification_runner.py` (`5f034945^`) | `providers.console` |
| Configured-but-disabled channels | LINE Notify, Slack webhook, SMTP email, console (`enabled: false` except console) in the retired Go notifier | `solo-empire@3bf3637c^:infra/services/notifier/configs/config.yaml:13-34` | `providers.line|slack|email` (status `configured-disabled`) |
| Quiet hours | 22:00-07:00 Asia/Bangkok, nothing sent | `dispatch.py:57`; Go config `:47` | not in this gate (recorded only) |
| Opt-out | **none**: no opt-out store, field, route or check in the API, dispatcher, client or PR #1 | `grep -i opt.out` over the sources above returns nothing | `opt_out.status: proposed`; `test_telegram_is_the_current_external_provider` (runs in CI against the pinned checkout) fails once opt-out appears in the server source it reads, forcing a status update |

## Provider adapter fixture mapping

| Provider | Status | Request fixture | Caller idempotency key | Outcomes covered |
|---|---|---|---|---|
| telegram | current | `POST api.telegram.org/bot<redacted>/sendMessage` | none | delivered, retry (429), dead (400/403), uncertain (502, timeout, malformed 2xx); 403 "blocked by the user" = opt-out signal |
| line | configured-disabled | `POST api.line.me/v2/bot/message/push` | `X-Line-Retry-Key` = uuid5 of `message/<id>`, identical on every retry (409 = already accepted → delivered; LINE honours the key for 24 h) | delivered, retry, dead (401), uncertain |
| slack | configured-disabled | `POST hooks.slack.com/services/<redacted>` | none | delivered, retry (429, refused), dead (404 `no_service`), uncertain |
| email | configured-disabled | SMTP 587 STARTTLS, `Message-ID`, RFC 8058 `List-Unsubscribe-Post` | none — stable `Message-ID` is hygiene only, not deduplication | delivered (250), retry (4xx), dead (5xx), uncertain (timeout after DATA, no reply) |
| console | current | stdout | n/a | recorded as `console`, never `delivered` |

All values are synthetic (`example.invalid`, placeholder IDs); every credential position is
`<redacted>` and a test fails if a bot-token-shaped string or a non-`example.invalid` email domain
appears in the fixture.

## Gap list

1. **Opt-out is not implemented anywhere** (current server, client, PR #1). The fixture and the
   reference module define it (suppress before any provider call, no attempt counted, `*` scope,
   signal dedupe by external event ID, console exempt) but it is `proposed` only.
2. **No recipient model.** Today the only external recipient is the owner's Telegram chat from
   environment; there is no `recipient_ref`. Opt-out needs one; the consumer contract proposes an
   opaque `recipient_ref` (addresses stay inside the boundary).
3. **Retries can duplicate on non-idempotent providers.** Telegram, Slack and email accept no
   caller key that the provider deduplicates on, so an `uncertain` result (timeout/5xx) retried
   under at-least-once can send twice. In particular an SMTP retry after an uncertain DATA timeout can deliver twice:
   submission servers do not deduplicate on `Message-ID` (kept stable as hygiene; any receiver-side
   dedupe is best effort). Only LINE is keyed: `X-Line-Retry-Key` must be a UUID, so the reference
   derives uuid5 of `message/<id>` and every retry of one entry sends the same key; LINE honours
   it for 24 h, so a retry later than that can duplicate too. The current dispatcher sends no key.
4. **Dead outcomes are retried today.** The current dispatcher records any send exception as
   `failed` and retries up to 3 attempts; the reference proposes that a non-429 4xx stops retries.
5. **LINE Notify is discontinued** upstream; the retired config's `line` channel cannot be revived
   as-is. The fixture models the LINE Messaging API push endpoint instead.
6. **The registry `source_paths` are stale** (`notifications_store.py` and `operations` were
   removed from `solo-empire`); the registry should point at `platform-notifications-api`.
7. `contract.json` `current_implementation` already names `platform-notifications-api`, but the
   `interfaces[].source` paths still name the retired Go notifier. Not changed here (scope).
8. Inbound `/api/webhook/line` currently queues into `webhook_log`; nothing routes `unfollow`
   events to the communication boundary. The reference interprets every `unfollow` in a batch
   (recipient per event from `source.userId`). Every LINE opt-out is also keyed by the hashed userId
   and the send-time check uses that key, so an unfollow from a user not yet in the recipient model
   still blocks later sends to that userId once it is mapped. An `unfollow` without `webhookEventId`
   or `userId` is a per-event 400 error (never collapsed into a shared key); the valid opt-outs of
   the same batch are still returned for recording.

## Owner decision — PROPOSED, NOT ACCEPTED

The owner (bookchaowalit) is asked to accept or reject, as a single decision:

1. Adopt `contracts/consumer/communication-delivery.consumer.v1.json` as the caller contract for
   the communication boundary, with `current` rules frozen as compatibility surface and
   `proposed` rules (opt-out route, `recipient_ref`, provider idempotency keys, dead-stops-retry)
   to be implemented in the runtime that wins (current server or PR #1) before any cutover.
2. Opt-out policy: enforced by the boundary before any provider call; scopes per channel and `*`;
   signals = LINE `unfollow` via the signed `/api/webhook/line` route, Telegram 403 blocked,
   RFC 8058 one-click email unsubscribe, and owner global; console exempt; no transactional
   exemption until the owner names one.
3. Retry policy: `max_attempts = 3`, at-least-once, a provider key only where the provider
   deduplicates on it (LINE: `X-Line-Retry-Key` = uuid5 of `message/<id>`), telegram/slack/email
   documented as possibly duplicating after an uncertain outcome, non-429 4xx is terminal.
4. Update the registry `source_paths` for `communications` to the live server.

Until accepted: no status change, no runtime move, no consumer change, no cutover. This document
does not claim any production state.

## Verification

```bash
bash scripts/check.sh   # SourceDriftTests skip locally unless the variable below is set
PLATFORM_NOTIFICATIONS_API=/path/to/platform-notifications-api python3 -m unittest tests.test_delivery_gate -v
```

CI (`.github/workflows/check.yml`) checks out the public `bookchaowalit/platform-notifications-api`
pinned at `57dd097` into `_upstream/platform-notifications-api` and sets
`PLATFORM_NOTIFICATIONS_API` plus `REQUIRE_SERVER_PARITY=1`, so the five server-parity tests run
hosted; with that flag and no checkout they fail instead of skipping. The flag is not tied to the
generic `CI` variable, so other workflows that run `tests/` without the checkout (PR #1's
`shared-runtime.yml` pytest job) skip them. The tripwire reads the pinned SHA, so it fires when the
pin is moved, not when upstream `main` changes. Moving the pin is a deliberate change reviewed with the
fixture.
