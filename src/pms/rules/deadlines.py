"""DL-1.0 - reporting deadlines (concept section 6.3; design element D-10).

* Awareness anchor = earliest receipt of any signal of the case by the fictional
  manufacturer (MDCG 2023-3 Rev. 2 Q15, source MDCG-Q15; 03-scenario.md line 1391,
  SCN-AWARE; assumption A-02). Day 0 is the calendar date of the anchor in
  Europe/Dublin (assumption A-01, OI-020; configurable).
* Day 1 = the calendar day after awareness, from 00:00:01; day N ends 23:59:59 local
  (MDCG 2023-3 Rev. 2 Q14, MDCG-Q14; regulatory analysis p. 8, RA-P8). Calendar days,
  no weekend or holiday extension (stricter rule, conflict K-05, OI-004).
* Candidates per rule set: serious public health threat 2 days (MDR Art. 87(4),
  MDR-87-4); death 10 days (MDR Art. 87(5), MDR-87-5; UK MDR reg. 44ZH(5), UK-44ZH-5);
  serious deterioration 10 days until a human classifies it as anticipated, then 15
  days (MDR Art. 87(5) and 87(3); MDCG 2023-3 Q12, MDCG-Q12; assumption A-03);
  otherwise provisionally 15 days while reportability is unclear (MDR Art. 87(3) and
  87(7), MDR-87-3, MDR-87-7; MDCG 2023-3 Q1, MDCG-Q1; assumption A-04). GB uses the same
  2/10/15 calendar days (UK MDR reg. 44ZH(3)-(5), UK-44ZH-3, UK-44ZH-5; regulatory
  analysis p. 4, RA-P4).
* Every candidate of every rule set is shown; the earliest drives priority (Goal Card
  QUALITY, GC-GBNI, GC-EARLY). Overdue when the clock is not stopped.
* The clock stops only when a person records a submission (regulatory role, approved
  version). Closing as non-reportable does not stop the clock; the deadline is shown as
  "entfällt laut menschlicher Entscheidung" with user and reason (decision Q-4,
  concept 7.2, conflict K-13).
* Follow-ups and duplicates never reset the anchor; a deadline is never moved later
  automatically (GC-DUP, SCN-AWARE; conflict K-06, OI-005).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Iterable, Sequence

from ..models import UTC, Severity, parse_utc
from ..versions import DL

DEFAULT_TIMEZONE = "Europe/Dublin"  # fictional manufacturer in Ireland: assumption A-01, OI-020 (configurable)

# --------------------------------------------------------------------------- bases
PUBLIC_HEALTH_THREAT = Severity.PUBLIC_HEALTH_THREAT.value
DEATH = Severity.DEATH.value
SERIOUS_DETERIORATION = Severity.SERIOUS_DETERIORATION.value
#: Only after a human medical reviewer classified the deterioration as anticipated (A-03).
SERIOUS_DETERIORATION_ANTICIPATED = "SERIOUS_DETERIORATION_ANTICIPATED"
PROVISIONAL_REPORTABILITY_UNCLEAR = "PROVISIONAL_REPORTABILITY_UNCLEAR"

#: Calendar days per basis (MDR Art. 87(3)-(5); UK MDR reg. 44ZH(3)-(5); concept 4.3).
DAYS: dict[str, int] = {
    PUBLIC_HEALTH_THREAT: 2,  # MDR-87-4; RA-P2; RA-P4
    DEATH: 10,  # MDR-87-5; UK-44ZH-5
    SERIOUS_DETERIORATION: 10,  # MDR-87-5; UK-44ZH-5; MDCG-Q12 (unanticipated until a human decides)
    SERIOUS_DETERIORATION_ANTICIPATED: 15,  # MDR-87-3; UK-44ZH-3 (human classification only)
    PROVISIONAL_REPORTABILITY_UNCLEAR: 15,  # MDR-87-3; MDR-87-7; MDCG-Q1; A-04
}

#: Source references (expected.json source_catalogue) per regime and basis.
SOURCES: dict[tuple[str, str], tuple[str, ...]] = {
    ("EU", PUBLIC_HEALTH_THREAT): ("MDR-87-4", "RA-P2", "RA-P4"),
    ("EU", DEATH): ("MDR-87-5", "RA-P2", "RA-P4"),
    ("EU", SERIOUS_DETERIORATION): ("MDR-87-5", "MDCG-Q12", "RA-P4"),
    ("EU", SERIOUS_DETERIORATION_ANTICIPATED): ("MDR-87-3", "RA-P4"),
    ("EU", PROVISIONAL_REPORTABILITY_UNCLEAR): ("MDR-87-3", "MDR-87-7", "MDCG-Q1", "GC-EARLY"),
    ("UK", PUBLIC_HEALTH_THREAT): ("RA-P4",),  # UK MDR reg. 44ZH(3)-(5), concept 4.3
    ("UK", DEATH): ("UK-44ZH-5", "RA-P4"),
    ("UK", SERIOUS_DETERIORATION): ("UK-44ZH-5", "RA-P4"),
    ("UK", SERIOUS_DETERIORATION_ANTICIPATED): ("UK-44ZH-3", "RA-P4"),
    ("UK", PROVISIONAL_REPORTABILITY_UNCLEAR): ("UK-44ZH-3", "GC-EARLY"),
}
COMMON_SOURCES: tuple[str, ...] = ("MDCG-Q14", "MDCG-Q15", "RA-P8", "GC-EARLY", "GC-GBNI", "GC-DUP")

#: Display text when a medical reviewer closed the case as non-reportable (decision Q-4).
WAIVED_LABEL_DE = "entfällt laut menschlicher Entscheidung"
WAIVED_LABEL_EN = "not applicable according to a human decision"

# Deadline statuses
OPEN = "OPEN"
OVERDUE = "OVERDUE"
STOPPED_BY_SUBMISSION = "STOPPED_BY_SUBMISSION"
WAIVED_BY_HUMAN = "WAIVED_BY_HUMAN"
SUPERSEDED_BY_HUMAN = "SUPERSEDED_BY_HUMAN"


# --------------------------------------------------------------------------- calendar
def _last_sunday(year: int, month: int) -> _dt.date:
    day = _dt.date(year, month + 1, 1) - _dt.timedelta(days=1) if month < 12 else _dt.date(year, 12, 31)
    return day - _dt.timedelta(days=(day.weekday() + 1) % 7)


class _DublinRule:
    """Stdlib fallback for Europe/Dublin when the tz database is unavailable.

    Irish Standard Time (UTC+1) from the last Sunday in March 01:00 UTC to the last
    Sunday in October 01:00 UTC, otherwise GMT (UTC+0) - EU summer-time rule.
    """

    @staticmethod
    def offset_for_utc(dt_utc: _dt.datetime) -> _dt.timedelta:
        y = dt_utc.year
        start = _dt.datetime.combine(_last_sunday(y, 3), _dt.time(1, 0), tzinfo=UTC)
        end = _dt.datetime.combine(_last_sunday(y, 10), _dt.time(1, 0), tzinfo=UTC)
        return _dt.timedelta(hours=1) if start <= dt_utc < end else _dt.timedelta(0)

    @classmethod
    def to_utc(cls, local_naive: _dt.datetime) -> _dt.datetime:
        # Try the summer offset first; keep it only if it is consistent.
        for hours in (1, 0):
            candidate = (local_naive - _dt.timedelta(hours=hours)).replace(tzinfo=UTC)
            if cls.offset_for_utc(candidate) == _dt.timedelta(hours=hours):
                return candidate
        return local_naive.replace(tzinfo=UTC)


class Calendar:
    """Converts between UTC and the manufacturer's local calendar (default Europe/Dublin)."""

    def __init__(self, timezone: str = DEFAULT_TIMEZONE, *, use_zoneinfo: bool = True) -> None:
        self.timezone = timezone
        self._zone = None
        if use_zoneinfo:
            try:
                from zoneinfo import ZoneInfo

                self._zone = ZoneInfo(timezone)
            except Exception:  # tz database missing: fall back to the stdlib rule
                self._zone = None
        if self._zone is None and timezone != DEFAULT_TIMEZONE:
            raise ValueError(f"time zone {timezone!r} needs the tz database (zoneinfo/tzdata)")

    @property
    def uses_fallback(self) -> bool:
        return self._zone is None

    def local_date(self, dt: _dt.datetime) -> _dt.date:
        dt_utc = parse_utc(dt)
        if self._zone is not None:
            return dt_utc.astimezone(self._zone).date()
        return (dt_utc + _DublinRule.offset_for_utc(dt_utc)).date()

    def local_to_utc(self, local_naive: _dt.datetime) -> _dt.datetime:
        if self._zone is not None:
            return local_naive.replace(tzinfo=self._zone).astimezone(UTC)
        return _DublinRule.to_utc(local_naive)

    def end_of_day_utc(self, day: _dt.date) -> _dt.datetime:
        """23:59:59 local time of ``day`` in UTC."""
        return self.local_to_utc(_dt.datetime.combine(day, _dt.time(23, 59, 59)))

    def start_of_day1_utc(self, day: _dt.date) -> _dt.datetime:
        """00:00:01 local time of ``day`` in UTC (start of the period, MDCG-Q14)."""
        return self.local_to_utc(_dt.datetime.combine(day, _dt.time(0, 0, 1)))


# --------------------------------------------------------------------------- candidates
@dataclass(frozen=True)
class DeadlineCandidate:
    rule_set: str
    basis: str
    days: int
    anchor_utc: _dt.datetime
    awareness_local_date: _dt.date
    start_date: _dt.date  # day 1
    due_date: _dt.date  # day N
    due_end_local: str  # "YYYY-MM-DDT23:59:59 Europe/Dublin"
    due_end_utc: _dt.datetime
    sources: tuple[str, ...]
    rule_version: str = DL

    @property
    def key(self) -> tuple[str, str]:
        return (self.rule_set, self.basis)

    def expected_view(self) -> dict[str, object]:
        """The five fields asserted by expected.json ``deadline_candidates``."""
        return {
            "rule_set": self.rule_set,
            "basis": self.basis,
            "days": self.days,
            "due_date": self.due_date.isoformat(),
            "due_end_local": self.due_end_local,
        }


def regime(rule_set: str) -> str:
    """Regime of a rule-set label: "UK" for UK MDR Part 4A, otherwise "EU"."""
    return "UK" if rule_set.startswith("UK-MDR") else "EU"


def bases_for(groups: Iterable[str]) -> list[str]:
    """Deadline bases triggered by one signal's CRIT-1.0 indicator groups.

    Public health threat adds the 2-day basis. Death or serious deterioration share the
    10-day paragraph (MDR Art. 87(5)) and give one candidate whose basis is the most
    serious of the two. Without any indicator the provisional 15-day basis applies.
    """
    found = set(groups)
    out: list[str] = []
    if PUBLIC_HEALTH_THREAT in found:
        out.append(PUBLIC_HEALTH_THREAT)
    if DEATH in found:
        out.append(DEATH)
    elif SERIOUS_DETERIORATION in found:
        out.append(SERIOUS_DETERIORATION)
    if not out:
        out.append(PROVISIONAL_REPORTABILITY_UNCLEAR)
    return out


def make_candidate(rule_set: str, basis: str, anchor: _dt.datetime, calendar: Calendar) -> DeadlineCandidate:
    anchor_utc = parse_utc(anchor)
    days = DAYS[basis]
    day0 = calendar.local_date(anchor_utc)
    due = day0 + _dt.timedelta(days=days)
    return DeadlineCandidate(
        rule_set=rule_set,
        basis=basis,
        days=days,
        anchor_utc=anchor_utc,
        awareness_local_date=day0,
        start_date=day0 + _dt.timedelta(days=1),
        due_date=due,
        due_end_local=f"{due.isoformat()}T23:59:59 {calendar.timezone}",
        due_end_utc=calendar.end_of_day_utc(due),
        sources=SOURCES.get((regime(rule_set), basis), ()) + COMMON_SOURCES,
    )


def candidates_for(rule_sets: Sequence[str], groups: Iterable[str], anchor: _dt.datetime,
                   calendar: Calendar) -> list[DeadlineCandidate]:
    """Candidates of one signal: every rule set (outer) x every triggered basis (inner)."""
    bases = bases_for(groups)
    return [make_candidate(rs, b, anchor, calendar) for rs in rule_sets for b in bases]


def merge(existing: Sequence[DeadlineCandidate], new: Sequence[DeadlineCandidate]) -> list[DeadlineCandidate]:
    """Append new candidates whose (rule set, basis) is not shown yet; never drop one."""
    out = list(existing)
    keys = {c.key for c in out}
    for c in new:
        if c.key not in keys:
            out.append(c)
            keys.add(c.key)
    return out


def rebase(candidates: Sequence[DeadlineCandidate], anchor: _dt.datetime, calendar: Calendar) -> list[DeadlineCandidate]:
    """Recompute candidates for a new anchor. Only an EARLIER anchor is accepted."""
    out = []
    for c in candidates:
        if parse_utc(anchor) > c.anchor_utc:
            raise ValueError("the awareness anchor can only move earlier, never later")
        out.append(make_candidate(c.rule_set, c.basis, anchor, calendar))
    return out


def earliest_anchor(received_ats: Iterable[str | _dt.datetime]) -> _dt.datetime:
    """Awareness anchor = earliest receipt among the case's signals (MDCG-Q15, SCN-AWARE)."""
    return min(parse_utc(r) for r in received_ats)


def driving(candidates: Sequence[DeadlineCandidate], *, exclude_keys: Iterable[tuple[str, str]] = ()) -> DeadlineCandidate | None:
    """Earliest-ending candidate (ties: first shown). ``exclude_keys`` skips stopped or
    human-superseded candidates for priority purposes."""
    excluded = set(exclude_keys)
    active = [c for c in candidates if c.key not in excluded]
    if not active:
        return None
    return min(active, key=lambda c: (c.due_end_utc, candidates.index(c)))


def status(candidate: DeadlineCandidate, now: _dt.datetime, *, stopped: bool = False, waived: bool = False,
           superseded: bool = False) -> str:
    """Deadline status at ``now`` (no timer ever changes a case status - display only)."""
    if stopped:
        return STOPPED_BY_SUBMISSION
    if superseded:
        return SUPERSEDED_BY_HUMAN
    if waived:
        return WAIVED_BY_HUMAN
    return OVERDUE if parse_utc(now) > candidate.due_end_utc else OPEN


def seconds_remaining(candidate: DeadlineCandidate, now: _dt.datetime) -> int:
    """Countdown to the end of the due date (negative when overdue)."""
    return int((candidate.due_end_utc - parse_utc(now)).total_seconds())


def from_row(row: dict[str, object]) -> DeadlineCandidate:
    """Rebuild a candidate from a stored row (see :class:`pms.models.DeadlineRow`)."""
    return DeadlineCandidate(
        rule_set=str(row["rule_set"]),
        basis=str(row["basis"]),
        days=int(row["days"]),  # type: ignore[arg-type]
        anchor_utc=parse_utc(str(row["anchor_utc"])),
        awareness_local_date=_dt.date.fromisoformat(str(row["awareness_local_date"])),
        start_date=_dt.date.fromisoformat(str(row["start_date"])),
        due_date=_dt.date.fromisoformat(str(row["due_date"])),
        due_end_local=str(row["due_end_local"]),
        due_end_utc=parse_utc(str(row["due_end_utc"])),
        sources=tuple(row.get("sources") or ()),  # type: ignore[arg-type]
    )
