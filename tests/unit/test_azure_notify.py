"""Azure e-mail path: the upload never waits for the e-mail service, the send result is audited, the worker
retries pending/failed alerts and two processes never send the same alert (FAKE ACS + Cosmos DB, no network)."""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from pms import audit as A
from pms import runtime as rt
from pms.config import Settings, local_test_settings
from pms.governance import audit_completeness
from pms.models import FixedClock
from pms.notify import AcsEmailNotifier, BackgroundAlertSender, RecipientRefused
from pms.pipeline import ALERT_WORKER_GRACE_S, Pipeline
from pms.telemetry import Telemetry

from ._pipeline_support import cleanup_runs, ingest, letter_text, work_dir
from ._support import RECEIVED_AT
from .test_azure_fakes import ACS, CREDENTIALS, FakeAzureTestCase, FakeCosmosAccount, cosmos_account_for

RECIPIENTS = ("demo1@contoso.example", "demo2@contoso.example")


def tearDownModule() -> None:
    cleanup_runs()


class AzureEmail(FakeAzureTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.account = FakeCosmosAccount()
        self.pipelines: list[Pipeline] = []

    def tearDown(self) -> None:
        if ACS.gate is not None:
            ACS.gate.set()
        for p in self.pipelines:
            background = getattr(p.notifier, "background", None)
            if background is not None:
                background.drain(5)
                background.close()

    def process(self, name: str, *, at: str = RECEIVED_AT["L01"], background: object | None = "default",
                files: object | None = None) -> tuple[Pipeline, FixedClock]:
        """One container (web or worker): own store object, clock, notifier and sender thread."""
        from pms.azure.cosmos_store import CosmosStore

        clock = FixedClock(at)
        settings = local_test_settings(work_dir(name), app_env="azure", local_test_users=False,
                                       worker_mode="separate", alert_recipients=RECIPIENTS)
        store = CosmosStore(cosmos_account_for(self.account), clock=clock)
        notifier = AcsEmailNotifier(endpoint="https://fake-acs.communication.azure.com",
                                    sender="DoNotReply@demo.example", recipients=RECIPIENTS, client_id="mi-client",
                                    timeout_s=5, files=files or rt.MemoryFileStore(),
                                    background=BackgroundAlertSender() if background == "default" else background)
        pipeline = Pipeline(settings, store=store, clock=clock, notifier=notifier, files=rt.MemoryFileStore(),
                            telemetry=Telemetry(trace_path=None), worker_id=f"{name}-1")
        self.pipelines.append(pipeline)
        return pipeline, clock

    def sent_records(self, pipeline: Pipeline) -> list[dict]:
        return pipeline.store.audit.records(event_type=A.ALERT_SENT)

    def test_upload_returns_before_the_email_is_sent_and_the_result_is_audited(self) -> None:
        import threading

        ACS.gate = threading.Event()
        web, clock = self.process("web")
        t0 = time.perf_counter()
        result = ingest(web, clock, "L01")
        upload_s = time.perf_counter() - t0
        self.assertTrue(ACS.entered.wait(5))  # the sender thread is inside the (blocked) e-mail call
        self.assertLess(upload_s, 3.0)
        self.assertEqual([(a["status"], a["channel"]) for a in result.alerts], [("QUEUED", "acs-email")])
        alert = web.store.list_alerts(case_id=result.case_id)[0]
        self.assertEqual(alert.status, "PENDING")  # recorded immediately, delivery still running
        self.assertTrue(web.store.list_tasks(case_id=result.case_id, kind="TRIAGE"))
        self.assertEqual(len(web.store.audit.records(event_type=A.ALERT_REQUESTED)), 1)
        self.assertEqual(self.sent_records(web), [])
        ACS.gate.set()
        self.assertTrue(web.notifier.background.drain(5))
        alert = web.store.list_alerts(case_id=result.case_id)[0]
        self.assertEqual((alert.status, alert.channel, alert.detail["acs_status"]), ("SENT", "acs-email", "Succeeded"))
        records = self.sent_records(web)
        self.assertEqual([r["after"] for r in records], [{"status": "SENT", "channel": "acs-email"}])
        self.assertEqual([r["address"] for r in ACS.sent[0]["recipients"]["to"]], list(RECIPIENTS))
        self.assertNotIn("P-DE-", ACS.sent[0]["content"]["plainText"])
        latency = web.telemetry.events("ALERT_SENT")[0]["pms_latency_s"]
        self.assertLess(latency, 60)
        self.assertGreater(latency, 0)
        entries = web.notifier.entries()
        self.assertEqual([(e["alert_id"], e["delivery_status"]) for e in entries], [(alert.alert_id, "SENT")])
        copies = [f.name for f in web.notifier.files.list("outbox")]
        self.assertEqual(len(copies), 2)  # .eml and .json
        self.assertEqual(alert.detail["outbox_entry"], [n for n in copies if n.endswith(".json")][0])
        for sentence in [s for s in letter_text("L01").split("\n") if len(s) > 40]:
            self.assertNotIn(sentence, ACS.sent[0]["content"]["plainText"])

    def test_failed_send_is_audited_and_the_worker_retries_after_the_pause(self) -> None:
        ACS.mode = "raise"
        web, clock = self.process("web")
        result = ingest(web, clock, "L02")
        web.notifier.background.drain(5)
        alert = web.store.list_alerts(case_id=result.case_id)[0]
        self.assertEqual((alert.status, alert.detail["attempts"]), ("FAILED", 1))
        self.assertIn("Service unavailable", alert.detail["error"])
        self.assertEqual([r["after"]["status"] for r in self.sent_records(web)], ["FAILED"])
        ACS.mode = "ok"
        worker, worker_clock = self.process("worker", at=RECEIVED_AT["L02"])
        worker.run_worker_once(max_jobs=0)
        worker.notifier.background.drain(5)
        self.assertEqual(ACS.calls, 1)  # still inside the retry pause (5 s)
        worker_clock.set("2026-11-16T09:30:06Z")
        worker.run_worker_once(max_jobs=0)
        worker.notifier.background.drain(5)
        alert = worker.store.list_alerts(case_id=result.case_id)[0]
        self.assertEqual((alert.status, alert.detail["attempts"]), ("SENT", 2))
        self.assertEqual([r["after"]["status"] for r in self.sent_records(worker)], ["FAILED", "SENT"])
        self.assertTrue(audit_completeness(worker.store)["checks"]["alert_deliveries"]["ok"])

    def test_two_processes_never_send_the_same_alert_twice(self) -> None:
        import threading

        ACS.gate = threading.Event()
        web, clock = self.process("web")
        result = ingest(web, clock, "L01")
        self.assertTrue(ACS.entered.wait(5))
        worker, worker_clock = self.process("worker")
        worker_clock.set("2026-11-16T08:15:30Z")  # older than the worker's grace period
        self.assertGreater(30, ALERT_WORKER_GRACE_S)
        worker.run_worker_once(max_jobs=0)
        worker.notifier.background.drain(5)
        self.assertEqual(ACS.calls, 1)  # the web sender holds the delivery claim
        ACS.gate.set()
        web.notifier.background.drain(5)
        self.assertEqual((len(ACS.sent), len(self.sent_records(web))), (1, 1))
        self.assertEqual(web.store.list_alerts(case_id=result.case_id)[0].status, "SENT")

    def test_an_alert_left_pending_by_a_stopped_web_process_is_delivered_by_the_worker(self) -> None:
        class StoppedSender(BackgroundAlertSender):
            def submit(self, task):  # the web container stopped before its sender thread ran
                self.submitted += 1

        web, clock = self.process("web", background=StoppedSender())
        result = ingest(web, clock, "L03")
        self.assertEqual(web.store.list_alerts(case_id=result.case_id)[0].status, "PENDING")
        worker, worker_clock = self.process("worker", at=RECEIVED_AT["L03"])
        worker.run_worker_once(max_jobs=0)
        worker.notifier.background.drain(5)
        self.assertEqual(ACS.calls, 0)  # within the grace period the web process may still deliver
        worker_clock.set("2026-11-16T10:05:11Z")
        worker.run_worker_once(max_jobs=0)
        worker.notifier.background.drain(5)
        self.assertEqual(worker.store.list_alerts(case_id=result.case_id)[0].status, "SENT")
        self.assertEqual(len(ACS.sent), 1)

    def test_acs_final_states(self) -> None:
        for mode, status, acs_status in (("failed", "FAILED", "Failed"), ("running", "SENT", "Running")):
            with self.subTest(mode):
                ACS.reset()
                ACS.mode = mode
                self.account = FakeCosmosAccount()
                web, clock = self.process(f"web-{mode}")
                result = ingest(web, clock, "L01")
                web.notifier.background.drain(5)
                alert = web.store.list_alerts(case_id=result.case_id)[0]
                self.assertEqual((alert.status, alert.detail["acs_status"]), (status, acs_status))

    def test_email_client_uses_the_managed_identity_and_recipients_stay_configured(self) -> None:
        notifier = AcsEmailNotifier(endpoint="https://fake-acs.communication.azure.com",
                                    sender="DoNotReply@demo.example", recipients=RECIPIENTS, client_id="mi-client")
        with mock.patch.dict(os.environ, {"IDENTITY_ENDPOINT": "http://localhost:42356/msi/token"}):
            client = notifier._client()
        self.assertIs(client, notifier._client())  # one client per process
        self.assertEqual(ACS.clients[0][0], "https://fake-acs.communication.azure.com")
        self.assertEqual(CREDENTIALS.created, [("managed", {"client_id": "mi-client"})])
        self.assertFalse(notifier.asynchronous)
        with self.assertRaises(RecipientRefused):
            AcsEmailNotifier(endpoint="https://x", sender="s@demo.example", recipients=["md@ec.europa.eu"])

    def test_runtime_wires_the_async_notifier_with_blob_outbox_copies(self) -> None:
        settings = Settings(app_env="azure", local_test_users=False, acs_endpoint="https://fake-acs.example",
                            acs_sender_address="DoNotReply@demo.example", alert_recipients=RECIPIENTS,
                            storage_account_url="https://fakeacct.blob.core.windows.net/", azure_client_id="mi")
        from pms.azure.blob_store import BlobFileStore

        files = rt.get_file_store(settings)
        notifier = rt.get_notifier(settings, files)
        self.assertIsInstance(files, BlobFileStore)
        self.assertIsInstance(notifier, AcsEmailNotifier)
        self.assertIsInstance(notifier.background, BackgroundAlertSender)
        self.assertIs(notifier.files, files)
        self.assertEqual(notifier.timeout_s, rt.ACS_SEND_TIMEOUT_S)
        self.assertFalse(notifier.background.describe()["running"])  # the thread starts on the first alert


if __name__ == "__main__":
    unittest.main()
