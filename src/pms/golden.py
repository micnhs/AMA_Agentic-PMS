"""Golden-set comparator for every rule-based field (concept 11 and 12: T03, T21; Goal Card check 3).

``run_golden`` creates a fresh database (in memory by default - no files), processes the
12 letters and the 6 seed messages in ``received_at`` order through the deterministic rule
pipeline (no agents are needed for rule-based fields) with a fixed demo clock (assumption
A-09: "now" = receipt time of each message), and compares the results with
``expected.json``:

* letter labels: critical, severity, rule_set, authority_label, deadline_days, deadline_start;
* letter rule expectations: awareness_anchor, anchor_from_letter, deadline_candidates,
  driving_due_date, deadline_not_reset, linking, registry_check, injection_flag,
  route_unverified, correlation.event_tags, correlation.must_include (subset; extra
  results are allowed) - plus the case grouping (case_key);
* seed labels: linking and completeness.

``causality_range`` is an AI field (agent A3) and is not compared here; it is checked
against the live model in Gate C (T30). Labels are "expected" values of the frozen golden
set v1.0; this module only reads them - it never changes them, and the rule engine never
sees them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .knowledge import ReferenceData
from .models import FixedClock, SignalInput, iso_utc, parse_utc, utcnow
from .rules.engine import RuleEngine
from .store import Store
from .versions import default_data_dir, manifest

LABEL_FIELDS: tuple[str, ...] = ("critical", "severity", "rule_set", "authority_label", "deadline_days", "deadline_start")
RULE_FIELDS: tuple[str, ...] = (
    "awareness_anchor", "anchor_from_letter", "deadline_candidates", "driving_due_date", "deadline_not_reset",
    "linking", "registry_check", "injection_flag", "route_unverified", "correlation.event_tags",
    "correlation.must_include",
)
GROUPING_FIELDS: tuple[str, ...] = ("case_key",)
SEED_FIELDS: tuple[str, ...] = ("linking", "completeness")
EXCLUDED_FIELDS: dict[str, str] = {
    "causality_range": "AI field (agent A3, 4-level MDCG 2020-10/1 scale); checked against the live model in Gate C (T30)",
}


@dataclass(frozen=True)
class GoldenItem:
    kind: str  # letter | seed
    item_id: str
    received_at: str
    signal: SignalInput
    expected: dict[str, Any]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_items(data_dir: Path, expected: dict[str, Any]) -> tuple[list[GoldenItem], list[dict[str, Any]]]:
    """Golden inputs in received_at order plus input-integrity findings."""
    items: list[GoldenItem] = []
    integrity: list[dict[str, Any]] = []
    for letter in expected["letters"]:
        path = data_dir / str(letter["file"]).removeprefix("data/synthetic/")
        raw = path.read_bytes()
        if _sha256_bytes(raw) != letter.get("text_sha256"):
            integrity.append({"item": letter["id"], "problem": "letter text differs from the golden-set hash"})
        signal = SignalInput(text=raw.decode("utf-8"), received_at=letter["received_at"], source="upload",
                             ref=letter["id"], filename=path.name)
        items.append(GoldenItem("letter", letter["id"], letter["received_at"], signal, letter))
    seeds = {s.ref: s for s in ReferenceData.load(data_dir).seed_signals()}
    for seed in expected["seeds"]:
        signal = seeds.get(seed["message_id"])
        if signal is None:
            integrity.append({"item": seed["id"], "problem": "seed missing in seeds.json"})
            continue
        if _sha256_bytes(signal.text.encode("utf-8")) != seed.get("text_sha256"):
            integrity.append({"item": seed["id"], "problem": "seed text differs from the golden-set hash"})
        items.append(GoldenItem("seed", seed["id"], str(signal.received_at), signal, seed))
    items.sort(key=lambda i: (parse_utc(i.received_at), i.item_id))
    return items, integrity


def _letter_actual(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "critical": "yes" if snapshot["critical"] else "no",
        "severity": snapshot["severity"],
        "rule_set": snapshot["rule_set"],
        "authority_label": snapshot["authority_label"],
        "deadline_days": snapshot["deadline_days"],
        "deadline_start": snapshot["deadline_start"],
        "awareness_anchor": snapshot["awareness_anchor"],
        "anchor_from_letter": snapshot["anchor_ref"],
        "deadline_candidates": snapshot["deadline_candidates"],
        "driving_due_date": snapshot["driving_due_date"],
        "deadline_not_reset": snapshot["deadline_not_reset"],
        "linking": snapshot["linking"],
        "registry_check": snapshot["registry_check"],
        "injection_flag": snapshot["injection_flag"],
        "route_unverified": snapshot["route_unverified"],
        "correlation.event_tags": snapshot["event_tags"],
        "correlation.must_include": snapshot["correlation_refs"],
        "case_key": snapshot["case_key"],
    }


def _letter_expected(letter: dict[str, Any]) -> dict[str, Any]:
    labels = letter["labels"]
    rules = letter["rule_expectations"]
    out = {name: labels[name]["value"] for name in LABEL_FIELDS}
    for name in RULE_FIELDS:
        if name == "correlation.event_tags":
            out[name] = rules["correlation"]["event_tags"]
        elif name == "correlation.must_include":
            out[name] = [m["ref"] for m in rules["correlation"]["must_include"]]
        else:
            out[name] = rules[name]
    out["case_key"] = letter["case_key"]
    return out


def _compare(field: str, expected: Any, actual: Any) -> tuple[bool, Any]:
    """Return (match, detail). must_include is a subset check; event tags are a set."""
    if field == "correlation.must_include":
        missing = [ref for ref in expected if ref not in set(actual or [])]
        return (not missing, {"missing": missing} if missing else None)
    if field == "correlation.event_tags":
        return (sorted(expected) == sorted(actual or []), None)
    return (expected == actual, None)


def run_golden(data_dir: str | Path | None = None, out_path: str | Path | None = None, *,
               db_path: str = ":memory:") -> bool:
    """Run the golden comparison; write a JSON summary; return True only at 100 % match."""
    base = Path(data_dir) if data_dir is not None else default_data_dir()
    expected = json.loads((base / "expected.json").read_text(encoding="utf-8"))
    items, integrity = load_items(base, expected)
    ref = ReferenceData.load(base)
    clock = FixedClock(items[0].received_at)
    store = Store(db_path, clock=clock)
    engine = RuleEngine(store, ref, clock=clock)

    results: dict[str, dict[str, Any]] = {}
    mismatches: list[dict[str, Any]] = []
    per_field: dict[str, dict[str, int]] = {}
    compared = matched = 0

    def record(item: str, field: str, exp: Any, act: Any) -> None:
        nonlocal compared, matched
        ok, detail = _compare(field, exp, act)
        stats = per_field.setdefault(field if not field.startswith("seed.") else field, {"compared": 0, "matched": 0})
        stats["compared"] += 1
        compared += 1
        if ok:
            stats["matched"] += 1
            matched += 1
        else:
            entry = {"item": item, "field": field, "expected": exp, "actual": act}
            if detail:
                entry["detail"] = detail
            mismatches.append(entry)

    order: list[str] = []
    for item in items:
        clock.set(item.received_at)
        outcome = engine.process_signal(item.signal, now=parse_utc(item.received_at))
        engine.run_correlation(outcome.case_id, outcome.signal_id, now=parse_utc(item.received_at))
        snapshot = engine.case_labels(outcome.case_id, outcome.signal_id, now=parse_utc(item.received_at))
        order.append(item.item_id)
        if item.kind == "letter":
            actual = _letter_actual(snapshot)
            exp = _letter_expected(item.expected)
            for field in LABEL_FIELDS + RULE_FIELDS + GROUPING_FIELDS:
                record(item.item_id, field, exp[field], actual[field])
            results[item.item_id] = {"kind": "letter", "actual": actual,
                                     "correlation_status": snapshot["correlation_status"]}
        else:
            actual = {
                "linking": {"case_id": snapshot["case_key"], "relation": snapshot["linking"],
                            "awareness_anchor": snapshot["awareness_anchor"]},
                "completeness": snapshot["completeness"],
            }
            for field in SEED_FIELDS:
                record(item.item_id, f"seed.{field}", item.expected["labels"][field]["value"], actual[field])
            results[item.item_id] = {"kind": "seed", "actual": actual}

    audit_check = store.audit.verify()
    passed = compared > 0 and matched == compared and not integrity and audit_check.ok
    summary = {
        "golden_set_version": expected.get("golden_set_version"),
        "generated_at_utc": iso_utc(utcnow()),
        "versions": manifest(base),
        "database": "in-memory (fresh)" if db_path == ":memory:" else db_path,
        "demo_clock": "fixed: now = received_at of each message (assumption A-09)",
        "processing_order": order,
        "fields_compared": compared,
        "matches": matched,
        "mismatch_count": len(mismatches),
        "match_rate": round(matched / compared, 6) if compared else 0.0,
        "passed": passed,
        "per_field": per_field,
        "mismatches": mismatches,
        "input_integrity": {"checked": len(items), "problems": integrity},
        "audit_chain": audit_check.to_dict(),
        "audit_records": store.audit.count(),
        "excluded_fields": EXCLUDED_FIELDS,
        "notes": [
            "correlation.must_include is a subset check: extra correlation results are allowed; "
            "an empty result never means all-clear.",
            "correlation.event_tags is compared as a set.",
            "case_key (case grouping of letters) is compared in addition to the listed rule fields.",
        ],
        "items": results,
    }
    if out_path is not None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    store.close()
    engine.index.close()
    return passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Golden run: compare rule-based fields with expected.json")
    parser.add_argument("--data-dir", default=None, help="golden-set data directory (default data/synthetic)")
    parser.add_argument("--out", default="var/golden-summary.json", help="path of the JSON summary")
    args = parser.parse_args(argv)
    ok = run_golden(args.data_dir, args.out)
    summary = json.loads(Path(args.out).read_text(encoding="utf-8"))
    print(f"golden run: {summary['matches']}/{summary['fields_compared']} fields matched "
          f"({summary['match_rate']:.2%}); passed={ok}; summary: {args.out}")
    for m in summary["mismatches"]:
        print(f"  MISMATCH {m['item']} {m['field']}: expected {m['expected']!r}, actual {m['actual']!r}")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
