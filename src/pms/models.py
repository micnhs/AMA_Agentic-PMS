"""Shared data model: enums, timestamp helpers, clock and record dataclasses.

The dataclasses are plain value objects returned by :mod:`pms.store`. They carry no
business logic; rules live in :mod:`pms.rules`, the state machine in
:mod:`pms.workflow` and the audit trail in :mod:`pms.audit`.

Timestamps are timezone-aware ``datetime`` objects in UTC inside the code and ISO 8601
strings with a trailing ``Z`` in storage (``2026-11-16T08:15:00Z``; audit records use
milliseconds as required by concept section 8).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

UTC = _dt.timezone.utc


# --------------------------------------------------------------------------- time
def parse_utc(value: str | _dt.datetime) -> _dt.datetime:
    """Parse an ISO 8601 timestamp (``Z`` or offset) into an aware UTC datetime.

    Naive datetimes are rejected: an ambiguous timestamp must never silently shift a
    reporting deadline.
    """
    if isinstance(value, _dt.datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone is not accepted: {value!r}")
    return dt.astimezone(UTC)


def iso_utc(value: _dt.datetime, *, ms: bool = False) -> str:
    """Format an aware datetime as UTC ISO 8601 with ``Z`` (optionally milliseconds)."""
    dt = parse_utc(value)
    if ms:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(UTC)


class Clock(Protocol):
    """Source of 'now'. The golden run uses a fixed demo clock (assumption A-09)."""

    def now(self) -> _dt.datetime: ...


class SystemClock:
    """Wall clock in UTC."""

    def now(self) -> _dt.datetime:
        return utcnow()


class FixedClock:
    """Settable demo clock for reproducible deadlines, countdowns and overdue states."""

    def __init__(self, at: str | _dt.datetime) -> None:
        self._at = parse_utc(at)

    def now(self) -> _dt.datetime:
        return self._at

    def set(self, at: str | _dt.datetime) -> None:
        self._at = parse_utc(at)


# --------------------------------------------------------------------------- enums
class Role(StrEnum):
    """Human roles (concept section 7.3; Entra app roles PMS.Viewer ... PMS.Operator)."""

    VIEWER = "viewer"
    ANALYST = "analyst"
    REGULATORY = "regulatory"
    MEDICAL_REVIEWER = "medical_reviewer"
    OPERATOR = "operator"


class ActorType(StrEnum):
    """Audit actor types (concept section 8)."""

    HUMAN = "human"
    AGENT = "agent"
    RULE = "rule"
    SYSTEM = "system"


class Status(StrEnum):
    """Fixed case statuses (concept section 7.1). Values are the concept identifiers."""

    EINGEGANGEN = "EINGEGANGEN"
    NICHT_LESBAR = "NICHT_LESBAR"
    TRIAGE_ERFORDERLICH = "TRIAGE_ERFORDERLICH"
    IN_ANALYSE = "IN_ANALYSE"
    ENTWURF_BEREIT = "ENTWURF_BEREIT"
    IN_MEDIZINISCHER_PRUEFUNG = "IN_MEDIZINISCHER_PRUEFUNG"
    RUECKFRAGE = "RUECKFRAGE"
    FREIGEGEBEN = "FREIGEGEBEN"
    ABGELEHNT = "ABGELEHNT"
    MELDUNG_ERFASST = "MELDUNG_ERFASST"
    ABGESCHLOSSEN = "ABGESCHLOSSEN"
    ABGESCHLOSSEN_NICHT_MELDEPFLICHTIG = "ABGESCHLOSSEN_NICHT_MELDEPFLICHTIG"


#: English UI labels for the concept status identifiers.
STATUS_LABELS_EN: dict[Status, str] = {
    Status.EINGEGANGEN: "Received",
    Status.NICHT_LESBAR: "Unreadable",
    Status.TRIAGE_ERFORDERLICH: "Triage required",
    Status.IN_ANALYSE: "In analysis",
    Status.ENTWURF_BEREIT: "Draft ready",
    Status.IN_MEDIZINISCHER_PRUEFUNG: "In medical review",
    Status.RUECKFRAGE: "Information requested",
    Status.FREIGEGEBEN: "Approved (this version)",
    Status.ABGELEHNT: "Rejected",
    Status.MELDUNG_ERFASST: "Submission recorded",
    Status.ABGESCHLOSSEN: "Closed",
    Status.ABGESCHLOSSEN_NICHT_MELDEPFLICHTIG: "Closed as non-reportable",
}

CLOSED_STATUSES: frozenset[Status] = frozenset(
    {Status.ABGESCHLOSSEN, Status.ABGESCHLOSSEN_NICHT_MELDEPFLICHTIG}
)


class Severity(StrEnum):
    """CRIT-1.0 severity values (expected.json ``conventions.severity``)."""

    DEATH = "DEATH"
    PUBLIC_HEALTH_THREAT = "PUBLIC_HEALTH_THREAT"
    SERIOUS_DETERIORATION = "SERIOUS_DETERIORATION"
    NONE_DETECTED = "NONE_DETECTED"  # no indicator in the raw text - NOT "non-serious"
    UNKNOWN = "UNKNOWN"  # essential data are missing


SERIOUS_SEVERITIES: frozenset[str] = frozenset(
    {Severity.DEATH, Severity.PUBLIC_HEALTH_THREAT, Severity.SERIOUS_DETERIORATION}
)

#: Reject reason codes (concept section 7.2).
REJECT_REASON_CODES: dict[str, str] = {
    "R1": "insufficient evidence",
    "R2": "facts extracted incorrectly",
    "R3": "wrong product assignment",
    "R4": "causality not traceable",
    "R5": "rule set or deadline wrong",
    "R6": "other (free text mandatory)",
}


# --------------------------------------------------------------------------- actors
@dataclass(frozen=True)
class Actor:
    """Who performs an action. Humans carry roles; agents and the system carry none."""

    actor_id: str
    actor_type: ActorType = ActorType.HUMAN
    roles: frozenset[str] = frozenset()
    display_name: str | None = None
    authenticated: bool = True

    def has(self, role: str) -> bool:
        return self.actor_type == ActorType.HUMAN and self.authenticated and role in self.roles

    @staticmethod
    def anonymous() -> "Actor":
        return Actor(actor_id="anonymous", actor_type=ActorType.HUMAN, authenticated=False)

    @staticmethod
    def system(actor_id: str = "system") -> "Actor":
        return Actor(actor_id=actor_id, actor_type=ActorType.SYSTEM)

    @staticmethod
    def rule(actor_id: str) -> "Actor":
        return Actor(actor_id=actor_id, actor_type=ActorType.RULE)

    @staticmethod
    def agent(agent_id: str) -> "Actor":
        return Actor(actor_id=agent_id, actor_type=ActorType.AGENT)


# --------------------------------------------------------------------------- inputs
@dataclass
class SignalInput:
    """One incoming message (uploaded letter, seed message or API post).

    ``metadata`` carries structured fields of imported seed messages (``message_id``,
    ``case_id``, ``previous_message_id``, ``duplicate_of_message_id``, ``product_family``,
    ``market``, ``event_date``); LINK-1.0 honours them (concept section 6.3).
    """

    text: str
    received_at: str | _dt.datetime
    source: str = "upload"  # upload | seed | api
    ref: str | None = None  # human-readable signal reference (e.g. letter id or message id)
    filename: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    readable: bool = True  # False when text extraction failed (status NICHT_LESBAR)
    actor_id: str = "system"


# --------------------------------------------------------------------------- records
@dataclass
class Case:
    case_id: str
    case_key: str
    status: str
    critical: bool
    severity: str
    urgency: dict[str, Any]
    rule_sets: list[str]
    authority_labels: list[str]
    route_unverified: bool
    rs_notes: list[str]
    incident_states: list[str]
    rs_basis: str | None
    awareness_anchor: str | None
    anchor_signal_id: str | None
    anchor_ref: str | None
    product_family: str | None
    metadata: dict[str, Any]
    analysis_state: str
    deadline_waiver: dict[str, Any] | None
    content_version: int
    created_at: str
    updated_at: str


@dataclass
class Signal:
    signal_id: str
    case_id: str | None
    ref: str
    source: str
    filename: str | None
    received_at: str
    text: str
    text_sha256: str
    readable: bool
    language: str | None
    metadata: dict[str, Any]
    relation: str | None
    link: dict[str, Any]
    registry: dict[str, Any]
    injection: dict[str, Any]
    crit: dict[str, Any]
    extraction: dict[str, Any]
    rule_sets: list[str]
    deadline_not_reset: bool
    completeness: dict[str, Any]
    created_at: str


@dataclass
class DeadlineRow:
    deadline_id: str
    case_id: str
    position: int
    rule_set: str
    basis: str
    days: int
    anchor_utc: str
    awareness_local_date: str
    start_date: str
    due_date: str
    due_end_local: str
    due_end_utc: str
    origin_signal_id: str | None
    sources: list[str]
    stopped_by_submission_id: str | None
    stopped_at: str | None
    superseded: dict[str, Any] | None
    created_at: str


@dataclass
class Task:
    task_id: str
    case_id: str | None
    kind: str
    status: str
    title: str
    detail: dict[str, Any]
    created_at: str
    created_by: str
    closed_at: str | None
    closed_by: str | None
    close_reason: str | None


@dataclass
class Alert:
    alert_id: str
    case_id: str
    reason: str
    status: str
    created_at: str
    sent_at: str | None
    channel: str | None
    detail: dict[str, Any]


@dataclass
class MetadataEdit:
    edit_id: str
    case_id: str
    field: str
    old_value: Any
    new_value: Any
    reason: str
    user_id: str
    ts: str


@dataclass
class Approval:
    approval_id: str
    case_id: str
    content_hash: str
    content_version: int
    reviewer_id: str
    reason: str
    ts: str
    valid: bool
    voided_at: str | None
    voided_reason: str | None


@dataclass
class Submission:
    submission_id: str
    case_id: str
    approval_id: str
    content_hash: str
    channel_label: str
    reference: str
    submitted_on: str
    receipt_status: str
    recorded_by: str
    ts: str


@dataclass
class Decision:
    decision_id: str
    case_id: str
    kind: str
    reason: str
    reason_code: str | None
    actor_id: str
    status_before: str
    status_after: str
    ts: str


@dataclass
class Finding:
    finding_id: str
    kind: str  # approved_assessment | process_finding
    ref: str | None
    case_id: str | None
    status: str
    summary: str
    content_hash: str | None
    reviewer_id: str | None
    reason: str | None
    data: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass
class CostRecord:
    cost_id: str
    case_id: str | None
    run_id: str | None
    agent_id: str
    model: str | None
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float
    price_table_version: str | None
    simulated: bool
    ts: str


@dataclass
class AgentRun:
    run_id: str
    case_id: str
    signal_id: str | None
    agent_id: str
    status: str  # QUEUED | RUNNING | DONE | FAILED
    model: str | None
    model_version: str | None
    prompt_version: str | None
    agent_version: str | None
    started_at: str | None
    finished_at: str | None
    output: dict[str, Any] | None
    error: str | None
    usage: dict[str, Any] | None
    draft: bool


@dataclass
class Recommendation:
    rec_id: str
    case_id: str
    version: int
    content: dict[str, Any]
    content_hash: str
    draft: bool
    run_id: str | None
    created_at: str


@dataclass
class Fact:
    fact_id: str
    case_id: str
    signal_id: str | None
    name: str
    value: Any
    quote: str | None
    line: int | None
    verified: bool
    origin: str  # rule | agent | human
    created_at: str


@dataclass
class CorrelationRecord:
    corr_id: str
    case_id: str
    signal_id: str | None
    result: dict[str, Any]
    corpus_version: str
    created_at: str
