"""Reference delivery policy for the communication boundary (BOO-150).

NOT WIRED TO RUNTIME. This module is a pure-Python, stdlib-only reference that
encodes the rules a delivery runtime must follow so they can be tested against
fixtures in ``fixtures/gates/communication-delivery.json``. Nothing in this
repository (on ``main``) imports it at run time, it sends no messages and holds
no credentials. It mirrors the current implementation where one exists
(``bookchaowalit/platform-notifications-api``) and marks proposed rules
(opt-out) as such; see ``docs/gates/BOO-150-communication-delivery-gate.md``.

Rules encoded here:

* ``dedupe_key`` - the same construction as the current source, so a queue
  repeat returns the first entry (``existing``) instead of a second message.
* ``Outbox`` - queue idempotency, delivery recording (``console`` is never a
  delivery), attempt counting and the ``max_attempts`` = 3 retry ceiling.
* ``classify`` - maps a synthetic provider response to
  delivered / retry / dead / uncertain, and detects provider opt-out signals.
* ``build_request`` - the request each provider adapter would send, with the
  provider idempotency key where the provider supports one and credentials
  replaced by ``<redacted>``.
* opt-out - a delivery to an opted-out recipient/channel is suppressed before
  any provider call (PROPOSED: the current source has no opt-out store).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

MAX_ATTEMPTS = 3  # platform-notifications-api dispatch.MAX_ATTEMPTS
REDACTED = "<redacted>"
PROVIDERS = ("telegram", "line", "slack", "email", "console")
DELIVERY_OUTCOMES = ("delivered", "failed", "console")  # POST /v1/notifications/{id}/delivery
PROVIDER_OUTCOMES = ("delivered", "retry", "dead", "uncertain")
# Providers whose API accepts a caller idempotency key, so a retry after an
# uncertain outcome cannot produce a second message.
IDEMPOTENT_PROVIDERS = {"line": "X-Line-Retry-Key", "email": "Message-ID"}


class PolicyError(ValueError):
    """A request the API would reject (the reason mirrors its 4xx ``reason``)."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"{status}:{reason}")
        self.status = status
        self.reason = reason


def dedupe_key(*parts: Any) -> str:
    """Same construction as solo-empire notifications_store.dedupe_key / dispatch.dedupe_key."""
    return "k-" + hashlib.sha256(json.dumps(parts, default=str, ensure_ascii=False).encode()).hexdigest()[:60]


def provider_idempotency_key(notification_id: int, attempt_scope: str = "message") -> str:
    """Stable per message (not per attempt): every retry of one entry reuses it."""
    return f"{attempt_scope}/{int(notification_id)}"


# --- opt-out ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class OptOut:
    recipient_ref: str
    channel: str  # a provider name, or "*" for every channel
    source: str  # how it was learned (e.g. line.unfollow, telegram.blocked, email.list_unsubscribe)
    event_id: str  # external event id; the dedupe key for repeated signals


@dataclass
class OptOutRegistry:
    entries: dict[tuple[str, str], OptOut] = field(default_factory=dict)
    seen_events: set[str] = field(default_factory=set)

    def record(self, opt_out: OptOut) -> str:
        """``created`` or ``existing`` (a replayed external event id changes nothing)."""
        if opt_out.event_id in self.seen_events:
            return "existing"
        self.seen_events.add(opt_out.event_id)
        key = (opt_out.recipient_ref, opt_out.channel)
        if key in self.entries:
            return "existing"
        self.entries[key] = opt_out
        return "created"

    def blocks(self, recipient_ref: str | None, channel: str) -> bool:
        if not recipient_ref or channel == "console":  # console is a local observation, never a send
            return False
        return (recipient_ref, channel) in self.entries or (recipient_ref, "*") in self.entries


def opt_out_from_inbound(event: dict[str, Any]) -> OptOut | None:
    """An opt-out from a verified inbound provider event, or None if the event is not one.

    The caller must have verified the provider signature on the raw body first
    (contract: signed webhook routes); this function only interprets content.
    """
    provider = event.get("provider")
    if provider == "line":
        body = event.get("body") or {}
        for item in body.get("events") or []:
            if item.get("type") == "unfollow":
                return OptOut(event["recipient_ref"], "line", "line.unfollow",
                              f"line:{item.get('webhookEventId')}")
        return None
    if provider == "email" and event.get("kind") == "list_unsubscribe_post":
        if (event.get("body") or "").strip() == "List-Unsubscribe=One-Click":
            return OptOut(event["recipient_ref"], "email", "email.list_unsubscribe", f"email:{event['event_id']}")
        return None
    if provider == "owner" and event.get("kind") == "global_opt_out":
        return OptOut(event["recipient_ref"], "*", "owner.global", f"owner:{event['event_id']}")
    return None


# --- provider adapters (synthetic) -----------------------------------------------------------------

def build_request(provider: str, message: dict[str, Any]) -> dict[str, Any]:
    """The HTTP/SMTP request an adapter sends for ``message``; credentials are always redacted."""
    text = message["text"]
    if provider == "telegram":
        return {"method": "POST", "url": f"https://api.telegram.org/bot{REDACTED}/sendMessage",
                "headers": {"Content-Type": "application/json"},
                "json": {"chat_id": message["address"], "text": text[:4096], "disable_web_page_preview": True}}
    if provider == "line":
        return {"method": "POST", "url": "https://api.line.me/v2/bot/message/push",
                "headers": {"Authorization": f"Bearer {REDACTED}", "Content-Type": "application/json",
                            "X-Line-Retry-Key": message["idempotency_uuid"]},
                "json": {"to": message["address"], "messages": [{"type": "text", "text": text[:5000]}]}}
    if provider == "slack":
        return {"method": "POST", "url": f"https://hooks.slack.com/services/{REDACTED}",
                "headers": {"Content-Type": "application/json"}, "json": {"text": text}}
    if provider == "email":
        return {"method": "SMTP", "host": "smtp.example.invalid", "port": 587, "starttls": True,
                "auth": {"username": REDACTED, "password": REDACTED},
                "envelope": {"mail_from": "notifier@example.invalid", "rcpt_to": [message["address"]]},
                "headers": {"Message-ID": f"<{provider_idempotency_key(message['id'])}@notifier.example.invalid>",
                            "Subject": message.get("subject", "Notification"),
                            "List-Unsubscribe": "<https://notify.example.invalid/u/" + message["recipient_ref"] + ">",
                            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
                "body": text}
    if provider == "console":
        return {"method": "LOCAL", "stream": "stdout", "text": text}
    raise PolicyError(422, "unknown_provider")


def classify(provider: str, response: dict[str, Any]) -> dict[str, Any]:
    """``{"outcome": delivered|retry|dead|uncertain, "opt_out": bool, "error": str|None}``.

    retry = the provider did not accept the message (safe to resend);
    uncertain = it may have accepted it (timeout, 5xx, malformed 2xx);
    dead = it will never accept it (4xx other than rate limit).
    """
    if response.get("transport_error") == "timeout":
        return {"outcome": "uncertain", "opt_out": False, "error": f"{provider}_timeout"}
    if response.get("transport_error") == "connection_refused":
        return {"outcome": "retry", "opt_out": False, "error": f"{provider}_unavailable"}
    status = response.get("status")
    body = response.get("body")
    if provider == "email":
        code = int(response.get("smtp_code", 0))
        if code == 250:
            return {"outcome": "delivered", "opt_out": False, "error": None}
        if 400 <= code < 500:
            return {"outcome": "retry", "opt_out": False, "error": f"smtp_{code}"}
        return {"outcome": "dead", "opt_out": False, "error": f"smtp_{code}"}
    if provider == "console":
        return {"outcome": "delivered", "opt_out": False, "error": None, "record_as": "console"}
    if status == 429:
        return {"outcome": "retry", "opt_out": False, "error": f"{provider}_http_429"}
    if provider == "line" and status == 409:
        # Same X-Line-Retry-Key already accepted: the earlier attempt delivered it.
        return {"outcome": "delivered", "opt_out": False, "error": None}
    if status is not None and (status >= 500 or 300 <= status < 400):
        return {"outcome": "uncertain", "opt_out": False, "error": f"{provider}_http_{status}"}
    if status is not None and status >= 400:
        blocked = (provider == "telegram" and status == 403 and isinstance(body, dict)
                   and "blocked by the user" in str(body.get("description", "")))
        return {"outcome": "dead", "opt_out": blocked, "error": f"{provider}_http_{status}"}
    if status == 200:
        if provider == "telegram" and isinstance(body, dict) and body.get("ok") is True \
                and type((body.get("result") or {}).get("message_id")) is int:
            return {"outcome": "delivered", "opt_out": False, "error": None}
        if provider == "line" and body == {}:
            return {"outcome": "delivered", "opt_out": False, "error": None}
        if provider == "slack" and body == "ok":
            return {"outcome": "delivered", "opt_out": False, "error": None}
    return {"outcome": "uncertain", "opt_out": False, "error": f"{provider}_unexpected_response"}


# --- outbox (queue idempotency + retry) ------------------------------------------------------------

@dataclass
class Outbox:
    """In-memory model of the notifications log semantics (queue + delivery)."""

    max_attempts: int = MAX_ATTEMPTS
    opt_outs: OptOutRegistry = field(default_factory=OptOutRegistry)
    rows: dict[int, dict[str, Any]] = field(default_factory=dict)
    by_key: dict[str, int] = field(default_factory=dict)
    sends: list[dict[str, Any]] = field(default_factory=list)

    def queue(self, fields: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        key = fields.get("dedupe_key")
        if key is not None and not 8 <= len(key) <= 200:
            raise PolicyError(422, "invalid_dedupe_key")
        if key is not None and key in self.by_key:
            return "existing", dict(self.rows[self.by_key[key]])
        row = {"id": len(self.rows) + 1, "channel": fields.get("channel", "console"),
               "recipient_ref": fields.get("recipient_ref"), "dedupe_key": key, "delivered_at": None,
               "delivery_channel": None, "delivery_error": None, "delivery_attempts": 0, "suppressed": False,
               **{k: v for k, v in fields.items() if k not in ("dedupe_key",)}}
        self.rows[row["id"]] = row
        if key is not None:
            self.by_key[key] = row["id"]
        return "created", dict(row)

    def record_delivery(self, notification_id: int, outcome: str, *, channel: str | None = None,
                        error: str | None = None) -> dict[str, Any]:
        if outcome not in DELIVERY_OUTCOMES:
            raise PolicyError(422, "invalid_outcome")
        if outcome == "delivered" and (not channel or channel == "console"):
            raise PolicyError(422, "console_is_not_delivery")
        row = self.rows.get(notification_id)
        if row is None:
            raise PolicyError(404, "not_found")
        if outcome == "delivered":
            row.update(delivered_at="now", delivery_channel=channel, delivery_error=None,
                       delivery_attempts=row["delivery_attempts"] + 1)
        elif outcome == "failed":
            row.update(delivery_error=error or "delivery_failed", delivery_attempts=row["delivery_attempts"] + 1)
        else:
            row.update(channel="console", delivery_error=None)
        return dict(row)

    def pending(self) -> list[dict[str, Any]]:
        """Undelivered, not suppressed, attempts below the ceiling (``undelivered=true&max_attempts=3``)."""
        return [dict(r) for r in self.rows.values()
                if r["delivered_at"] is None and not r["suppressed"] and r["delivery_attempts"] < self.max_attempts]

    def attempt(self, notification_id: int, provider: str, response: dict[str, Any]) -> str:
        """One delivery attempt: opt-out gate, idempotent skip, provider call, outcome record.

        Returns ``suppressed``, ``skipped_delivered``, ``skipped_exhausted`` or the provider outcome.
        """
        row = self.rows[notification_id]
        if row["delivered_at"] is not None:
            return "skipped_delivered"  # a retry never resends a delivered entry
        if row["delivery_attempts"] >= self.max_attempts:
            return "skipped_exhausted"
        if self.opt_outs.blocks(row.get("recipient_ref"), provider):
            row.update(suppressed=True, delivery_error="opted_out")  # no attempt counted, no provider call
            return "suppressed"
        self.sends.append({"id": notification_id, "provider": provider,
                           "idempotency_key": provider_idempotency_key(notification_id)
                           if provider in IDEMPOTENT_PROVIDERS else None})
        result = classify(provider, response)
        if result["opt_out"] and row.get("recipient_ref"):
            self.opt_outs.record(OptOut(row["recipient_ref"], provider, f"{provider}.blocked",
                                        f"{provider}:blocked:{row['recipient_ref']}"))
        if result.get("record_as") == "console":
            self.record_delivery(notification_id, "console")
            return "console"
        if result["outcome"] == "delivered":
            self.record_delivery(notification_id, "delivered", channel=provider)
        else:
            self.record_delivery(notification_id, "failed", error=result["error"])
            if result["outcome"] == "dead":
                row["delivery_attempts"] = max(row["delivery_attempts"], self.max_attempts)
        return result["outcome"]
