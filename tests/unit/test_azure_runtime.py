"""Runtime selection by APP_ENV: offline imports nothing Azure; Azure wires Cosmos DB, the job queue in records,
AI Search, Blob Storage, the asynchronous e-mail and the Foundry adapter; web and worker containers share the
store (FAKE SDK clients, no network)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from pms import runtime as rt
from pms.config import ConfigError
from pms.models import Status

from ._pipeline_support import RUNS, cleanup_runs, letter_text
from ._support import REPO
from .test_azure_fakes import (
    ACS, CREDENTIALS, FakeAzureTestCase, FakeCosmosClient, FakeSearchService,
)

AZURE_ENV = {
    "APP_ENV": "azure", "LOCAL_TEST_USERS": "false", "WORKER_MODE": "separate", "AGENT_BACKEND": "offline",
    "COSMOS_ENDPOINT": "https://fake-cosmos.documents.azure.com:443/", "COSMOS_DATABASE": "pms",
    "AZURE_SEARCH_ENDPOINT": "https://fake-search.search.windows.net", "AZURE_SEARCH_INDEX": "pms-knowledge",
    "STORAGE_ACCOUNT_URL": "https://fakeacct.blob.core.windows.net/",
    "ACS_ENDPOINT": "https://fake-acs.communication.azure.com", "ACS_SENDER_ADDRESS": "DoNotReply@demo.example",
    "ALERT_RECIPIENTS": "demo@contoso.example", "AZURE_CLIENT_ID": "mi-client-id",
    "IDENTITY_ENDPOINT": "http://localhost:42356/msi/token",
}


def tearDownModule() -> None:
    cleanup_runs()


def index_docs() -> dict:
    return FakeSearchService.get(AZURE_ENV["AZURE_SEARCH_ENDPOINT"]).indexes["pms-knowledge"]["docs"]


class OfflineStaysOffline(unittest.TestCase):
    def test_nothing_azure_is_imported_when_app_env_is_local(self) -> None:
        data_dir = RUNS / "offline-imports"
        code = (
            "import sys\n"
            "from pms import runtime as rt\n"
            "from pms.pipeline import build_pipeline\n"
            "p = build_pipeline(rt.get_settings())\n"
            "text = open(sys.argv[1], encoding='utf-8').read()\n"
            "r = p.ingest_letter(filename='L01.txt', text=text, received_at='2026-11-16T08:15:00Z', ref='L01')\n"
            "p.run_worker_once()\n"
            "print(type(p.store).__name__, type(p.jobs).__name__, type(p.search).__name__, type(p.files).__name__,"
            " type(p.notifier).__name__, r.alerts[0]['status'])\n"
            "print(sorted(m for m in sys.modules if m.split('.')[0] in ('azure', 'requests', 'agent_framework')"
            " or m.startswith('pms.azure')))\n"
        )
        env = {k: v for k, v in os.environ.items() if k not in AZURE_ENV}
        env.update(PYTHONPATH=str(REPO / "src"), APP_ENV="local", DATA_DIR=str(data_dir))
        letter = next((REPO / "data" / "synthetic" / "letters").glob("L01_*.txt"))
        out = subprocess.run([sys.executable, "-c", code, str(letter)], capture_output=True, text=True, env=env,
                             cwd=REPO, timeout=300)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.strip().splitlines()
        self.assertEqual(lines[-2], "Store SqliteJobQueue KnowledgeIndex LocalFileStore OutboxNotifier SENT")
        self.assertEqual(lines[-1], "[]")


class AzureSelection(FakeAzureTestCase):
    def settings(self, **extra: str):
        with mock.patch.dict(os.environ, {**AZURE_ENV, **extra}, clear=False):
            return rt.get_settings()

    def build(self, **extra: str):
        from pms.pipeline import build_pipeline

        with mock.patch.dict(os.environ, {**AZURE_ENV, **extra}, clear=False):
            pipeline = build_pipeline(rt.get_settings())
        self.addCleanup(pipeline.notifier.background.close)
        return pipeline

    def test_azure_services_are_selected_by_configuration(self) -> None:
        from pms.agents.foundry import FoundryRuntime
        from pms.azure.blob_store import BlobFileStore
        from pms.azure.cosmos_store import CosmosJobQueue, CosmosStore
        from pms.azure.search import AzureKnowledgeSearch
        from pms.notify import AcsEmailNotifier

        pipeline = self.build(AGENT_BACKEND="foundry", FOUNDRY_PROJECT_ENDPOINT="https://fake.services.ai.azure.com/x")
        self.assertIsInstance(pipeline.store, CosmosStore)
        self.assertIsInstance(pipeline.jobs, CosmosJobQueue)
        self.assertIsInstance(pipeline.search, AzureKnowledgeSearch)
        self.assertIsInstance(pipeline.files, BlobFileStore)
        self.assertIsInstance(pipeline.notifier, AcsEmailNotifier)
        self.assertTrue(pipeline.notifier.asynchronous)
        self.assertIs(pipeline.notifier.files, pipeline.files)
        self.assertIsInstance(pipeline.runtime, FoundryRuntime)
        self.assertEqual(pipeline.sweep_interval_s, rt.AZURE_DEADLINE_SWEEP_S)
        self.assertEqual(FakeCosmosClient.created[0][0], AZURE_ENV["COSMOS_ENDPOINT"])
        self.assertEqual(CREDENTIALS.created, [("managed", {"client_id": "mi-client-id"})])
        self.assertEqual(rt.get_agent_registry(pipeline.settings).describe(),
                         "Cosmos DB records (type foundry_agent_versions)")

    def test_a_separate_worker_needs_the_shared_store(self) -> None:
        settings = self.settings(COSMOS_ENDPOINT="")
        with self.assertRaisesRegex(ConfigError, "COSMOS_ENDPOINT"):
            rt.get_store(settings)
        inprocess = self.settings(COSMOS_ENDPOINT="", WORKER_MODE="inprocess", DATA_DIR=str(RUNS / "inproc"))
        self.assertEqual(type(rt.get_store(inprocess)).__name__, "Store")

    def test_web_and_worker_containers_share_cases_jobs_and_alerts(self) -> None:
        web, worker = self.build(), self.build()
        result = web.ingest_letter(filename="L01.txt", text=letter_text("L01"), ref="L01")
        self.assertEqual([a["status"] for a in result.alerts], ["QUEUED"])
        self.assertTrue(web.notifier.background.drain(10))
        alert = worker.store.list_alerts(case_id=result.case_id)[0]
        self.assertEqual((alert.status, alert.channel), ("SENT", "acs-email"))
        self.assertLess(alert.detail["latency_s"], 60)
        self.assertEqual(len(ACS.sent), 1)
        self.assertEqual(worker.jobs.pending_count(), 1)
        t0 = time.monotonic()
        self.assertEqual(worker.run_worker_once()["jobs"], 1)
        self.assertLess(time.monotonic() - t0, 30)
        worker.notifier.background.drain(10)
        case = web.store.get_case(result.case_id)
        self.assertEqual((case.analysis_state, case.status), ("DONE", Status.TRIAGE_ERFORDERLICH))
        self.assertEqual({r.agent_id[:2] for r in web.store.list_agent_runs(result.case_id)},
                         {"A1", "A2", "A3", "A4", "A6"})
        self.assertEqual(len(ACS.sent), 1)  # no second e-mail from the worker
        self.assertTrue(web.verify_audit()["ok"])
        self.assertEqual(len(index_docs()), 44)
        self.assertEqual(web.case_view(result.case_id)["progress"]["latest_job"]["status"], "DONE")

    def test_the_azure_worker_sweeps_deadlines_once_a_minute(self) -> None:
        worker = self.build()
        calls = []
        worker.sweep_deadlines = lambda now=None: calls.append(now) or []  # type: ignore[method-assign]
        worker.run_worker_once(max_jobs=0)
        worker.run_worker_once(max_jobs=0)
        self.assertEqual(len(calls), 1)
        worker._last_sweep -= rt.AZURE_DEADLINE_SWEEP_S
        worker.run_worker_once(max_jobs=0)
        self.assertEqual(len(calls), 2)

    def test_worker_start_prepares_the_search_index_in_the_background(self) -> None:
        worker = self.build()
        thread = rt.start_azure_setup(worker.settings, worker)
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(index_docs()), 44)
        from pms.config import local_test_settings

        self.assertIsNone(rt.start_azure_setup(local_test_settings(RUNS / "x"), worker))

    def run_cli(self, *argv: str, **extra: str) -> tuple[int, str]:
        from pms.cli import main

        buffer = io.StringIO()
        with mock.patch.dict(os.environ, {**AZURE_ENV, **extra}, clear=False), contextlib.redirect_stdout(buffer):
            code = main(list(argv))
        return code, buffer.getvalue()

    def test_cli_seed_index_only_and_verify_audit_in_azure(self) -> None:
        code, out = self.run_cli("seed", "--index-only")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["search_index"]["uploaded"], 44)
        code, out = self.run_cli("seed", "--index-only")
        self.assertEqual(json.loads(out)["search_index"]["uploaded"], 0)
        code, out = self.run_cli("verify-audit")
        self.assertEqual(code, 0, out)
        self.assertIn("audit chain OK", out)

    def test_cli_worker_once_in_azure(self) -> None:
        before = {t.ident for t in threading.enumerate()}
        code, _out = self.run_cli("worker", "--once", "--host", "127.0.0.1", "--port", "0")
        self.assertEqual(code, 0)
        for thread in threading.enumerate():
            if thread.ident not in before and thread.name == "pms-azure-setup":
                thread.join(30)
        self.assertIn("pms-knowledge", FakeSearchService.get(AZURE_ENV["AZURE_SEARCH_ENDPOINT"]).indexes)


if __name__ == "__main__":
    unittest.main()
