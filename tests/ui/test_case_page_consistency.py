"""Case-page consistency found in the review of the CI browser screenshots (cycle 13, GitHub CI run #18).

* A new signal shows its queued analysis at once - case summary, progress section, API and case report - instead
  of the DONE of the previous job (the live view then also notices when the new analysis has finished).
* A signal without location evidence leaves a resolved case without the "incident state unknown" note; the note
  stays where the UNRESOLVED placeholder is the case's rule set.
* Each signal names the words that made it critical (CRIT-1.0 group, matched term, line).
* A6 that ran without naming a correlation says so instead of "No A6 draft yet".
* Wide tables scroll inside their panel instead of widening the page.
"""
from __future__ import annotations

import unittest

from pms.reports import build_case_report
from pms.rules import rulesets

from tests.unit._pipeline_support import cleanup_runs, ingest, new_pipeline
from tests.unit._support import ANALYST, REPO

from ._support import LiveServer, Node

FOLLOW_UP = ("SYNTHETIC DEMO DOCUMENT - FICTIONAL DATA\n\nFollow-up (fictional): the patient is doing well; "
             "no further event.\n")
PREVIOUS_RUN_HINT = "the drafts below are from the previous run"


def tearDownModule() -> None:
    cleanup_runs()


def field(scope: Node, label: str) -> str:
    """Text of the <dd> that belongs to the <dt> with this label."""
    for dt, dd in zip(scope.find_all("dt"), scope.find_all("dd")):
        if dt.text().strip() == label:
            return " ".join(dd.text().split())
    raise AssertionError(f"no field {label!r}")


class CasePageConsistency(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline, self.clock = new_pipeline("ui-consistency", at="2026-11-18T10:30:00Z")
        self.server = LiveServer(self.pipeline).start()

    def tearDown(self) -> None:
        self.server.stop()
        self.pipeline.close()

    def progress_state(self, case_id: str) -> str:
        return self.server.get(f"/api/cases/{case_id}/progress").json()["analysis_state"]

    # ------------------------------------------------------------------ analysis state
    def test_a_new_signal_shows_its_queued_analysis_until_the_job_has_run(self) -> None:
        case_id = ingest(self.pipeline, self.clock, "L11").case_id
        self.assertEqual(self.pipeline.drain(), 1)
        self.assertEqual(self.progress_state(case_id), "DONE")

        self.clock.set("2026-11-19T09:00:00Z")
        added = self.server.request("POST", f"/api/cases/{case_id}/signals", user="analyst",
                                    multipart={"text": FOLLOW_UP, "filename": "followup.txt"})
        self.assertEqual(added.status, 303, added.text[:400])
        self.assertEqual(self.pipeline.store.get_case(case_id).analysis_state, "DONE",
                         "the stored state still belongs to the last finished job")
        dom = self.server.page(f"/cases/{case_id}")
        self.assertEqual(dom.find("span", id="analysis-summary").text(), "PENDING")
        self.assertIn(PREVIOUS_RUN_HINT, dom.find("section", id="summary").text())
        self.assertEqual(dom.find("section", id="progress").get("data-analysis-state"), "PENDING")
        self.assertEqual(self.progress_state(case_id), "PENDING")
        self.assertEqual(self.server.get(f"/api/cases/{case_id}").json()["case"]["analysis_state"], "PENDING")
        self.assertEqual(build_case_report(self.pipeline, case_id)["case"]["analysis_state"], "PENDING")

        job = self.pipeline.jobs.claim_next("test-worker")
        self.assertIsNotNone(job)
        self.assertEqual(self.progress_state(case_id), "RUNNING")
        self.pipeline.process_job(job)
        self.assertEqual(self.progress_state(case_id), "DONE")
        dom = self.server.page(f"/cases/{case_id}")
        self.assertEqual(dom.find("span", id="analysis-summary").text(), "DONE")
        self.assertNotIn(PREVIOUS_RUN_HINT, dom.find("section", id="summary").text())

    # ------------------------------------------------------------------ RS-1.0 notes
    def test_a_signal_without_location_adds_no_unknown_state_note_to_a_resolved_case(self) -> None:
        case_id = ingest(self.pipeline, self.clock, "L11").case_id
        self.clock.set("2026-11-19T09:00:00Z")
        self.pipeline.add_signal(case_id, ANALYST, filename="followup.txt", text=FOLLOW_UP)
        case = self.pipeline.store.get_case(case_id)
        self.assertEqual((case.rule_sets, case.incident_states), (["EU-MDR:DE"], ["DE"]))
        self.assertNotIn(rulesets.UNRESOLVED_NOTE, case.rs_notes)
        summary = self.server.page(f"/cases/{case_id}").find("section", id="summary")
        self.assertIsNone(summary.find("ul", id="rs-notes"))
        self.assertEqual(field(summary, "Rule-set notes (RS-1.0)"), "none")
        self.assertEqual(field(summary, "Route unverified"), "no")

    def test_an_unresolved_case_keeps_the_unknown_state_note(self) -> None:
        result = self.pipeline.ingest_letter(filename="no-location.txt", text=FOLLOW_UP)
        case = self.pipeline.store.get_case(result.case_id)
        self.assertEqual(case.rule_sets, [rulesets.UNRESOLVED_RULE_SET])
        self.assertIn(rulesets.UNRESOLVED_NOTE, case.rs_notes)
        notes = self.server.page(f"/cases/{result.case_id}").find("ul", id="rs-notes")
        self.assertIsNotNone(notes)
        self.assertIn("Incident state unknown", notes.text())

    # ------------------------------------------------------------------ CRIT-1.0 indicators
    def test_each_signal_names_the_words_that_made_it_critical(self) -> None:
        case_id = ingest(self.pipeline, self.clock, "L02").case_id
        article = self.server.page(f"/cases/{case_id}").find("article", class_="signal")
        text = field(article, "Critical indicators (CRIT-1.0)")
        self.assertRegex(text, r"^DEATH - matched: (DEATH|SERIOUS_DETERIORATION): '[^']+' \(line \d+\)")
        self.assertIn("DEATH: 'died'", text)
        self.assertNotIn("DEATH DEATH", text)
        unknown = self.pipeline.ingest_letter(filename="no-location.txt", text=FOLLOW_UP)
        article = self.server.page(f"/cases/{unknown.case_id}").find("article", class_="signal")
        self.assertEqual(field(article, "Critical indicators (CRIT-1.0)"), "UNKNOWN")

    # ------------------------------------------------------------------ A6 without a correlation
    def test_a6_that_ran_without_a_correlation_says_so(self) -> None:
        case_id = self.pipeline.ingest_letter(filename="no-location.txt", text=FOLLOW_UP).case_id
        before = self.server.page(f"/cases/{case_id}").find("section", id="correlation").text()
        self.assertIn("No A6 draft yet.", before)
        self.assertEqual(self.pipeline.drain(), 1)
        after = self.server.page(f"/cases/{case_id}").find("section", id="correlation").text()
        self.assertNotIn("No A6 draft yet.", after)
        self.assertIn("A6 ran and named no correlation for this case - this is not an all-clear.", after)

    # ------------------------------------------------------------------ layout
    def test_wide_tables_scroll_inside_their_panel(self) -> None:
        case_id = ingest(self.pipeline, self.clock, "L02").case_id
        self.pipeline.drain()
        for path in ("/", f"/cases/{case_id}", "/audit"):
            tables = self.server.page(path).find_all("table")
            self.assertTrue(tables, path)
            for table in tables:
                self.assertIn("table-wrap", table.parent.classes, f"{path}: table {table.get('id')}")
        audit = self.server.page(f"/cases/{case_id}").find("table", id="audit-table")
        self.assertTrue(audit.find_all("td", class_="versions"))
        css = (REPO / "src" / "pms" / "web" / "static" / "app.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.table-wrap \{[^}]*overflow-x: auto")
        self.assertRegex(css, r"td\.before, td\.after, td\.versions \{[^}]*overflow-wrap: anywhere")
        self.assertRegex(css, r"td\.countdown-cell \.countdown\.overdue \{[^}]*white-space: normal")


if __name__ == "__main__":
    unittest.main()
