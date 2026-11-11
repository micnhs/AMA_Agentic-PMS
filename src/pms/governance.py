"""Governance and monitoring view and audit-quality checks (concept 6.6, 8, 9; ADR-009; D-33, D-34; T06, T29).

* :func:`governance_view` - versions, rule sets, model deployments, alert latency, worker heartbeat,
  preview-feature flag (off), trace settings, policy/budget information, evaluation status, open
  human decisions, deadlines and audit status.
* :func:`audit_quality` - records per event type and missing mandatory fields (Goal Card check 6:
  0 missing fields).
* :func:`audit_completeness` - every status change, agent run, tool call, cost, recommendation,
  human decision and metadata edit has exactly one audit record.
"""
from __future__ import annotations

import datetime as _dt
import json
import statistics
from collections import Counter
from typing import Any

from . import audit as A
from .agents.specs import DEPLOYMENT_TYPE, model_version_of
from .interfaces import CaseStore
from .models import Status, iso_utc, parse_utc
from .reports import DISCLAIMER_DE, DISCLAIMER_EN
from .versions import CORR, CRIT, DL, INJ, LINK, REG, RS

RULE_PURPOSES: dict[str, str] = {
    CRIT: "critical detection (raw-text scan DE/EN/FR/IT, 48-hour window; AI may only raise)",
    DL: "reporting deadlines (awareness anchor, candidates per rule set, earliest drives priority)",
    RS: "rule set and authority label per incident state (EU, IE, NI, GB separately; labels only)",
    LINK: "duplicates and follow-ups (anchor never reset)",
    REG: "registry check (MATCH, MISMATCH, NO_IDENTIFIERS, NOT_FOUND; always visible)",
    INJ: "prompt-injection detection (flag; letter content is data)",
    CORR: "correlation index (named matches and counts only; no hits is not an all-clear)",
}
#: Events that always belong to a case.
CASE_EVENTS: frozenset[str] = frozenset({
    A.STATUS_CHANGED, A.AGENT_RUN, A.TOOL_CALL, A.CORRELATION_RUN, A.DECISION, A.METADATA_EDITED, A.SIGNAL_ADDED,
    A.ALERT_REQUESTED, A.ALERT_SENT, A.URGENCY_RAISED, A.URGENCY_LOWERED, A.APPROVAL_VOIDED, A.SUBMISSION_RECORDED,
    A.RECOMMENDATION_ADDED, A.CASE_CREATED, A.RULE_EVALUATED, A.REREVIEW_FLAGGED,
})
_BEFORE_AFTER = frozenset({A.STATUS_CHANGED, A.METADATA_EDITED, A.DECISION, A.URGENCY_RAISED, A.URGENCY_LOWERED,
                           A.APPROVAL_VOIDED})
_HUMAN_REASON = frozenset({A.DECISION, A.METADATA_EDITED, A.URGENCY_LOWERED, A.SUBMISSION_RECORDED})
OPEN_DECISION_STATUSES: tuple[str, ...] = (
    Status.TRIAGE_ERFORDERLICH, Status.ENTWURF_BEREIT, Status.IN_MEDIZINISCHER_PRUEFUNG, Status.RUECKFRAGE,
    Status.FREIGEGEBEN,
)


# --------------------------------------------------------------------------- audit quality (check 6)
def missing_fields(rec: dict[str, Any]) -> list[str]:
    """Mandatory fields of one audit record that are absent or empty (concept 8)."""
    missing = [f for f in A.RECORD_FIELDS if f not in rec]
    for name in ("record_id", "seq", "ts_utc", "source", "actor_type", "actor_id", "event_type", "app_version",
                 "prev_hash", "hash"):
        if not rec.get(name):
            missing.append(name)
    event, actor = rec.get("event_type"), rec.get("actor_type")
    if event in CASE_EVENTS and not rec.get("case_id"):
        missing.append("case_id")
    if event in (A.SIGNAL_ADDED, A.TOOL_CALL, A.CORRELATION_RUN) and not rec.get("signal_id"):
        missing.append("signal_id")
    if actor == "agent" and event != A.ACCESS_DENIED:
        missing += [f for f in ("agent_version", "model", "model_version", "prompt_version") if not rec.get(f)]
    if actor == "rule" and not rec.get("rule_versions"):
        missing.append("rule_versions")
    if event == A.AGENT_RUN:
        missing += [f for f in ("usage", "cost_estimate", "rule_versions", "corpus_version") if not rec.get(f)]
    if event == A.COST_RECORDED:
        missing += [f for f in ("usage", "cost_estimate") if not rec.get(f)]
    if event in (A.TOOL_CALL, A.CORRELATION_RUN, A.RETRIEVAL_RUN):
        if not rec.get("corpus_version"):
            missing.append("corpus_version")
        if rec.get("source_ids") is None:
            missing.append("source_ids")
    if actor == "human" and (event in _HUMAN_REASON or event in A.REASON_REQUIRED) and not rec.get("reason"):
        missing.append("reason")
    if event in _BEFORE_AFTER:
        missing += [f for f in ("before", "after") if rec.get(f) is None]
    if event in (A.SIGNAL_ADDED, A.CASE_CREATED, A.ALERT_SENT, A.AGENT_RUN, A.COST_RECORDED, A.RECOMMENDATION_ADDED,
                 A.TOOL_CALL, A.TASK_CREATED, A.ALERT_REQUESTED) and rec.get("after") is None:
        missing.append("after")
    return sorted(set(missing))


def audit_quality(store: CaseStore) -> dict[str, Any]:
    records = store.audit.records()
    by_type = Counter(r["event_type"] for r in records)
    problems = []
    total_missing = 0
    for rec in records:
        miss = missing_fields(rec)
        if miss:
            total_missing += len(miss)
            if len(problems) < 20:
                problems.append({"seq": rec.get("seq"), "event_type": rec.get("event_type"), "missing": miss})
    return {"records": len(records), "by_event_type": dict(sorted(by_type.items())), "missing_fields": total_missing,
            "records_with_missing_fields": sum(1 for r in records if missing_fields(r)), "examples": problems}


def audit_completeness(store: CaseStore) -> dict[str, Any]:
    """Every step has exactly one audit record (Goal Card check 6)."""
    records = store.audit.records()
    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, expected: Counter, actual: Counter) -> None:
        missing = [k for k in expected if actual.get(k, 0) < expected[k]]
        duplicate = [k for k in actual if actual[k] > expected.get(k, 0)]
        checks[name] = {"steps": sum(expected.values()), "records": sum(actual.values()), "missing": missing[:10],
                        "unexpected_or_duplicate": duplicate[:10], "ok": not missing and not duplicate}

    by = lambda event: [r for r in records if r["event_type"] == event]  # noqa: E731
    runs = [r for c in store.list_cases() for r in store.list_agent_runs(c.case_id)]
    check("agent_runs", Counter(r.run_id for r in runs), Counter((r["after"] or {}).get("run_id") for r in by(A.AGENT_RUN)))
    tool_steps = Counter()
    for r in runs:
        for call in (r.output or {}).get("tool_calls") or []:
            tool_steps[(r.case_id, r.agent_id, call.get("audit_seq"))] += 1
    tool_records = Counter((r["case_id"], r["actor_id"], r["seq"]) for r in by(A.TOOL_CALL)
                           if (r["after"] or {}).get("call_no") is not None)
    check("tool_calls", tool_steps, tool_records)
    costs = [c for case in store.list_cases() for c in store.list_costs(case.case_id)]
    check("costs", Counter(c.cost_id for c in costs), Counter((r["after"] or {}).get("cost_id") for r in by(A.COST_RECORDED)))
    recs = [x for case in store.list_cases() for x in store.list_recommendations(case.case_id)]
    check("recommendations", Counter(x.rec_id for x in recs),
          Counter((r["after"] or {}).get("rec_id") for r in by(A.RECOMMENDATION_ADDED)))
    signals = store.list_signals()
    check("signals", Counter(s.signal_id for s in signals), Counter(r["signal_id"] for r in by(A.SIGNAL_ADDED)))
    decisions = store.list_decisions()
    check("human_decisions", Counter((d.case_id, d.kind, d.ts) for d in decisions if d.kind != "record_submission"),
          Counter((r["case_id"], (r["after"] or {}).get("decision"), r["ts_utc"]) for r in by(A.DECISION)))
    check("submissions", Counter((d.case_id, d.ts) for d in decisions if d.kind == "record_submission"),
          Counter((r["case_id"], r["ts_utc"]) for r in by(A.SUBMISSION_RECORDED)))
    edits = store.list_metadata_edits()
    check("metadata_edits", Counter((e.case_id, e.field, e.ts) for e in edits),
          Counter((r["case_id"], next(iter((r["after"] or {}).keys()), None), r["ts_utc"]) for r in by(A.METADATA_EDITED)))
    correlations = store.list_correlations()
    check("correlations", Counter((c.case_id, c.signal_id, c.created_at) for c in correlations),
          Counter((r["case_id"], r["signal_id"], r["ts_utc"]) for r in by(A.CORRELATION_RUN)))
    sent_alerts = [a for a in store.list_alerts() if a.status in ("SENT", "FAILED")]
    check("alert_deliveries", Counter({a.alert_id: int(a.detail.get("attempts", 1)) for a in sent_alerts}),
          Counter((r["before"] or {}).get("alert_id") for r in by(A.ALERT_SENT)))
    # Status changes: the STATUS_CHANGED chain of every case must lead from creation to the current status.
    chain_problems = []
    status_changes = 0
    for case in store.list_cases():
        created = [r for r in records if r["event_type"] == A.CASE_CREATED and r["case_id"] == case.case_id]
        current = (created[0]["after"] or {}).get("status") if created else None
        for rec in [r for r in records if r["event_type"] == A.STATUS_CHANGED and r["case_id"] == case.case_id]:
            status_changes += 1
            before, after = (rec["before"] or {}).get("status"), (rec["after"] or {}).get("status")
            if before != current or before == after:
                chain_problems.append({"case_id": case.case_id, "seq": rec["seq"], "expected_before": current,
                                       "before": before, "after": after})
            current = after
        if current != case.status:
            chain_problems.append({"case_id": case.case_id, "chain_end": current, "case_status": case.status})
    checks["status_changes"] = {"steps": status_changes, "records": status_changes, "missing": chain_problems[:10],
                                "unexpected_or_duplicate": [], "ok": not chain_problems}
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks}


# --------------------------------------------------------------------------- operations
def alert_latency_stats(store: CaseStore) -> dict[str, Any]:
    alerts = store.list_alerts()
    sent = [a for a in alerts if a.status == "SENT"]
    latencies = []
    for a in sent:
        value = a.detail.get("latency_s")
        if value is None and a.sent_at:
            value = (parse_utc(a.sent_at) - parse_utc(a.created_at)).total_seconds()
        if value is not None:
            latencies.append(float(value))
    return {"alerts": len(alerts), "sent": len(sent), "pending": sum(1 for a in alerts if a.status == "PENDING"),
            "failed": sum(1 for a in alerts if a.status == "FAILED"),
            "target_seconds": 60, "within_target": sum(1 for v in latencies if v <= 60),
            "max_seconds": round(max(latencies), 3) if latencies else None,
            "median_seconds": round(statistics.median(latencies), 3) if latencies else None,
            "note": "operational measurement of this demo instance (pilot target 60 s)"}


def governance_view(pipeline: Any, now: _dt.datetime | None = None) -> dict[str, Any]:
    settings, store = pipeline.settings, pipeline.store
    at = now or pipeline.now()
    cases = store.list_cases()
    overdue = within_48h = 0
    for case in cases:
        if Status(case.status) in (Status.ABGESCHLOSSEN, Status.ABGESCHLOSSEN_NICHT_MELDEPFLICHTIG):
            continue
        drive = pipeline.workflow.deadline_overview(case.case_id, at)["driving"]
        if drive and drive["seconds_remaining"] < 0:
            overdue += 1
        elif drive and drive["seconds_remaining"] <= 48 * 3600:
            within_48h += 1
    evaluation: dict[str, Any] = {"status": "no golden run summary in DATA_DIR yet (run: python -m pms golden)"}
    if settings.golden_summary_path.exists():
        try:
            data = json.loads(settings.golden_summary_path.read_text(encoding="utf-8"))
            evaluation = {"status": "golden run recorded", "passed": data.get("passed"),
                          "generated_at": data.get("generated_at_utc"),
                          "rule_fields": data.get("rule_fields"), "reports": data.get("reports")}
        except (OSError, ValueError):
            evaluation = {"status": "golden run summary unreadable"}
    verify = store.audit.verify()
    quality = audit_quality(store)
    return {
        "generated_at": iso_utc(at),
        "versions": pipeline.versions(),
        "rule_sets": [{"rule": rule, "purpose": purpose} for rule, purpose in RULE_PURPOSES.items()],
        "model_deployments": {
            "analysis": {"deployment": settings.model_deployment_analysis,
                         "model_version": model_version_of(settings.model_deployment_analysis), "agents": ["A3", "A6"]},
            "general": {"deployment": settings.model_deployment_general,
                        "model_version": model_version_of(settings.model_deployment_general),
                        "agents": ["A1", "A2", "A4"]},
            "deployment_type": DEPLOYMENT_TYPE, "fallback": "gpt-6.1-sol for all agents (MODEL_FALLBACK_ALL_GENERAL)",
            "agent_backend": pipeline.runtime.backend, "runtime": pipeline.runtime.describe(),
        },
        "alert_latency": alert_latency_stats(store),
        "worker": {"mode": settings.worker_mode, "heartbeat": store.get_meta("worker_heartbeat"),
                   "heartbeat_wall_clock": store.get_meta("worker_heartbeat_wall"),
                   "last_audit_verify": store.get_meta("audit_verify_last"),
                   "pending_jobs": pipeline.jobs.pending_count()},
        "preview_features": {"enabled": settings.preview_monitoring,
                             "features": ["Agent Monitoring Dashboard", "continuous evaluation", "red teaming",
                                          "insights"],
                             "note": "Preview monitoring is optional and off by default; activation only after "
                                     "Michael's decision (OI-032)."},
        "tracing": {**pipeline.telemetry.describe(), "retention_days": 30,
                    "read_access": "operator role only (A-16, OI-033)",
                    "foundry_tracing": "Application Insights connected" if settings.applicationinsights_connection_string
                    else "offline: local spans in DATA_DIR/traces"},
        "policy_and_budget": {
            "allowed_region_policy": "Azure Policy 'Allowed locations' on the resource group (only the chosen EU region)",
            "tag_policy": "required tag 'workload' (report only)",
            "budget_usd": settings.budget_usd, "budget_alerts": "50 / 80 / 100 % actual and 100 % forecast",
            "cost_scope_configured": bool(settings.cost_scope)},
        "content_safety": {"prompt_shields": "content filter with Prompt Shields on the model deployments "
                                             "(Bicep; to be confirmed, OI-034)",
                           "injection_rule": INJ, "tools": "read-only, validated and audited by app code"},
        "evaluation": evaluation,
        "open_human_decisions": {s.value: sum(1 for c in cases if c.status == s) for s in OPEN_DECISION_STATUSES},
        "deadlines": {"overdue": overdue, "due_within_48h": within_48h},
        "audit": {**verify.to_dict(), "records": quality["records"], "missing_fields": quality["missing_fields"]},
        "links": {"workbook": "Azure Monitor workbook 'PMS agent demo - operations' (Bicep)",
                  "traces": "Application Insights / Foundry tracing (operator role only)"},
        "disclaimer": {"de": DISCLAIMER_DE, "en": DISCLAIMER_EN},
    }
