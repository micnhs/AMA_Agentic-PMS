"""Version manifest of the demo: app, fixed rule sets and fictional knowledge corpus.

Every audit record and case report carries these versions (concept section 6.3:
"Jede Regel traegt eine Versionsnummer, die in jedem Audit-Eintrag und Fallbericht
erscheint"; design element D-21). A rule change is a new version with a unit test
and a cycle-log entry - never an in-place edit of an existing version.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

APP_NAME = "pms-agent-demo"
APP_VERSION = "0.1.0"

#: Fixed rule sets ("Regelpaket 1.0", concept section 6.3).
CRIT = "CRIT-1.0"  # critical detection (raw-text scan DE/EN/FR/IT + 48-hour window)
DL = "DL-1.0"  # reporting deadlines (awareness anchor, candidates, earliest drives priority)
RS = "RS-1.0"  # rule set and authority label per incident state (EU, IE, NI, GB separately)
LINK = "LINK-1.0"  # duplicates and follow-ups
REG = "REG-1.0"  # registry check
INJ = "INJ-1.0"  # prompt-injection detection
CORR = "CORR-1.0"  # deterministic correlation index

RULE_VERSIONS: dict[str, str] = {
    "CRIT": CRIT,
    "DL": DL,
    "RS": RS,
    "LINK": LINK,
    "REG": REG,
    "INJ": INJ,
    "CORR": CORR,
}
ALL_RULE_VERSIONS: tuple[str, ...] = tuple(RULE_VERSIONS.values())

#: Repository root (src/pms/versions.py -> parents[2]).
REPO_ROOT = Path(__file__).resolve().parents[2]


def default_data_dir() -> Path:
    """Directory of the frozen synthetic golden-set data inside the repository."""
    return REPO_ROOT / "data" / "synthetic"


def corpus_version(data_dir: str | Path | None = None) -> str:
    """Version of the fictional knowledge corpus (``knowledge/knowledge_index.json``).

    Returns ``"unknown"`` when the index cannot be read - an unreadable corpus is
    reported, never silently treated as empty.
    """
    base = Path(data_dir) if data_dir is not None else default_data_dir()
    try:
        index = json.loads((base / "knowledge" / "knowledge_index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    return str(index.get("corpus_version", "unknown"))


def manifest(data_dir: str | Path | None = None) -> dict[str, Any]:
    """Version manifest used in audit records, reports and ``/healthz``."""
    return {
        "app": APP_VERSION,
        "rules": dict(RULE_VERSIONS),
        "corpus": corpus_version(data_dir),
    }
