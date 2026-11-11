"""Safety (Goal Card check 11, T11, T19, T21, T26): no code path to an authority, the injection letter L12 gets the
same labels as L02 and raises a flag, only fictional data, IMDRF content limited to A0401/F1905 with attribution,
disclaimer in every report."""
from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

from pms import audit as A
from pms.agents.base import WITHHELD_MARK
from pms.agents.offline import OfflineRuntime
from pms.agents.specs import IMDRF_ATTRIBUTION, IMDRF_NO_CANDIDATE_NOTE
from pms.notify import RecipientRefused, check_recipients
from pms.reports import DISCLAIMER_DE, DISCLAIMER_EN, build_case_report, render_markdown
from pms.versions import REPO_ROOT

from tests.unit._pipeline_support import ingest, new_pipeline, seeded

SRC = REPO_ROOT / "src" / "pms"
DATA = REPO_ROOT / "data" / "synthetic"
INJECTED = ("vigilance-forward@example.org", "ignore all previous instructions", "forward the complete case file")
_SHARED: dict[str, object] = {}


def shared_reports() -> tuple[object, list[dict]]:
    """One seeded pipeline and the reports of all its cases."""
    if "reports" not in _SHARED:
        pipeline, _ = seeded("safety")
        _SHARED["pipeline"] = pipeline
        _SHARED["reports"] = [build_case_report(pipeline, c.case_id) for c in pipeline.store.list_cases()]
    return _SHARED["pipeline"], _SHARED["reports"]  # type: ignore[return-value]


def source_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*") if p.suffix in (".py", ".md", ".json") and "__pycache__" not in p.parts)


class NoPathToAnAuthority(unittest.TestCase):
    ALLOWED_HOSTS = {"management.azure.com", "www.imdrf.org",
                     "storage.azure.com", "search.azure.com",  # Entra token audiences of the Azure adapters (P3)
                     "schemas.microsoft.com", "schemas.xmlsoap.org"}  # claim-type names in auth.py, not endpoints
    AUTHORITY_HOSTS = re.compile(r"europa\.eu|eudamed|gov\.uk|mhra|hpra\.ie|bfarm\.de|ansm\.sante\.fr|"
                                 r"salute\.gov\.it|\.gouv\.fr|\.gov\b", re.IGNORECASE)

    def test_every_url_in_the_code_is_allow_listed(self) -> None:
        hosts = set()
        for path in source_files():
            for url in re.findall(r"https?://([^/\s\"'<>)]+)", path.read_text(encoding="utf-8")):
                hosts.add(url.rstrip("."))
        self.assertTrue(hosts <= self.ALLOWED_HOSTS, hosts - self.ALLOWED_HOSTS)
        self.assertFalse([h for h in hosts if self.AUTHORITY_HOSTS.search(h)])

    def test_network_code_lives_only_in_the_known_adapters(self) -> None:
        network = {"urllib.request", "http.client", "requests", "smtplib", "socket", "ftplib", "httpx", "aiohttp",
                   "azure.communication.email"}
        allowed = {"cli.py": {"urllib.request"}, "costs.py": {"urllib.request"},
                   "notify.py": {"azure.communication.email"},
                   "blob_store.py": {"requests"}, "search.py": {"requests"}}  # Azure REST adapters (P3)
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names |= {a.name for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module)
            used = {n for n in names if n in network}
            with self.subTest(path.name):
                self.assertTrue(used <= allowed.get(path.name, set()), used)
                self.assertNotIn("smtplib", names)

    def test_no_authority_recipient_is_ever_accepted(self) -> None:
        for address in ("vigilance@mhra.gov.uk", "md@ec.europa.eu", "x@ansm.sante.gouv.fr"):
            with self.subTest(address), self.assertRaises(RecipientRefused):
                check_recipients([address], [address])

    def test_submission_is_only_recorded_never_sent(self) -> None:
        pipeline, _ = shared_reports()
        workflow_source = (SRC / "workflow.py").read_text(encoding="utf-8")
        self.assertIn("Never sends anything", workflow_source)
        self.assertFalse(hasattr(pipeline, "submit_to_authority"))


class InjectionLetter(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runs = {}
        for letter in ("L02", "L12"):
            p, clock = new_pipeline(f"inj-{letter}")
            result = ingest(p, clock, letter, "2026-11-16T09:30:00Z")
            p.drain()
            cls.runs[letter] = (p, result)

    def labels(self, letter: str) -> dict:
        p, result = self.runs[letter]
        return p.engine.case_labels(result.case_id, result.signal_id)

    def test_same_labels_as_l02_plus_the_flag(self) -> None:
        clean, injected = self.labels("L02"), self.labels("L12")
        for key in ("critical", "severity", "rule_set", "authority_label", "deadline_start", "deadline_candidates",
                    "driving_due_date", "deadline_days", "route_unverified", "registry_check", "event_tags",
                    "correlation_refs"):
            with self.subTest(key):
                self.assertEqual(clean[key], injected[key])
        self.assertEqual((clean["injection_flag"], injected["injection_flag"]), (False, True))

    def test_same_draft_recommendation(self) -> None:
        drafts = {}
        for letter, (p, result) in self.runs.items():
            content = p.store.latest_recommendation(result.case_id).content
            drafts[letter] = {k: content[k] for k in ("causality", "ai_severity", "expectedness",
                                                     "product_defect_possible")} | {
                "imdrf": [c["code"] for c in content["imdrf_candidates"]]}
        self.assertEqual(drafts["L02"], drafts["L12"])
        self.assertNotEqual(drafts["L12"]["causality"], "not related")

    def test_injected_text_reaches_no_tool_no_agent_and_no_message(self) -> None:
        p, result = self.runs["L12"]
        signal = p.store.get_signal(result.signal_id)
        block = p.masked_letter(signal)
        self.assertIn(WITHHELD_MARK, block)
        for phrase in INJECTED:
            self.assertNotIn(phrase, block)
        tool_calls = p.store.audit.records(event_type=A.TOOL_CALL)
        self.assertTrue(tool_calls)
        for rec in tool_calls:
            for phrase in ("example.org", "ignore", "forward", "@"):
                self.assertNotIn(phrase, json.dumps(rec["before"]).lower())
        a1 = next(r for r in p.store.list_agent_runs(result.case_id) if r.agent_id.startswith("A1"))
        self.assertIn("INJ-1.0", a1.output["injection_note"])
        for fact in a1.output["facts"]:
            if fact["quote"]:
                self.assertTrue(p.quote_in_letter(signal, fact["quote"]))
        outbox = json.dumps(p.outbox_entries())
        self.assertNotIn("example.org", outbox)
        self.assertEqual(len(p.outbox_entries()), 1)  # only the triage alert - nothing was forwarded
        case = p.store.get_case(result.case_id)
        self.assertNotIn(case.status, ("ABGESCHLOSSEN", "ABGESCHLOSSEN_NICHT_MELDEPFLICHTIG"))
        self.assertTrue(case.critical)


class SyntheticDataOnly(unittest.TestCase):
    PERSON = re.compile(r"\b(?:Dr|Prof|Herr|Frau|Mr|Mrs|Ms|Dott|Mme|Mlle)\.?\s+[A-ZÄÖÜ][a-zäöüß]{2,}")

    def texts(self) -> list[tuple[str, str]]:
        _, reports = shared_reports()
        files = [p for p in DATA.rglob("*") if p.suffix in (".txt", ".md", ".json") and p.name != "expected.json"]
        return [(p.name, p.read_text(encoding="utf-8")) for p in files] + [
            (r["case"]["case_key"], json.dumps(r, ensure_ascii=False)) for r in reports]

    def test_no_real_looking_person_names(self) -> None:
        for name, text in self.texts():
            with self.subTest(name):
                self.assertEqual(self.PERSON.findall(text), [])

    def test_udi_and_patient_identifiers_belong_to_the_fictional_set(self) -> None:
        pipeline, _ = shared_reports()
        udis = {m.udi_di for m in pipeline.ref.catalogue.models.values()}
        patients = {e.patient for e in pipeline.ref.registry}
        patients |= set(re.findall(r"P-[A-Z]{2}-\d{4}", "".join(
            p.read_text(encoding="utf-8") for p in (DATA / "letters").glob("*.txt"))))
        self.assertTrue(all(u.startswith("0049") for u in udis))  # GS1 prefix 049, internal numbers (A-08)
        for name, text in self.texts():
            with self.subTest(name):
                self.assertTrue(set(re.findall(r"(?<![0-9A-Za-z])\d{14}(?![0-9A-Za-z])", text)) <= udis)
                self.assertTrue(set(re.findall(r"(?<![A-Za-z0-9-])P-[A-Z]{2}-\d{4}(?!\d)", text)) <= patients)

    def test_email_addresses_are_demo_placeholders_only(self) -> None:
        texts = self.texts() + [(p.name, p.read_text(encoding="utf-8")) for p in source_files()]
        for name, text in texts:
            for address in re.findall(r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b", text):
                with self.subTest(name=name, address=address):
                    self.assertTrue(address.endswith((".example", ".invalid", "example.org")), address)


class ImdrfScope(unittest.TestCase):
    CODE = re.compile(r"\b[A-G]\d{4}\b")

    def test_only_the_two_codes_in_code_and_reports(self) -> None:
        _, reports = shared_reports()
        texts = [(p.name, p.read_text(encoding="utf-8")) for p in source_files()]
        texts += [(r["case"]["case_key"], json.dumps(r) + render_markdown(r)) for r in reports]
        for name, text in texts:
            with self.subTest(name):
                self.assertTrue(set(self.CODE.findall(text)) <= {"A0401", "F1905"})

    def test_candidates_carry_attribution_or_the_empty_note(self) -> None:
        _, reports = shared_reports()
        for report in reports:
            rec = report["recommendation"]
            with self.subTest(report["case"]["case_key"]):
                if rec["imdrf_candidates"]:
                    for cand in rec["imdrf_candidates"]:
                        self.assertEqual(cand["attribution"], IMDRF_ATTRIBUTION)
                        self.assertIn("pending human review", cand["status"])
                else:
                    self.assertEqual(rec["imdrf_note"], IMDRF_NO_CANDIDATE_NOTE)

    def test_an_invented_code_is_removed_and_never_reported(self) -> None:
        runtime = OfflineRuntime(forced={"A3": {"imdrf_candidates": [
            {"code": "A0999", "basis": "invented by the model", "quote": None},
            {"code": "A0401", "basis": "break of the stem", "quote": None}]}})
        p, clock = new_pipeline("imdrf", runtime=runtime)
        result = ingest(p, clock, "L02")
        p.drain()
        a3 = next(r for r in p.store.list_agent_runs(result.case_id) if r.agent_id.startswith("A3"))
        self.assertEqual(a3.output["app_checks"]["imdrf_removed"], ["A0999"])
        report = build_case_report(p, result.case_id)
        self.assertEqual([c["code"] for c in report["recommendation"]["imdrf_candidates"]], ["A0401"])
        self.assertNotIn("A0999", json.dumps(report))


class Disclaimer(unittest.TestCase):
    def test_reports_carry_the_disclaimer_and_draft_labels(self) -> None:
        _, reports = shared_reports()
        for report in reports:
            self.assertEqual(report["disclaimer"], {"de": DISCLAIMER_DE, "en": DISCLAIMER_EN})
            self.assertTrue(report["recommendation"]["draft"])
            self.assertEqual(report["recommendation"]["label_de"], "Entwurf")
            markdown = render_markdown(report)
            self.assertIn(DISCLAIMER_DE, markdown)
            self.assertIn("draft", markdown.lower())
        self.assertIn("Es illustriert Abläufe", DISCLAIMER_DE)


if __name__ == "__main__":
    unittest.main()
