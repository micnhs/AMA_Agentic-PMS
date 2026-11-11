"""Pipeline behaviour and audit completeness (Goal Card check 6, T06): every status change, agent run, tool call,
cost, human decision and metadata edit has exactly one audit record with 0 missing fields; plus linking, progress,
views, governance and telemetry (T29)."""
from __future__ import annotations

import time
import unittest
from collections import Counter

from pms import audit as A
from pms.agents.offline import OfflineRuntime
from pms.governance import audit_completeness, audit_quality, governance_view, missing_fields
from pms.models import Actor, Role, Status, parse_utc
from pms.telemetry import Telemetry
from pms.workflow import PermissionDenied

from ._pipeline_support import cleanup_runs, ingest, new_pipeline, seeded

ANALYST = Actor("analyst-1", roles=frozenset({Role.ANALYST}))
REVIEWER = Actor("reviewer-1", roles=frozenset({Role.MEDICAL_REVIEWER}))
REGULATORY = Actor("regulatory-1", roles=frozenset({Role.REGULATORY}))
OPERATOR = Actor("operator-1", roles=frozenset({Role.OPERATOR}))


def tearDownModule() -> None:
    cleanup_runs()


class AuditCompleteness(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.p, cls.clock = seeded("audit")
        p = cls.p
        cls.clock.set("2026-11-24T08:00:00Z")
        p.take_triage("CASE-L01", ANALYST, "triage")
        p.edit_metadata("CASE-L01", ANALYST, "note", "sender asked for the explant report", "follow-up note")
        p.start_review("CASE-L01", REVIEWER)
        p.decide("CASE-L01", REVIEWER, "approve", "draft checked against the sources")
        p.record_submission("CASE-L01", REGULATORY, channel_label="EU form (label)", reference="REF-1",
                            submitted_on="2026-11-24", receipt_status="unconfirmed")
        p.take_triage("CASE-L03", REVIEWER)
        p.start_review("CASE-L03", REVIEWER)
        p.decide("CASE-L03", REVIEWER, "reject", "lot evidence insufficient", reason_code="R1")
        p.start_review("CASE-L06", REVIEWER)
        p.decide("CASE-L06", REVIEWER, "approve", "minor known side-effect")
        p.request_reanalysis("CASE-L06", ANALYST, "new dossier version")
        p.drain()
        try:
            p.workflow.approve("CASE-L11", Actor.agent("A3-medical-analysis"), "agents cannot decide")
        except PermissionDenied:
            pass
        else:  # pragma: no cover
            raise AssertionError("an agent must never approve")
        cls.records = p.store.audit.records()

    def test_zero_missing_fields(self) -> None:
        quality = audit_quality(self.p.store)
        self.assertEqual(quality["missing_fields"], 0, quality["examples"])
        self.assertGreater(quality["records"], 500)

    def test_exactly_one_record_per_step(self) -> None:
        result = audit_completeness(self.p.store)
        for name, check in result["checks"].items():
            with self.subTest(name):
                self.assertTrue(check["ok"], check)
        counts = Counter(r["event_type"] for r in self.records)
        runs = [r for c in self.p.store.list_cases() for r in self.p.store.list_agent_runs(c.case_id)]
        self.assertEqual(counts[A.AGENT_RUN], len(runs))
        self.assertEqual(len(runs), 19 * 5)  # 18 signals + one re-analysis
        self.assertEqual(counts[A.DECISION], 3)
        self.assertEqual(counts[A.METADATA_EDITED], 1)
        self.assertEqual(counts[A.SUBMISSION_RECORDED], 1)
        self.assertEqual(counts[A.ACCESS_DENIED], 1)

    def test_agent_records_carry_versions_usage_and_cost(self) -> None:
        for rec in [r for r in self.records if r["event_type"] == A.AGENT_RUN]:
            self.assertTrue(rec["model"] and rec["model_version"] and rec["prompt_version"] and rec["agent_version"])
            self.assertIn("CRIT-1.0", rec["rule_versions"])
            self.assertEqual(rec["corpus_version"], "1.0")
            self.assertEqual(rec["cost_estimate"]["price_table_version"], "2026-11-10")
            self.assertTrue(rec["usage"]["input"] > 0)
            self.assertIs(rec["after"]["draft"], True)

    def test_human_records_carry_user_reason_before_and_after(self) -> None:
        human = [r for r in self.records if r["actor_type"] == "human" and r["event_type"] in (
            A.DECISION, A.METADATA_EDITED, A.STATUS_CHANGED)]
        self.assertTrue(human)
        for rec in human:
            self.assertEqual(missing_fields(rec), [])
            self.assertIsNotNone(rec["before"])
            self.assertIsNotNone(rec["after"])
        edit = next(r for r in self.records if r["event_type"] == A.METADATA_EDITED)
        self.assertEqual((edit["actor_id"], edit["before"], edit["after"], edit["reason"]),
                         ("analyst-1", {"note": None}, {"note": "sender asked for the explant report"}, "follow-up note"))

    def test_no_decision_by_agents_or_the_system(self) -> None:
        self.assertFalse([r for r in self.records if r["event_type"] == A.DECISION and r["actor_type"] != "human"])
        self.assertFalse([r for r in self.records if r["event_type"] == A.URGENCY_LOWERED
                          and r["actor_type"] != "human"])

    def test_reanalysis_voided_the_approval_and_ran_again(self) -> None:
        case = self.p.store.get_case("CASE-L06")
        self.assertIsNone(self.p.store.valid_approval("CASE-L06"))
        self.assertEqual((case.status, case.analysis_state), (Status.ENTWURF_BEREIT, "DONE"))
        self.assertEqual(len(self.p.jobs.list(case_id="CASE-L06")), 2)

    def test_chain_verifies(self) -> None:
        self.assertTrue(self.p.verify_audit()["ok"])


class PipelineBehaviour(unittest.TestCase):
    def test_follow_up_attaches_keeps_the_deadline_and_gets_its_own_analysis(self) -> None:
        p, clock = new_pipeline("followup")
        first = ingest(p, clock, "L09")
        p.drain()
        before = p.workflow.deadline_overview(first.case_id)["awareness_anchor"]
        second = ingest(p, clock, "L10")
        self.assertEqual((second.case_id, second.relation), (first.case_id, "follow_up_of:L09"))
        self.assertTrue(second.timings["alerts"][0]["within_target"])
        p.drain()
        self.assertEqual(p.workflow.deadline_overview(first.case_id)["awareness_anchor"], before)
        self.assertEqual([j.signal_id for j in p.jobs.list(case_id=first.case_id)],
                         [first.signal_id, second.signal_id])
        rec = p.store.latest_recommendation(first.case_id)
        self.assertEqual(rec.version, 2)
        self.assertEqual(p.store.get_case(first.case_id).status, Status.TRIAGE_ERFORDERLICH)

    def test_progress_per_agent_and_dashboard_order(self) -> None:
        p, clock = new_pipeline("dash")
        for letter in ("L06", "L01", "L03"):
            ingest(p, clock, letter)
        before = p.case_progress("CASE-L01")
        self.assertEqual(before["latest_job"]["status"], "QUEUED")
        p.drain()
        steps = p.case_progress("CASE-L01")["steps"]
        self.assertEqual({k: v["status"] for k, v in steps.items()},
                         {"A1": "DONE", "A2": "DONE", "CORR": "DONE", "A6": "DONE", "A3": "DONE", "A4": "DONE"})
        self.assertTrue(all(v.get("run_id") for k, v in steps.items() if k != "CORR"))
        board = p.dashboard()
        self.assertEqual([r["case_key"] for r in board["critical_queue"]], ["CASE-L03", "CASE-L01"])  # 2 days first
        self.assertEqual([r["case_key"] for r in board["cases"]], ["CASE-L06"])
        self.assertTrue(all(r["countdown_seconds"] > 0 for r in board["critical_queue"]))

    def test_ai_reading_can_raise_urgency_with_triage_and_alert(self) -> None:
        runtime = OfflineRuntime(forced={"A1": {"ai_severity": "SERIOUS_DETERIORATION"}})
        p, clock = new_pipeline("raise", runtime=runtime)
        result = ingest(p, clock, "L06")
        self.assertFalse(result.critical)
        p.drain()
        case = p.store.get_case(result.case_id)
        self.assertTrue(case.critical)
        self.assertEqual(case.status, Status.TRIAGE_ERFORDERLICH)
        raised = p.store.audit.records(case_id=case.case_id, event_type=A.URGENCY_RAISED)
        self.assertEqual((raised[0]["actor_type"], raised[0]["actor_id"]), ("agent", "A1-signal-ingestion"))
        self.assertEqual([a.status for a in p.store.list_alerts(case_id=case.case_id)], ["SENT"])

    def test_background_worker_thread(self) -> None:
        p, clock = new_pipeline("thread")
        p.start_worker(poll_s=0.05)
        try:
            result = ingest(p, clock, "L02")
            deadline = time.monotonic() + 30
            while p.jobs.pending_count() and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            p.stop_worker()
        self.assertEqual(p.jobs.list(case_id=result.case_id)[0].status, "DONE")
        self.assertIsNotNone(p.store.get_meta("worker_heartbeat"))

    def test_seed_is_idempotent_and_operator_only(self) -> None:
        p, clock = new_pipeline("seed")
        with self.assertRaises(PermissionDenied):
            p.seed_demo(ANALYST)
        self.assertEqual(p.store.audit.count(event_type=A.ACCESS_DENIED), 1)
        first = p.seed_demo(OPERATOR, replay_clock=clock)
        self.assertEqual(len(first["loaded"]), 18)
        second = p.seed_demo(OPERATOR, replay_clock=clock)
        self.assertEqual((len(second["loaded"]), len(second["skipped_already_loaded"])), (0, 18))
        self.assertEqual(len(p.store.list_findings(kind="process_finding")), 3)

    def test_reanalysis_needs_a_readable_signal_and_leaves_the_status_alone(self) -> None:
        p, _ = new_pipeline("reanalyse-unreadable")
        result = p.ingest_letter(filename="empty.txt", text="   ")
        with self.assertRaises(ValueError):
            p.request_reanalysis(result.case_id, ANALYST, "try again")
        self.assertEqual(p.store.get_case(result.case_id).status, Status.NICHT_LESBAR)
        self.assertEqual(p.jobs.list(case_id=result.case_id), [])

    def test_rejected_and_unmatched_views(self) -> None:
        p, clock = new_pipeline("views")
        ingest(p, clock, "L11")
        p.drain()
        p.start_review("CASE-L11", REVIEWER)
        p.decide("CASE-L11", REVIEWER, "reject", "lot and UDI-DI contradict the registry", reason_code="R3")
        rejected = p.rejected_view()
        self.assertEqual((rejected[0]["reason_code"], rejected[0]["reason_code_label"]),
                         ("R3", "wrong product assignment"))
        unmatched = p.unreadable_unmatched_view()
        self.assertEqual(unmatched[0]["kind"], "UNMATCHED")
        self.assertTrue(unmatched[0]["registry_check"].startswith("MISMATCH"))

    def test_deadline_sweep_raises_only_and_reports_overdue(self) -> None:
        p, clock = new_pipeline("sweep")
        result = ingest(p, clock, "L06")
        p.drain()
        clock.set("2026-12-10T08:00:00Z")
        with self.assertLogs("pms.telemetry", level="INFO") as logs:
            raised = p.sweep_deadlines()
        self.assertEqual(raised, [result.case_id])
        case = p.store.get_case(result.case_id)
        self.assertEqual((case.critical, case.status), (True, Status.TRIAGE_ERFORDERLICH))
        events = [getattr(r, "pms_event", None) for r in logs.records]
        self.assertIn("ALERT_SENT", events)
        self.assertIn("CASE_OVERDUE", events)


class GovernanceAndTelemetry(unittest.TestCase):
    def test_governance_view(self) -> None:
        p, clock = new_pipeline("gov")
        ingest(p, clock, "L01")
        p.drain()
        view = governance_view(p)
        self.assertFalse(view["preview_features"]["enabled"])
        self.assertEqual(view["model_deployments"]["analysis"]["deployment"], "gpt-6-astra")
        self.assertEqual(view["model_deployments"]["general"]["model_version"], "2026-09-29")
        self.assertEqual([r["rule"] for r in view["rule_sets"]],
                         ["CRIT-1.0", "DL-1.0", "RS-1.0", "LINK-1.0", "REG-1.0", "INJ-1.0", "CORR-1.0"])
        self.assertEqual(view["alert_latency"]["sent"], 1)
        self.assertEqual(view["alert_latency"]["within_target"], 1)
        self.assertTrue(view["audit"]["ok"])
        self.assertEqual(view["audit"]["missing_fields"], 0)
        self.assertEqual(view["open_human_decisions"]["TRIAGE_ERFORDERLICH"], 1)
        self.assertIn("operator", view["tracing"]["read_access"])
        self.assertEqual(len(view["versions"]["agents"]), 6)

    def test_contract_events_and_local_spans(self) -> None:
        p, clock = new_pipeline("telemetry", real_files=True)
        p.telemetry = Telemetry(trace_path=p.settings.trace_path)
        with self.assertLogs("pms.telemetry", level="INFO") as logs:
            ingest(p, clock, "L01")
            p.drain()
            p.heartbeat(force=True)
        events = Counter(getattr(r, "pms_event", None) for r in logs.records)
        self.assertEqual((events["AGENT_RUN"], events["ALERT_SENT"]), (5, 1))
        self.assertEqual((events["WORKER_HEARTBEAT"], events["AUDIT_VERIFY"]), (1, 1))
        run = next(r for r in logs.records if getattr(r, "pms_event", None) == "AGENT_RUN")
        for attr in ("pms_status", "pms_agent", "pms_model", "pms_duration_ms", "pms_tokens_in", "pms_tokens_out",
                     "pms_cost_usd"):
            self.assertTrue(hasattr(run, attr), attr)
        self.assertEqual(run.pms_status, "ok")
        alert = next(r for r in logs.records if getattr(r, "pms_event", None) == "ALERT_SENT")
        self.assertEqual(alert.pms_case_id, "CASE-L01")
        self.assertLess(alert.pms_latency_s, 60)
        spans = p.telemetry.spans()
        self.assertEqual(len(spans), 5)
        for span in spans:
            attrs = span["attributes"]
            self.assertTrue(attrs["pms_model"] and attrs["pms_prompt_version"] and attrs["pms_corpus_version"])
            self.assertIn("CRIT-1.0", attrs["pms_rule_versions"])
            self.assertLessEqual(parse_utc(span["start_utc"]), parse_utc(span["end_utc"]))


if __name__ == "__main__":
    unittest.main()
