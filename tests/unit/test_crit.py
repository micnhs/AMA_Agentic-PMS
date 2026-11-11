"""CRIT-1.0: raw-text scan per language, severity values, raise-only urgency."""
from __future__ import annotations

import datetime as dt
import unittest

from pms.models import UTC, Role
from pms.rules import crit
from pms.rules.extract import extract

from ._support import letter_text, ref_data

NOW = dt.datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def scan(text: str) -> crit.CritScan:
    return crit.scan(text, extract(text, ref_data().catalogue))


class DeathTermsPerLanguage(unittest.TestCase):
    CASES = {
        "de-exitus-letalis": "Am 08.11.2026 trat der Exitus letalis ein.",
        "de-verstorben": "Der Patient ist am 3. November 2026 verstorben.",
        "de-tod": "Der Tod trat am Folgetag ein.",
        "de-verstarb": "Die Patientin verstarb auf der Intensivstation.",
        "en-died": "On 6 November 2026 the patient collapsed on the ward and died.",
        "en-death": "We report the death of a patient.",
        "fr-decede": "Le patient est décédé le 4 novembre 2026.",
        "fr-deces": "Nous déplorons le décès du patient.",
        "it-deceduto": "Il paziente è deceduto il 5 novembre 2026.",
        "it-decesso": "Si segnala il decesso della paziente.",
    }

    def test_each_language_term_gives_death(self) -> None:
        for name, sentence in self.CASES.items():
            with self.subTest(name):
                result = scan(sentence)
                self.assertEqual(result.severity, crit.DEATH)
                self.assertTrue(result.critical)

    def test_letter_with_exitus_letalis_as_only_death_term(self) -> None:
        result = scan(letter_text("L01"))
        self.assertEqual(result.severity, crit.DEATH)
        death_matches = [i.match for i in result.indicators if i.group == crit.DEATH]
        self.assertEqual(death_matches, ["Exitus letalis"])
        self.assertEqual(next(i.line for i in result.indicators if i.group == crit.DEATH), 14)


class OtherGroups(unittest.TestCase):
    def test_serious_deterioration_terms(self) -> None:
        for sentence in (
            "Er wurde am 09.11.2026 stationär aufgenommen.",
            "Am 20.11.2026 erfolgte eine Revisionsoperation.",
            "The patient was admitted and underwent revision surgery.",
            "He needed intensive care treatment for five days.",
            "Le patient a été hospitalisé le 06/11/2026.",
            "Il paziente è stato ricoverato in terapia intensiva.",
        ):
            with self.subTest(sentence):
                self.assertEqual(scan(sentence).severity, crit.SD)

    def test_public_health_threat_by_cluster_statement(self) -> None:
        self.assertEqual(scan("In our view this cluster may represent a risk to other patients.").severity, crit.PHT)

    def test_public_health_threat_by_several_patients_with_one_lot(self) -> None:
        text = "Patients P-IE-1102 and P-IE-1107 received valves from LOT CB21-2607-A. Both are well."
        result = scan(text)
        self.assertEqual(result.severity, crit.PHT)
        self.assertIn("several-patients-one-lot", {i.term_id for i in result.indicators})

    def test_l03_cluster_letter_is_public_health_threat_with_serious_deterioration(self) -> None:
        result = scan(letter_text("L03"))
        self.assertEqual(result.severity, crit.PHT)
        self.assertEqual(result.groups, frozenset({crit.PHT, crit.SD}))

    def test_none_detected_vs_unknown(self) -> None:
        self.assertEqual(scan(letter_text("L06")).severity, crit.NONE_DETECTED)  # device identified, no indicator
        self.assertEqual(scan(letter_text("L11")).severity, crit.NONE_DETECTED)
        self.assertEqual(scan(letter_text("L09")).severity, crit.UNKNOWN)  # essential data missing

    def test_no_negation_logic_in_doubt_critical(self) -> None:
        self.assertEqual(scan("Der Patient ist nicht verstorben.").severity, crit.DEATH)

    def test_word_boundaries_avoid_false_hits(self) -> None:
        for sentence in ("Laut Operationsbericht wurde eine Pfanne implantiert.",
                         "Angaben zum Operationsdatum liegen nicht vor.",
                         "Il paziente verrà ricontrollato in ambulatorio.",
                         "Wir melden dies nur zur Information."):
            with self.subTest(sentence):
                self.assertFalse(scan(sentence).critical)


class UrgencyState(unittest.TestCase):
    def test_raise_is_idempotent_and_never_lowers(self) -> None:
        state, became = crit.raise_urgency(crit.UrgencyState(), reason="r1", source="rule", at=NOW)
        self.assertTrue(state.critical and became)
        state2, became2 = crit.raise_urgency(state, reason="r2", source="rule", at=NOW)
        self.assertTrue(state2.critical)
        self.assertFalse(became2)

    def test_lowering_requires_medical_reviewer_and_reason(self) -> None:
        state, _ = crit.raise_urgency(crit.UrgencyState(), reason="r", source="rule", at=NOW)
        with self.assertRaises(crit.UrgencyLoweringRefused):
            crit.lower_urgency(state, actor_id="a", actor_roles=[Role.ANALYST], reason="unrelated", at=NOW)
        with self.assertRaises(crit.UrgencyLoweringRefused):
            crit.lower_urgency(state, actor_id="r", actor_roles=[Role.MEDICAL_REVIEWER], reason="  ", at=NOW)
        lowered = crit.lower_urgency(state, actor_id="r", actor_roles=[Role.MEDICAL_REVIEWER],
                                     reason="appendicitis, prosthesis normal", at=NOW)
        self.assertFalse(lowered.critical)
        self.assertEqual(lowered.lowered[-1]["by"], "r")

    def test_ai_reading_can_raise_but_never_lower(self) -> None:
        critical, _ = crit.raise_urgency(crit.UrgencyState(), reason="rule", source="rule", at=NOW)
        same, outcome = crit.apply_ai_reading(critical, crit.NONE_DETECTED, agent_id="A3", at=NOW)
        self.assertEqual(outcome, "ignored_lower")
        self.assertTrue(same.critical)
        raised, outcome2 = crit.apply_ai_reading(crit.UrgencyState(), crit.DEATH, agent_id="A1", at=NOW)
        self.assertEqual(outcome2, "raised")
        self.assertTrue(raised.critical)

    def test_48_hour_window(self) -> None:
        due = dt.datetime(2026, 11, 18, 23, 59, 59, tzinfo=UTC)
        self.assertFalse(crit.deadline_window_hit(due, dt.datetime(2026, 11, 16, 23, 59, 58, tzinfo=UTC)))
        self.assertTrue(crit.deadline_window_hit(due, dt.datetime(2026, 11, 16, 23, 59, 59, tzinfo=UTC)))
        self.assertTrue(crit.deadline_window_hit(due, dt.datetime(2026, 11, 20, tzinfo=UTC)))  # overdue


if __name__ == "__main__":
    unittest.main()
