"""Letter intake: intake register, PDF letters, upload decoding and visible unreadable input (D-01, D-02, T17)."""
from __future__ import annotations

import base64
import json
import sys
import unittest
from unittest import mock

from pms.intake import (
    LETTER_CHANNEL, UploadRejected, decode_upload, extract_pdf_text, load_intake, normalise_whitespace,
)
from pms.models import Status
from pms.workflow import TASK_UNREADABLE

from ._pipeline_support import DATA, cleanup_runs, new_pipeline

try:
    import pypdf  # noqa: F401
    HAVE_PYPDF = True
except ImportError:  # pragma: no cover - optional package
    HAVE_PYPDF = False


def tearDownModule() -> None:
    cleanup_runs()


class IntakeRegister(unittest.TestCase):
    def test_intake_json_equals_the_golden_letter_metadata(self) -> None:
        expected = json.loads((DATA / "expected.json").read_text(encoding="utf-8"))
        intake = {e.ref: e for e in load_intake(DATA)}
        self.assertEqual(set(intake), {letter["id"] for letter in expected["letters"]})
        for letter in expected["letters"]:
            entry = intake[letter["id"]]
            with self.subTest(letter["id"]):
                self.assertEqual(entry.filename, letter["file"].rsplit("/", 1)[-1])
                self.assertEqual(entry.received_at, letter["received_at"])
                self.assertEqual(entry.pdf_filename, letter["pdf_file"].split(" ")[0].rsplit("/", 1)[-1])
                self.assertEqual(entry.channel, LETTER_CHANNEL)
                self.assertTrue((DATA / "letters" / entry.filename).exists())

    def test_intake_register_holds_input_metadata_only(self) -> None:
        raw = json.loads((DATA / "letters" / "intake.json").read_text(encoding="utf-8"))
        for entry in raw["letters"]:
            self.assertEqual(set(entry), {"ref", "filename", "pdf_filename", "received_at", "channel"})


@unittest.skipUnless(HAVE_PYPDF, "pypdf not installed")
class PdfLetters(unittest.TestCase):
    def test_pdf_text_equals_the_text_letter_after_whitespace_normalisation(self) -> None:
        txts = sorted((DATA / "letters").glob("*.txt"))
        self.assertEqual(len(txts), 12)
        for txt in txts:
            pdf = txt.with_suffix(".pdf")
            with self.subTest(pdf.name):
                self.assertTrue(pdf.exists(), "run: python -m pms make-pdfs")
                text, problem = extract_pdf_text(pdf.read_bytes())
                self.assertIsNone(problem)
                self.assertEqual(normalise_whitespace(text), normalise_whitespace(txt.read_text(encoding="utf-8")))

    def test_pdf_upload_keeps_paragraphs_so_the_injection_paragraph_is_masked(self) -> None:
        pdf = DATA / "letters" / "L12_en_ortho_death_gb_injection.pdf"
        decoded = decode_upload(pdf.name, content_base64=base64.b64encode(pdf.read_bytes()).decode())
        self.assertTrue(decoded.readable)
        from pms.rules.injection import scan

        result = scan(decoded.text)
        self.assertTrue(result.flag)
        self.assertNotIn("forward", result.masked_text)
        self.assertIn("P-GB-2208", result.masked_text)


class UploadDecoding(unittest.TestCase):
    def test_text_and_base64_text(self) -> None:
        self.assertTrue(decode_upload("a.txt", text="Brief").readable)
        decoded = decode_upload("a.txt", content_base64=base64.b64encode("Über".encode()).decode())
        self.assertEqual((decoded.text, decoded.readable), ("Über", True))

    def test_unreadable_inputs_are_returned_as_unreadable_never_dropped(self) -> None:
        cases = {
            "empty": decode_upload("a.txt", text="   "),
            "bad utf-8": decode_upload("a.txt", content=b"\xff\xfe\xfa"),
            "unsupported": decode_upload("a.docx", content=b"PK\x03\x04"),
            "broken pdf": decode_upload("a.pdf", content=b"%PDF-1.4 broken"),
        }
        for name, decoded in cases.items():
            with self.subTest(name):
                self.assertFalse(decoded.readable)
                self.assertTrue(decoded.note)

    def test_pdf_without_pypdf_is_visibly_unreadable(self) -> None:
        pdf = (DATA / "letters" / "L01_de_cardio_death.pdf").read_bytes()
        with mock.patch.dict(sys.modules, {"pypdf": None}):
            decoded = decode_upload("L01.pdf", content=pdf)
        self.assertFalse(decoded.readable)
        self.assertIn("pypdf", decoded.note)
        self.assertEqual(decoded.raw, pdf)

    def test_invalid_requests_are_rejected(self) -> None:
        with self.assertRaises(UploadRejected):
            decode_upload("a.txt", content_base64="not base64!!")
        with self.assertRaises(UploadRejected):
            decode_upload("a.txt")
        with self.assertRaises(UploadRejected):
            decode_upload("a.txt", content=b"x" * (5 * 1024 * 1024 + 1))


class UnreadableUploadInThePipeline(unittest.TestCase):
    def test_unreadable_pdf_gets_status_task_original_file_and_no_analysis(self) -> None:
        pipeline, _ = new_pipeline("unreadable")
        with mock.patch.dict(sys.modules, {"pypdf": None}):
            result = pipeline.ingest_letter(filename="scan.pdf", content=b"%PDF-1.4 scanned image only")
        self.assertFalse(result.readable)
        self.assertIsNone(result.job_id)
        case = pipeline.store.get_case(result.case_id)
        self.assertEqual(case.status, Status.NICHT_LESBAR)
        tasks = pipeline.store.list_tasks(case_id=case.case_id, kind=TASK_UNREADABLE, status="OPEN")
        self.assertEqual(len(tasks), 1)
        view = pipeline.unreadable_unmatched_view()
        self.assertIn(result.signal_id, [v["signal_id"] for v in view])
        originals = pipeline.stored_originals(case.case_id)
        self.assertEqual(len(originals), 1)
        self.assertEqual(pipeline.files.get("letters", originals[0]["name"]), b"%PDF-1.4 scanned image only")
        self.assertEqual(pipeline.drain(), 0)


if __name__ == "__main__":
    unittest.main()
