"""BOO-150: provider adapter fixtures, retry idempotency, opt-out and the consumer contract.

The fixture is checked against the reference policy code (``reference/communication_policy.py``),
``contract.json`` and the consumer contract, so a provider, rule or field that drifts in one place
fails here. ``SourceDriftTests`` also compares with a local checkout of the current server
(``platform-notifications-api``) when ``PLATFORM_NOTIFICATIONS_API`` points at one; CI skips it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures/gates/communication-delivery.json"
CONSUMER = ROOT / "contracts/consumer/communication-delivery.consumer.v1.json"


def _load_policy():
    spec = importlib.util.spec_from_file_location("communication_policy", ROOT / "reference/communication_policy.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


policy = _load_policy()
fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
consumer = json.loads(CONSUMER.read_text(encoding="utf-8"))
contract = json.loads((ROOT / "contract.json").read_text(encoding="utf-8"))


def _strings(value, path="$"):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


class CoverageTests(unittest.TestCase):
    def test_every_migration_gate_has_a_fixture_section(self) -> None:
        gates = fixture["gates"]
        self.assertEqual(set(contract["migration_gates"]), set(gates))
        for section in gates.values():
            self.assertIn(section, fixture)

    def test_every_configured_channel_has_a_provider_fixture(self) -> None:
        channels = next(i for i in contract["interfaces"] if i["id"] == "communication.config.channels")
        named = set(re.findall(r"\b(line|slack|email|console|telegram|sms|push)\b", channels["name"]))
        self.assertTrue(named)
        self.assertLessEqual(named, set(fixture["providers"]))

    def test_provider_sets_agree(self) -> None:
        self.assertEqual(set(fixture["providers"]), set(policy.PROVIDERS))
        self.assertEqual(set(consumer["providers"]), set(policy.PROVIDERS))

    def test_fixture_is_synthetic_and_not_wired(self) -> None:
        self.assertTrue(fixture["synthetic_only"])
        self.assertFalse(fixture["runtime_wired"])
        self.assertIn("NOT WIRED TO RUNTIME", policy.__doc__)
        for path in (ROOT / "scripts").glob("*.py"):
            self.assertNotIn("communication_policy", path.read_text(encoding="utf-8"), path)


class ProviderAdapterTests(unittest.TestCase):
    def test_build_request_matches_fixture(self) -> None:
        for name, spec in fixture["providers"].items():
            with self.subTest(provider=name):
                self.assertEqual(policy.build_request(name, spec["message"]), spec["expected_request"])

    def test_classify_matches_every_response_case(self) -> None:
        for name, spec in fixture["providers"].items():
            self.assertTrue(spec["responses"], name)
            for case in spec["responses"]:
                with self.subTest(provider=name, case=case["case"]):
                    result = policy.classify(name, case["response"])
                    self.assertEqual(result["outcome"], case["outcome"])
                    self.assertEqual(result["opt_out"], case["opt_out"])
                    self.assertEqual(result.get("record_as"), case.get("record_as"))

    def test_external_providers_cover_success_failure_and_timeout(self) -> None:
        for name, spec in fixture["providers"].items():
            if name == "console":
                continue
            outcomes = {c["outcome"] for c in spec["responses"]}
            with self.subTest(provider=name):
                self.assertLessEqual({"delivered", "retry", "dead", "uncertain"}, outcomes)
                self.assertTrue(any(c["response"].get("transport_error") for c in spec["responses"]))

    def test_idempotency_headers_agree(self) -> None:
        from_fixture = {n: s["idempotency_key_header"] for n, s in fixture["providers"].items()
                        if s["idempotency_key_header"]}
        self.assertEqual(from_fixture, policy.IDEMPOTENT_PROVIDERS)
        self.assertEqual(consumer["retry"]["provider_idempotency_keys"], policy.IDEMPOTENT_PROVIDERS)
        for name, header in from_fixture.items():
            self.assertIn(header, fixture["providers"][name]["expected_request"]["headers"])

    def test_no_credentials_in_requests_or_fixture(self) -> None:
        for name, spec in fixture["providers"].items():
            request = policy.build_request(name, spec["message"])
            for path, text in _strings(request):
                lowered = path.lower()
                if any(word in lowered for word in ("authorization", "password", "username")):
                    self.assertIn(policy.REDACTED, text, path)
            if request.get("url", "").startswith(("https://api.telegram.org/bot", "https://hooks.slack.com/")):
                self.assertIn(policy.REDACTED, request["url"])
        for path, text in _strings(fixture):
            for domain in re.findall(r"@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", text):
                self.assertTrue(domain.endswith("example.invalid"), f"{path}: {domain}")
            self.assertNotRegex(text, r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", path)  # a Telegram bot token


class RetryIdempotencyTests(unittest.TestCase):
    rules = fixture["retry_idempotency"]

    def test_max_attempts_agree(self) -> None:
        self.assertEqual(self.rules["max_attempts"], policy.MAX_ATTEMPTS)
        self.assertEqual(consumer["retry"]["max_attempts"], policy.MAX_ATTEMPTS)
        pending = next(o for o in consumer["operations"] if o["id"] == "list-pending")
        self.assertIn(f"max_attempts={policy.MAX_ATTEMPTS}", pending["retry_selector"])

    def test_golden_dedupe_keys(self) -> None:
        for item in self.rules["golden_dedupe_keys"]:
            self.assertEqual(policy.dedupe_key(*item["parts"]), item["key"])
            self.assertLessEqual(len(item["key"]), 64)

    def test_queue_is_idempotent_by_dedupe_key(self) -> None:
        box = policy.Outbox()
        key = self.rules["golden_dedupe_keys"][0]["key"]
        first = box.queue({"title": "t", "message": "m", "notification_type": "overdue", "dedupe_key": key})
        again = box.queue({"title": "changed", "message": "m2", "notification_type": "overdue", "dedupe_key": key})
        self.assertEqual(first[0], "created")
        self.assertEqual(again, ("existing", first[1]))
        self.assertEqual(len(box.rows), 1)
        with self.assertRaises(policy.PolicyError) as short:
            box.queue({"title": "t", "message": "m", "notification_type": "x", "dedupe_key": "short"})
        self.assertEqual(short.exception.reason, "invalid_dedupe_key")

    def test_console_is_never_a_delivery(self) -> None:
        box = policy.Outbox()
        _, row = box.queue({"title": "t", "message": "m", "notification_type": "x"})
        for channel in (None, "console"):
            with self.assertRaises(policy.PolicyError) as error:
                box.record_delivery(row["id"], "delivered", channel=channel)
            self.assertEqual((error.exception.status, error.exception.reason), (422, "console_is_not_delivery"))
        self.assertEqual(box.record_delivery(row["id"], "console")["delivery_attempts"], 0)
        record = next(o for o in consumer["operations"] if o["id"] == "record-delivery")
        self.assertEqual(tuple(record["outcome"]), policy.DELIVERY_OUTCOMES)
        self.assertIn("console_is_not_delivery", [e["reason"] for e in record["errors"]])

    def test_scenarios(self) -> None:
        for scenario in self.rules["scenarios"]:
            with self.subTest(case=scenario["case"]):
                box = policy.Outbox()
                _, row = box.queue({"title": "t", "message": "m", "notification_type": "x",
                                    "recipient_ref": "rcpt-9", "dedupe_key": policy.dedupe_key(scenario["case"])})
                got = [box.attempt(row["id"], scenario["provider"], r) for r in scenario["responses"]]
                self.assertEqual(got, scenario["expect"])
                final = box.rows[row["id"]]
                self.assertEqual(final["delivery_attempts"], scenario["final_attempts"])
                self.assertEqual(final["delivered_at"] is not None, scenario["delivered"])
                self.assertEqual(final["id"] in {p["id"] for p in box.pending()},
                                 not scenario["delivered"] and final["delivery_attempts"] < policy.MAX_ATTEMPTS)

    def test_provider_key_is_stable_across_retries(self) -> None:
        box = policy.Outbox()
        _, row = box.queue({"title": "t", "message": "m", "notification_type": "x", "recipient_ref": "r"})
        box.attempt(row["id"], "line", {"status": 500})
        box.attempt(row["id"], "line", {"status": 409})
        keys = {s["idempotency_key"] for s in box.sends}
        self.assertEqual(keys, {policy.provider_idempotency_key(row["id"])})

    def test_every_rule_has_a_status(self) -> None:
        for rule in self.rules["rules"]:
            self.assertIn(rule["status"], ("current", "proposed"), rule["id"])


class OptOutTests(unittest.TestCase):
    rules = fixture["opt_out"]

    def test_opt_out_is_marked_proposed_everywhere(self) -> None:
        self.assertEqual(self.rules["status"], "proposed")
        self.assertEqual(consumer["opt_out"]["status"], "proposed")
        op = next(o for o in consumer["operations"] if o["id"] == "record-opt-out")
        self.assertEqual(op["status"], "proposed")

    def test_inbound_signals(self) -> None:
        sources = set()
        for case in self.rules["inbound_signals"]:
            with self.subTest(case=case["case"]):
                result = policy.opt_out_from_inbound(case["event"])
                if case["expect"] is None:
                    self.assertIsNone(result)
                    continue
                sources.add(result.source)
                self.assertEqual({"channel": result.channel, "source": result.source, "event_id": result.event_id},
                                 case["expect"])
        self.assertLessEqual(sources, set(consumer["opt_out"]["signals"]))

    def test_replayed_signal_changes_nothing(self) -> None:
        registry = policy.OptOutRegistry()
        event = self.rules["inbound_signals"][0]["event"]
        self.assertEqual(registry.record(policy.opt_out_from_inbound(event)), "created")
        self.assertEqual(registry.record(policy.opt_out_from_inbound(event)), "existing")
        self.assertEqual(len(registry.entries), 1)

    def test_enforcement_scenarios(self) -> None:
        responses = {"line": {"status": 200, "body": {}}, "email": {"smtp_code": 250}, "console": {},
                     "telegram": {"status": 200, "body": {"ok": True, "result": {"message_id": 1}}}}
        for scenario in self.rules["enforcement_scenarios"]:
            with self.subTest(case=scenario["case"]):
                box = policy.Outbox()
                for recipient, channel in scenario["opt_outs"]:
                    box.opt_outs.record(policy.OptOut(recipient, channel, "owner.global", f"e:{recipient}:{channel}"))
                _, row = box.queue({"title": "t", "message": "m", "notification_type": "x",
                                    "recipient_ref": scenario["recipient_ref"]})
                outcome = box.attempt(row["id"], scenario["provider"], responses[scenario["provider"]])
                self.assertEqual(outcome, scenario["expect"])
                self.assertEqual(len(box.sends), scenario["provider_calls"])
                if outcome == "suppressed":
                    final = box.rows[row["id"]]
                    self.assertEqual((final["delivery_attempts"], final["delivery_error"]), (0, "opted_out"))
                    self.assertEqual(box.pending(), [])

    def test_provider_block_records_opt_out_and_suppresses_next_send(self) -> None:
        box = policy.Outbox()
        blocked = next(c for c in fixture["providers"]["telegram"]["responses"] if c["case"] == "blocked_by_user")
        _, first = box.queue({"title": "a", "message": "m", "notification_type": "x", "recipient_ref": "rcpt-7"})
        _, second = box.queue({"title": "b", "message": "m", "notification_type": "x", "recipient_ref": "rcpt-7"})
        self.assertEqual(box.attempt(first["id"], "telegram", blocked["response"]), "dead")
        self.assertTrue(box.opt_outs.blocks("rcpt-7", "telegram"))
        self.assertEqual(box.attempt(second["id"], "telegram", {"status": 200}), "suppressed")
        self.assertEqual(len(box.sends), 1)


class ConsumerContractTests(unittest.TestCase):
    def test_shape(self) -> None:
        self.assertEqual(consumer["contract"], "communication-delivery.consumer.v1")
        self.assertEqual(consumer["owner_acceptance"], "pending")
        self.assertEqual(consumer["upstream_contract"], fixture["contract"])
        self.assertEqual(fixture["consumer_contract"], str(CONSUMER.relative_to(ROOT)))
        ids = [o["id"] for o in consumer["operations"]]
        self.assertEqual(len(ids), len(set(ids)))
        for op in consumer["operations"]:
            self.assertIn(op["status"], ("current", "proposed"), op["id"])
            self.assertRegex(op["path"], r"^/v1/")

    def test_inbound_line_route_matches_upstream_contract_row(self) -> None:
        route = consumer["inbound_webhooks"][0]
        self.assertEqual((route["method"], route["path"], route["auth"]), ("POST", "/api/webhook/line", "x-line-signature"))

    def test_gate_doc_links_artifacts(self) -> None:
        doc = (ROOT / "docs/gates/BOO-150-communication-delivery-gate.md").read_text(encoding="utf-8")
        for needle in (fixture["next_gate"], fixture["contract"], "Owner decision — PROPOSED, NOT ACCEPTED",
                       str(FIXTURE.relative_to(ROOT)), str(CONSUMER.relative_to(ROOT)), fixture["reference_module"]):
            self.assertIn(needle, doc)
        surface = (ROOT / "docs/CONTRACT-SURFACE.md").read_text(encoding="utf-8")
        self.assertIn("gates/BOO-150-communication-delivery-gate.md", surface)


SOURCE = os.environ.get("PLATFORM_NOTIFICATIONS_API")


@unittest.skipUnless(SOURCE and Path(SOURCE, "src/solo_notifications/app.py").is_file(),
                     "set PLATFORM_NOTIFICATIONS_API to a platform-notifications-api checkout (local only)")
class SourceDriftTests(unittest.TestCase):
    """Compares current-status rules with the server that implements them today."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = Path(SOURCE, "src/solo_notifications/app.py").read_text(encoding="utf-8")
        cls.dispatch = Path(SOURCE, "src/solo_notifications/dispatch.py").read_text(encoding="utf-8")

    def test_max_attempts(self) -> None:
        self.assertEqual(int(re.search(r"^MAX_ATTEMPTS = (\d+)", self.dispatch, re.M).group(1)), policy.MAX_ATTEMPTS)

    def test_dedupe_construction(self) -> None:
        body = re.search(r"def dedupe_key\(\*parts: Any\) -> str:\n(?:    \"\"\".*\"\"\"\n)?    (return .+)\n",
                         self.dispatch).group(1)
        namespace: dict = {}
        exec("import hashlib, json\nfrom typing import Any\ndef dedupe_key(*parts):\n    " + body, namespace)
        for item in fixture["retry_idempotency"]["golden_dedupe_keys"]:
            self.assertEqual(namespace["dedupe_key"](*item["parts"]), item["key"])

    def test_delivery_outcomes_and_console_rule(self) -> None:
        literal = re.search(r"class Delivery\(BaseModel\):.*?outcome: Literal\[([^\]]+)\]", self.app, re.S).group(1)
        self.assertEqual(tuple(re.findall(r'"(\w+)"', literal)), policy.DELIVERY_OUTCOMES)
        self.assertIn('"console_is_not_delivery"', self.app)

    def test_queue_fields_and_limits(self) -> None:
        model = re.search(r"class NotificationCreate\(BaseModel\):(.*?)\nclass ", self.app, re.S).group(1)
        fields = set(re.findall(r"^    (\w+):", model, re.M)) - {"model_config", "legacy_id", "sent_at"}
        queue = next(o for o in consumer["operations"] if o["id"] == "queue")
        self.assertEqual(fields, set(queue["request_required"]) | set(queue["request_optional"]))
        self.assertIn("dedupe_key: str | None = Field(default=None, min_length=8, max_length=200)", model)
        self.assertIn("ON CONFLICT (workspace_id, dedupe_key)", self.app)

    def test_telegram_is_the_current_external_provider(self) -> None:
        self.assertIn("https://api.telegram.org/bot", self.dispatch)
        self.assertEqual(fixture["providers"]["telegram"]["status"], "current")
        self.assertNotIn("opt_out", self.app.replace("-", "_").lower())  # flip opt-out to current when this fails


if __name__ == "__main__":
    unittest.main()
