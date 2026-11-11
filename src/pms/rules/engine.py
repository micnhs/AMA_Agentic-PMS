"""Deterministic rule pipeline for one incoming signal (concept 6.4 step 1-3 and 4a).

Order (concept 6.4): receipt with UTC time, source and SHA-256 hash; text extraction;
then the rule code before any agent: INJ-1.0, CRIT-1.0, LINK-1.0, REG-1.0, RS-1.0,
DL-1.0. A critical result opens triage (status TRIAGE_ERFORDERLICH, triage task, alert
request) before any analysis runs. CORR-1.0 runs separately (:meth:`RuleEngine.run_correlation`)
because the concept places it in the extended analysis (step 4a); it never delays triage.

Rule results cannot be overwritten by agents. Every rule evaluation writes one audit
record with its rule version.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import audit as A
from ..knowledge import KnowledgeIndex, ReferenceData
from ..models import (
    Actor, ActorType, Alert, Case, Clock, Severity, SignalInput, Status, SystemClock, CLOSED_STATUSES,
    SERIOUS_SEVERITIES, iso_utc, parse_utc,
)
from ..store import Store
from ..versions import CORR, CRIT, DL, INJ, LINK, REG, RS
from ..workflow import (
    TASK_TRIAGE, TASK_UNMATCHED, TASK_UNREADABLE, Action, Workflow, candidate_values,
)
from . import correlation as corr
from . import crit, injection, linking, registry, rulesets
from . import deadlines as dl
from .extract import Extraction, completeness, extract

RULE_ACTOR = Actor.rule("rule-engine")
_SOURCES = ("upload", "seed", "api")
#: Shown in the alert (and triage task) of a critical letter that carries the injection flag.
INJECTION_ALERT_NOTE = (f"injection flag raised ({INJ}): instructions embedded in the letter were ignored and "
                        "withheld from the agents")


@dataclass
class ProcessResult:
    """Outcome of :meth:`RuleEngine.process_signal`."""

    signal_id: str
    case_id: str
    case_key: str
    relation: str
    status: str
    critical: bool
    newly_critical: bool
    severity: str
    signal_severity: str
    registry_check: str
    rule_sets: list[str]
    injection_flag: bool
    deadline_not_reset: bool
    driving_due_date: str | None
    tasks: list[str] = field(default_factory=list)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rank(sev: str) -> int:
    return {Severity.DEATH: 3, Severity.PUBLIC_HEALTH_THREAT: 2, Severity.SERIOUS_DETERIORATION: 1}.get(sev, 0)


class RuleEngine:
    """Runs the fixed rule set over incoming signals and keeps case-level rule state."""

    def __init__(self, store: Store, ref_data: ReferenceData, index: KnowledgeIndex | None = None, *,
                 workflow: Workflow | None = None, clock: Clock | None = None,
                 calendar: dl.Calendar | None = None,
                 alert_sink: Callable[[Alert, Case], None] | None = None) -> None:
        self.store = store
        self.ref = ref_data
        self.index = index or KnowledgeIndex(ref_data)
        self.clock = clock or store.clock or SystemClock()
        self.calendar = calendar or dl.Calendar()
        self.workflow = workflow or Workflow(store, clock=self.clock, calendar=self.calendar, alert_sink=alert_sink)

    # ------------------------------------------------------------------ helpers
    def _audit_rule(self, rule_version: str, case_id: str | None, signal_id: str, after: Any, at: _dt.datetime,
                    *, event_type: str = A.RULE_EVALUATED, before: Any = None, **extra: Any) -> None:
        self.store.audit.append(event_type=event_type, source="rule", actor_type="rule", actor_id=rule_version,
                                rule_versions=[rule_version], case_id=case_id, signal_id=signal_id,
                                before=before, after=after, ts=at, **extra)

    def _link_profile(self, *, ref: str, received_at: _dt.datetime, sha: str, ex: Extraction,
                      event_tags: list[str], metadata: dict[str, Any], signal_id: str | None = None,
                      case_id: str | None = None) -> linking.LinkProfile:
        return linking.LinkProfile(
            ref=ref, received_at=received_at, text_sha256=sha, signal_id=signal_id, case_id=case_id,
            sender_refs=tuple(ex.sender_ref_values), patients=tuple(ex.patient_values),
            models=tuple(ex.model_values), families=tuple(ex.family_values) or (
                (metadata["product_family"],) if metadata.get("product_family") else ()),
            event_tags=tuple(event_tags), event_dates=tuple(d.value for d in ex.event_dates),
            follow_up_marker=ex.follow_up_marker is not None,
            message_id=metadata.get("message_id"), previous_message_id=metadata.get("previous_message_id"),
            duplicate_of_message_id=metadata.get("duplicate_of_message_id"),
            external_case_id=metadata.get("case_id"),
        )

    def _profile_of_stored(self, s: Any) -> linking.LinkProfile:
        ex = s.extraction or {}
        values = lambda key: tuple(e["value"] for e in ex.get(key, []))  # noqa: E731
        families = values("families") or ((s.metadata["product_family"],) if s.metadata.get("product_family") else ())
        return linking.LinkProfile(
            ref=s.ref, received_at=parse_utc(s.received_at), text_sha256=s.text_sha256, signal_id=s.signal_id,
            case_id=s.case_id, sender_refs=values("sender_refs"), patients=values("patient_refs"),
            models=values("models"), families=families,
            event_tags=tuple(t["tag"] for t in ex.get("event_tags", [])),
            event_dates=values("event_dates"), follow_up_marker=bool(ex.get("follow_up_marker")),
            message_id=s.metadata.get("message_id"), previous_message_id=s.metadata.get("previous_message_id"),
            duplicate_of_message_id=s.metadata.get("duplicate_of_message_id"),
            external_case_id=s.metadata.get("case_id"),
        )

    def _new_case_key(self, sig: SignalInput, ref: str) -> str:
        base = str(sig.metadata.get("case_id") or f"CASE-{ref}")
        key, n = base, 2
        while not self.store.case_key_available(key):
            key, n = f"{base}-{n}", n + 1
        return key

    # ------------------------------------------------------------------ main entry
    def process_signal(self, sig: SignalInput, *, actor: Actor | None = None,
                       now: _dt.datetime | None = None, target_case_id: str | None = None) -> ProcessResult:
        """Receive one signal and run INJ, CRIT, LINK, REG, RS and DL before any agent.

        ``target_case_id``: a person adds the signal to this case ("Signal zu einem Fall
        hinzufügen"). LINK-1.0 still runs; if it does not point to that case, the signal is
        attached as a follow-up of the case's first signal with the reason "human assignment"."""
        actor = actor or Actor.system()
        if actor.actor_type == ActorType.HUMAN:
            self.workflow.authorize(actor, Action.UPLOAD, source=sig.source if sig.source in _SOURCES else "api")
        at = parse_utc(now) if now is not None else self.clock.now()
        received = parse_utc(sig.received_at)
        source = sig.source if sig.source in _SOURCES else "api"
        text = sig.text or ""
        readable = bool(sig.readable and text.strip())
        sha = _sha256(text)
        metadata = dict(sig.metadata or {})

        with self.store.transaction():
            signal_id = self.store.next_id("SIG")
            ref = sig.ref or metadata.get("message_id") or signal_id

            # INJ-1.0 and extraction (masked text) ---------------------------------
            inj = injection.scan(text)
            ex = extract(inj.masked_text, self.ref.catalogue)
            tags = corr.tag_events(inj.masked_text, self.ref.event_terms)
            tag_names = [t.tag for t in tags]
            ex_dict = ex.to_dict()
            ex_dict["event_tags"] = [t.__dict__ for t in tags]

            # CRIT-1.0 on the full raw text ------------------------------------------
            cs = crit.scan(text, ex)
            if not readable:
                cs = crit.CritScan(Severity.UNKNOWN.value, frozenset(), ())

            # LINK-1.0 ----------------------------------------------------------------
            existing = [self._profile_of_stored(s) for s in self.store.list_signals() if s.case_id]
            profile = self._link_profile(ref=ref, received_at=received, sha=sha, ex=ex, event_tags=tag_names,
                                         metadata=metadata)
            link = linking.decide(profile, existing) if readable or metadata else linking.LinkDecision(
                linking.NEW_CASE, "new_case", note="unreadable input: no linking possible")
            if target_case_id is not None:
                target_case = self.store.get_case(target_case_id)
                if link.target is None or link.target.case_id != target_case.case_id:
                    first = min((p for p in existing if p.case_id == target_case.case_id),
                                key=lambda p: (p.received_at, p.ref))
                    link = linking.LinkDecision(linking.FOLLOW_UP, f"follow_up_of:{first.ref}", first,
                                                ("human_assignment",), f"added to {target_case.case_key} by {actor.actor_id}")

            # REG-1.0 -----------------------------------------------------------------
            ids = registry.Identifiers(tuple(ex.patient_values), tuple(ex.lot_values), tuple(ex.udi_values),
                                       tuple(ex.model_values))
            reg = registry.check(ids, self.ref.registry, self.ref.catalogue)

            # RS-1.0 ------------------------------------------------------------------
            rs = rulesets.assign(ex if readable else None, reg.entries, metadata.get("market"))

            # Case: create or attach ---------------------------------------------------
            if link.kind == linking.NEW_CASE:
                case_key = self._new_case_key(sig, ref)
                family = (ex.family_values[0] if ex.family_values else None) or metadata.get("product_family") or (
                    reg.entries[0].family if reg.entries else None)
                case = self.store.insert_case(
                    case_id=case_key, case_key=case_key, status=Status.EINGEGANGEN.value, critical=False,
                    severity=cs.severity, urgency=crit.UrgencyState().to_dict(), rule_sets=[], authority_labels=[],
                    route_unverified=False, rs_notes=[], incident_states=[], rs_basis=rs.basis,
                    awareness_anchor=None, anchor_signal_id=None, anchor_ref=None, product_family=family,
                    metadata={}, analysis_state="PENDING", deadline_waiver=None, content_version=0,
                    created_at=iso_utc(at, ms=True), updated_at=iso_utc(at, ms=True),
                )
                self._audit_rule(LINK, case.case_id, signal_id, {"case_created": case.case_id,
                                                                 "status": case.status},
                                 at, event_type=A.CASE_CREATED)
            else:
                case = self.store.get_case(str(link.target.case_id))  # type: ignore[union-attr]
            anchor_before = case.awareness_anchor

            self.store.insert_signal(
                signal_id=signal_id, case_id=case.case_id, ref=ref, source=source, filename=sig.filename,
                received_at=iso_utc(received), text=text, text_sha256=sha, readable=readable, language=ex.language,
                metadata=metadata, relation=link.relation, link=link.to_dict(), registry=reg.to_dict(),
                injection=inj.to_dict(), crit=cs.to_dict(), extraction=ex_dict, rule_sets=list(rs.rule_sets),
                deadline_not_reset=False,
                completeness=completeness(text, ex, metadata) if readable else {"present": [], "missing": ["text"]},
                created_at=iso_utc(at, ms=True),
            )
            self.store.audit.append(
                event_type=A.SIGNAL_ADDED, source=source, actor_type=actor.actor_type.value, actor_id=actor.actor_id,
                case_id=case.case_id, signal_id=signal_id,
                after={"ref": ref, "received_at": iso_utc(received), "sha256": sha, "filename": sig.filename,
                       "readable": readable, "source": source}, ts=at,
            )
            self._store_rule_facts(case.case_id, signal_id, ex, at)

            # One audit record per rule evaluation -----------------------------------------
            self._audit_rule(INJ, case.case_id, signal_id, inj.to_dict(), at)
            self._audit_rule(CRIT, case.case_id, signal_id,
                             {"severity": cs.severity, "groups": sorted(cs.groups),
                              "indicators": [(i.group, i.match, i.line) for i in cs.indicators]}, at)
            self._audit_rule(LINK, case.case_id, signal_id, link.to_dict(), at)
            self._audit_rule(REG, case.case_id, signal_id, reg.to_dict(), at)
            self._audit_rule(RS, case.case_id, signal_id, rs.to_dict(), at)

            # Case-level rule state ------------------------------------------------------
            case, case_signals = self._update_case_state(case, at, rs)
            deadline_not_reset = link.kind != linking.NEW_CASE and (
                anchor_before is None or parse_utc(case.awareness_anchor) <= parse_utc(anchor_before))
            self.store.update_signal(signal_id, deadline_not_reset=deadline_not_reset)

            # DL-1.0 --------------------------------------------------------------------
            candidates = self._update_deadlines(case, case_signals, signal_id, at)

            # Visibility tasks ------------------------------------------------------------
            tasks: list[str] = []
            if not readable:
                case = self.workflow.system_transition(case.case_id, Status.NICHT_LESBAR, reason="text not readable",
                                                       source="rule", actor=RULE_ACTOR, now=at) \
                    if case.status == Status.EINGEGANGEN else case
                tasks.append(self.workflow.create_task(case.case_id, TASK_UNREADABLE, "Unreadable input - check the file",
                                                       actor=RULE_ACTOR, source="rule", now=at,
                                                       detail={"signal_id": signal_id}).task_id)
            if readable and reg.status != registry.MATCH:
                tasks.append(self.workflow.create_task(
                    case.case_id, TASK_UNMATCHED, f"Registry check {reg.status}: human review needed",
                    actor=RULE_ACTOR, source="rule", now=at,
                    detail={"signal_id": signal_id, "registry_check": reg.value}).task_id)

            # Linked signal: content change (voids approvals); follow-up reopens closed cases
            if link.kind != linking.NEW_CASE:
                case = self.workflow.content_changed(case.case_id, kind="signal", actor=RULE_ACTOR, source="rule",
                                                     now=at)
                if link.kind == linking.FOLLOW_UP and (Status(case.status) in CLOSED_STATUSES
                                                       or case.status == Status.MELDUNG_ERFASST):
                    case = self._reopen(case, signal_id, at)

            # CRIT-1.0 case level: raise only ------------------------------------------------
            was_critical = case.critical
            if cs.severity in SERIOUS_SEVERITIES:
                first = next((i for i in cs.indicators if i.group == cs.severity), None)
                reason = f"{CRIT}: {cs.severity} indicator in signal {ref}" + (f" ('{first.match}')" if first else "")
                if inj.flag:
                    reason += f" - {INJECTION_ALERT_NOTE}"
                case = self.workflow.raise_urgency(case.case_id, RULE_ACTOR, reason, source="rule", now=at,
                                                   rule_versions=[CRIT],
                                                   triage_detail={"signal_id": signal_id, "injection_flag": inj.flag})
                if was_critical:
                    # Every critical letter gets its own triage task and alert, also on an already critical case.
                    case = self.workflow.triage_new_critical_signal(
                        case.case_id, signal_id=signal_id, reason=reason, injection_flag=inj.flag, actor=RULE_ACTOR,
                        source="rule", now=at, rule_versions=[CRIT])
            case = self.check_deadline_window(case.case_id, now=at) or case
            if case.status == Status.TRIAGE_ERFORDERLICH:
                tasks.extend(t.task_id for t in self.store.list_tasks(case_id=case.case_id, status="OPEN",
                                                                      kind=TASK_TRIAGE))

        self.workflow._flush_after_commit()
        case = self.store.get_case(case.case_id)
        drive = dl.driving(candidates)
        return ProcessResult(
            signal_id=signal_id, case_id=case.case_id, case_key=case.case_key, relation=link.relation,
            status=case.status, critical=case.critical, newly_critical=case.critical and not was_critical,
            severity=case.severity, signal_severity=cs.severity, registry_check=reg.value,
            rule_sets=list(case.rule_sets), injection_flag=inj.flag, deadline_not_reset=deadline_not_reset,
            driving_due_date=drive.due_date.isoformat() if drive else None, tasks=tasks,
        )

    # ------------------------------------------------------------------ case state
    def _update_case_state(self, case: Case, at: _dt.datetime,
                           rs: rulesets.RuleSetAssignment | None = None) -> tuple[Case, list[Any]]:
        """Recompute case-level labels from all signals (raise-only for severity)."""
        sigs = self.store.list_signals(case.case_id)
        anchor_sig = min(sigs, key=lambda s: (parse_utc(s.received_at), s.signal_id))
        serious = [s.crit.get("severity") for s in sigs if s.crit.get("severity") in SERIOUS_SEVERITIES]
        if serious or case.severity in SERIOUS_SEVERITIES:
            best = case.severity if case.severity in SERIOUS_SEVERITIES else serious[0]
            for sev in serious:
                best = sev if _rank(sev) > _rank(best) else best
            severity = best
        else:
            has_ids = any(
                s.extraction.get("models") or s.extraction.get("unknown_models") or s.extraction.get("lots")
                or s.extraction.get("udi_dis") for s in sigs
            )
            severity = Severity.NONE_DETECTED.value if has_ids else Severity.UNKNOWN.value
        rule_sets: list[str] = list(case.rule_sets)
        labels: list[str] = list(case.authority_labels)
        states: list[str] = list(case.incident_states)
        notes: list[str] = list(case.rs_notes)
        for note in (rs.notes if rs is not None else ()):
            if note not in notes:
                notes.append(note)
        all_labels = [label for s in sigs for label in s.rule_sets]
        resolved = any(label != rulesets.UNRESOLVED_RULE_SET for label in all_labels + rule_sets)
        for rs_label in all_labels:
            # A signal without any location evidence adds no placeholder to a resolved case
            # (existing rule sets are never removed automatically).
            if rs_label == rulesets.UNRESOLVED_RULE_SET and resolved:
                continue
            if rs_label not in rule_sets:
                rule_sets.append(rs_label)
        for rs_label in rule_sets[len(labels):]:
            state = rs_label.split(":", 1)[1] if ":" in rs_label else "UNRESOLVED"
            labels.append(rulesets.authority_for_state(state)[0])
            if state not in states:
                states.append(state)
        route_unverified = any(rulesets.authority_for_state(st)[1] for st in states)
        family = case.product_family
        if family is None:
            for s in sigs:
                fams = [e["value"] for e in s.extraction.get("families", [])] or (
                    [s.metadata["product_family"]] if s.metadata.get("product_family") else [])
                if fams:
                    family = fams[0]
                    break
        case = self.store.update_case(
            case.case_id, severity=severity, rule_sets=rule_sets, authority_labels=labels,
            incident_states=states, route_unverified=route_unverified, rs_notes=notes,
            awareness_anchor=anchor_sig.received_at, anchor_signal_id=anchor_sig.signal_id,
            anchor_ref=anchor_sig.ref, product_family=family, updated_at=iso_utc(at, ms=True),
        )
        return case, sigs

    def _update_deadlines(self, case: Case, sigs: list[Any], signal_id: str, at: _dt.datetime) -> list[dl.DeadlineCandidate]:
        """Every rule set of the case x every basis seen in its signals; never drop one."""
        anchor = parse_utc(str(case.awareness_anchor))
        rows = self.store.list_deadlines(case.case_id)
        before = [dl.from_row(r.__dict__).expected_view() for r in rows]
        bases: list[str] = []
        seen_serious = False
        for s in sorted(sigs, key=lambda x: (parse_utc(x.received_at), x.signal_id)):
            for b in dl.bases_for(s.crit.get("groups", [])):
                # "otherwise provisionally 15 days": only while no serious basis is known
                if b == dl.PROVISIONAL_REPORTABILITY_UNCLEAR and seen_serious:
                    continue
                seen_serious = seen_serious or b != dl.PROVISIONAL_REPORTABILITY_UNCLEAR
                if b not in bases:
                    bases.append(b)
        # Refresh existing candidates for the (possibly earlier) anchor, then append new ones.
        for row in rows:
            fresh = dl.make_candidate(row.rule_set, row.basis, anchor, self.calendar)
            if fresh.due_end_utc != parse_utc(row.due_end_utc):
                self.store.upsert_deadline(case.case_id, row.position, candidate_values(fresh, row.origin_signal_id))
        position = len(rows)
        for rule_set in case.rule_sets:
            for basis in bases:
                if any(r.rule_set == rule_set and r.basis == basis for r in rows):
                    continue
                cand = dl.make_candidate(rule_set, basis, anchor, self.calendar)
                self.store.upsert_deadline(case.case_id, position, candidate_values(cand, signal_id))
                position += 1
        rows = self.store.list_deadlines(case.case_id)
        candidates = [dl.from_row(r.__dict__) for r in rows]
        drive = dl.driving(candidates)
        self._audit_rule(DL, case.case_id, signal_id,
                         {"awareness_anchor": case.awareness_anchor, "anchor_ref": case.anchor_ref,
                          "candidates": [c.expected_view() for c in candidates],
                          "driving_due_date": drive.due_date.isoformat() if drive else None},
                         at, before={"candidates": before})
        return candidates

    def _reopen(self, case: Case, signal_id: str, at: _dt.datetime) -> Case:
        """A follow-up to a closed case reopens it; a human must triage it again (7.2)."""
        if case.deadline_waiver is not None:
            self.store.update_case(case.case_id, deadline_waiver=None)
        case = self.workflow.system_transition(case.case_id, Status.TRIAGE_ERFORDERLICH,
                                               reason=f"follow-up {signal_id} to a closed case: new human triage",
                                               source="rule", actor=RULE_ACTOR, now=at)
        if not self.store.list_tasks(case_id=case.case_id, status="OPEN", kind=TASK_TRIAGE):
            self.workflow.create_task(case.case_id, TASK_TRIAGE, "Follow-up to a closed case: new triage",
                                      actor=RULE_ACTOR, source="rule", now=at, detail={"signal_id": signal_id})
        return case

    def _store_rule_facts(self, case_id: str, signal_id: str, ex: Extraction, at: _dt.datetime) -> None:
        """Deterministically extracted facts with their verbatim source quote (origin 'rule')."""
        groups = (("patient_reference", ex.patient_refs), ("lot", ex.lots), ("udi_di", ex.udi_dis),
                  ("model", ex.models), ("product_family", ex.families), ("sender_reference", ex.sender_refs),
                  ("implant_date", ex.implant_dates), ("implant_site_state", ex.implant_sites),
                  ("event_date", ex.event_dates))
        for name, items in groups:
            for e in items:
                self.store.insert_fact(case_id=case_id, signal_id=signal_id, name=name, value=e.value, quote=e.quote,
                                       line=e.line, verified=True, origin="rule", created_at=iso_utc(at, ms=True))
        if ex.incident_location is not None:
            loc = ex.incident_location
            self.store.insert_fact(case_id=case_id, signal_id=signal_id, name="incident_location",
                                   value=list(loc.states), quote=loc.quote, line=loc.line, verified=True,
                                   origin="rule", created_at=iso_utc(at, ms=True))

    # ------------------------------------------------------------------ 48-hour window
    def check_deadline_window(self, case_id: str, *, now: _dt.datetime | None = None) -> Case | None:
        """CRIT-1.0 window: raise when the earliest open deadline ends within 48 hours.

        Raise-only and at most once per deadline candidate, so a human lowering stays in
        force. Waived (non-reportable) and stopped deadlines are ignored. Safe to call
        periodically (alert worker); it never makes a decision."""
        at = parse_utc(now) if now is not None else self.clock.now()
        case = self.store.get_case(case_id)
        overview = self.workflow.deadline_overview(case_id, at)
        drive = overview["driving"]
        if drive is None or overview["waiver"] is not None or drive["status"] not in (dl.OPEN, dl.OVERDUE):
            return case
        if drive["seconds_remaining"] > crit.DEADLINE_WINDOW_HOURS * 3600:
            return case
        key = f"{drive['rule_set']}|{drive['basis']}|{drive['due_date']}"
        if key in crit.UrgencyState.from_dict(case.urgency).window_raised_for:
            return case
        reason = f"{CRIT}: earliest deadline {drive['due_date']} ({drive['rule_set']}) within {crit.DEADLINE_WINDOW_HOURS} hours"
        return self.workflow.raise_urgency(case_id, RULE_ACTOR, reason, source="rule", now=at,
                                           rule_versions=[CRIT, DL], window_key=key)

    def check_all_deadline_windows(self, *, now: _dt.datetime | None = None) -> list[str]:
        """Periodic sweep for the alert worker: returns the cases that became critical."""
        raised = []
        for case in self.store.list_cases():
            if case.critical or Status(case.status) in CLOSED_STATUSES:
                continue
            updated = self.check_deadline_window(case.case_id, now=now)
            if updated is not None and updated.critical:
                raised.append(case.case_id)
        return raised

    # ------------------------------------------------------------------ CORR-1.0
    def case_profiles(self) -> list[corr.CaseProfile]:
        out = []
        for case in self.store.list_cases():
            models: list[str] = []
            lots: list[str] = []
            tags: list[str] = []
            for s in self.store.list_signals(case.case_id):
                ex = s.extraction or {}
                models += [e["value"] for e in ex.get("models", []) if e["value"] not in models]
                lots += [e["value"] for e in ex.get("lots", []) if e["value"] not in lots]
                tags += [t["tag"] for t in ex.get("event_tags", []) if t["tag"] not in tags]
            out.append(corr.CaseProfile(case.case_id, case.case_key, case.product_family, tuple(models), tuple(lots),
                                        tuple(tags)))
        return out

    def run_correlation(self, case_id: str, signal_id: str, *, now: _dt.datetime | None = None) -> corr.CorrelationResult:
        """CORR-1.0 for one signal: deterministic filter queries only (never letter text)."""
        at = parse_utc(now) if now is not None else self.clock.now()
        signal = self.store.get_signal(signal_id)
        case = self.store.get_case(case_id)
        ex = signal.extraction or {}
        reg_ids = list(signal.registry.get("entry_ids", []))
        matched = signal.registry.get("status") == registry.MATCH
        entries = [e for e in self.ref.registry if e.reg_id in reg_ids]
        lots = [e["value"] for e in ex.get("lots", [])]
        models = [e["value"] for e in ex.get("models", [])]
        if matched:
            lots += [e.lot for e in entries if e.lot not in lots]
            models += [e.model for e in entries if e.model not in models]
        families = [e["value"] for e in ex.get("families", [])]
        family = families[0] if families else (signal.metadata.get("product_family") or (
            entries[0].family if entries and matched else None))
        if family is None and (lots or models):
            family = case.product_family
        tags = [t["tag"] for t in ex.get("event_tags", [])]
        result = corr.correlate(event_tags=tags, family=family, models=models, lots=lots, index=self.index,
                                registry=self.ref.registry, own_registry_ids=reg_ids, cases=self.case_profiles(),
                                own_case_id=case_id, dictionary=self.ref.event_terms)
        with self.store.transaction():
            self.store.insert_correlation(case_id=case_id, signal_id=signal_id, result=result.to_dict(),
                                          corpus_version=result.corpus_version, created_at=iso_utc(at, ms=True))
            self.store.audit.append(
                event_type=A.CORRELATION_RUN, source="rule", actor_type="rule", actor_id=CORR, rule_versions=[CORR],
                corpus_version=result.corpus_version, source_ids=result.refs, case_id=case_id, signal_id=signal_id,
                after={"status": result.status, "event_tags": list(result.event_tags), "hits": len(result.hits),
                       "queries": len(result.queries), "note": result.note}, ts=at,
            )
            self.workflow.content_changed(case_id, kind="correlation", actor=RULE_ACTOR, source="rule", now=at)
        return result

    # ------------------------------------------------------------------ snapshot
    def case_labels(self, case_id: str, signal_id: str | None = None, *, now: _dt.datetime | None = None) -> dict[str, Any]:
        """Rule-based labels of a case (and one of its signals) in expected.json terms."""
        case = self.store.get_case(case_id)
        rows = self.store.list_deadlines(case_id)
        cands = [dl.from_row(r.__dict__) for r in rows]
        drive = dl.driving(cands)
        sig = self.store.get_signal(signal_id) if signal_id else None
        latest_corr = None
        if sig is not None:
            found = self.store.list_correlations(case_id=case_id, signal_id=sig.signal_id)
            latest_corr = found[-1].result if found else None
        anchor_local = self.calendar.local_date(parse_utc(case.awareness_anchor)) if case.awareness_anchor else None
        return {
            "case_id": case.case_id,
            "case_key": case.case_key,
            "status": case.status,
            "critical": case.critical,
            "severity": case.severity,
            "rule_set": list(case.rule_sets),
            "authority_label": list(case.authority_labels),
            "route_unverified": case.route_unverified,
            "awareness_anchor": iso_utc(parse_utc(case.awareness_anchor)) if case.awareness_anchor else None,
            "anchor_ref": case.anchor_ref,
            "deadline_start": (anchor_local + _dt.timedelta(days=1)).isoformat() if anchor_local else None,
            "deadline_candidates": [c.expected_view() for c in cands],
            "driving_due_date": drive.due_date.isoformat() if drive else None,
            "deadline_days": drive.days if drive else None,
            "linking": sig.relation if sig else None,
            "registry_check": sig.registry.get("value") if sig else None,
            "injection_flag": bool(sig.injection.get("flag")) if sig else None,
            "deadline_not_reset": sig.deadline_not_reset if sig else None,
            "completeness": sig.completeness if sig else None,
            "event_tags": list(latest_corr["event_tags"]) if latest_corr else (
                [t["tag"] for t in sig.extraction.get("event_tags", [])] if sig else []),
            "correlation_refs": [h["ref"] for h in latest_corr["hits"]] if latest_corr else [],
            "correlation_status": latest_corr["status"] if latest_corr else None,
        }
