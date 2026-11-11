"""LINK-1.0 - duplicates and follow-ups (concept section 6.3; design element D-12).

* Duplicate: same sender reference, or same patient pseudonym with the same product and
  event; an identical text is a duplicate as well.
* Follow-up: a follow-up marker ('Nachtrag', 'follow-up', 'suite à', 'a seguito') AND a
  resolvable reference to an existing case (sender reference or patient pseudonym).
  A follow-up without a resolvable reference becomes a new case with the note
  "possible follow-up" ("möglicher Nachtrag").
* Seed messages: the structured fields ``previous_message_id`` and
  ``duplicate_of_message_id`` (and the external ``case_id``) apply.
* Every signal stays visible; the awareness anchor of the case never changes because of
  a duplicate or follow-up (03-scenario.md lines 1391, 1399-1413, 1435, sources
  SCN-AWARE, SCN-SEEDS; Goal Card QUALITY, GC-DUP).

Relation values follow expected.json: ``new_case``, ``duplicate_of:<ref>``,
``follow_up_of:<ref>`` where ``<ref>`` is the reference of the earlier signal.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Sequence

from ..versions import LINK

NEW_CASE = "NEW_CASE"
DUPLICATE = "DUPLICATE"
FOLLOW_UP = "FOLLOW_UP"

NOTE_POSSIBLE_FOLLOW_UP = "possible_follow_up"


@dataclass(frozen=True)
class LinkProfile:
    """Link-relevant view of one signal (from extraction or seed metadata)."""

    ref: str
    received_at: _dt.datetime
    text_sha256: str
    signal_id: str | None = None
    case_id: str | None = None
    sender_refs: tuple[str, ...] = ()
    patients: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    families: tuple[str, ...] = ()
    event_tags: tuple[str, ...] = ()
    event_dates: tuple[str, ...] = ()
    follow_up_marker: bool = False
    message_id: str | None = None
    previous_message_id: str | None = None
    duplicate_of_message_id: str | None = None
    external_case_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["received_at"] = self.received_at.isoformat()
        return data


@dataclass(frozen=True)
class LinkDecision:
    kind: str
    relation: str
    target: LinkProfile | None = None
    matched_on: tuple[str, ...] = ()
    note: str | None = None
    rule_version: str = LINK

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "relation": self.relation,
            "target_signal_id": self.target.signal_id if self.target else None,
            "target_case_id": self.target.case_id if self.target else None,
            "target_ref": self.target.ref if self.target else None,
            "matched_on": list(self.matched_on),
            "note": self.note,
            "rule_version": self.rule_version,
        }


def _earliest(profiles: Sequence[LinkProfile]) -> LinkProfile:
    return min(profiles, key=lambda p: (p.received_at, p.ref))


def _overlap(a: Sequence[str], b: Sequence[str]) -> bool:
    return bool(set(a) & set(b))


def _new_case(note: str | None = None) -> LinkDecision:
    return LinkDecision(NEW_CASE, "new_case", note=note)


def _decide_seed(new: LinkProfile, existing: Sequence[LinkProfile]) -> LinkDecision | None:
    by_message = {p.message_id: p for p in existing if p.message_id}
    if new.duplicate_of_message_id:
        target = by_message.get(new.duplicate_of_message_id)
        if target is not None:
            return LinkDecision(DUPLICATE, f"duplicate_of:{target.ref}", target, ("metadata:duplicate_of_message_id",))
        return _new_case(f"unresolved duplicate_of_message_id {new.duplicate_of_message_id}")
    if new.previous_message_id:
        target = by_message.get(new.previous_message_id)
        if target is not None:
            return LinkDecision(FOLLOW_UP, f"follow_up_of:{target.ref}", target, ("metadata:previous_message_id",))
        return _new_case(f"unresolved previous_message_id {new.previous_message_id}")
    if new.external_case_id:
        same_case = [p for p in existing if p.external_case_id == new.external_case_id]
        if same_case:
            target = _earliest(same_case)
            return LinkDecision(FOLLOW_UP, f"follow_up_of:{target.ref}", target, ("metadata:case_id",),
                                "linked by the external case id")
        return _new_case()
    return None


def decide(new: LinkProfile, existing: Sequence[LinkProfile]) -> LinkDecision:
    """Decide whether ``new`` opens a case, duplicates or follows up an existing signal."""
    if new.message_id or new.external_case_id:
        seeded = _decide_seed(new, existing)
        if seeded is not None:
            return seeded

    if new.follow_up_marker:
        reference_matches = [
            p for p in existing
            if _overlap(new.sender_refs, p.sender_refs) or _overlap(new.patients, p.patients)
        ]
        if reference_matches:
            target = _earliest(reference_matches)
            matched = []
            if _overlap(new.sender_refs, target.sender_refs):
                matched.append("same_sender_reference")
            if _overlap(new.patients, target.patients):
                matched.append("same_patient_pseudonym")
            return LinkDecision(FOLLOW_UP, f"follow_up_of:{target.ref}", target, ("follow_up_marker", *matched))
        return _new_case(NOTE_POSSIBLE_FOLLOW_UP)

    candidates: list[tuple[LinkProfile, str]] = []
    for p in existing:
        if p.text_sha256 == new.text_sha256:
            candidates.append((p, "identical_text"))
        elif _overlap(new.sender_refs, p.sender_refs):
            candidates.append((p, "same_sender_reference"))
        elif (
            _overlap(new.patients, p.patients)
            and (_overlap(new.models, p.models) or _overlap(new.families, p.families))
            and (_overlap(new.event_tags, p.event_tags) or _overlap(new.event_dates, p.event_dates))
        ):
            candidates.append((p, "same_patient_product_event"))
    if candidates:
        target = _earliest([c[0] for c in candidates])
        reasons = tuple(sorted({why for p, why in candidates if p is target}))
        return LinkDecision(DUPLICATE, f"duplicate_of:{target.ref}", target, reasons)
    return _new_case()
