"""Workflow: permission matrix, human-only decisions, approval binding, deadline clock."""
from __future__ import annotations

import unittest

from pms import audit as A
from pms.governance import missing_fields
from pms.models import Actor, Status, parse_utc
from pms.rules import deadlines as dl
from pms.workflow import (
    ANALYSIS_DONE, ANALYSIS_RUNNING, Action, ApprovalRequired, InvalidTransition, PermissionDenied, ReasonRequired,
    Workflow,
)

from ._support import (
    AGENT, AGENT_META, ANALYST, ANONYMOUS, OPERATOR, REGULATORY, REVIEWER, SYSTEM, VIEWER, new_engine, process,
)

RULE = Actor.rule("rule-engine")
NON_REVIEWERS = {"anonymous": ANONYMOUS, "viewer": VIEWER, "analyst": ANALYST, "regulatory": REGULATORY,
                 "operator": OPERATOR, "agent": AGENT, "system": SYSTEM, "rule": RULE}


def draft_ready(letter: str = "L06"):
    """Process a letter and bring its case to ENTWURF_BEREIT via the technical steps."""
    store, engine, clock = new_engine()
    result = process(engine, clock, letter)
    wf: Workflow = engine.workflow
    case = store.get_case(result.case_id)
    if case.status == Status.TRIAGE_ERFORDERLICH:
        wf.take_triage(case.case_id, ANALYST, now=clock.now())
    wf.mark_analysis(case.case_id, ANALYSIS_RUNNING, now=clock.now())
    run = wf.record_agent_run(case.case_id, AGENT, agent_meta=AGENT_META, status="DONE",
                              output={"causality": "possible"}, now=clock.now())
    wf.record_recommendation(case.case_id, AGENT, {"causality": "possible", "product_defect_possible": "unclear"},
                             agent_meta=AGENT_META, run_id=run.run_id, now=clock.now())
    wf.mark_analysis(case.case_id, ANALYSIS_DONE, now=clock.now())
    return store, engine, clock, wf, store.get_case(case.case_id)


class PermissionMatrix(unittest.TestCase):
    def test_decisions_are_refused_for_everybody_but_the_medical_reviewer(self) -> None:
        store, _, clock, wf, case = draft_ready()
        attempts = 0
        for decision in ("approve", "reject", "close", "non_reportable", "request_info"):
            for name, actor in NON_REVIEWERS.items():
                with self.subTest(decision=decision, actor=name):
                    with self.assertRaises(PermissionDenied):
                        wf.decide(case.case_id, actor, decision, "a reason", reason_code="R1", now=clock.now())
                    attempts += 1
        self.assertEqual(store.audit.count(event_type=A.ACCESS_DENIED), attempts)
        self.assertEqual(store.get_case(case.case_id).status, Status.ENTWURF_BEREIT)
        self.assertEqual(store.list_decisions(case_id=case.case_id), [])
        denied = store.audit.records(event_type=A.ACCESS_DENIED)
        self.assertIn("agent", {r["actor_type"] for r in denied})
        self.assertIn("anonymous", {r["actor_id"] for r in denied})

    def test_rule_agent_and_system_attempts_are_refused_and_audited_once(self) -> None:
        """No decision rights for non-human actors: PermissionDenied (never an AuditError) and exactly one
        complete ACCESS_DENIED record per refused attempt - as for human roles."""
        store, _, clock, wf, case = draft_ready("L01")  # critical case with a draft, waiting for review
        cid = case.case_id
        attempts = {
            "approve": lambda a: wf.decide(cid, a, "approve", "a reason", now=clock.now()),
            "reject": lambda a: wf.decide(cid, a, "reject", "a reason", reason_code="R1", now=clock.now()),
            "close": lambda a: wf.decide(cid, a, "close", "a reason", now=clock.now()),
            "non_reportable": lambda a: wf.decide(cid, a, "non_reportable", "a reason", now=clock.now()),
            "lower_urgency": lambda a: wf.lower_urgency(cid, a, "a reason", now=clock.now()),
            "record_submission": lambda a: wf.record_submission(
                cid, a, channel_label="label", reference="REF-1", submitted_on="2026-11-16", now=clock.now()),
            "approve without a reason": lambda a: wf.decide(cid, a, "approve", "", now=clock.now()),
        }
        for name, attempt in attempts.items():
            for actor in (RULE, AGENT, SYSTEM):
                with self.subTest(decision=name, actor=actor.actor_type.value):
                    seq = store.audit.count()
                    with self.assertRaises(PermissionDenied):
                        attempt(actor)
                    records = store.audit.records(after_seq=seq)
                    self.assertEqual([r["event_type"] for r in records], [A.ACCESS_DENIED])
                    record = records[0]
                    self.assertEqual((record["actor_type"], record["actor_id"], record["case_id"]),
                                     (actor.actor_type.value, actor.actor_id, cid))
                    self.assertIn("no decision rights", record["reason"])
                    self.assertEqual(missing_fields(record), [])
        case = store.get_case(cid)
        self.assertEqual((case.status, case.critical), (Status.ENTWURF_BEREIT, True))
        self.assertEqual(store.list_decisions(case_id=cid), [])
        self.assertEqual(store.list_submissions(cid), [])
        self.assertEqual(store.audit.count(event_type=A.ACCESS_DENIED), len(attempts) * 3)
        self.assertTrue(store.audit.verify().ok)

    def test_reason_is_mandatory_and_reject_needs_a_code(self) -> None:
        _, _, clock, wf, case = draft_ready()
        with self.assertRaises(ReasonRequired):
            wf.approve(case.case_id, REVIEWER, "   ", now=clock.now())
        with self.assertRaises(ReasonRequired):
            wf.reject(case.case_id, REVIEWER, "not traceable", "R9", now=clock.now())
        rejected = wf.reject(case.case_id, REVIEWER, "causality not traceable", "R4", now=clock.now())
        self.assertEqual(rejected.status, Status.ABGELEHNT)

    def test_role_matrix_basics(self) -> None:
        self.assertTrue(Workflow.can(ANALYST, Action.UPLOAD))
        self.assertFalse(Workflow.can(OPERATOR, Action.UPLOAD))
        self.assertTrue(Workflow.can(OPERATOR, Action.LOAD_DEMO_DATA))
        self.assertTrue(Workflow.can(REGULATORY, Action.RECORD_SUBMISSION))
        self.assertFalse(Workflow.can(REVIEWER, Action.RECORD_SUBMISSION))
        self.assertTrue(Workflow.can(AGENT, Action.RAISE_URGENCY))
        self.assertFalse(Workflow.can(AGENT, Action.LOWER_URGENCY))
        self.assertFalse(Workflow.can(ANONYMOUS, Action.VIEW))
        self.assertTrue(Workflow.can(OPERATOR, Action.VIEW_TRACES))
        self.assertFalse(Workflow.can(REVIEWER, Action.VIEW_TRACES))
        for actor in (VIEWER, ANALYST, REGULATORY, REVIEWER, OPERATOR, AGENT, SYSTEM):
            self.assertFalse(Workflow.can(actor, Action.MODIFY_AUDIT))


class Urgency(unittest.TestCase):
    def test_only_the_medical_reviewer_lowers_urgency_with_a_reason(self) -> None:
        store, engine, clock = new_engine()
        result = process(engine, clock, "L07")  # appendicitis: the rule stays critical until a human lowers it
        wf = engine.workflow
        for actor in (ANALYST, REGULATORY, AGENT, SYSTEM, ANONYMOUS):
            with self.assertRaises(PermissionDenied):
                wf.lower_urgency(result.case_id, actor, "unrelated", now=clock.now())
        with self.assertRaises(ReasonRequired):
            wf.lower_urgency(result.case_id, REVIEWER, "", now=clock.now())
        self.assertTrue(store.get_case(result.case_id).critical)
        lowered = wf.lower_urgency(result.case_id, REVIEWER, "appendicitis, prosthesis normal", now=clock.now())
        self.assertFalse(lowered.critical)
        self.assertEqual(store.audit.count(case_id=result.case_id, event_type=A.URGENCY_LOWERED), 1)

    def test_ai_answer_not_serious_never_lowers(self) -> None:
        store, engine, clock = new_engine()
        result = process(engine, clock, "L07")
        outcome = engine.workflow.apply_ai_reading(result.case_id, AGENT, "NONE_DETECTED", agent_meta=AGENT_META,
                                                   now=clock.now())
        self.assertEqual(outcome, "ignored_lower")
        self.assertTrue(store.get_case(result.case_id).critical)

    def test_ai_reading_can_raise(self) -> None:
        store, engine, clock = new_engine()
        result = process(engine, clock, "L06")
        outcome = engine.workflow.apply_ai_reading(result.case_id, AGENT, "SERIOUS_DETERIORATION",
                                                   agent_meta=AGENT_META, now=clock.now())
        self.assertEqual(outcome, "raised")
        case = store.get_case(result.case_id)
        self.assertTrue(case.critical)
        self.assertEqual(case.status, Status.TRIAGE_ERFORDERLICH)


class ApprovalBinding(unittest.TestCase):
    def test_approval_is_bound_to_the_reviewed_version(self) -> None:
        store, _, clock, wf, case = draft_ready()
        approval = wf.approve(case.case_id, REVIEWER, "plausible, listed side effect", now=clock.now())
        self.assertEqual(approval.content_hash, wf.content_hash(case.case_id))
        self.assertEqual(store.get_case(case.case_id).status, Status.FREIGEGEBEN)
        self.assertEqual(len(store.list_findings(kind="approved_assessment")), 1)

    def test_any_change_after_approval_voids_it(self) -> None:
        store, _, clock, wf, case = draft_ready()
        wf.approve(case.case_id, REVIEWER, "ok", now=clock.now())
        wf.edit_metadata(case.case_id, ANALYST, "implant_site", "Ospedale Dimostrativo del Lago (fittizio)",
                         "confirmed by phone", now=clock.now())
        self.assertIsNone(store.valid_approval(case.case_id))
        self.assertEqual(store.get_case(case.case_id).status, Status.IN_MEDIZINISCHER_PRUEFUNG)
        self.assertEqual(store.audit.count(case_id=case.case_id, event_type=A.APPROVAL_VOIDED), 1)
        with self.assertRaises(InvalidTransition):
            wf.record_submission(case.case_id, REGULATORY, channel_label="NSIS-Dispovigilance (label)",
                                 reference="DEMO-1", submitted_on="2026-11-20", now=clock.now())

    def test_new_agent_run_voids_the_approval(self) -> None:
        store, _, clock, wf, case = draft_ready()
        wf.approve(case.case_id, REVIEWER, "ok", now=clock.now())
        wf.record_agent_run(case.case_id, AGENT, agent_meta=AGENT_META, status="DONE", output={}, now=clock.now())
        self.assertIsNone(store.valid_approval(case.case_id))

    def test_new_signal_voids_the_approval(self) -> None:
        store, engine, clock, wf, case = draft_ready("L09")
        wf.approve(case.case_id, REVIEWER, "provisional assessment", now=clock.now())
        process(engine, clock, "L10")
        self.assertIsNone(store.valid_approval(case.case_id))
        self.assertEqual(store.get_case(case.case_id).status, Status.TRIAGE_ERFORDERLICH)  # L10 is critical

    def test_submission_refused_when_content_changed_behind_the_approval(self) -> None:
        store, _, clock, wf, case = draft_ready()
        wf.approve(case.case_id, REVIEWER, "ok", now=clock.now())
        store.insert_fact(case_id=case.case_id, signal_id=None, name="note", value="changed", quote=None, line=None,
                          verified=False, origin="human")
        with self.assertRaises(ApprovalRequired):
            wf.record_submission(case.case_id, REGULATORY, channel_label="label", reference="R",
                                 submitted_on="2026-11-20", now=clock.now())


class DeadlineClock(unittest.TestCase):
    def test_clock_stops_only_when_a_person_records_the_submission(self) -> None:
        store, engine, clock, wf, case = draft_ready("L01")  # death, due 2026-11-26
        late = parse_utc("2026-11-27T08:00:00Z")
        self.assertTrue(wf.deadline_overview(case.case_id, late)["overdue"])
        wf.approve(case.case_id, REVIEWER, "causality possible", now=clock.now())
        self.assertTrue(wf.deadline_overview(case.case_id, late)["overdue"])  # approval does not stop it
        for actor in (REVIEWER, ANALYST, AGENT, SYSTEM):
            with self.assertRaises(PermissionDenied):
                wf.record_submission(case.case_id, actor, channel_label="BfArM (label)", reference="X",
                                     submitted_on="2026-11-20", now=clock.now())
        sub = wf.record_submission(case.case_id, REGULATORY, channel_label="BfArM (label only)",
                                   reference="DEMO-REF-1", submitted_on="2026-11-20", receipt_status="unconfirmed",
                                   now=clock.now())
        overview = wf.deadline_overview(case.case_id, late)
        self.assertFalse(overview["overdue"])
        self.assertEqual(overview["clock"], "STOPPED_BY_SUBMISSION")
        self.assertTrue(all(c["status"] == dl.STOPPED_BY_SUBMISSION for c in overview["candidates"]))
        self.assertEqual(store.get_case(case.case_id).status, Status.MELDUNG_ERFASST)
        self.assertEqual(store.list_submissions(case.case_id)[0].submission_id, sub.submission_id)

    def test_non_reportable_shows_entfaellt_and_does_not_stop_the_clock(self) -> None:
        store, _, clock, wf, case = draft_ready()
        wf.close_non_reportable(case.case_id, REVIEWER, "listed, non-serious, documented", now=clock.now())
        overview = wf.deadline_overview(case.case_id, parse_utc("2026-12-05T00:00:00Z"))
        self.assertEqual(overview["clock"], "WAIVED")
        self.assertFalse(overview["overdue"])
        self.assertEqual(overview["candidates"][0]["label"], "entfällt laut menschlicher Entscheidung")
        self.assertEqual(overview["waiver"]["by"], REVIEWER.actor_id)
        self.assertTrue(all(r.stopped_by_submission_id is None for r in store.list_deadlines(case.case_id)))

    def test_follow_up_reopens_a_closed_case_for_new_triage(self) -> None:
        store, engine, clock, wf, case = draft_ready("L09")
        wf.close_non_reportable(case.case_id, REVIEWER, "no product information", now=clock.now())
        process(engine, clock, "L10")
        reopened = store.get_case(case.case_id)
        self.assertEqual(reopened.status, Status.TRIAGE_ERFORDERLICH)
        self.assertIsNone(reopened.deadline_waiver)
        self.assertEqual(reopened.awareness_anchor, "2026-11-17T11:00:00Z")

    def test_no_timer_ever_makes_a_decision(self) -> None:
        store, engine, clock, wf, case = draft_ready()
        clock.set("2027-01-31T00:00:00Z")  # far past every deadline
        wf.deadline_overview(case.case_id)
        engine.check_all_deadline_windows(now=clock.now())
        after = store.get_case(case.case_id)
        self.assertNotIn(after.status, (Status.FREIGEGEBEN, Status.ABGESCHLOSSEN,
                                        Status.ABGESCHLOSSEN_NICHT_MELDEPFLICHTIG, Status.MELDUNG_ERFASST))
        self.assertEqual(store.list_decisions(case_id=case.case_id), [])
        self.assertEqual(store.list_approvals(case.case_id), [])
        self.assertTrue(after.critical)  # time pressure may only raise urgency


class AuditPerStep(unittest.TestCase):
    def test_exactly_one_status_record_per_status_change(self) -> None:
        store, _, clock, wf, case = draft_ready("L01")
        wf.start_review(case.case_id, REVIEWER, now=clock.now())
        wf.approve(case.case_id, REVIEWER, "ok", now=clock.now())
        wf.record_submission(case.case_id, REGULATORY, channel_label="label", reference="R",
                             submitted_on="2026-11-20", now=clock.now())
        changes = store.audit.records(case_id=case.case_id, event_type=A.STATUS_CHANGED)
        path = [r["after"]["status"] for r in changes]
        self.assertEqual(path, [Status.TRIAGE_ERFORDERLICH, Status.IN_ANALYSE, Status.ENTWURF_BEREIT,
                                Status.IN_MEDIZINISCHER_PRUEFUNG, Status.FREIGEGEBEN, Status.MELDUNG_ERFASST])
        for prev, rec in zip([Status.EINGEGANGEN] + path, changes):
            self.assertEqual(rec["before"]["status"], prev)
        self.assertEqual(store.audit.count(case_id=case.case_id, event_type=A.DECISION), 1)
        self.assertEqual(store.audit.count(case_id=case.case_id, event_type=A.SUBMISSION_RECORDED), 1)
        self.assertTrue(store.audit.verify().ok)

    def test_metadata_edit_logs_user_old_and_new_value(self) -> None:
        store, _, clock, wf, case = draft_ready()
        with self.assertRaises(ReasonRequired):
            wf.edit_metadata(case.case_id, ANALYST, "lot", "OA-K36-2508-A", "", now=clock.now())
        with self.assertRaises(PermissionDenied):
            wf.edit_metadata(case.case_id, VIEWER, "lot", "OA-K36-2508-A", "typo", now=clock.now())
        wf.edit_metadata(case.case_id, ANALYST, "lot", "OA-K36-2508-A", "confirmed with the clinic", now=clock.now())
        wf.edit_metadata(case.case_id, ANALYST, "lot", "OA-K36-2508-B", "second correction", now=clock.now())
        edits = store.list_metadata_edits(case.case_id)
        self.assertEqual([(e.old_value, e.new_value, e.user_id) for e in edits],
                         [(None, "OA-K36-2508-A", "analyst-1"), ("OA-K36-2508-A", "OA-K36-2508-B", "analyst-1")])
        records = store.audit.records(case_id=case.case_id, event_type=A.METADATA_EDITED)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1]["before"], {"lot": "OA-K36-2508-A"})
        self.assertEqual(records[1]["reason"], "second correction")


class Findings(unittest.TestCase):
    def test_only_the_reviewer_withdraws_findings_and_closes_process_findings(self) -> None:
        store, engine, clock, wf, case = draft_ready()
        wf.approve(case.case_id, REVIEWER, "ok", now=clock.now())
        finding = store.list_findings(kind="approved_assessment")[0]
        with self.assertRaises(PermissionDenied):
            wf.update_finding_status(finding.finding_id, ANALYST, "withdrawn", "wrong", now=clock.now())
        self.assertEqual(wf.update_finding_status(finding.finding_id, REVIEWER, "withdrawn", "new evidence",
                                                  now=clock.now()).status, "withdrawn")
        created = wf.import_process_findings(engine.ref.process_findings(), SYSTEM, now=clock.now())
        self.assertEqual([f.status for f in created], ["Open", "Open", "Open"])
        self.assertEqual(wf.import_process_findings(engine.ref.process_findings(), SYSTEM, now=clock.now()), [])
        closed = wf.update_finding_status(created[0].finding_id, REVIEWER, "Closed", "handled", now=clock.now())
        self.assertEqual(closed.status, "Closed")


if __name__ == "__main__":
    unittest.main()
