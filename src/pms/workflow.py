"""Fixed-rule case state machine, permissions and human decisions (concept 7.1-7.4; D-08, D-17).

Rules enforced here:

* The system only sets technical statuses and only raises urgency (concept 7.2).
* Approve, reject, request information, close and "non-reportable" are set only by the
  medical reviewer, always with a reason; a rejection also needs a reason code R1-R6.
* An approval is bound to the content hash of the reviewed version. Every later change
  (metadata, new signal, new agent run) voids it and sends the case back to review.
* "Submission recorded" is set only by the regulatory role and only for the approved
  version (date, channel label, reference, receipt status). This stops the deadline
  clock. Nothing is ever sent, not even simulated.
* Closing as non-reportable does not stop the clock; the deadline shows "entfällt laut
  menschlicher Entscheidung" with user and reason (decision Q-4).
* A follow-up to a closed case reopens it; it needs a new human triage.
* No timers: no status ever changes into a decision because a deadline is near or a
  reviewer is absent (no automatic substitute approval).
* Agents and the system have no decision rights; an attempt is refused and audited.

Every status change, human decision and metadata edit writes exactly one audit record.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
from enum import StrEnum
from typing import Any, Callable, Iterable

from . import audit as A
from .audit import canonical_json
from .models import (
    CLOSED_STATUSES, REJECT_REASON_CODES, Actor, ActorType, Alert, Approval, Case, Clock, Finding, MetadataEdit,
    Recommendation, Role, Status, Submission, SystemClock, iso_utc, parse_utc,
)
from .rules import crit
from .rules import deadlines as dl
from .store import Store
from .versions import ALL_RULE_VERSIONS


class Action(StrEnum):
    VIEW = "view"
    UPLOAD = "upload"  # upload a letter or add a signal to a case
    EDIT_METADATA = "edit_metadata"
    RERUN_ANALYSIS = "rerun_analysis"
    TAKE_TRIAGE = "take_triage"
    RAISE_URGENCY = "raise_urgency"
    LOWER_URGENCY = "lower_urgency"
    START_REVIEW = "start_review"
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_INFO = "request_info"
    CLOSE = "close"
    CLOSE_NON_REPORTABLE = "close_non_reportable"
    CLASSIFY_ANTICIPATED = "classify_anticipated"
    RECORD_SUBMISSION = "record_submission"
    VIEW_COSTS_GOVERNANCE = "view_costs_governance"
    VIEW_TRACES = "view_traces"
    MANAGE_FINDINGS = "manage_findings"
    LOAD_DEMO_DATA = "load_demo_data"
    MODIFY_AUDIT = "modify_audit"


_V, _AN, _RG, _MR, _OP = Role.VIEWER, Role.ANALYST, Role.REGULATORY, Role.MEDICAL_REVIEWER, Role.OPERATOR

#: Permission matrix for human roles (concept section 7.3). Traces: operator only (A-16).
PERMISSIONS: dict[Action, frozenset[str]] = {
    Action.VIEW: frozenset({_V, _AN, _RG, _MR, _OP}),
    Action.UPLOAD: frozenset({_AN, _RG, _MR}),
    Action.EDIT_METADATA: frozenset({_AN, _RG, _MR}),
    Action.RERUN_ANALYSIS: frozenset({_AN, _MR}),
    Action.TAKE_TRIAGE: frozenset({_AN, _MR}),
    Action.RAISE_URGENCY: frozenset({_AN, _RG, _MR}),
    Action.LOWER_URGENCY: frozenset({_MR}),
    Action.START_REVIEW: frozenset({_MR}),
    Action.APPROVE: frozenset({_MR}),
    Action.REJECT: frozenset({_MR}),
    Action.REQUEST_INFO: frozenset({_MR}),
    Action.CLOSE: frozenset({_MR}),
    Action.CLOSE_NON_REPORTABLE: frozenset({_MR}),
    Action.CLASSIFY_ANTICIPATED: frozenset({_MR}),
    Action.RECORD_SUBMISSION: frozenset({_RG}),
    Action.VIEW_COSTS_GOVERNANCE: frozenset({_V, _AN, _RG, _MR, _OP}),
    Action.VIEW_TRACES: frozenset({_OP}),
    Action.MANAGE_FINDINGS: frozenset({_MR}),
    Action.LOAD_DEMO_DATA: frozenset({_OP}),
    Action.MODIFY_AUDIT: frozenset(),  # nobody
}
#: Agents and the system: internal viewing and raising urgency (rule or AI) only.
NON_HUMAN_ACTIONS: frozenset[Action] = frozenset({Action.VIEW, Action.RAISE_URGENCY})
#: The system may additionally load demo data on behalf of an operator command.
SYSTEM_ONLY_ACTIONS: frozenset[Action] = frozenset({Action.LOAD_DEMO_DATA})

REASON_MANDATORY: frozenset[Action] = frozenset({
    Action.EDIT_METADATA, Action.LOWER_URGENCY, Action.APPROVE, Action.REJECT, Action.REQUEST_INFO,
    Action.CLOSE, Action.CLOSE_NON_REPORTABLE, Action.CLASSIFY_ANTICIPATED, Action.MANAGE_FINDINGS,
})

_ALL_OPEN = frozenset(Status) - CLOSED_STATUSES

#: Human decisions: allowed source statuses and target status.
HUMAN_TRANSITIONS: dict[Action, tuple[frozenset[Status], Status | None]] = {
    Action.TAKE_TRIAGE: (frozenset({Status.TRIAGE_ERFORDERLICH}), None),  # target depends on analysis state
    Action.START_REVIEW: (frozenset({Status.ENTWURF_BEREIT, Status.RUECKFRAGE, Status.ABGELEHNT}),
                          Status.IN_MEDIZINISCHER_PRUEFUNG),
    Action.APPROVE: (frozenset({Status.ENTWURF_BEREIT, Status.IN_MEDIZINISCHER_PRUEFUNG}), Status.FREIGEGEBEN),
    Action.REJECT: (frozenset({Status.ENTWURF_BEREIT, Status.IN_MEDIZINISCHER_PRUEFUNG, Status.FREIGEGEBEN}),
                    Status.ABGELEHNT),
    Action.REQUEST_INFO: (frozenset({Status.ENTWURF_BEREIT, Status.IN_MEDIZINISCHER_PRUEFUNG}), Status.RUECKFRAGE),
    Action.CLOSE: (_ALL_OPEN, Status.ABGESCHLOSSEN),
    Action.CLOSE_NON_REPORTABLE: (_ALL_OPEN - {Status.MELDUNG_ERFASST}, Status.ABGESCHLOSSEN_NICHT_MELDEPFLICHTIG),
    Action.RECORD_SUBMISSION: (frozenset({Status.FREIGEGEBEN}), Status.MELDUNG_ERFASST),
    Action.RERUN_ANALYSIS: (frozenset({Status.EINGEGANGEN, Status.NICHT_LESBAR, Status.ENTWURF_BEREIT,
                                       Status.IN_MEDIZINISCHER_PRUEFUNG, Status.RUECKFRAGE, Status.ABGELEHNT,
                                       Status.FREIGEGEBEN}), Status.IN_ANALYSE),
}

#: Technical transitions the system may make. TRIAGE_ERFORDERLICH has no system exit:
#: only a human moves a critical case on.
SYSTEM_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.EINGEGANGEN: frozenset({Status.NICHT_LESBAR, Status.TRIAGE_ERFORDERLICH, Status.IN_ANALYSE}),
    Status.NICHT_LESBAR: frozenset({Status.EINGEGANGEN, Status.TRIAGE_ERFORDERLICH, Status.IN_ANALYSE}),
    Status.IN_ANALYSE: frozenset({Status.ENTWURF_BEREIT, Status.TRIAGE_ERFORDERLICH}),
    Status.ENTWURF_BEREIT: frozenset({Status.IN_ANALYSE, Status.TRIAGE_ERFORDERLICH}),
    Status.IN_MEDIZINISCHER_PRUEFUNG: frozenset({Status.IN_ANALYSE, Status.TRIAGE_ERFORDERLICH}),
    Status.RUECKFRAGE: frozenset({Status.IN_ANALYSE, Status.TRIAGE_ERFORDERLICH}),
    Status.FREIGEGEBEN: frozenset({Status.IN_MEDIZINISCHER_PRUEFUNG, Status.TRIAGE_ERFORDERLICH}),
    Status.ABGELEHNT: frozenset({Status.IN_ANALYSE, Status.TRIAGE_ERFORDERLICH}),
    Status.MELDUNG_ERFASST: frozenset({Status.TRIAGE_ERFORDERLICH}),
    Status.ABGESCHLOSSEN: frozenset({Status.TRIAGE_ERFORDERLICH}),
    Status.ABGESCHLOSSEN_NICHT_MELDEPFLICHTIG: frozenset({Status.TRIAGE_ERFORDERLICH}),
    Status.TRIAGE_ERFORDERLICH: frozenset(),
}

#: Case metadata a human may edit (with reason; logged with user, old and new value).
EDITABLE_FIELDS: tuple[str, ...] = (
    "product_family", "model", "lot", "udi_di", "patient_reference", "incident_state", "event_date",
    "implant_date", "implant_site", "reporter_reference", "outcome", "note",
)

ANALYSIS_PENDING, ANALYSIS_RUNNING, ANALYSIS_DONE, ANALYSIS_FAILED = "PENDING", "RUNNING", "DONE", "FAILED"
TASK_TRIAGE, TASK_UNREADABLE, TASK_UNMATCHED, TASK_REREVIEW = "TRIAGE", "UNREADABLE", "UNMATCHED", "RE_REVIEW"
RECEIPT_STATUSES: tuple[str, ...] = ("confirmed", "unconfirmed")


class WorkflowError(Exception):
    """Base class of refused workflow actions."""


class PermissionDenied(WorkflowError):
    pass


class ReasonRequired(WorkflowError):
    pass


class InvalidTransition(WorkflowError):
    pass


class ApprovalRequired(WorkflowError):
    pass


def _actor_audit_type(actor: Actor) -> str:
    return actor.actor_type.value if isinstance(actor.actor_type, ActorType) else str(actor.actor_type)


class Workflow:
    """State machine and human decisions on top of :class:`pms.store.Store`."""

    def __init__(self, store: Store, *, clock: Clock | None = None, calendar: dl.Calendar | None = None,
                 alert_sink: Callable[[Alert, Case], None] | None = None) -> None:
        self.store = store
        self.clock = clock or store.clock or SystemClock()
        self.calendar = calendar or dl.Calendar()
        self.alert_sink = alert_sink
        self._after_commit: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ permissions
    @staticmethod
    def can(actor: Actor, action: Action) -> bool:
        """Pure permission check (no audit)."""
        if actor.actor_type in (ActorType.AGENT, ActorType.RULE):
            return action in NON_HUMAN_ACTIONS
        if actor.actor_type == ActorType.SYSTEM:
            return action in NON_HUMAN_ACTIONS or action in SYSTEM_ONLY_ACTIONS
        if not actor.authenticated:
            return False
        return bool(set(actor.roles) & PERMISSIONS.get(action, frozenset()))

    def authorize(self, actor: Actor, action: Action, *, case_id: str | None = None, source: str = "api",
                  agent_meta: dict[str, Any] | None = None) -> None:
        """Raise :class:`PermissionDenied` and write an ACCESS_DENIED record when refused."""
        if self.can(actor, action):
            return
        meta = agent_meta or {}
        if actor.actor_type == ActorType.HUMAN:
            reason = (f"action '{action.value}' refused for roles {sorted(actor.roles) or ['none']}"
                      f"{'' if actor.authenticated else ' (not authenticated)'}")
        else:
            reason = (f"action '{action.value}' refused: {_actor_audit_type(actor)} actors have no decision "
                      f"rights (concept 7.2)")
        # A refused rule, agent or system attempt is recorded like a human one (exactly one record); a
        # rule record carries the rule versions so that the refusal itself is a complete audit record.
        self.store.audit.append(
            event_type=A.ACCESS_DENIED, source=source, actor_type=_actor_audit_type(actor),
            actor_id=actor.actor_id or "anonymous", case_id=case_id, reason=reason,
            after={"action": action.value, "actor_type": _actor_audit_type(actor)},
            agent_version=meta.get("agent_version"), model=meta.get("model"),
            model_version=meta.get("model_version"), prompt_version=meta.get("prompt_version"),
            rule_versions=list(ALL_RULE_VERSIONS) if actor.actor_type == ActorType.RULE else None,
            ts=self.clock.now(),
        )
        raise PermissionDenied(f"{actor.actor_id} may not {action.value}")

    @staticmethod
    def _reason(action: Action, reason: str | None) -> str | None:
        text = (reason or "").strip()
        if action in REASON_MANDATORY and not text:
            raise ReasonRequired(f"a reason is mandatory for '{action.value}'")
        return text or None

    # ------------------------------------------------------------------ helpers
    def _now(self, now: _dt.datetime | None = None) -> _dt.datetime:
        return parse_utc(now) if now is not None else self.clock.now()

    def _flush_after_commit(self) -> None:
        """Run deferred callbacks (alert sink) once no transaction is open any more."""
        if self.store.conn.in_transaction:
            return
        callbacks, self._after_commit = self._after_commit, []
        for cb in callbacks:
            cb()

    def _set_status(self, case: Case, new: Status, *, actor: Actor, source: str, reason: str | None,
                    now: _dt.datetime, rule_versions: Iterable[str] | None = None) -> Case:
        if case.status == new:
            return case
        updated = self.store.update_case(case.case_id, status=new.value, updated_at=iso_utc(now, ms=True))
        self.store.audit.append(
            event_type=A.STATUS_CHANGED, source=source, actor_type=_actor_audit_type(actor), actor_id=actor.actor_id,
            case_id=case.case_id, before={"status": case.status}, after={"status": new.value}, reason=reason,
            rule_versions=list(rule_versions) if rule_versions else (
                list(ALL_RULE_VERSIONS) if actor.actor_type == ActorType.RULE else None),
            ts=now,
        )
        return updated

    def system_transition(self, case_id: str, new: Status, *, reason: str, source: str = "system",
                          actor: Actor | None = None, now: _dt.datetime | None = None) -> Case:
        """Technical status change by the system (validated against SYSTEM_TRANSITIONS)."""
        actor = actor or Actor.system()
        if actor.actor_type == ActorType.HUMAN:
            raise InvalidTransition("system transitions are not available to human actors")
        at = self._now(now)
        with self.store.transaction():
            case = self.store.get_case(case_id)
            current = Status(case.status)
            if new == current:
                return case
            if new not in SYSTEM_TRANSITIONS[current]:
                raise InvalidTransition(f"system may not move {current} -> {new}")
            return self._set_status(case, new, actor=actor, source=source, reason=reason, now=at)

    def _decision(self, case: Case, kind: str, actor: Actor, reason: str | None, status_after: str, now: _dt.datetime,
                  *, reason_code: str | None = None, source: str, before: Any = None, after: Any = None) -> None:
        self.store.insert_decision(case_id=case.case_id, kind=kind, reason=reason or "", reason_code=reason_code,
                                   actor_id=actor.actor_id, status_before=case.status, status_after=status_after,
                                   ts=iso_utc(now, ms=True))
        self.store.audit.append(
            event_type=A.DECISION, source=source, actor_type=_actor_audit_type(actor), actor_id=actor.actor_id,
            case_id=case.case_id, before=before if before is not None else {"status": case.status},
            after={"decision": kind, "status": status_after, "reason_code": reason_code, **(after or {})},
            reason=reason, ts=now,
        )

    # ------------------------------------------------------------------ content and approvals
    def content_hash(self, case_id: str) -> str:
        """SHA-256 of the reviewable content: metadata, signals, facts, agent runs,
        recommendations, correlations and the content version counter."""
        s = self.store
        case = s.get_case(case_id)
        payload = {
            "case_id": case.case_id,
            "content_version": case.content_version,
            "metadata": case.metadata,
            "product_family": case.product_family,
            "signals": [[x.signal_id, x.text_sha256] for x in s.list_signals(case_id)],
            "facts": [[f.fact_id, f.name, f.value, f.origin] for f in s.list_facts(case_id)],
            "agent_runs": [[r.run_id, r.status] for r in s.list_agent_runs(case_id)],
            "recommendations": [[r.rec_id, r.content_hash] for r in s.list_recommendations(case_id)],
            "correlations": [c.corr_id for c in s.list_correlations(case_id)],
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def content_changed(self, case_id: str, *, kind: str, actor: Actor | None = None, source: str = "system",
                        now: _dt.datetime | None = None) -> Case:
        """Record a content change: bump the version and void any approval (concept 7.2)."""
        actor = actor or Actor.system()
        at = self._now(now)
        with self.store.transaction():
            case = self.store.get_case(case_id)
            case = self.store.update_case(case_id, content_version=case.content_version + 1,
                                          updated_at=iso_utc(at, ms=True))
            approval = self.store.valid_approval(case_id)
            if approval is not None:
                reason = f"content changed after approval ({kind})"
                self.store.update_approval(approval.approval_id, valid=False, voided_at=iso_utc(at, ms=True),
                                           voided_reason=reason)
                self.store.audit.append(
                    event_type=A.APPROVAL_VOIDED, source=source, actor_type=_actor_audit_type(actor),
                    actor_id=actor.actor_id, case_id=case_id,
                    before={"approval_id": approval.approval_id, "valid": True, "content_hash": approval.content_hash},
                    after={"valid": False, "change": kind}, reason=reason,
                    rule_versions=list(ALL_RULE_VERSIONS) if actor.actor_type == ActorType.RULE else None, ts=at,
                )
                if case.status == Status.FREIGEGEBEN:
                    case = self._set_status(case, Status.IN_MEDIZINISCHER_PRUEFUNG, actor=Actor.system(),
                                            source="system", reason=reason, now=at)
            return case

    # ------------------------------------------------------------------ urgency
    def _open_triage(self, case: Case, reason: str, *, actor: Actor, source: str, now: _dt.datetime,
                     rule_versions: Iterable[str] | None = None, detail: dict[str, Any] | None = None) -> Case:
        """Critical: triage status, a visible triage task and an alert request."""
        case = self._set_status(case, Status.TRIAGE_ERFORDERLICH, actor=actor, source=source, reason=reason, now=now,
                                rule_versions=rule_versions)
        open_triage = self.store.list_tasks(case_id=case.case_id, status="OPEN", kind=TASK_TRIAGE)
        if not open_triage:
            self.create_task(case.case_id, TASK_TRIAGE, f"Triage required: {reason}", actor=actor, source=source,
                             now=now, detail={"reason": reason, **(detail or {})}, rule_versions=rule_versions)
        self._request_alert(case, reason, actor=actor, source=source, now=now, rule_versions=rule_versions,
                            detail=detail)
        return case

    def _request_alert(self, case: Case, reason: str, *, actor: Actor, source: str, now: _dt.datetime,
                       rule_versions: Iterable[str] | None = None, detail: dict[str, Any] | None = None) -> Alert:
        alert = self.store.insert_alert(case_id=case.case_id, reason=reason, created_at=iso_utc(now, ms=True),
                                        detail={"case_key": case.case_key, **(detail or {})})
        self.store.audit.append(
            event_type=A.ALERT_REQUESTED, source=source, actor_type=_actor_audit_type(actor), actor_id=actor.actor_id,
            case_id=case.case_id, signal_id=(detail or {}).get("signal_id"),
            after={"alert_id": alert.alert_id, "reason": reason},
            rule_versions=list(rule_versions) if rule_versions else (
                list(ALL_RULE_VERSIONS) if actor.actor_type == ActorType.RULE else None),
            ts=now,
        )
        if self.alert_sink is not None:
            sink, snapshot = self.alert_sink, case
            self._after_commit.append(lambda: sink(alert, snapshot))
        return alert

    def triage_new_critical_signal(self, case_id: str, *, signal_id: str, reason: str, actor: Actor, source: str,
                                   injection_flag: bool = False, now: _dt.datetime | None = None,
                                   rule_versions: Iterable[str] | None = None) -> Case:
        """A critical signal on a case that is ALREADY critical (duplicate, follow-up or a copy such as L12).

        Raising urgency changes nothing then, yet every critical letter needs its own triage task and alert
        (Goal Card check 4): the case stays critical and (again) needs a human triage, a triage task references
        the new signal and a new alert "new signal on critical case ..." is requested. Deadlines are not
        touched (no reset)."""
        at = self._now(now)
        with self.store.transaction():
            case = self.store.get_case(case_id)
            if not case.critical:
                raise InvalidTransition("the case is not critical - raising urgency opens the triage")
            text = f"new signal on critical case {case.case_key}: {reason}"
            detail = {"signal_id": signal_id, "injection_flag": bool(injection_flag),
                      "kind": "new_signal_on_critical_case"}
            case = self._set_status(case, Status.TRIAGE_ERFORDERLICH, actor=actor, source=source, reason=text,
                                    now=at, rule_versions=rule_versions)
            open_triage = self.store.list_tasks(case_id=case_id, status="OPEN", kind=TASK_TRIAGE)
            if not any(t.detail.get("signal_id") == signal_id for t in open_triage):
                self.create_task(case_id, TASK_TRIAGE, f"Triage required: {text}", actor=actor, source=source,
                                 now=at, detail={"reason": text, **detail}, rule_versions=rule_versions)
            self._request_alert(case, text, actor=actor, source=source, now=at, rule_versions=rule_versions,
                                detail=detail)
        self._flush_after_commit()
        return self.store.get_case(case_id)

    def raise_urgency(self, case_id: str, actor: Actor, reason: str | None = None, *, source: str = "api",
                      now: _dt.datetime | None = None, rule_versions: Iterable[str] | None = None,
                      window_key: str | None = None, agent_meta: dict[str, Any] | None = None,
                      triage_detail: dict[str, Any] | None = None) -> Case:
        """Raise urgency (analyst, regulatory, medical reviewer, rule or AI). Never lowers."""
        self.authorize(actor, Action.RAISE_URGENCY, case_id=case_id, source=source, agent_meta=agent_meta)
        at = self._now(now)
        text = (reason or "").strip() or f"urgency raised by {actor.actor_id}"
        meta = agent_meta or {}
        with self.store.transaction():
            case = self.store.get_case(case_id)
            state = crit.UrgencyState.from_dict(case.urgency)
            if window_key and window_key in state.window_raised_for:
                return case
            new_state, became = crit.raise_urgency(state, reason=text, source=_actor_audit_type(actor), at=at,
                                                   window_key=window_key)
            case = self.store.update_case(case_id, critical=True, urgency=new_state.to_dict(),
                                          updated_at=iso_utc(at, ms=True))
            if became:
                self.store.audit.append(
                    event_type=A.URGENCY_RAISED, source=source, actor_type=_actor_audit_type(actor),
                    actor_id=actor.actor_id, case_id=case_id, before={"critical": False},
                    after={"critical": True, "reason": text}, reason=text,
                    rule_versions=list(rule_versions) if rule_versions else (
                        list(ALL_RULE_VERSIONS) if actor.actor_type == ActorType.RULE else None),
                    agent_version=meta.get("agent_version"), model=meta.get("model"),
                    model_version=meta.get("model_version"), prompt_version=meta.get("prompt_version"), ts=at,
                )
                # Triage status, task and alert are set by the system (concept 7.1) - or by the
                # rule when a rule raised the urgency.
                followup = actor if actor.actor_type == ActorType.RULE else Actor.system()
                case = self._open_triage(case, text, actor=followup, source=source if followup is actor else "system",
                                         now=at, rule_versions=rule_versions if followup is actor else None,
                                         detail=triage_detail)
        self._flush_after_commit()
        return case

    def lower_urgency(self, case_id: str, actor: Actor, reason: str, *, source: str = "ui",
                      now: _dt.datetime | None = None) -> Case:
        """Lower urgency - medical reviewer with a reason only (status is not changed)."""
        self.authorize(actor, Action.LOWER_URGENCY, case_id=case_id, source=source)
        text = self._reason(Action.LOWER_URGENCY, reason)
        at = self._now(now)
        with self.store.transaction():
            case = self.store.get_case(case_id)
            if not case.critical:
                raise InvalidTransition("case is not critical")
            state = crit.lower_urgency(crit.UrgencyState.from_dict(case.urgency), actor_id=actor.actor_id,
                                       actor_roles=actor.roles, reason=text or "", at=at)
            updated = self.store.update_case(case_id, critical=False, urgency=state.to_dict(),
                                             updated_at=iso_utc(at, ms=True))
            self.store.audit.append(
                event_type=A.URGENCY_LOWERED, source=source, actor_type="human", actor_id=actor.actor_id,
                case_id=case_id, before={"critical": True}, after={"critical": False}, reason=text, ts=at,
            )
        return updated

    def apply_ai_reading(self, case_id: str, agent: Actor, ai_severity: str | None, *, agent_meta: dict[str, Any],
                         now: _dt.datetime | None = None) -> str:
        """Second detector (A1, A3): raise when the AI reads a serious event; a lower
        reading (e.g. "not serious") never lowers urgency. Returns the outcome."""
        at = self._now(now)
        case = self.store.get_case(case_id)
        _, outcome = crit.apply_ai_reading(crit.UrgencyState.from_dict(case.urgency), ai_severity,
                                           agent_id=agent.actor_id, at=at)
        if outcome == "raised":
            self.raise_urgency(case_id, agent, f"AI reading {ai_severity} ({agent.actor_id})", source="agent",
                               now=at, agent_meta=agent_meta)
        return outcome

    # ------------------------------------------------------------------ tasks
    def create_task(self, case_id: str | None, kind: str, title: str, *, actor: Actor, source: str,
                    now: _dt.datetime, detail: dict[str, Any] | None = None,
                    rule_versions: Iterable[str] | None = None):
        task = self.store.insert_task(case_id=case_id, kind=kind, title=title, detail=detail or {},
                                      created_at=iso_utc(now, ms=True), created_by=actor.actor_id)
        self.store.audit.append(
            event_type=A.TASK_CREATED, source=source, actor_type=_actor_audit_type(actor), actor_id=actor.actor_id,
            case_id=case_id, after={"task_id": task.task_id, "kind": kind, "title": title},
            rule_versions=list(rule_versions) if rule_versions else (
                list(ALL_RULE_VERSIONS) if actor.actor_type == ActorType.RULE else None),
            ts=now,
        )
        return task

    def _close_tasks(self, case_id: str, kind: str, actor: Actor, reason: str, source: str, now: _dt.datetime) -> None:
        for task in self.store.list_tasks(case_id=case_id, status="OPEN", kind=kind):
            self.store.update_task(task.task_id, status="DONE", closed_at=iso_utc(now, ms=True),
                                   closed_by=actor.actor_id, close_reason=reason)
            self.store.audit.append(
                event_type=A.TASK_CLOSED, source=source, actor_type=_actor_audit_type(actor), actor_id=actor.actor_id,
                case_id=case_id, before={"task_id": task.task_id, "status": "OPEN"}, after={"status": "DONE"},
                reason=reason, ts=now,
            )

    # ------------------------------------------------------------------ human actions
    def _check_transition(self, case: Case, action: Action) -> Status | None:
        allowed, target = HUMAN_TRANSITIONS[action]
        if Status(case.status) not in allowed:
            raise InvalidTransition(f"'{action.value}' is not allowed in status {case.status}")
        return target

    def take_triage(self, case_id: str, actor: Actor, reason: str | None = None, *, source: str = "ui",
                    now: _dt.datetime | None = None) -> Case:
        """A human takes the triage of a critical case (analyst or medical reviewer)."""
        self.authorize(actor, Action.TAKE_TRIAGE, case_id=case_id, source=source)
        at = self._now(now)
        text = (reason or "").strip() or f"triage taken by {actor.actor_id}"
        with self.store.transaction():
            case = self.store.get_case(case_id)
            self._check_transition(case, Action.TAKE_TRIAGE)
            target = Status.ENTWURF_BEREIT if case.analysis_state == ANALYSIS_DONE else Status.IN_ANALYSE
            self._close_tasks(case_id, TASK_TRIAGE, actor, text, source, at)
            return self._set_status(case, target, actor=actor, source=source, reason=text, now=at)

    def start_review(self, case_id: str, actor: Actor, reason: str | None = None, *, source: str = "ui",
                     now: _dt.datetime | None = None) -> Case:
        self.authorize(actor, Action.START_REVIEW, case_id=case_id, source=source)
        at = self._now(now)
        with self.store.transaction():
            case = self.store.get_case(case_id)
            target = self._check_transition(case, Action.START_REVIEW)
            return self._set_status(case, target, actor=actor, source=source,
                                    reason=(reason or "").strip() or "review taken over", now=at)

    def approve(self, case_id: str, actor: Actor, reason: str, *, source: str = "ui",
                now: _dt.datetime | None = None) -> Approval:
        """Approve exactly the current version (bound to its content hash)."""
        self.authorize(actor, Action.APPROVE, case_id=case_id, source=source)
        text = self._reason(Action.APPROVE, reason)
        at = self._now(now)
        with self.store.transaction():
            case = self.store.get_case(case_id)
            target = self._check_transition(case, Action.APPROVE)
            digest = self.content_hash(case_id)
            approval = self.store.insert_approval(case_id=case_id, content_hash=digest,
                                                  content_version=case.content_version, reviewer_id=actor.actor_id,
                                                  reason=text, ts=iso_utc(at, ms=True), valid=True)
            self._decision(case, "approve", actor, text, target.value, at, source=source,
                           after={"approval_id": approval.approval_id, "content_hash": digest})
            rec = self.store.latest_recommendation(case_id)
            finding = self.store.insert_finding(
                kind="approved_assessment", ref=None, case_id=case_id, status="approved",
                summary=f"Assessment of case {case.case_key} approved by the medical reviewer",
                content_hash=digest, reviewer_id=actor.actor_id, reason=text,
                data={"recommendation_id": rec.rec_id if rec else None,
                      "recommendation_version": rec.version if rec else None},
                created_at=iso_utc(at, ms=True), updated_at=iso_utc(at, ms=True),
            )
            self.store.audit.append(
                event_type=A.FINDING_CREATED, source=source, actor_type="human", actor_id=actor.actor_id,
                case_id=case_id, after={"finding_id": finding.finding_id, "kind": finding.kind,
                                        "content_hash": digest}, reason=text, ts=at,
            )
            self._set_status(case, target, actor=actor, source=source, reason=text, now=at)
        return approval

    def reject(self, case_id: str, actor: Actor, reason: str, reason_code: str, *, source: str = "ui",
               now: _dt.datetime | None = None) -> Case:
        self.authorize(actor, Action.REJECT, case_id=case_id, source=source)
        text = self._reason(Action.REJECT, reason)
        if reason_code not in REJECT_REASON_CODES:
            raise ReasonRequired(f"reject needs a reason code {sorted(REJECT_REASON_CODES)}")
        at = self._now(now)
        with self.store.transaction():
            case = self.store.get_case(case_id)
            target = self._check_transition(case, Action.REJECT)
            approval = self.store.valid_approval(case_id)
            if approval is not None:
                self.store.update_approval(approval.approval_id, valid=False, voided_at=iso_utc(at, ms=True),
                                           voided_reason="rejected by the medical reviewer")
            self._decision(case, "reject", actor, text, target.value, at, reason_code=reason_code, source=source)
            return self._set_status(case, target, actor=actor, source=source, reason=text, now=at)

    def request_info(self, case_id: str, actor: Actor, reason: str, *, source: str = "ui",
                     now: _dt.datetime | None = None) -> Case:
        """Ask for missing data. Nothing is sent; deadline monitoring continues."""
        self.authorize(actor, Action.REQUEST_INFO, case_id=case_id, source=source)
        text = self._reason(Action.REQUEST_INFO, reason)
        at = self._now(now)
        with self.store.transaction():
            case = self.store.get_case(case_id)
            target = self._check_transition(case, Action.REQUEST_INFO)
            self._decision(case, "request_info", actor, text, target.value, at, source=source)
            return self._set_status(case, target, actor=actor, source=source, reason=text, now=at)

    def close(self, case_id: str, actor: Actor, reason: str, *, source: str = "ui",
              now: _dt.datetime | None = None) -> Case:
        self.authorize(actor, Action.CLOSE, case_id=case_id, source=source)
        text = self._reason(Action.CLOSE, reason)
        at = self._now(now)
        with self.store.transaction():
            case = self.store.get_case(case_id)
            target = self._check_transition(case, Action.CLOSE)
            self._decision(case, "close", actor, text, target.value, at, source=source)
            return self._set_status(case, target, actor=actor, source=source, reason=text, now=at)

    def close_non_reportable(self, case_id: str, actor: Actor, reason: str, *, source: str = "ui",
                             now: _dt.datetime | None = None) -> Case:
        """Close as non-reportable. The clock is NOT stopped; the deadline is shown as
        'entfällt laut menschlicher Entscheidung' with user and reason (decision Q-4)."""
        self.authorize(actor, Action.CLOSE_NON_REPORTABLE, case_id=case_id, source=source)
        text = self._reason(Action.CLOSE_NON_REPORTABLE, reason)
        at = self._now(now)
        with self.store.transaction():
            case = self.store.get_case(case_id)
            target = self._check_transition(case, Action.CLOSE_NON_REPORTABLE)
            waiver = {"label": dl.WAIVED_LABEL_DE, "label_en": dl.WAIVED_LABEL_EN, "by": actor.actor_id,
                      "reason": text, "at": iso_utc(at, ms=True)}
            self.store.update_case(case_id, deadline_waiver=waiver, updated_at=iso_utc(at, ms=True))
            self._decision(case, "non_reportable", actor, text, target.value, at, source=source,
                           after={"deadline_waiver": waiver})
            return self._set_status(self.store.get_case(case_id), target, actor=actor, source=source, reason=text,
                                    now=at)

    def decide(self, case_id: str, actor: Actor, decision: str, reason: str, *, reason_code: str | None = None,
               source: str = "api", now: _dt.datetime | None = None) -> Any:
        """Dispatcher for ``POST /api/cases/{id}/decision``."""
        handlers: dict[str, Callable[[], Any]] = {
            "approve": lambda: self.approve(case_id, actor, reason, source=source, now=now),
            "reject": lambda: self.reject(case_id, actor, reason, reason_code or "", source=source, now=now),
            "close": lambda: self.close(case_id, actor, reason, source=source, now=now),
            "non_reportable": lambda: self.close_non_reportable(case_id, actor, reason, source=source, now=now),
            "request_info": lambda: self.request_info(case_id, actor, reason, source=source, now=now),
        }
        if decision not in handlers:
            raise InvalidTransition(f"unknown decision {decision!r}")
        return handlers[decision]()

    def record_submission(self, case_id: str, actor: Actor, *, channel_label: str, reference: str,
                          submitted_on: str, receipt_status: str = "unconfirmed", reason: str | None = None,
                          source: str = "ui", now: _dt.datetime | None = None) -> Submission:
        """Record a submission made OUTSIDE the system (regulatory role, approved version
        only). Stops the deadline clock. Never sends anything."""
        self.authorize(actor, Action.RECORD_SUBMISSION, case_id=case_id, source=source)
        if not channel_label.strip() or not reference.strip():
            raise ReasonRequired("channel label and reference are mandatory")
        if receipt_status not in RECEIPT_STATUSES:
            raise ReasonRequired(f"receipt status must be one of {RECEIPT_STATUSES}")
        _dt.date.fromisoformat(submitted_on)
        at = self._now(now)
        with self.store.transaction():
            case = self.store.get_case(case_id)
            target = self._check_transition(case, Action.RECORD_SUBMISSION)
            approval = self.store.valid_approval(case_id)
            if approval is None:
                raise ApprovalRequired("no valid approval for the current version")
            if approval.content_hash != self.content_hash(case_id):
                raise ApprovalRequired("the content changed after approval - a new review is required")
            sub = self.store.insert_submission(
                case_id=case_id, approval_id=approval.approval_id, content_hash=approval.content_hash,
                channel_label=channel_label.strip(), reference=reference.strip(), submitted_on=submitted_on,
                receipt_status=receipt_status, recorded_by=actor.actor_id, ts=iso_utc(at, ms=True),
            )
            stopped = []
            for row in self.store.list_deadlines(case_id):
                if row.stopped_by_submission_id is None:
                    self.store.update_deadline(row.deadline_id, stopped_by_submission_id=sub.submission_id,
                                               stopped_at=iso_utc(at, ms=True))
                    stopped.append(row.deadline_id)
            self.store.insert_decision(case_id=case_id, kind="record_submission", reason=(reason or "").strip(),
                                       reason_code=None, actor_id=actor.actor_id, status_before=case.status,
                                       status_after=target.value, ts=iso_utc(at, ms=True))
            self.store.audit.append(
                event_type=A.SUBMISSION_RECORDED, source=source, actor_type="human", actor_id=actor.actor_id,
                case_id=case_id, before={"clock": "running", "deadlines": stopped},
                after={"submission_id": sub.submission_id, "approval_id": approval.approval_id,
                       "channel_label": sub.channel_label, "reference": sub.reference,
                       "submitted_on": submitted_on, "receipt_status": receipt_status, "clock": "stopped"},
                reason=(reason or "").strip() or "submission made outside the system recorded", ts=at,
            )
            self._set_status(case, target, actor=actor, source=source, reason="submission recorded", now=at)
        return sub

    def edit_metadata(self, case_id: str, actor: Actor, field: str, new_value: Any, reason: str, *,
                      source: str = "ui", now: _dt.datetime | None = None) -> MetadataEdit:
        """Change one metadata field (reason mandatory; user, old and new value logged)."""
        self.authorize(actor, Action.EDIT_METADATA, case_id=case_id, source=source)
        text = self._reason(Action.EDIT_METADATA, reason)
        if field not in EDITABLE_FIELDS:
            raise InvalidTransition(f"field {field!r} is not editable; allowed: {EDITABLE_FIELDS}")
        at = self._now(now)
        with self.store.transaction():
            case = self.store.get_case(case_id)
            old = case.metadata.get(field, case.product_family if field == "product_family" else None)
            metadata = {**case.metadata, field: new_value}
            values: dict[str, Any] = {"metadata": metadata, "updated_at": iso_utc(at, ms=True)}
            if field == "product_family":
                values["product_family"] = new_value
            self.store.update_case(case_id, **values)
            edit = self.store.insert_metadata_edit(case_id=case_id, field=field, old_value=old, new_value=new_value,
                                                   reason=text, user_id=actor.actor_id, ts=iso_utc(at, ms=True))
            self.store.audit.append(
                event_type=A.METADATA_EDITED, source=source, actor_type="human", actor_id=actor.actor_id,
                case_id=case_id, before={field: old}, after={field: new_value}, reason=text, ts=at,
            )
            self.content_changed(case_id, kind="metadata", actor=actor, source=source, now=at)
        return edit

    def request_reanalysis(self, case_id: str, actor: Actor, reason: str | None = None, *, source: str = "ui",
                           now: _dt.datetime | None = None) -> Case:
        """Analyst or medical reviewer re-triggers the analysis (any approval is voided)."""
        self.authorize(actor, Action.RERUN_ANALYSIS, case_id=case_id, source=source)
        at = self._now(now)
        text = (reason or "").strip() or f"re-analysis requested by {actor.actor_id}"
        with self.store.transaction():
            case = self.store.get_case(case_id)
            target = self._check_transition(case, Action.RERUN_ANALYSIS)
            approval = self.store.valid_approval(case_id)
            if approval is not None:
                self.store.update_approval(approval.approval_id, valid=False, voided_at=iso_utc(at, ms=True),
                                           voided_reason="re-analysis requested")
                self.store.audit.append(
                    event_type=A.APPROVAL_VOIDED, source=source, actor_type="human", actor_id=actor.actor_id,
                    case_id=case_id, before={"approval_id": approval.approval_id, "valid": True},
                    after={"valid": False}, reason="re-analysis requested", ts=at,
                )
            self.store.update_case(case_id, analysis_state=ANALYSIS_PENDING)
            return self._set_status(self.store.get_case(case_id), target, actor=actor, source=source, reason=text,
                                    now=at)

    def classify_deterioration_anticipated(self, case_id: str, actor: Actor, reason: str, *,
                                           source: str = "ui", now: _dt.datetime | None = None) -> list[str]:
        """Medical reviewer classifies a serious deterioration as anticipated: the 10-day
        candidate is superseded (still shown) and a 15-day candidate is added (A-03)."""
        self.authorize(actor, Action.CLASSIFY_ANTICIPATED, case_id=case_id, source=source)
        text = self._reason(Action.CLASSIFY_ANTICIPATED, reason)
        at = self._now(now)
        added: list[str] = []
        with self.store.transaction():
            case = self.store.get_case(case_id)
            rows = self.store.list_deadlines(case_id)
            sd_rows = [r for r in rows if r.basis == dl.SERIOUS_DETERIORATION and r.superseded is None]
            if not sd_rows:
                raise InvalidTransition("no open serious-deterioration deadline to classify")
            if case.awareness_anchor is None:
                raise InvalidTransition("case has no awareness anchor")
            for row in sd_rows:
                info = {"by": actor.actor_id, "reason": text, "at": iso_utc(at, ms=True),
                        "replaced_by_basis": dl.SERIOUS_DETERIORATION_ANTICIPATED}
                self.store.update_deadline(row.deadline_id, superseded=info)
                cand = dl.make_candidate(row.rule_set, dl.SERIOUS_DETERIORATION_ANTICIPATED,
                                         parse_utc(case.awareness_anchor), self.calendar)
                new_row = self.store.upsert_deadline(case_id, len(rows) + len(added), _candidate_values(cand, None))
                added.append(new_row.deadline_id)
            self._decision(case, "classify_deterioration_anticipated", actor, text, case.status, at, source=source,
                           before={"deadlines": [r.deadline_id for r in sd_rows]}, after={"added": added})
        return added

    # ------------------------------------------------------------------ agents and recommendations
    def record_agent_run(self, case_id: str, agent: Actor, *, agent_meta: dict[str, Any], status: str,
                         output: dict[str, Any] | None = None, usage: dict[str, Any] | None = None,
                         error: str | None = None, signal_id: str | None = None,
                         started_at: _dt.datetime | None = None, cost_estimate: dict[str, Any] | None = None,
                         now: _dt.datetime | None = None):
        """Store one agent run (always a draft) with one AGENT_RUN audit record.

        A finished run is a content change: it voids an existing approval."""
        if agent.actor_type != ActorType.AGENT:
            raise InvalidTransition("agent runs are recorded for agent actors only")
        at = self._now(now)
        with self.store.transaction():
            run = self.store.insert_agent_run(
                case_id=case_id, signal_id=signal_id, agent_id=agent.actor_id, status=status,
                model=agent_meta.get("model"), model_version=agent_meta.get("model_version"),
                prompt_version=agent_meta.get("prompt_version"), agent_version=agent_meta.get("agent_version"),
                started_at=iso_utc(started_at or at, ms=True), finished_at=iso_utc(at, ms=True),
                output=output, error=error, usage=usage, draft=True,
            )
            self.store.audit.append(
                event_type=A.AGENT_RUN, source="agent", actor_type="agent", actor_id=agent.actor_id,
                case_id=case_id, signal_id=signal_id, after={"run_id": run.run_id, "status": status, "draft": True,
                                                             "error": error},
                agent_version=agent_meta.get("agent_version"), model=agent_meta.get("model"),
                model_version=agent_meta.get("model_version"), prompt_version=agent_meta.get("prompt_version"),
                rule_versions=agent_meta.get("rule_versions"), corpus_version=agent_meta.get("corpus_version"),
                usage=usage, cost_estimate=cost_estimate, ts=at,
            )
            if status == "DONE":
                self.content_changed(case_id, kind="agent_run", actor=Actor.system(), now=at)
        return run

    def record_recommendation(self, case_id: str, agent: Actor, content: dict[str, Any], *,
                              agent_meta: dict[str, Any], run_id: str | None = None,
                              now: _dt.datetime | None = None) -> Recommendation:
        """Store a recommendation draft (always labelled draft / "Entwurf")."""
        at = self._now(now)
        body = {**content, "draft": True, "label": "Entwurf"}
        digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        with self.store.transaction():
            version = len(self.store.list_recommendations(case_id)) + 1
            rec = self.store.insert_recommendation(case_id=case_id, version=version, content=body,
                                                   content_hash=digest, draft=True, run_id=run_id,
                                                   created_at=iso_utc(at, ms=True))
            self.store.audit.append(
                event_type=A.RECOMMENDATION_ADDED, source="agent", actor_type="agent", actor_id=agent.actor_id,
                case_id=case_id, after={"rec_id": rec.rec_id, "version": version, "content_hash": digest,
                                        "draft": True},
                agent_version=agent_meta.get("agent_version"), model=agent_meta.get("model"),
                model_version=agent_meta.get("model_version"), prompt_version=agent_meta.get("prompt_version"),
                ts=at,
            )
            self.content_changed(case_id, kind="recommendation", actor=Actor.system(), now=at)
        return rec

    def mark_analysis(self, case_id: str, state: str, *, now: _dt.datetime | None = None) -> Case:
        """Technical analysis progress. RUNNING moves EINGEGANGEN/ENTWURF_BEREIT cases to
        IN_ANALYSE; DONE moves IN_ANALYSE to ENTWURF_BEREIT. A critical case stays in
        TRIAGE_ERFORDERLICH until a human takes the triage."""
        if state not in (ANALYSIS_PENDING, ANALYSIS_RUNNING, ANALYSIS_DONE, ANALYSIS_FAILED):
            raise ValueError(f"unknown analysis state {state}")
        at = self._now(now)
        with self.store.transaction():
            case = self.store.update_case(case_id, analysis_state=state, updated_at=iso_utc(at, ms=True))
            current = Status(case.status)
            if state == ANALYSIS_RUNNING and current in (Status.EINGEGANGEN, Status.ENTWURF_BEREIT,
                                                         Status.NICHT_LESBAR):
                case = self._set_status(case, Status.IN_ANALYSE, actor=Actor.system(), source="system",
                                        reason="analysis started", now=at)
            elif state == ANALYSIS_DONE and current == Status.IN_ANALYSE:
                case = self._set_status(case, Status.ENTWURF_BEREIT, actor=Actor.system(), source="system",
                                        reason="draft ready", now=at)
        return case

    def record_cost(self, *, case_id: str | None, agent_id: str, model: str | None, input_tokens: int,
                    cached_tokens: int, output_tokens: int, cost_usd: float, price_table_version: str | None,
                    simulated: bool, run_id: str | None = None, now: _dt.datetime | None = None):
        """Store the estimated model cost of one agent run with one COST_RECORDED record."""
        at = self._now(now)
        with self.store.transaction():
            rec = self.store.insert_cost(case_id=case_id, run_id=run_id, agent_id=agent_id, model=model,
                                         input_tokens=int(input_tokens), cached_tokens=int(cached_tokens),
                                         output_tokens=int(output_tokens), cost_usd=float(cost_usd),
                                         price_table_version=price_table_version, simulated=simulated,
                                         ts=iso_utc(at, ms=True))
            self.store.audit.append(
                event_type=A.COST_RECORDED, source="system", actor_type="system", actor_id="cost-service",
                case_id=case_id, after={"cost_id": rec.cost_id, "agent_id": agent_id, "run_id": run_id,
                                        "simulated": simulated},
                usage={"input": int(input_tokens), "cached": int(cached_tokens), "output": int(output_tokens)},
                cost_estimate={"usd": float(cost_usd), "price_table_version": price_table_version}, ts=at,
            )
        return rec

    def mark_alert_sent(self, alert_id: str, *, channel: str, status: str = "SENT",
                        detail: dict[str, Any] | None = None, now: _dt.datetime | None = None) -> Alert:
        """Record the delivery of a triage alert (outbox file or demo e-mail) - one ALERT_SENT record."""
        if status not in ("SENT", "FAILED"):
            raise ValueError("alert status must be SENT or FAILED")
        at = self._now(now)
        with self.store.transaction():
            alert = self.store.update_alert(alert_id, status=status, channel=channel, sent_at=iso_utc(at, ms=True),
                                            detail=detail or {})
            self.store.audit.append(
                event_type=A.ALERT_SENT, source="system", actor_type="system", actor_id="alert-worker",
                case_id=alert.case_id, before={"alert_id": alert_id, "status": "PENDING"},
                after={"status": status, "channel": channel}, ts=at,
            )
        return alert

    # ------------------------------------------------------------------ findings
    def import_process_findings(self, items: Iterable[Any], actor: Actor, *, source: str = "seed",
                                now: _dt.datetime | None = None) -> list[Finding]:
        """Load the process findings (status Open) once; humans need the operator role."""
        self.authorize(actor, Action.LOAD_DEMO_DATA, source=source)
        at = self._now(now)
        created = []
        with self.store.transaction():
            existing = {f.ref for f in self.store.list_findings(kind="process_finding")}
            for item in items:
                if item.item_id in existing:
                    continue
                f = self.store.insert_finding(kind="process_finding", ref=item.item_id, case_id=None,
                                              status=item.status or "Open", summary=item.title, content_hash=None,
                                              reviewer_id=None, reason=None, data={"file": item.file},
                                              created_at=iso_utc(at, ms=True), updated_at=iso_utc(at, ms=True))
                self.store.audit.append(event_type=A.FINDING_CREATED, source=source,
                                        actor_type=_actor_audit_type(actor), actor_id=actor.actor_id,
                                        after={"finding_id": f.finding_id, "ref": f.ref, "status": f.status}, ts=at)
                created.append(f)
        return created

    def update_finding_status(self, finding_id: str, actor: Actor, new_status: str, reason: str, *,
                              source: str = "ui", now: _dt.datetime | None = None) -> Finding:
        """Withdraw an approved finding or close a process finding (medical reviewer, reason)."""
        self.authorize(actor, Action.MANAGE_FINDINGS, source=source)
        text = self._reason(Action.MANAGE_FINDINGS, reason)
        allowed = {"approved_assessment": {"withdrawn"}, "process_finding": {"Closed"}}
        at = self._now(now)
        with self.store.transaction():
            finding = self.store.get_finding(finding_id)
            if new_status not in allowed.get(finding.kind, set()):
                raise InvalidTransition(f"{finding.kind} cannot change to {new_status}")
            updated = self.store.update_finding(finding_id, status=new_status, reason=text,
                                                updated_at=iso_utc(at, ms=True))
            self.store.audit.append(
                event_type=A.FINDING_UPDATED, source=source, actor_type="human", actor_id=actor.actor_id,
                case_id=finding.case_id, before={"finding_id": finding_id, "status": finding.status},
                after={"status": new_status}, reason=text, ts=at,
            )
        return updated

    def flag_for_rereview(self, source_ids: Iterable[str], reason: str, *, actor: Actor | None = None,
                          now: _dt.datetime | None = None) -> list[str]:
        """Corpus change: mark every case whose correlation used an affected source (T24)."""
        actor = actor or Actor.system()
        at = self._now(now)
        affected_ids = set(source_ids)
        flagged: list[str] = []
        with self.store.transaction():
            for corr in self.store.list_correlations():
                refs = {h.get("ref") for h in corr.result.get("hits", [])}
                if refs & affected_ids and corr.case_id not in flagged:
                    flagged.append(corr.case_id)
            for case_id in flagged:
                self.create_task(case_id, TASK_REREVIEW, f"Re-review: {reason}", actor=actor, source="system",
                                 now=at, detail={"sources": sorted(affected_ids)})
                self.store.audit.append(event_type=A.REREVIEW_FLAGGED, source="system",
                                        actor_type=_actor_audit_type(actor), actor_id=actor.actor_id,
                                        case_id=case_id, after={"sources": sorted(affected_ids)}, reason=reason,
                                        ts=at)
        return flagged

    # ------------------------------------------------------------------ deadline overview
    def deadline_overview(self, case_id: str, now: _dt.datetime | None = None) -> dict[str, Any]:
        """All deadline candidates with status and countdown; the earliest open one drives
        priority. Read-only: no timer ever changes a status or makes a decision."""
        at = self._now(now)
        case = self.store.get_case(case_id)
        rows = self.store.list_deadlines(case_id)
        waived = case.deadline_waiver is not None and case.status == Status.ABGESCHLOSSEN_NICHT_MELDEPFLICHTIG
        items, inactive = [], []
        cands = []
        for row in rows:
            cand = dl.from_row(row.__dict__)
            cands.append(cand)
            stopped = row.stopped_by_submission_id is not None
            superseded = row.superseded is not None
            if stopped or superseded:
                inactive.append(cand.key)
            st = dl.status(cand, at, stopped=stopped, waived=waived, superseded=superseded)
            items.append({**cand.expected_view(), "deadline_id": row.deadline_id, "status": st,
                          "start_date": cand.start_date.isoformat(), "due_end_utc": iso_utc(cand.due_end_utc),
                          "seconds_remaining": dl.seconds_remaining(cand, at),
                          "label": dl.WAIVED_LABEL_DE if st == dl.WAIVED_BY_HUMAN else None,
                          "superseded": row.superseded, "stopped_by_submission_id": row.stopped_by_submission_id,
                          "sources": row.sources})
        drive = dl.driving(cands, exclude_keys=inactive)
        drive_status = None
        if drive is not None:
            drive_status = next(i["status"] for i in items
                                if (i["rule_set"], i["basis"]) == drive.key)
        clock = ("STOPPED_BY_SUBMISSION" if rows and len(inactive) == len(rows) and
                 any(r.stopped_by_submission_id for r in rows) else ("WAIVED" if waived else "RUNNING"))
        return {
            "case_id": case_id,
            "awareness_anchor": case.awareness_anchor,
            "candidates": items,
            "driving": None if drive is None else {**drive.expected_view(), "status": drive_status,
                                                   "seconds_remaining": dl.seconds_remaining(drive, at)},
            "overdue": drive_status == dl.OVERDUE,
            "clock": clock,
            "waiver": case.deadline_waiver if waived else None,
        }


def _candidate_values(cand: dl.DeadlineCandidate, origin_signal_id: str | None) -> dict[str, Any]:
    """Store representation of a deadline candidate."""
    return {
        "rule_set": cand.rule_set,
        "basis": cand.basis,
        "days": cand.days,
        "anchor_utc": iso_utc(cand.anchor_utc),
        "awareness_local_date": cand.awareness_local_date.isoformat(),
        "start_date": cand.start_date.isoformat(),
        "due_date": cand.due_date.isoformat(),
        "due_end_local": cand.due_end_local,
        "due_end_utc": iso_utc(cand.due_end_utc),
        "origin_signal_id": origin_signal_id,
        "sources": list(cand.sources),
    }


candidate_values = _candidate_values
