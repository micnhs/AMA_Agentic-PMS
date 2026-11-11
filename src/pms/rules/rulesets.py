"""RS-1.0 - rule set and authority label per incident state (concept 6.3; D-11).

Incident state (assumption A-07): the place stated in the letter, otherwise the implant
site in the registry, otherwise the sender country; a human confirms. Seed messages
carry a structured ``market`` field that is used before the sender country.

* EU member states: ``EU-MDR:<state>`` with the authority of the incident state
  (MDR Art. 92(5), source MDR-92-5; MDR Art. 87(1), MDR-87-1; regulatory analysis
  p. 6 and 9, RA-P6, RA-P9).
* Northern Ireland: ``EU-MDR:NI`` - MHRA, MORE portal with the EU form (regulatory
  analysis p. 13, RA-P13; 03-scenario.md lines 192, 200, 205, SCN-GBNI).
* England, Scotland, Wales: ``UK-MDR-PART4A:GB`` - MHRA, MORE portal with the GB form
  (UK MDR Part 4A, annotation F349, UK-PART4A; reg. 44ZD, UK-44ZD; regulatory analysis
  p. 5 and 12, RA-P5, RA-P12). Units placed on the GB market before 16 June 2025 (or
  with unknown date) are provisionally also Part 4A with a check note (assumption A-06;
  GB commencement date conflict K-08, OI-006; 03-scenario.md line 578, SCN-GBDATE).
* GB or NI unclear: both rule sets with their own deadlines (Goal Card QUALITY, GC-GBNI).
* Authorities are labels only; nothing is ever sent (Goal Card QUALITY, GC-AUTH).
  IE HPRA, DE BfArM and IT NSIS-Dispovigilance are published starting points
  (03-scenario.md lines 214-216, SCN-IE, SCN-DE, SCN-IT); the French ANSM manufacturer
  route is not evidenced and shows "route unverified" (line 217, SCN-FR; OI-008).
  States without a sourced route are labelled the same way (unknown stays unknown).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Sequence

from ..knowledge import RegistryEntry
from ..versions import RS
from .extract import Extraction

#: Part 4A applies to devices placed on the GB market from this date (stricter of
#: 16/17 June 2025; RA-P12, UK-PART4A, conflict K-08).
GB_PART4A_FROM = _dt.date(2025, 6, 16)

EU_MEMBER_STATES: frozenset[str] = frozenset(
    "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split()
)

GB_RULE_SET = "UK-MDR-PART4A:GB"
NI_RULE_SET = "EU-MDR:NI"
UNRESOLVED_RULE_SET = "UNRESOLVED"

#: Authority labels (label only) and whether the route is verified.
_AUTHORITY: dict[str, tuple[str, bool]] = {
    "DE": ("DE BfArM (published starting point)", False),  # SCN-DE, GC-AUTH, RA-P6
    "IE": ("IE HPRA (published route)", False),  # SCN-IE, RA-P5, RA-P9, GC-AUTH
    "IT": ("IT Ministero della Salute NSIS-Dispovigilance (published starting point)", False),  # SCN-IT
    "FR": ("FR ANSM (route unverified)", True),  # SCN-FR, GC-AUTH, OI-008
    "GB": ("GB MHRA, MORE portal, GB form", False),  # RA-P5, RA-P6, UK-44ZH-1
    "NI": ("NI MHRA, MORE portal, EU form", False),  # RA-P5, RA-P13, SCN-GBNI
}
UNRESOLVED_AUTHORITY = "Incident state unresolved - a human must confirm the authority (route unverified)"

# Bases of the incident-state decision (A-07 order).
LETTER_UNCERTAIN = "LETTER_UNCERTAIN"
LETTER_EXPLICIT = "LETTER_EXPLICIT"
REGISTRY_IMPLANT_SITE = "REGISTRY_IMPLANT_SITE"
SEED_MARKET = "SEED_MARKET"
SENDER_COUNTRY = "SENDER_COUNTRY"
UNRESOLVED = "UNRESOLVED"


def state_from_registry_country(country: str | None) -> str | None:
    """Map registry country codes (ISO 3166-2 for UK nations) to incident states."""
    if not country:
        return None
    code = country.upper()
    if code == "GB-NIR":
        return "NI"
    if code.startswith("GB") or code == "UK":
        return "GB"
    return code


def state_from_market(market: str | None) -> list[str]:
    if not market:
        return []
    code = market.upper()
    if code == "UK":
        return ["GB", "NI"]
    if code in ("GB-NIR", "NI"):
        return ["NI"]
    if code.startswith("GB"):
        return ["GB"]
    return [code]


def rule_set_for_state(state: str) -> str:
    if state == "GB":
        return GB_RULE_SET
    if state == "NI":
        return NI_RULE_SET
    if state in EU_MEMBER_STATES:
        return f"EU-MDR:{state}"
    return UNRESOLVED_RULE_SET


def authority_for_state(state: str) -> tuple[str, bool]:
    """Authority label and ``route_unverified`` flag for an incident state."""
    if state in _AUTHORITY:
        return _AUTHORITY[state]
    if state in EU_MEMBER_STATES:
        return (f"{state} competent authority of the incident state (route unverified)", True)
    return (UNRESOLVED_AUTHORITY, True)


@dataclass(frozen=True)
class RuleSetAssignment:
    incident_states: tuple[str, ...]
    basis: str
    rule_sets: tuple[str, ...]
    authority_labels: tuple[str, ...]
    route_unverified: bool
    notes: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    provisional: bool = True  # until a human confirms the incident state (A-07)
    rule_version: str = RS

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_states": list(self.incident_states),
            "basis": self.basis,
            "rule_sets": list(self.rule_sets),
            "authority_labels": list(self.authority_labels),
            "route_unverified": self.route_unverified,
            "notes": list(self.notes),
            "evidence": list(self.evidence),
            "provisional": self.provisional,
            "rule_version": self.rule_version,
        }


def _part4a_note(entries: Sequence[RegistryEntry]) -> str | None:
    dates = []
    for e in entries:
        if state_from_registry_country(e.country) == "GB" or (e.market or "").upper() == "GB":
            dates.append(e.placed_on_gb_market)
    if dates and all(d and _dt.date.fromisoformat(d) >= GB_PART4A_FROM for d in dates):
        return None
    return ("UK MDR Part 4A applied provisionally: GB placing-on-market date before 16 June 2025 or unknown "
            "- check (A-06, K-08)")


def assign(extraction: Extraction | None, registry_entries: Sequence[RegistryEntry] = (),
           seed_market: str | None = None) -> RuleSetAssignment:
    """Decide incident state(s), rule set(s) and authority label(s) for one signal.

    ``registry_entries`` are the entries identified by REG-1.0 for this signal (matched
    or mismatched by patient pseudonym) - their implant site is A-07 step 2.
    """
    states: list[str] = []
    basis = UNRESOLVED
    evidence: list[str] = []
    loc = extraction.incident_location if extraction is not None else None
    if loc is not None:
        states, basis = list(loc.states), loc.basis
        evidence.append(f"letter line {loc.line}: {loc.quote}")
    if not states:
        for e in registry_entries:
            st = state_from_registry_country(e.country)
            if st and st not in states:
                states.append(st)
                evidence.append(f"registry {e.reg_id}: implant site country {e.country}")
        if states:
            basis = REGISTRY_IMPLANT_SITE
    if not states and seed_market:
        states = state_from_market(seed_market)
        if states:
            basis = SEED_MARKET
            evidence.append(f"message metadata market = {seed_market}")
    if not states and extraction is not None and extraction.sender_country is not None:
        states = list(extraction.sender_country.states())
        basis = SENDER_COUNTRY
        evidence.append(f"letter line {extraction.sender_country.line}: sender country "
                        f"'{extraction.sender_country.quote}'")
    if not states:
        states = ["UNRESOLVED"]
        basis = UNRESOLVED

    rule_sets: list[str] = []
    labels: list[str] = []
    unverified = False
    for st in states:
        rs = rule_set_for_state(st)
        label, unv = authority_for_state(st)
        if rs not in rule_sets:
            rule_sets.append(rs)
            labels.append(label)
        unverified = unverified or unv
    notes: list[str] = []
    if GB_RULE_SET in rule_sets:
        note = _part4a_note(registry_entries)
        if note:
            notes.append(note)
    if basis == UNRESOLVED:
        notes.append("Incident state unknown: deadlines are tracked provisionally; a human must confirm the state.")
    return RuleSetAssignment(tuple(states), basis, tuple(rule_sets), tuple(labels), unverified,
                             tuple(notes), tuple(evidence))
