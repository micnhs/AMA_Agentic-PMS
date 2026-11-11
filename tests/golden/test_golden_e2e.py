"""End-to-end offline golden run (Goal Card checks 3, 4, 6, 7; tests T03, T04, T06, T07):
rule fields 100 %, a complete report for every case, critical alerts in the outbox within 60 s and before the
analysis finished, an offline "not serious" answer never lowers urgency, and only a human moves a critical case on."""
from __future__ import annotations

import time
import unittest

from pms import audit as A
from pms.agents.offline import OfflineRuntime
from pms.golden_e2e import run_golden_e2e
from pms.models import Actor, Role, Status
from pms.workflow import TASK_TRIAGE, InvalidTransition, PermissionDenied

from tests.unit._pipeline_support import cleanup_runs, ingest, new_pipeline, work_dir

CRITICAL_LETTERS = ("L01", "L02", "L03", "L04", "L05", "L07", "L08", "L12", "L10")  # in received_at order


def tearDownModule() -> None:
    cleanup_runs()


class GoldenEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        work = work_dir("golden-e2e", create=True)  # this test run only - never another run's folder
        cls.summary = run_golden_e2e(out_path=work / "golden-e2e-test-summary.json", work_dir=work)

    def test_passed(self) -> None:
        self.assertTrue(self.summary["passed"])
        self.assertEqual(self.summary["analysis_jobs"], {"total": 18, "not_done": []})

    def test_every_rule_based_field_matches(self) -> None:
        rf = self.summary["rule_fields"]
        self.assertEqual(rf["mismatches"], [])
        self.assertEqual(rf["input_integrity"], [])
        self.assertEqual((rf["compared"], rf["matched"]), (12 * 18 + 6 * 2, 12 * 18 + 6 * 2))

    def test_every_case_has_a_complete_report(self) -> None:
        reports = self.summary["reports"]
        self.assertEqual((reports["cases"], reports["complete"]), (14, 14))
        for item in reports["items"]:
            self.assertEqual(item["missing"], [], item["case"])
            self.assertIn(item["causality_draft"], ("not related", "possible", "probable", "causal"))
            self.assertTrue(set(item["imdrf_candidates"]) <= {"A0401", "F1905"})
        self.assertEqual(sum(item["signals"] for item in reports["items"]), 18)  # duplicates and follow-ups included

    def test_every_critical_letter_alerts_within_60_s_before_the_analysis_finished(self) -> None:
        alerts = self.summary["critical_alerts"]
        self.assertEqual([t["letter"] for t in alerts["letters"]], list(CRITICAL_LETTERS))
        self.assertEqual((alerts["critical_letters"], alerts["letters_ok"]), (9, 9))
        for t in alerts["letters"]:
            with self.subTest(t["letter"]):
                self.assertTrue(t["ok"], t)
                self.assertEqual(len(t["triage_tasks"]), 1)  # its own task, referencing this letter
                self.assertEqual(len(t["alert_ids"]), 1)
                self.assertLess(t["alert_latency_ms"], 60_000)
                self.assertLess(t["alert_latency_ms"], t["analysis_finished_ms"])
                self.assertTrue(t["outbox_entry"].endswith(".json"))
                self.assertTrue(t["case_critical"])
        l12 = alerts["letters"][CRITICAL_LETTERS.index("L12")]
        self.assertEqual((l12["case"], l12["relation"], l12["newly_critical"], l12["injection_flag"]),
                         ("CASE-L02", "duplicate_of:L02", False, True))
        self.assertIn("new signal on critical case CASE-L02", l12["alert_reason"])
        self.assertIn("injection flag raised (INJ-1.0)", l12["alert_reason"])
        # 10 alerts of newly critical cases + L12 + the critical seed duplicate demo-msg-002 on demo-case-001
        self.assertEqual(alerts["outbox_entries"], 12)
        self.assertEqual(sorted(alerts["critical_cases_waiting_for_human_triage_after_analysis"]),
                         sorted(alerts["critical_cases"]))

    def test_audit_trail_complete_and_tamper_evident(self) -> None:
        audit = self.summary["audit"]
        self.assertEqual(audit["missing_fields"], 0, audit["missing_examples"])
        self.assertTrue(audit["one_record_per_step"]["ok"], audit["one_record_per_step"])
        self.assertTrue(audit["chain"]["ok"])
        tamper = audit["tamper_test"]
        self.assertTrue(tamper["update_blocked_by_trigger"])
        self.assertTrue(tamper["detected"])
        self.assertEqual(tamper["first_break_seq"], tamper["edited_seq"])
        for event in (A.AGENT_RUN, A.TOOL_CALL, A.STATUS_CHANGED, A.DECISION, A.METADATA_EDITED, A.ALERT_SENT,
                      A.COST_RECORDED, A.CORRELATION_RUN, A.SUBMISSION_RECORDED):
            self.assertGreater(audit["by_event_type"].get(event, 0), 0, event)


class CriticalFlowWithTheBackgroundWorker(unittest.TestCase):
    def test_alert_and_triage_task_exist_when_the_upload_returns(self) -> None:
        p, clock = new_pipeline("realtime", real_files=True)
        p.start_worker(poll_s=0.02)
        try:
            started = time.monotonic()
            result = ingest(p, clock, "L01")
            returned = time.monotonic() - started
            outbox = [f.name for f in p.files.list("outbox")]
            self.assertEqual(len(outbox), 2)  # .eml + .json written before the upload call returned
            self.assertTrue(p.store.list_tasks(case_id=result.case_id, kind=TASK_TRIAGE, status="OPEN"))
            self.assertLess(returned, 60)
            deadline = time.monotonic() + 30
            while p.jobs.pending_count() and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            p.stop_worker()
        timing = p.alert_timings()[result.signal_id]
        self.assertLess(timing["alerts"][0]["latency_ms"], timing["analysis_finished_ms"])
        board = p.dashboard()
        self.assertEqual(board["critical_queue"][0]["case_key"], "CASE-L01")
        self.assertGreater(board["critical_queue"][0]["countdown_seconds"], 0)


class EveryCriticalLetterAlerts(unittest.TestCase):
    def test_critical_copy_on_a_critical_case_after_the_triage_was_taken(self) -> None:
        p, clock = new_pipeline("critical-copy")
        analyst = Actor("analyst-1", roles=frozenset({Role.ANALYST}))
        first = ingest(p, clock, "L02")
        p.drain()
        self.assertEqual(p.take_triage(first.case_id, analyst, "triage taken").status, Status.ENTWURF_BEREIT)
        due = p.engine.case_labels(first.case_id)["driving_due_date"]

        result = ingest(p, clock, "L12")  # the injection copy of L02 lands in the critical case
        self.assertEqual((result.case_id, result.relation, result.newly_critical, result.critical),
                         (first.case_id, "duplicate_of:L02", False, True))
        self.assertEqual(result.status, Status.TRIAGE_ERFORDERLICH)
        self.assertEqual([(a["status"], a["within_target"]) for a in result.alerts], [("SENT", True)])
        triage = p.store.list_tasks(case_id=result.case_id, kind=TASK_TRIAGE, status="OPEN")
        self.assertEqual([t.detail["signal_id"] for t in triage], [result.signal_id])
        outbox = p.outbox_entries()
        self.assertEqual([e["alert_id"] for e in outbox[1:]], [result.alerts[0]["alert_id"]])
        self.assertIn("new signal on critical case CASE-L02", outbox[-1]["reason"])
        self.assertIn("injection flag raised (INJ-1.0)", outbox[-1]["reason"])
        self.assertNotIn("example.org", outbox[-1]["reason"])
        self.assertEqual(p.jobs.pending_count(), 1, "the alert went out before the analysis ran")

        p.drain()
        timing = p.alert_timings()[result.signal_id]
        self.assertLess(timing["alerts"][0]["latency_ms"], 60_000)
        self.assertLess(timing["alerts"][0]["latency_ms"], timing["analysis_finished_ms"])
        case = p.store.get_case(result.case_id)
        self.assertEqual((case.critical, case.status), (True, Status.TRIAGE_ERFORDERLICH))
        self.assertEqual(p.engine.case_labels(case.case_id)["driving_due_date"], due)  # no deadline reset
        self.assertEqual(p.take_triage(case.case_id, analyst, "new letter triaged").status, Status.ENTWURF_BEREIT)
        self.assertEqual(p.store.list_tasks(case_id=case.case_id, kind=TASK_TRIAGE, status="OPEN"), [])


class NotSeriousNeverLowersUrgency(unittest.TestCase):
    def assert_still_critical(self, p, case_id: str) -> None:
        case = p.store.get_case(case_id)
        self.assertTrue(case.critical)
        self.assertEqual(case.status, Status.TRIAGE_ERFORDERLICH)
        self.assertEqual(p.store.audit.count(case_id=case_id, event_type=A.URGENCY_LOWERED), 0)

    def test_offline_answer_for_the_product_unrelated_letter(self) -> None:
        p, clock = new_pipeline("l07")
        result = ingest(p, clock, "L07")
        p.drain()
        rec = p.store.latest_recommendation(result.case_id).content
        self.assertEqual((rec["ai_severity"], rec["ai_reading_outcome"]), ("NOT_SERIOUS", "ignored_lower"))
        self.assertEqual(rec["causality"], "not related")
        self.assert_still_critical(p, result.case_id)

    def test_forced_not_serious_answers(self) -> None:
        runtime = OfflineRuntime(forced={"A1": {"ai_severity": "NOT_SERIOUS"},
                                         "A3": {"ai_severity": "NOT_SERIOUS", "causality": "not related"}})
        p, clock = new_pipeline("forced-not-serious", runtime=runtime)
        result = ingest(p, clock, "L01")
        p.drain()
        self.assert_still_critical(p, result.case_id)
        outcomes = [r.output.get("ai_severity") for r in p.store.list_agent_runs(result.case_id)
                    if r.agent_id[:2] in ("A1", "A3")]
        self.assertEqual(outcomes, ["NOT_SERIOUS", "NOT_SERIOUS"])


class OnlyAHumanMovesACriticalCaseOn(unittest.TestCase):
    def test_no_timer_agent_or_system_moves_it(self) -> None:
        p, clock = new_pipeline("human-only")
        result = ingest(p, clock, "L02")
        p.drain()
        case_id = result.case_id
        self.assertEqual(p.store.get_case(case_id).analysis_state, "DONE")
        for at in ("2026-11-26T23:00:00Z", "2027-02-01T00:00:00Z"):  # deadline near and long overdue
            clock.set(at)
            p.run_worker_once()
            self.assertEqual(p.store.get_case(case_id).status, Status.TRIAGE_ERFORDERLICH)
        with self.assertRaises(PermissionDenied):
            p.take_triage(case_id, Actor.agent("A3-medical-analysis"))
        with self.assertRaises(PermissionDenied):
            p.take_triage(case_id, Actor("viewer-1", roles=frozenset({Role.VIEWER})))
        with self.assertRaises(InvalidTransition):
            p.workflow.system_transition(case_id, Status.ENTWURF_BEREIT, reason="timer")
        self.assertEqual(p.store.audit.count(case_id=case_id, event_type=A.ACCESS_DENIED), 2)
        moved = p.take_triage(case_id, Actor("analyst-1", roles=frozenset({Role.ANALYST})), "taken")
        self.assertEqual(moved.status, Status.ENTWURF_BEREIT)


if __name__ == "__main__":
    unittest.main()
