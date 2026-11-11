"""A5 light trend view - plain code, no model (concept 6.2, 9; D-07; test T18).

Shows the six synthetic monthly rows (2026-04 to 2026-09) with unit, period and the missing
denominator. No statistical statement, no rate, no signal score; interpretation by humans only
(Goal Card QUALITY; conflict K-04).
"""
from __future__ import annotations

import re
from typing import Any

from ..knowledge import ReferenceData
from .specs import A5_INFO

NOTE_EN = "Counts only. Denominator unknown - no rates. Interpretation by humans only."
NOTE_DE = "Nenner unbekannt, keine Raten. Interpretation nur durch Menschen."
COLUMNS: tuple[tuple[str, str], ...] = (
    ("period", "Month"), ("messages", "Messages"), ("new_cases", "New cases"),
    ("duplicate_messages", "Duplicate messages"), ("followups", "Follow-ups"),
    ("candidate_break_cases", "Candidate break cases"), ("exposed_device_denominator", "Exposed devices (denominator)"),
)
#: Statistical vocabulary that must not appear in the trend view (T18 forbidden list).
FORBIDDEN_STATISTICAL_TERMS: tuple[str, ...] = (
    "rate", "incidence", "prevalence", "significan", "p-value", "confidence interval", "statistically",
    "signal score", "risk score", "trend increase", "increasing trend", "per 1000", "per 100 ", "%",
)
_ALLOWED_PHRASES = (NOTE_EN, NOTE_DE, "no rates", "keine Raten", "no signal score")


def trend_view(ref: ReferenceData) -> dict[str, Any]:
    trend = ref.trend or {}
    rows = []
    for row in trend.get("rows", []):
        rows.append({key: row.get(key) for key, _ in COLUMNS} | {
            "exposed_device_denominator": row.get("exposed_device_denominator") if row.get(
                "exposed_device_denominator") is not None else "unknown",
            "source_line": row.get("source_line")})
    periods = [r["period"] for r in rows]
    return {
        "agent": dict(A5_INFO),
        "title": "Light trend view (synthetic monthly counts)",
        "unit": "counts per calendar month (synthetic demo data)",
        "period": f"{periods[0]} to {periods[-1]}" if periods else None,
        "columns": [{"key": k, "label": label} for k, label in COLUMNS],
        "rows": rows,
        "denominator": "unknown (null in the source)",
        "note": NOTE_EN,
        "note_de": NOTE_DE,
        "source": trend.get("source"),
        "synthetic": bool(trend.get("synthetic", True)),
    }


def statistical_terms_in(text: str) -> list[str]:
    """Forbidden statistical terms in ``text`` (the fixed 'no rates' notes are ignored)."""
    cleaned = text
    for phrase in _ALLOWED_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    lowered = cleaned.lower()
    return [term for term in FORBIDDEN_STATISTICAL_TERMS
            if (re.search(r"\b" + re.escape(term), lowered) if term[0].isalnum() else term in lowered)]
