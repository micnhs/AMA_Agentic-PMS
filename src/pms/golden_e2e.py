"""End-to-end offline golden run (extends :mod:`pms.golden`; Goal Card checks 3, 4, 6 and 7).

Fresh in-memory database, offline agent stand-in, fixed demo clock (assumption A-09: "now" =
receipt time of each message). The 12 letters (receipt times from ``letters/intake.json``) and the
6 seed messages run in received_at order through the FULL pipeline: rules -> alert -> analysis
A1, A2, CORR-1.0, A6, A3, A4. Then:

* every rule-based field is compared with ``expected.json`` (comparator only - the pipeline never
  reads it) with the field list of :mod:`pms.golden` (check 3);
* EVERY critical letter (also a duplicate, follow-up or copy landing in an already critical case):
  a triage task that references the letter and an outbox alert within 60 s of upload and before
  the analysis of that letter finished, with measured timings (check 4); critical cases still wait
  for a human after the analysis;
* scripted test actors perform human steps (triage, review, decisions, one metadata edit, one
  recorded submission) so that reports carry reviewer decisions; they are labelled as scripted;
* every case gets a report (JSON + Markdown) that must be complete (check 7);
* audit: records per event type, 0 missing fields, exactly one record per step, chain check and a
  tamper test on a copy (check 6).

Without ``work_dir`` each run writes into its own new folder ``var/golden-e2e/run-<timestamp>``; folders of
other (possibly concurrent) runs are never touched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import runtime as rt
from .config import local_test_settings
from .golden import GROUPING_FIELDS, LABEL_FIELDS, RULE_FIELDS, SEED_FIELDS, _compare, _letter_actual, _letter_expected
from .governance import audit_completeness, audit_quality
from .models import Actor, FixedClock, Role, Status, iso_utc, utcnow
from .pipeline import ALERT_TARGET_SECONDS, Pipeline
from .reports import build_case_report, render_markdown
from .versions import REPO_ROOT, default_data_dir, manifest
from .workflow import TASK_TRIAGE

REVIEWER = Actor("golden-reviewer (scripted test actor)", roles=frozenset({Role.MEDICAL_REVIEWER}))
ANALYST = Actor("golden-analyst (scripted test actor)", roles=frozenset({Role.ANALYST}))
REGULATORY = Actor("golden-regulatory (scripted test actor)", roles=frozenset({Role.REGULATORY}))


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scripted_review(pipeline: Pipeline, at: str) -> list[dict[str, Any]]:
    """Human steps by scripted TEST actors (golden e2e only; not real decisions)."""
    steps: list[dict[str, Any]] = []
    wf = pipeline.workflow

    def step(case_key: str, action: str, fn: Any) -> None:
        fn()
        steps.append({"case": case_key, "action": action, "status_after": pipeline.store.get_case(case_key).status})

    for case in sorted(pipeline.store.list_cases(), key=lambda c: c.case_key):
        key = case.case_key
        if case.status == Status.TRIAGE_ERFORDERLICH:
            step(key, "take_triage (analyst)", lambda: wf.take_triage(key, ANALYST, "golden e2e: triage taken"))
        if key == "CASE-L08":
            step(key, "edit_metadata note (analyst)", lambda: wf.edit_metadata(
                key, ANALYST, "note", "GB or NI incident location to be confirmed with the sender",
                "golden e2e: scripted metadata edit"))
        if key == "CASE-L07":
            step(key, "lower_urgency (reviewer)", lambda: wf.lower_urgency(
                key, REVIEWER, "golden e2e: event judged product-unrelated by the scripted reviewer"))
            step(key, "close_non_reportable (reviewer)", lambda: wf.decide(
                key, REVIEWER, "non_reportable", "golden e2e: appendicitis without device involvement (scripted)"))
            continue
        step(key, "start_review (reviewer)", lambda: wf.start_review(key, REVIEWER, "golden e2e: review started"))
        if key == "CASE-L11":
            step(key, "reject R3 (reviewer)", lambda: wf.decide(
                key, REVIEWER, "reject", "golden e2e: lot and UDI-DI contradict the registry (scripted)",
                reason_code="R3"))
            continue
        step(key, "approve (reviewer)", lambda: wf.decide(
            key, REVIEWER, "approve", "golden e2e: scripted review of the draft against its sources"))
        if key == "CASE-L01":
            step(key, "record_submission (regulatory)", lambda: wf.record_submission(
                key, REGULATORY, channel_label="EU manufacturer incident report (label only)",
                reference="GOLDEN-E2E-0001", submitted_on=at[:10], receipt_status="unconfirmed",
                reason="golden e2e: submission made outside the system recorded"))
    return steps


def new_run_dir(root: Path) -> Path:
    """Create a folder that belongs to this run only (``run-<timestamp>``, unique even for concurrent runs)."""
    root.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
    path = root / f"run-{stamp}"
    try:
        path.mkdir()
    except FileExistsError:
        path = Path(tempfile.mkdtemp(prefix=f"run-{stamp}-", dir=root))
    return path


def run_golden_e2e(data_dir: str | Path | None = None, out_path: str | Path | None = None, *,
                   work_dir: str | Path | None = None, scripted_review: bool = True) -> dict[str, Any]:
    base = Path(data_dir) if data_dir is not None else default_data_dir()
    expected = json.loads((base / "expected.json").read_text(encoding="utf-8"))  # comparator only
    if work_dir is not None:
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
    else:
        work = new_run_dir(REPO_ROOT / "var" / "golden-e2e")
    settings = local_test_settings(work, synthetic_data_dir=base)
    letters_exp = {letter["id"]: letter for letter in expected["letters"]}
    seeds_exp = {seed["message_id"]: seed for seed in expected["seeds"]}

    clock = FixedClock("2026-01-01T00:00:00Z")
    pipeline = Pipeline(settings, store=rt.get_store(settings, path=":memory:", clock=clock), clock=clock)
    items = pipeline.demo_items()
    clock.set(items[0].received_at)
    pipeline.workflow.import_process_findings(pipeline.ref.process_findings(), Actor.system(), source="seed")

    integrity = []
    for item in items:
        exp = letters_exp.get(item.ref) if item.kind == "letter" else seeds_exp.get(item.ref)
        if exp is None:
            integrity.append({"item": item.ref, "problem": "not in expected.json"})
        elif _sha_bytes(item.text.encode("utf-8")) != exp.get("text_sha256"):
            integrity.append({"item": item.ref, "problem": "text differs from the golden-set hash"})
        if item.kind == "letter" and exp is not None and exp["received_at"] != item.received_at:
            integrity.append({"item": item.ref, "problem": "intake receipt time differs from the golden set"})

    compared = matched = 0
    per_field: dict[str, dict[str, int]] = {}
    mismatches: list[dict[str, Any]] = []
    critical_timings: list[dict[str, Any]] = []
    order: list[str] = []
    for item in items:
        clock.set(item.received_at)
        result = pipeline.ingest_item(item)
        pipeline.drain()
        timing = pipeline.timings.get(result.signal_id, {})
        snapshot = pipeline.engine.case_labels(result.case_id, result.signal_id)
        order.append(item.ref)
        if item.kind == "letter":
            exp = _letter_expected(letters_exp[item.ref])
            act = _letter_actual(snapshot)
            fields = [(f, exp[f], act[f]) for f in LABEL_FIELDS + RULE_FIELDS + GROUPING_FIELDS]
        else:
            act = {"linking": {"case_id": snapshot["case_key"], "relation": snapshot["linking"],
                               "awareness_anchor": snapshot["awareness_anchor"]},
                   "completeness": snapshot["completeness"]}
            fields = [(f"seed.{f}", seeds_exp[item.ref]["labels"][f]["value"], act[f]) for f in SEED_FIELDS]
        for name, exp_value, act_value in fields:
            ok, detail = _compare(name, exp_value, act_value)
            stats = per_field.setdefault(name, {"compared": 0, "matched": 0})
            stats["compared"] += 1
            compared += 1
            if ok:
                stats["matched"] += 1
                matched += 1
            else:
                mismatches.append({"item": item.ref, "field": name, "expected": exp_value, "actual": act_value,
                                   **({"detail": detail} if detail else {})})
        if item.kind == "letter" and letters_exp[item.ref]["labels"]["critical"]["value"] == "yes":
            # Every critical letter needs its OWN triage task and alert - also when it lands in a case that is
            # already critical (duplicate, follow-up, the injection copy L12).
            case = pipeline.store.get_case(result.case_id)
            triage = [t for t in pipeline.store.list_tasks(case_id=result.case_id, kind=TASK_TRIAGE)
                      if t.detail.get("signal_id") == result.signal_id]
            own = {a.alert_id: a for a in pipeline.store.list_alerts(case_id=result.case_id)
                   if a.detail.get("signal_id") == result.signal_id}
            alerts = [a for a in timing.get("alerts", []) if a["alert_id"] in own]
            finished = timing.get("analysis_finished_ms")
            entry = {"letter": item.ref, "case": result.case_key, "relation": result.relation,
                     "newly_critical": result.newly_critical, "injection_flag": result.injection_flag,
                     "case_critical": case.critical, "status_after_analysis": case.status,
                     "triage_tasks": [t.task_id for t in triage], "alert_ids": [a["alert_id"] for a in alerts],
                     "alert_reason": own[alerts[0]["alert_id"]].reason if alerts else None,
                     "alert_latency_ms": None, "analysis_finished_ms": finished,
                     "alert_before_analysis_finished": None, "within_60s": None, "outbox_entry": None}
            if alerts:
                first = alerts[0]
                entry.update(alert_latency_ms=first["latency_ms"], outbox_entry=first.get("outbox_entry"),
                             within_60s=first["status"] == "SENT" and first["latency_ms"] <= ALERT_TARGET_SECONDS * 1000,
                             alert_before_analysis_finished=finished is not None and first["latency_ms"] < finished)
            entry["ok"] = bool(entry["within_60s"] and entry["alert_before_analysis_finished"] and triage
                               and entry["outbox_entry"] and case.critical)
            critical_timings.append(entry)

    critical_cases = [c for c in pipeline.store.list_cases() if c.critical]
    waiting = [c.case_key for c in critical_cases if c.status == Status.TRIAGE_ERFORDERLICH]
    human_steps = _scripted_review(pipeline, items[-1].received_at) if scripted_review else []

    reports = []
    for case in sorted(pipeline.store.list_cases(), key=lambda c: c.case_key):
        report = build_case_report(pipeline, case.case_id)
        pipeline.files.put("reports", f"{case.case_id}.json",
                           json.dumps(report, ensure_ascii=False, indent=2, default=str).encode("utf-8"))
        pipeline.files.put("reports", f"{case.case_id}.md", render_markdown(report).encode("utf-8"))
        reports.append({"case": case.case_key, "complete": report["completeness"]["complete"],
                        "missing": report["completeness"]["missing"],
                        "causality_draft": (report["recommendation"] or {}).get("causality"),
                        "imdrf_candidates": [c["code"] for c in (report["recommendation"] or {}).get(
                            "imdrf_candidates", [])],
                        "signals": len(report["signals"])})

    quality = audit_quality(pipeline.store)
    completeness = audit_completeness(pipeline.store)
    chain = pipeline.store.audit.verify()
    tamper = rt.audit_tamper_test(pipeline.store)
    outbox = pipeline.outbox_entries()
    jobs = pipeline.jobs.list()
    failed_jobs = [{"job_id": j.job_id, "case_id": j.case_id, "error": j.error} for j in jobs if j.status != "DONE"]
    rule_ok = compared > 0 and matched == compared and not integrity
    reports_ok = all(r["complete"] for r in reports)
    timing_ok = bool(critical_timings) and all(t["ok"] for t in critical_timings)
    audit_ok = quality["missing_fields"] == 0 and completeness["ok"] and chain.ok and tamper["detected"]
    passed = (rule_ok and reports_ok and timing_ok and audit_ok and not failed_jobs
              and len(waiting) == len(critical_cases))
    summary = {
        "golden_set_version": expected.get("golden_set_version"),
        "generated_at_utc": iso_utc(utcnow()),
        "mode": "end-to-end offline run (rules, alerts, agents A1-A4 and A6 offline stand-in, reports, audit)",
        "versions": manifest(base),
        "database": "in-memory (fresh)",
        "demo_clock": "fixed: now = received_at of each message (assumption A-09)",
        "inputs": "letters/intake.json (receipt times) + seeds/seeds.json; expected.json read by the comparator only",
        "processing_order": order,
        "passed": passed,
        "rule_fields": {"compared": compared, "matched": matched, "mismatch_count": len(mismatches),
                        "match_rate": round(matched / compared, 6) if compared else 0.0, "per_field": per_field,
                        "mismatches": mismatches, "input_integrity": integrity},
        "critical_alerts": {"target_seconds": ALERT_TARGET_SECONDS,
                            "rule": "every critical letter: own triage task + outbox alert within the target and "
                                    "before the analysis of that letter finished",
                            "critical_letters": len(critical_timings),
                            "letters_ok": sum(1 for t in critical_timings if t["ok"]),
                            "letters": critical_timings, "all_ok": timing_ok,
                            "critical_cases_waiting_for_human_triage_after_analysis": waiting,
                            "critical_cases": [c.case_key for c in critical_cases],
                            "outbox_entries": len(outbox)},
        "analysis_jobs": {"total": len(jobs), "not_done": failed_jobs},
        "scripted_human_steps": {"note": "performed by scripted TEST actors to exercise the workflow; not real "
                                         "decisions", "steps": human_steps},
        "reports": {"cases": len(reports), "complete": sum(1 for r in reports if r["complete"]), "items": reports,
                    "folder": str(work / "reports")},
        "audit": {"records": quality["records"], "by_event_type": quality["by_event_type"],
                  "missing_fields": quality["missing_fields"], "missing_examples": quality["examples"],
                  "one_record_per_step": completeness, "chain": chain.to_dict(), "tamper_test": tamper},
        "excluded_fields": {"causality_range": "AI field; checked against the live model in Gate C (T30). The "
                                               "offline stand-in drafts are shown per report."},
    }
    if out_path is not None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    pipeline.close()
    return summary


def print_summary(summary: dict[str, Any], out: Any = sys.stdout) -> None:
    rf, rp, au, ca = summary["rule_fields"], summary["reports"], summary["audit"], summary["critical_alerts"]
    print(f"golden e2e: rule fields {rf['matched']}/{rf['compared']} ({rf['match_rate']:.2%}); reports complete "
          f"{rp['complete']}/{rp['cases']}; audit records {au['records']}, missing fields {au['missing_fields']}, "
          f"one record per step {'ok' if au['one_record_per_step']['ok'] else 'FAILED'}, chain "
          f"{'ok' if au['chain']['ok'] else 'BROKEN'}, tamper test {'detected' if au['tamper_test']['detected'] else 'NOT detected'}",
          file=out)
    print(f"critical letters with own triage task and alert < {ca['target_seconds']} s before the analysis "
          f"finished: {ca['letters_ok']}/{ca['critical_letters']}", file=out)
    for t in ca["letters"]:
        print(f"  critical {t['letter']} ({t['case']}, {t['relation']}): alert {t['alert_latency_ms']} ms, analysis "
              f"finished {t['analysis_finished_ms']} ms, ok={t['ok']}", file=out)
    for m in rf["mismatches"]:
        print(f"  MISMATCH {m['item']} {m['field']}: expected {m['expected']!r}, actual {m['actual']!r}", file=out)
    print(f"passed={summary['passed']}", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="End-to-end offline golden run")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--out", default="var/golden-summary.json")
    args = parser.parse_args(argv)
    summary = run_golden_e2e(args.data_dir, args.out)
    print_summary(summary)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
