"""Blob Storage file store over the Blob REST API with an Entra token, against a FAKE ``requests`` and Blob
service (no network): letters, reports and outbox copies, container creation on first use, retries, paging."""
from __future__ import annotations

import base64
import unittest

from pms import runtime as rt
from pms.azure.identity import AUTH_SCHEME

from ._pipeline_support import DATA, cleanup_runs, new_pipeline
from .test_azure_fakes import CREDENTIALS, FakeAzureTestCase, FakeBlobService, ManagedIdentityCredential

ACCOUNT = "https://fakeacct.blob.core.windows.net/"


def tearDownModule() -> None:
    cleanup_runs()


class BlobStore(FakeAzureTestCase):
    def setUp(self) -> None:
        super().setUp()
        from pms.azure.blob_store import BlobFileStore

        self.sleeps: list[float] = []
        self.files = BlobFileStore(ACCOUNT, credential_factory=lambda: ManagedIdentityCredential(client_id="mi"),
                                   sleep=self.sleeps.append)
        self.service = FakeBlobService.get(ACCOUNT)

    def test_put_get_list_with_an_entra_token(self) -> None:
        stored = self.files.put("letters", "SIG-00001_brief 1.pdf", b"%PDF-1.4 demo", content_type="application/pdf")
        self.assertEqual(stored.uri, "https://fakeacct.blob.core.windows.net/letters/SIG-00001_brief%201.pdf")
        self.assertEqual((stored.area, stored.name, stored.size, stored.modified_utc),
                         ("letters", "SIG-00001_brief 1.pdf", 13, "2026-11-10T21:00:00Z"))
        self.assertEqual(self.files.get("letters", "SIG-00001_brief 1.pdf"), b"%PDF-1.4 demo")
        self.files.put("letters", "SIG-00002_b.txt", b"b")
        self.assertEqual([f.name for f in self.files.list("letters")], ["SIG-00001_brief 1.pdf", "SIG-00002_b.txt"])
        method, path, headers = self.service.requests[0]
        self.assertEqual((method, headers["x-ms-blob-type"], headers["Content-Type"]),
                         ("PUT", "BlockBlob", "application/pdf"))
        self.assertEqual(headers["Authorization"], f"{AUTH_SCHEME} fake-token-managed")
        self.assertTrue(headers["x-ms-version"] and headers["x-ms-date"].endswith("GMT"))
        self.assertEqual(CREDENTIALS.tokens, ["https://storage.azure.com/.default"])  # one cached token

    def test_outbox_container_is_created_on_first_use_and_missing_things_stay_visible(self) -> None:
        self.assertEqual(self.files.list("outbox"), [])
        self.files.put("outbox", "x.json", b"{}", content_type="application/json")
        self.assertIn("outbox", self.service.containers)
        self.assertEqual([f.name for f in self.files.list("outbox")], ["x.json"])
        with self.assertRaises(FileNotFoundError):
            self.files.get("reports", "missing.json")
        for area, name in (("letters", "a/b"), ("letters", ".hidden"), ("letters", ""), ("bad area", "x")):
            with self.subTest(area=area, name=name), self.assertRaises(ValueError):
                self.files.put(area, name, b"x")

    def test_transient_errors_are_retried_and_permission_errors_explained(self) -> None:
        from pms.azure.blob_store import BlobStoreError

        self.service.fail_next = [503, 500]
        self.files.put("reports", "r.json", b"{}")
        self.assertEqual(self.sleeps, [0.5, 1.0])
        self.service.fail_next = [403]
        with self.assertRaisesRegex(BlobStoreError, "403.*Storage Blob Data Contributor"):
            self.files.get("reports", "r.json")

    def test_listing_follows_the_continuation_marker(self) -> None:
        self.service.page_size = 2
        for i in range(5):
            self.files.put("reports", f"CASE-{i}.json", b"{}")
        self.assertEqual([f.name for f in self.files.list("reports")], [f"CASE-{i}.json" for i in range(5)])
        self.assertIn("Azure Blob Storage", self.files.describe())

    def test_the_pipeline_keeps_uploaded_originals_in_blob_storage(self) -> None:
        pipeline, clock = new_pipeline("blob")
        pipeline.files = self.files
        clock.set("2026-11-16T14:55:00Z")
        raw = next(DATA.glob("letters/L06_*.pdf")).read_bytes()
        result = pipeline.ingest_letter(filename="L06_brief.pdf", content_base64=base64.b64encode(raw).decode(),
                                        received_at="2026-11-16T14:55:00Z", ref="L06")
        originals = pipeline.stored_originals(result.case_id)
        self.assertEqual(len(originals), 1)
        self.assertTrue(originals[0]["uri"].startswith("https://fakeacct.blob.core.windows.net/letters/"))
        self.assertEqual(self.files.get("letters", originals[0]["name"]), raw)
        self.assertIsInstance(rt.get_file_store(pipeline.settings), rt.LocalFileStore)  # offline settings


if __name__ == "__main__":
    unittest.main()
