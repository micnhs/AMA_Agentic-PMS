"""CRIT-1.0 - critical detection (concept section 6.3; design element D-09).

Fixed raw-text scan in DE, EN, FR and IT for three indicator groups:

* DEATH - e.g. 'verstorben', 'Tod', 'Exitus letalis', 'died', 'décédé', 'deceduto'
  (MDR Art. 2(65)(a), source MDR-2-65; UK MDR reg. 44ZC, source UK-44ZC);
* PUBLIC_HEALTH_THREAT - at least two patients with the same lot or an explicit cluster
  statement (MDR Art. 2(65)(c) and 2(66), MDR-2-65; MDCG 2023-3 Rev. 2 Q2, MDCG-Q2);
* SERIOUS_DETERIORATION - e.g. hospitalisation, revision surgery, intensive care,
  life-threatening (MDR Art. 2(65)(b), MDR-2-65; MDCG 2023-3 Rev. 2 Q2 "serious
  deterioration incl. hospitalisation and surgical intervention", MDCG-Q2; UK-44ZC).

Additionally critical: the earliest open deadline is due within at most 48 hours
(pilot window kept by decision Q-3; 02-assumptions.md line 648, ASM-SENT "time-critical
deadline loss -> immediate human interruption").

No negation logic: in doubt the case is critical (concept 6.3). The AI reading (A1, A3)
is a second detector and may only raise. Urgency is lowered only by a human medical
reviewer with a reason (Goal Card QUALITY, GC-CRIT). The role check itself is done by
:mod:`pms.workflow`; :func:`lower_urgency` refuses independently as a second barrier.

Severity values follow expected.json ``conventions.severity``: DEATH | PUBLIC_HEALTH_THREAT
| SERIOUS_DETERIORATION | NONE_DETECTED (no indicator in the raw text - not "non-serious")
| UNKNOWN (essential data missing; source GC-UNK, SCN-NULL).
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable

from ..models import Role, Severity, SERIOUS_SEVERITIES, iso_utc
from ..versions import CRIT
from .extract import Extraction, line_of, sentence_at

DEATH = Severity.DEATH.value
PHT = Severity.PUBLIC_HEALTH_THREAT.value
SD = Severity.SERIOUS_DETERIORATION.value
NONE_DETECTED = Severity.NONE_DETECTED.value
UNKNOWN = Severity.UNKNOWN.value

#: Pilot window of CRIT-1.0: earliest deadline within at most 48 hours (decision Q-3).
DEADLINE_WINDOW_HOURS = 48

#: Precedence of the case severity label (order of expected.json conventions).
SEVERITY_ORDER: tuple[str, ...] = (DEATH, PHT, SD)
_RANK = {DEATH: 3, PHT: 2, SD: 1}

# --------------------------------------------------------------------------- term lists
#: (group, language, term id, regex). Word boundaries avoid matches inside unrelated
#: words (e.g. "Operationsbericht" is not a surgical intervention, "ricontrollato" is
#: not a hospital admission).
_TERMS: tuple[tuple[str, str, str, str], ...] = (
    # ---- death: MDR Art. 2(65)(a) (MDR-2-65); UK MDR reg. 44ZC (UK-44ZC); concept 6.3 term examples
    (DEATH, "de", "verstorben", r"\bverstorben\w*|\bverstarb\b|\bgestorben\b|\bstarb\b"),
    (DEATH, "de", "tod", r"\btod(?:es|e)?\b|\btodes\w+|\btödlich\w*|\bableben\b"),
    (DEATH, "de", "exitus", r"\bexitus(?:\s+letalis)?\b|\bad\s+exitum\b"),
    (DEATH, "en", "died", r"\bdied\b|\bdeath\b|\bdead\b|\bdeceased\b|\bpassed\s+away\b|\bfatal(?:ity|ities)?\b"),
    (DEATH, "fr", "deces", r"\bdécéd(?:é|ée|és|ées|er)\b|\bdécès\b|\bmort(?:e|s|es)?\b|\bsuccomb(?:é|ée|er)\b"),
    (DEATH, "it", "deceduto", r"\bdecedut[oaie]\b|\bdecesso\b|\bmort[oaie]\b|\bmorte\b|\bvenut[oa]\s+a\s+mancare\b"),
    # ---- serious public health threat: MDR Art. 2(65)(c), 2(66) (MDR-2-65); MDCG 2023-3 Q2 (MDCG-Q2)
    (PHT, "en", "cluster", r"\bcluster\b|\bpublic\s+health\b|\brisk\s+to\s+other\s+patients\b|\b(?:several|multiple)\s+patients\b"),
    (PHT, "de", "cluster", r"\bcluster\b|\bhäufung\b|\böffentliche[n]?\s+gesundheit\b|\bmehrere(?:n)?\s+patient(?:en|innen)\b|\bweitere\s+patient(?:en|innen)\s+gefährdet\b"),
    (PHT, "fr", "cluster", r"\bcluster\b|\bregroupement\b|\bsanté\s+publique\b|\bplusieurs\s+patients\b|\brisque\s+pour\s+d'autres\s+patients\b"),
    (PHT, "it", "cluster", r"\bcluster\b|\bsalute\s+pubblica\b|\b(?:più|diversi|vari)\s+pazienti\b|\brischio\s+per\s+altri\s+pazienti\b"),
    # ---- serious deterioration incl. hospitalisation and surgical intervention: MDR Art. 2(65)(b)
    #      (MDR-2-65); MDCG 2023-3 Rev. 2 Q2 (MDCG-Q2); UK MDR reg. 44ZC (UK-44ZC)
    (SD, "de", "stationaer", r"\bstationär\w*\s+(?:aufgenommen|aufnahme|behandelt|eingewiesen)\b|\bstationäre[nr]?\s+aufnahme\b|\bhospitalisiert\b|\bkrankenhausaufnahme\b|\bnotaufnahme\b"),
    (SD, "de", "revision", r"\brevision\w*|\bre-?operation\w*|\bwechseloperation\w*|\bnotoperation\w*|\bexplantation\b|\bexplantiert\b"),
    (SD, "de", "intensiv", r"\bintensivstation\w*|\bintensivmedizinisch\w*|\blebensbedrohlich\w*|\bbleibende[nr]?\s+schädigung\b|\bdauerhafte[nr]?\s+beeinträchtigung\b"),
    (SD, "en", "hospital", r"\b(?:re-?)?admitted\b|\b(?:re-?)?admission\b|\bhospitali[sz](?:ed|ation)\b|\bemergency\s+department\b"),
    (SD, "en", "revision", r"\brevision\b|\bredo\s+(?:surgery|operation)\b|\bre-?operation\b|\bexplant(?:ed|ation)\b|\bemergency\s+surgery\b|\bsurgical\s+intervention\b"),
    (SD, "en", "intensive", r"\bintensive\s+care\b|\bICU\b|\blife[- ]threatening\b|\bpermanent\s+(?:impairment|damage)\b"),
    (SD, "fr", "hospital", r"\bhospitalis(?:é|ée|és|ées|ation)\b|\badmis(?:e)?\s+(?:aux\s+urgences|en\s+réanimation|à\s+l'hôpital)\b|\burgences\b"),
    (SD, "fr", "revision", r"\bréintervention\b|\breprise\s+chirurgicale\b|\brévision\b|\bexplantation\b|\bréopér\w+"),
    (SD, "fr", "intensive", r"\bréanimation\b|\bsoins\s+intensifs\b|\bpronostic\s+vital\b"),
    (SD, "it", "hospital", r"\bricoverat[oaie]\b|\bricovero\b|\bospedalizzat[oaie]\b|\bospedalizzazione\b|\bpronto\s+soccorso\b"),
    (SD, "it", "revision", r"\breintervento\b|\brevisione\s+chirurgica\b|\bintervento\s+di\s+revisione\b|\bespianto\b|\bespiantat[oaie]\b"),
    (SD, "it", "intensive", r"\bterapia\s+intensiva\b|\brianimazione\b|\bpericolo\s+di\s+vita\b"),
)
_COMPILED = tuple((g, lang, tid, re.compile(rx, re.IGNORECASE)) for g, lang, tid, rx in _TERMS)


@dataclass(frozen=True)
class Indicator:
    group: str
    language: str
    term_id: str
    match: str
    quote: str
    line: int
    start: int

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class CritScan:
    """Result of the CRIT-1.0 raw-text scan of one signal."""

    severity: str
    groups: frozenset[str]
    indicators: tuple[Indicator, ...]
    rule_version: str = CRIT

    @property
    def critical(self) -> bool:
        return self.severity in SERIOUS_SEVERITIES

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "groups": sorted(self.groups, key=SEVERITY_ORDER.index),
            "critical": self.critical,
            "indicators": [i.to_dict() for i in self.indicators],
            "rule_version": self.rule_version,
        }


def scan(raw_text: str, extraction: Extraction | None = None) -> CritScan:
    """Scan the full raw text (never the masked text) for CRIT-1.0 indicators.

    ``extraction`` adds the structural public-health-threat check (at least two distinct
    patient pseudonyms or a stated patient count of two or more, together with one lot or
    a "same lot" statement) and decides between NONE_DETECTED and UNKNOWN.
    """
    indicators: list[Indicator] = []
    for group, lang, term_id, pattern in _COMPILED:
        for m in pattern.finditer(raw_text):
            indicators.append(
                Indicator(group, lang, term_id, m.group(0), sentence_at(raw_text, m.start()),
                          line_of(raw_text, m.start()), m.start())
            )
    if extraction is not None:
        several = len(extraction.patient_refs) >= 2 or (extraction.patient_count or 0) >= 2
        one_lot = len(extraction.lots) == 1 or extraction.same_lot_phrase is not None
        if several and one_lot:
            anchor = extraction.same_lot_phrase or (extraction.lots[0] if extraction.lots else extraction.patient_refs[0])
            indicators.append(
                Indicator(PHT, extraction.language or "", "several-patients-one-lot",
                          f"{max(len(extraction.patient_refs), extraction.patient_count or 0)} patients, one lot",
                          anchor.quote, anchor.line, anchor.start)
            )
    unique: dict[tuple[str, int], Indicator] = {}
    for ind in sorted(indicators, key=lambda i: (i.start, i.group)):
        unique.setdefault((ind.group, ind.start), ind)  # same hit from several language lists
    indicators = list(unique.values())
    groups = frozenset(i.group for i in indicators)
    has_ids = extraction.has_device_identifiers if extraction is not None else True
    return CritScan(classify(groups, essential_data_missing=not has_ids), groups, tuple(indicators))


def classify(groups: Iterable[str], *, essential_data_missing: bool) -> str:
    """Severity label from indicator groups (DEATH > PHT > SD), else NONE_DETECTED/UNKNOWN."""
    found = set(groups)
    for sev in SEVERITY_ORDER:
        if sev in found:
            return sev
    return UNKNOWN if essential_data_missing else NONE_DETECTED


def more_serious(a: str, b: str) -> str:
    """The more serious of two severity labels (serious labels by rank)."""
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


def deadline_window_hit(due_end_utc: _dt.datetime, now: _dt.datetime, hours: int = DEADLINE_WINDOW_HOURS) -> bool:
    """True when the deadline ends within ``hours`` (or is already past)."""
    return due_end_utc - now <= _dt.timedelta(hours=hours)


# --------------------------------------------------------------------------- urgency state
class UrgencyLoweringRefused(PermissionError):
    """Raised when anybody but a medical reviewer with a reason tries to lower urgency."""


@dataclass(frozen=True)
class UrgencyState:
    """Case urgency. Code and AI can only raise it; only a human medical reviewer lowers it."""

    critical: bool = False
    reasons: tuple[str, ...] = ()
    window_raised_for: tuple[str, ...] = ()
    lowered: tuple[dict[str, Any], ...] = ()
    history: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "critical": self.critical,
            "reasons": list(self.reasons),
            "window_raised_for": list(self.window_raised_for),
            "lowered": [dict(x) for x in self.lowered],
            "history": [dict(x) for x in self.history],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "UrgencyState":
        data = data or {}
        return cls(
            critical=bool(data.get("critical", False)),
            reasons=tuple(data.get("reasons", ())),
            window_raised_for=tuple(data.get("window_raised_for", ())),
            lowered=tuple(data.get("lowered", ())),
            history=tuple(data.get("history", ())),
        )


def raise_urgency(state: UrgencyState, *, reason: str, source: str, at: _dt.datetime,
                  window_key: str | None = None) -> tuple[UrgencyState, bool]:
    """Raise urgency to critical (code, AI or a human). Never lowers.

    Returns the new state and whether the case became critical by this call.
    """
    became = not state.critical
    entry = {"action": "raise", "reason": reason, "source": source, "at": iso_utc(at, ms=True)}
    new = replace(
        state,
        critical=True,
        reasons=state.reasons + ((reason,) if reason not in state.reasons else ()),
        window_raised_for=state.window_raised_for + ((window_key,) if window_key and window_key not in state.window_raised_for else ()),
        history=state.history + (entry,),
    )
    return new, became


def apply_ai_reading(state: UrgencyState, ai_severity: str | None, *, agent_id: str,
                     at: _dt.datetime) -> tuple[UrgencyState, str]:
    """Second detector: an AI reading may raise urgency but never lower it.

    Returns the new state and an outcome: ``raised``, ``unchanged`` or ``ignored_lower``
    (e.g. an AI answer "not serious" for a case the rule flagged critical).
    """
    if ai_severity in SERIOUS_SEVERITIES:
        if state.critical:
            return state, "unchanged"
        new, _ = raise_urgency(state, reason=f"AI:{agent_id}:{ai_severity}", source="agent", at=at)
        return new, "raised"
    return state, ("ignored_lower" if state.critical else "unchanged")


def lower_urgency(state: UrgencyState, *, actor_id: str, actor_roles: Iterable[str], reason: str,
                  at: _dt.datetime) -> UrgencyState:
    """Lower urgency - only a human medical reviewer with a non-empty reason.

    The workflow performs the permission check first; this function refuses again so
    that no code path (agent, system, timer) can lower urgency by calling it directly.
    """
    if Role.MEDICAL_REVIEWER.value not in set(actor_roles):
        raise UrgencyLoweringRefused("only the medical reviewer may lower urgency")
    if not reason or not reason.strip():
        raise UrgencyLoweringRefused("a reason is mandatory to lower urgency")
    entry = {"action": "lower", "reason": reason.strip(), "by": actor_id, "at": iso_utc(at, ms=True)}
    return replace(state, critical=False, lowered=state.lowered + (entry,), history=state.history + (entry,))
