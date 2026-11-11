"""Rule pipeline end to end on the store: anchors, injection copy, registry visibility,
triage before analysis, the 48-hour window and the correlation queries."""
from __future__ import annotations

import unittest

from pms import audit as A
from pms.models import SignalInput, Status
from pms.rules import deadlines as dl
from pms.workflow import TASK_TRIAGE, TASK_UNMATCHED, TASK_UNREADABLE

from ._support import REVIEWER, new_engine, process

LABEL_KEYS = ("critical", "severity", "rule_set", "authority_label", "deadline_start", "deadline_days",
              "deadline_candidates", "driving_due_date", "route_unverified", "registry_check", "event_tags")


class FollowUpKeepsTheAnchor(unittest.TestCase):
    def test_follow_up_does_not_reset_the_anchor(self) -> None:
        store, engine, clock = new_engine()
        first = process(engine, clock, "L09")
        self.assertFalse(first.critical)
        before = engine.case_labels(first.case_id, first.signal_id)
        self.assertEqual(before["deadline_candidates"][0]["due_date"], "2026-12-02")

        second = process(engine, clock, "L10")
        self.assertEqual(second.case_id, first.case_id)
        self.assertEqual(second.relation, "follow_up_of:L09")
        self.assertTrue(second.deadline_not_reset)
        labels = engine.case_labels(second.case_id, second.signal_id)
        self.assertEqual(labels["awareness_anchor"], "2026-11-17T11:00:00Z")
        self.assertEqual(labels["anchor_ref"], "L09")
        self.assertEqual(labels["deadline_start"], "2026-11-18")
        self.assertEqual([c["basis"] for c in labels["deadline_candidates"]],
                         [dl.PROVISIONAL_REPORTABILITY_UNCLEAR, dl.SERIOUS_DETERIORATION])
        self.assertEqual(labels["driving_due_date"], "2026-11-27")  # earliest wins
        self.assertTrue(labels["critical"])
        self.assertEqual(store.get_case(second.case_id).status, Status.TRIAGE_ERFORDERLICH)


class InjectionCopy(unittest.TestCase):
    def labels_alone(self, letter: str) -> dict:
        _, engine, clock = new_engine()
        result = process(engine, clock, letter, "2026-11-16T09:30:00Z")
        engine.run_correlation(result.case_id, result.signal_id)
        return engine.case_labels(result.case_id, result.signal_id)

    def test_injected_copy_gets_identical_labels_and_only_adds_the_flag(self) -> None:
        clean, injected = self.labels_alone("L02"), self.labels_alone("L12")
        for key in LABEL_KEYS:
            with self.subTest(key):
                self.assertEqual(clean[key], injected[key])
        self.assertFalse(clean["injection_flag"])
        self.assertTrue(injected["injection_flag"])
        self.assertEqual(clean["correlation_refs"], injected["correlation_refs"])

    def test_injected_copy_links_as_duplicate_without_resetting_the_deadline(self) -> None:
        store, engine, clock = new_engine()
        l02 = process(engine, clock, "L02")
        l12 = process(engine, clock, "L12")
        self.assertEqual((l12.case_id, l12.relation, l12.injection_flag), (l02.case_id, "duplicate_of:L02", True))
        self.assertTrue(l12.deadline_not_reset)
        self.assertEqual(engine.case_labels(l12.case_id)["driving_due_date"], "2026-11-26")

    def test_injected_text_never_reaches_a_query(self) -> None:
        _, engine, clock = new_engine()
        result = process(engine, clock, "L12")
        corr = engine.run_correlation(result.case_id, result.signal_id)
        queries = repr(corr.queries)
        self.assertNotIn("example.org", queries)
        self.assertNotIn("ignore", queries.lower())
        self.assertTrue(all(q.get("text") in (None, "fracture stem") for q in corr.queries if q["index"] == "knowledge"))


class Visibility(unittest.TestCase):
    def test_registry_mismatch_stays_visible(self) -> None:
        store, engine, clock = new_engine()
        result = process(engine, clock, "L11")
        self.assertTrue(result.registry_check.startswith("MISMATCH:REG-0012"))
        tasks = store.list_tasks(case_id=result.case_id, kind=TASK_UNMATCHED, status="OPEN")
        self.assertEqual(len(tasks), 1)
        self.assertIn("lot OA-C54-2601-B not in registry", tasks[0].detail["registry_check"])
        self.assertFalse(result.critical)

    def test_no_identifiers_stays_visible(self) -> None:
        store, engine, clock = new_engine()
        result = process(engine, clock, "L09")
        self.assertEqual(result.registry_check, "NO_IDENTIFIERS")
        self.assertEqual(len(store.list_tasks(case_id=result.case_id, kind=TASK_UNMATCHED)), 1)
        corr = engine.run_correlation(result.case_id, result.signal_id)
        self.assertEqual(corr.status, "NOT_RUN_NO_IDENTIFIERS")
        self.assertIn("missing data stay visible", corr.note)

    def test_unreadable_input_is_visible_and_deadlines_are_tracked(self) -> None:
        store, engine, clock = new_engine()
        result = engine.process_signal(SignalInput(text="", received_at="2026-11-16T08:00:00Z", readable=False,
                                                   filename="scan.pdf"))
        case = store.get_case(result.case_id)
        self.assertEqual(case.status, Status.NICHT_LESBAR)
        self.assertEqual(case.severity, "UNKNOWN")
        self.assertEqual(len(store.list_tasks(case_id=case.case_id, kind=TASK_UNREADABLE)), 1)
        self.assertEqual(engine.case_labels(case.case_id)["deadline_candidates"][0]["rule_set"], "UNRESOLVED")


class TriageBeforeAnalysis(unittest.TestCase):
    def test_critical_letter_creates_triage_task_and_alert_immediately(self) -> None:
        sent = []
        store, engine, clock = new_engine(alert_sink=lambda alert, case: sent.append((alert.alert_id, case.case_id)))
        result = process(engine, clock, "L01")
        case = store.get_case(result.case_id)
        self.assertEqual(case.status, Status.TRIAGE_ERFORDERLICH)
        self.assertEqual(case.analysis_state, "PENDING")  # no agent has run yet
        self.assertEqual(len(store.list_tasks(case_id=case.case_id, kind=TASK_TRIAGE, status="OPEN")), 1)
        self.assertEqual(len(store.list_alerts(case_id=case.case_id, status="PENDING")), 1)
        self.assertEqual(sent, [(store.list_alerts(case_id=case.case_id)[0].alert_id, case.case_id)])

    def test_critical_copy_on_an_already_critical_case_gets_its_own_task_and_alert(self) -> None:
        sent = []
        store, engine, clock = new_engine(alert_sink=lambda alert, case: sent.append(alert.alert_id))
        l02 = process(engine, clock, "L02")
        due = engine.case_labels(l02.case_id)["driving_due_date"]
        l12 = process(engine, clock, "L12")
        self.assertEqual((l12.case_id, l12.critical, l12.newly_critical), (l02.case_id, True, False))
        case = store.get_case(l02.case_id)
        self.assertEqual((case.status, case.critical), (Status.TRIAGE_ERFORDERLICH, True))
        tasks = store.list_tasks(case_id=case.case_id, kind=TASK_TRIAGE, status="OPEN")
        self.assertEqual([t.detail["signal_id"] for t in tasks], [l02.signal_id, l12.signal_id])
        self.assertIn(tasks[1].task_id, l12.tasks)
        alerts = store.list_alerts(case_id=case.case_id, status="PENDING")
        self.assertEqual([a.detail["signal_id"] for a in alerts], [l02.signal_id, l12.signal_id])
        self.assertEqual(sent, [a.alert_id for a in alerts])
        self.assertIn("new signal on critical case CASE-L02", alerts[1].reason)
        self.assertIn("injection flag raised (INJ-1.0)", alerts[1].reason)
        self.assertNotIn("injection flag", alerts[0].reason)
        self.assertTrue(l12.deadline_not_reset)
        self.assertEqual(engine.case_labels(l12.case_id)["driving_due_date"], due)
        self.assertEqual(store.audit.count(case_id=case.case_id, event_type=A.ALERT_REQUESTED), 2)
        self.assertEqual(store.audit.count(case_id=case.case_id, event_type=A.URGENCY_RAISED), 1)

    def test_non_critical_letter_waits_for_analysis(self) -> None:
        store, engine, clock = new_engine()
        result = process(engine, clock, "L06")
        self.assertEqual(store.get_case(result.case_id).status, Status.EINGEGANGEN)
        self.assertEqual(store.list_alerts(case_id=result.case_id), [])


class DeadlineWindow(unittest.TestCase):
    def test_window_raises_once_and_a_human_lowering_stays(self) -> None:
        store, engine, clock = new_engine()
        result = process(engine, clock, "L06")  # provisional 15 days, due 2026-12-01
        self.assertFalse(engine.check_deadline_window(result.case_id, now=clock.now()).critical)
        clock.set("2026-11-30T00:00:01Z")  # less than 48 h before 2026-12-01T23:59:59
        case = engine.check_deadline_window(result.case_id, now=clock.now())
        self.assertTrue(case.critical)
        self.assertEqual(case.status, Status.TRIAGE_ERFORDERLICH)
        engine.workflow.lower_urgency(result.case_id, REVIEWER, "reviewed: minor, report prepared", now=clock.now())
        clock.set("2026-11-30T12:00:00Z")
        self.assertFalse(engine.check_deadline_window(result.case_id, now=clock.now()).critical)
        self.assertEqual(engine.check_all_deadline_windows(now=clock.now()), [])


class Correlation(unittest.TestCase):
    def test_lot_cluster_finds_corpus_and_registry_patients(self) -> None:
        _, engine, clock = new_engine()
        result = process(engine, clock, "L03")
        corr = engine.run_correlation(result.case_id, result.signal_id)
        refs = set(corr.refs)
        self.assertTrue({"TD-001", "H-001", "LIT-001", "REG:REG-0013", "REG:REG-0014",
                         "DOS:demo-cardio-b:CV-01"} <= refs)
        self.assertNotIn("REG:REG-0003", refs)  # the letter's own registry patients are not "further" patients
        self.assertEqual(corr.counts["registry_same_lot"], 2)
        self.assertIn("never means all-clear", corr.note)

    def test_unrelated_event_has_no_event_group(self) -> None:
        _, engine, clock = new_engine()
        result = process(engine, clock, "L07")
        corr = engine.run_correlation(result.case_id, result.signal_id)
        self.assertEqual(corr.event_tags, ())
        self.assertFalse(any("family_event_term" in h.match_types or "dossier_listed_side_effect" in h.match_types
                             for h in corr.hits))


if __name__ == "__main__":
    unittest.main()


class LinkedSignalsAddNoNoise(unittest.TestCase):
    def test_cover_note_without_indicators_adds_no_provisional_deadline_to_a_serious_case(self) -> None:
        store, engine, clock = new_engine()
        first = process(engine, clock, "L02")
        clock.set("2026-11-17T10:00:00Z")
        note = engine.process_signal(SignalInput(
            text="Our ref: DOCW/TO/2026/118. Please find the radiology report enclosed.",
            received_at="2026-11-17T10:00:00Z", ref="cover-note"), now=clock.now())
        self.assertEqual((note.case_id, note.relation), (first.case_id, "duplicate_of:L02"))
        labels = engine.case_labels(first.case_id)
        self.assertEqual([(c["rule_set"], c["basis"]) for c in labels["deadline_candidates"]],
                         [("UK-MDR-PART4A:GB", "DEATH")])
        self.assertEqual(labels["rule_set"], ["UK-MDR-PART4A:GB"])  # no UNRESOLVED placeholder added


class HumanAddsSignal(unittest.TestCase):
    def test_person_attaches_a_signal_to_a_chosen_case_and_the_anchor_stays(self) -> None:
        from ._support import ANALYST, VIEWER
        from pms.workflow import PermissionDenied

        store, engine, clock = new_engine()
        first = process(engine, clock, "L06")
        clock.set("2026-11-20T10:00:00Z")
        note = SignalInput(text="Telefonnotiz: Patient berichtet weiterhin über Geräusche.",
                           received_at="2026-11-20T10:00:00Z", ref="phone-note-1")
        with self.assertRaises(PermissionDenied):
            engine.process_signal(note, actor=VIEWER, target_case_id=first.case_id, now=clock.now())
        added = engine.process_signal(note, actor=ANALYST, target_case_id=first.case_id, now=clock.now())
        self.assertEqual((added.case_id, added.relation), (first.case_id, "follow_up_of:L06"))
        self.assertTrue(added.deadline_not_reset)
        signal = store.get_signal(added.signal_id)
        self.assertEqual(signal.link["matched_on"], ["human_assignment"])
        self.assertEqual(engine.case_labels(first.case_id)["awareness_anchor"], "2026-11-16T14:55:00Z")
        self.assertEqual(store.audit.records(event_type="SIGNAL_ADDED")[-1]["actor_id"], ANALYST.actor_id)
