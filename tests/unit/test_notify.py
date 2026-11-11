"""Notifications: local outbox (timestamped .eml/.json), ACS e-mail only to configured demo recipients, never an
authority; alert delivery is audited once per attempt and retried by the worker (ADR-006, D-18)."""
from __future__ import annotations

import json
import re
import unittest
from types import SimpleNamespace

from pms import audit as A
from pms import runtime as rt
from pms.config import Settings
from pms.governance import audit_completeness
from pms.interfaces import Delivery
from pms.notify import AcsEmailNotifier, OutboxNotifier, RecipientRefused, check_recipients

from ._pipeline_support import cleanup_runs, ingest, letter_text, new_pipeline


def tearDownModule() -> None:
    cleanup_runs()


class Outbox(unittest.TestCase):
    def test_critical_letter_writes_one_timestamped_outbox_message_without_letter_text(self) -> None:
        pipeline, clock = new_pipeline("outbox", real_files=True)
        result = ingest(pipeline, clock, "L01")
        self.assertEqual(len(result.alerts), 1)
        files = sorted(f.name for f in pipeline.files.list("outbox"))
        self.assertEqual(len(files), 2)
        self.assertTrue(all(re.match(r"^\d{8}T\d{9}Z_ALR-\d{5}_CASE-L01\.(eml|json)$", f) for f in files), files)
        entry = json.loads(pipeline.files.get("outbox", files[1]))
        self.assertEqual((entry["case_id"], entry["link"]), ("CASE-L01", "/cases/CASE-L01"))
        self.assertIn("CRIT-1.0", entry["reason"])
        eml = pipeline.files.get("outbox", files[0]).decode("utf-8")
        self.assertIn("CASE-L01", eml)
        for sentence in [s for s in letter_text("L01").split("\n") if len(s) > 40]:
            self.assertNotIn(sentence, eml)
        self.assertNotIn("P-DE-", eml)
        self.assertEqual(pipeline.outbox_entries()[0]["alert_id"], result.alerts[0]["alert_id"])
        records = pipeline.store.audit.records(event_type=A.ALERT_SENT)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["after"], {"status": "SENT", "channel": "outbox"})

    def test_non_critical_letter_sends_nothing(self) -> None:
        pipeline, clock = new_pipeline("quiet")
        result = ingest(pipeline, clock, "L06")
        self.assertEqual(result.alerts, [])
        self.assertEqual(pipeline.files.list("outbox"), [])


class Recipients(unittest.TestCase):
    def test_only_configured_demo_recipients_and_never_an_authority_domain(self) -> None:
        self.assertEqual(check_recipients(["demo@contoso.example"], ["demo@contoso.example"]), ["demo@contoso.example"])
        with self.assertRaises(RecipientRefused):
            check_recipients(["someone@contoso.example"], ["demo@contoso.example"])
        for address in ("vigilance@agency.gov.uk", "x@authority.europa.eu", "y@ministere.gouv.fr"):
            with self.subTest(address), self.assertRaises(RecipientRefused):
                check_recipients([address], [address])

    def test_acs_email_payload_goes_to_configured_recipients_only(self) -> None:
        sent = []

        class Poller:
            def __init__(self, payload):
                sent.append(payload)

            def result(self, timeout=None):
                return {"status": "Succeeded", "id": "msg-1"}

        client = SimpleNamespace(begin_send=Poller)
        notifier = AcsEmailNotifier(endpoint="https://acs.example", sender="DoNotReply@demo.example",
                                    recipients=["demo1@contoso.example", "demo2@contoso.example"],
                                    client_factory=lambda: client)
        pipeline, clock = new_pipeline("acs", notifier=notifier)
        result = ingest(pipeline, clock, "L02")
        self.assertEqual(result.alerts[0]["status"], "SENT")
        self.assertEqual(result.alerts[0]["channel"], "acs-email")
        payload = sent[0]
        self.assertEqual([r["address"] for r in payload["recipients"]["to"]],
                         ["demo1@contoso.example", "demo2@contoso.example"])
        self.assertEqual(payload["senderAddress"], "DoNotReply@demo.example")
        self.assertNotIn("P-GB-2208", payload["content"]["plainText"])
        with self.assertRaises(RecipientRefused):
            AcsEmailNotifier(endpoint="https://acs.example", sender="s@demo.example", recipients=[])

    def test_factory_selects_the_channel(self) -> None:
        azure = Settings(app_env="azure", local_test_users=False, acs_endpoint="https://acs.example",
                         acs_sender_address="DoNotReply@demo.example", alert_recipients=("demo@contoso.example",))
        self.assertIsInstance(rt.get_notifier(azure, rt.MemoryFileStore()), AcsEmailNotifier)
        self.assertIsInstance(rt.get_notifier(Settings(), rt.MemoryFileStore()), OutboxNotifier)
        no_acs = Settings(app_env="azure", local_test_users=False)
        self.assertIsInstance(rt.get_notifier(no_acs, rt.MemoryFileStore()), OutboxNotifier)


class Retry(unittest.TestCase):
    def test_failed_delivery_stays_visible_and_the_worker_retries(self) -> None:
        class Flaky:
            channel = "outbox"

            def __init__(self) -> None:
                self.calls = 0

            def send_alert(self, alert, case, *, link=None):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("mail relay unavailable")
                return Delivery(channel=self.channel, status="SENT", detail={"try": self.calls})

        pipeline, clock = new_pipeline("retry", notifier=Flaky())
        result = ingest(pipeline, clock, "L01")
        self.assertEqual(result.alerts[0]["status"], "FAILED")
        alert = pipeline.store.list_alerts(case_id=result.case_id)[0]
        self.assertEqual(alert.status, "FAILED")
        pipeline.run_worker_once(max_jobs=0)
        alert = pipeline.store.list_alerts(case_id=result.case_id)[0]
        self.assertEqual((alert.status, alert.detail["attempts"]), ("SENT", 2))
        self.assertEqual(len(pipeline.store.audit.records(event_type=A.ALERT_SENT)), 2)
        self.assertTrue(audit_completeness(pipeline.store)["checks"]["alert_deliveries"]["ok"])


if __name__ == "__main__":
    unittest.main()
