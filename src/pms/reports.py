"""Case report export - JSON and Markdown (concept 10; D-15; Goal Card check 7, test T07).

A report contains: all signals incl. duplicates and follow-ups with receipt time and source; facts
with the source text they came from; matched implant and product data with the registry check;
the recommendation (draft) with causality on the 4-level MDCG scale and IMDRF code candidates
(only A0401/F1905 with attribution, else an empty list with a note); evidence for and against;
uncertainty and missing data; correlations with source ID, quote and corpus version; deadlines per
rule set; case costs; the versions used (app, rules, prompts, models, corpus) and the reviewer
decision; plus the disclaimer. :func:`check_report` lists what is missing - check 7 counts only
complete reports.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict
from typing import Any

from .agents.specs import (
    AGENTS, CAUSALITY_NOTE, CAUSALITY_SCALE, IMDRF_ATTRIBUTION, IMDRF_CANDIDATE_STATUS, IMDRF_CODES,
    IMDRF_NO_CANDIDATE_NOTE,
)
from .costs import case_costs
from .intake import CHANNEL_LABELS
from .models import STATUS_LABELS_EN, Status, iso_utc
from .versions import manifest

REPORT_VERSION = "1.0"
DISCLAIMER_DE = ("Hinweis: Das Demo arbeitet ausschließlich mit erfundenen Daten. Es illustriert Abläufe und trifft "
                 "keine Aussage zur Konformität mit MDR, UK MDR, ISO 13485, GAMP 5 oder anderen Vorgaben. Es ersetzt "
                 "keine regulatorische oder medizinische Bewertung.")
DISCLAIMER_EN = ("Note: the demo works exclusively with invented data. It illustrates workflows and makes no statement "
                 "about conformity with MDR, UK MDR, ISO 13485, GAMP 5 or other requirements. It does not replace a "
                 "regulatory or medical assessment.")
DRAFT_NOTICE = ("AI content in this report is a draft ('Entwurf'); agents recommend, only the medical reviewer "
                "decides. Nothing is ever sent to an authority by this system.")
SECTIONS: tuple[str, ...] = (
    "signals", "facts", "implant_and_product", "recommendation", "evidence", "uncertainty_and_missing_data",
    "correlation", "deadlines", "costs", "versions", "reviewer_decision", "disclaimer",
)


def _latest(runs: list[Any], agent_id: str, signal_id: str | None = None) -> Any | None:
    found = [r for r in runs if r.agent_id == agent_id and r.status == "DONE"
             and (signal_id is None or r.signal_id == signal_id)]
    return found[-1] if found else None


def _imdrf(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for cand in candidates:
        code = cand.get("code")
        if code not in IMDRF_CODES:
            continue  # never shown: only the demo list exists
        out.append({**IMDRF_CODES[code], "code": code, "basis": cand.get("basis"), "quote": cand.get("quote"),
                    "status": IMDRF_CANDIDATE_STATUS, "attribution": IMDRF_ATTRIBUTION})
    return out


def build_case_report(pipeline: Any, case_id: str, *, now: _dt.datetime | None = None) -> dict[str, Any]:
    store, ref, workflow = pipeline.store, pipeline.ref, pipeline.workflow
    at = now or pipeline.now()
    case = store.get_case(case_id)
    signals = store.list_signals(case_id)
    runs = store.list_agent_runs(case_id)
    by_id = {s.signal_id: s for s in signals}

    signal_rows = [{
        "signal_id": s.signal_id, "ref": s.ref, "relation": s.relation, "received_at": s.received_at,
        "source": s.source, "channel": CHANNEL_LABELS.get(s.source, s.source), "filename": s.filename,
        "readable": s.readable, "language": s.language, "sha256": s.text_sha256,
        "injection_flag": bool(s.injection.get("flag")),
        "injection_patterns": sorted({h["pattern_id"] for h in s.injection.get("hits", [])}),
        "crit_severity": s.crit.get("severity"), "registry_check": s.registry.get("value"),
        "deadline_not_reset": s.deadline_not_reset, "completeness": s.completeness, "text": s.text,
    } for s in signals]

    rule_facts = [{"signal_ref": by_id[f.signal_id].ref if f.signal_id in by_id else None, "name": f.name,
                   "value": f.value, "quote": f.quote, "line": f.line, "verified": f.verified, "origin": f.origin}
                  for f in store.list_facts(case_id)]
    agent_facts = []
    matching = []
    for s in signals:
        a1 = _latest(runs, AGENTS["A1"].agent_id, s.signal_id)
        for fact in (a1.output or {}).get("facts", []) if a1 else []:
            agent_facts.append({"signal_ref": s.ref, "run_id": a1.run_id, **fact, "draft": True})
        a2 = _latest(runs, AGENTS["A2"].agent_id, s.signal_id)
        if a2:
            matching.append({"signal_ref": s.ref, "run_id": a2.run_id, **(a2.output or {})})

    registry_checks = []
    for s in signals:
        entries = [asdict(e) for e in ref.registry if e.reg_id in s.registry.get("entry_ids", [])]
        registry_checks.append({"signal_ref": s.ref, "status": s.registry.get("status"),
                                "value": s.registry.get("value"), "details": s.registry.get("details", []),
                                "entries": entries})
    models = sorted({e["model"] for rc in registry_checks for e in rc["entries"]} | {
        f.value for f in store.list_facts(case_id) if f.name == "model" and isinstance(f.value, str)})
    families = sorted({m.family for code, m in ref.catalogue.models.items() if code in models} |
                      ({case.product_family} if case.product_family else set()))
    products = [{"family": fam, "name_en": ref.catalogue.families[fam].name_en if fam in ref.catalogue.families
                 else None,
                 "models": [{"model": m.model, "udi_di": m.udi_di, "component": m.component,
                             "description": m.description} for code, m in ref.catalogue.models.items()
                            if m.family == fam and code in models]} for fam in families]

    a3 = _latest(runs, AGENTS["A3"].agent_id)
    rec = store.latest_recommendation(case_id)
    a3_out = (a3.output or {}) if a3 else {}
    rec_content = rec.content if rec else {}
    candidates = _imdrf(a3_out.get("imdrf_candidates", []))
    recommendation: dict[str, Any] | None = None
    if a3:
        recommendation = {
            "draft": True, "label": "draft", "label_de": "Entwurf", "run_id": a3.run_id,
            "recommendation_id": rec.rec_id if rec else None, "version": rec.version if rec else None,
            "content_hash": rec.content_hash if rec else None,
            "causality": a3_out.get("causality"), "causality_scale": list(CAUSALITY_SCALE),
            "causality_note": CAUSALITY_NOTE, "not_clinically_validated": True,
            "causality_rationale": a3_out.get("causality_rationale"),
            "product_defect_possible": a3_out.get("product_defect_possible"),
            "expectedness": a3_out.get("expectedness"), "expectedness_basis": a3_out.get("expectedness_basis"),
            "ai_severity": a3_out.get("ai_severity"), "ai_reading_outcome": rec_content.get("ai_reading_outcome"),
            "ai_reading_rule": "AI readings may only raise urgency; a lower reading never lowers it.",
            "imdrf_candidates": candidates,
            "imdrf_note": None if candidates else IMDRF_NO_CANDIDATE_NOTE,
            "imdrf_attribution": IMDRF_ATTRIBUTION,
            "fsca_check_hint": a3_out.get("fsca_check_hint"),
            "request_info_draft": (None if not a3_out.get("request_info_draft") else
                                   {**a3_out["request_info_draft"], "status": "draft - never sent"}),
            "generator": a3_out.get("generator"),
        }

    a6 = _latest(runs, AGENTS["A6"].agent_id)
    corr_rule = []
    for s in signals:
        found = store.list_correlations(case_id=case_id, signal_id=s.signal_id)
        if found:
            r = found[-1].result
            corr_rule.append({"signal_ref": s.ref, "status": r.get("status"), "event_tags": r.get("event_tags"),
                              "hits": r.get("hits", []), "counts": r.get("counts", {}), "note": r.get("note"),
                              "corpus_version": found[-1].corpus_version, "rule_version": r.get("rule_version")})
    tool_calls = [{"run_id": r.run_id, "agent": r.agent_id, **call} for r in runs
                  for call in ((r.output or {}).get("tool_calls") or [])]
    audit_tool_calls = [{"seq": rec_["seq"], "agent": rec_["actor_id"], "tool": (rec_["before"] or {}).get("tool"),
                         "arguments": (rec_["before"] or {}).get("arguments"), "ok": (rec_["after"] or {}).get("ok"),
                         "results": (rec_["after"] or {}).get("results"), "source_ids": rec_["source_ids"],
                         "round": (rec_["after"] or {}).get("round"), "corpus_version": rec_["corpus_version"]}
                        for rec_ in store.audit.records(case_id=case_id, event_type="TOOL_CALL")]

    a4 = _latest(runs, AGENTS["A4"].agent_id)
    decisions = [asdict(d) for d in store.list_decisions(case_id=case_id)]
    approvals = [asdict(a) for a in store.list_approvals(case_id)]
    submissions = [asdict(s) for s in store.list_submissions(case_id)]
    last = decisions[-1] if decisions else None
    summary = (f"{last['kind']} by {last['actor_id']} on {last['ts']} (reason: {last['reason']})" if last else
               "pending - no reviewer decision recorded yet (only the medical reviewer decides)")
    verify = store.audit.verify()
    report = {
        "report_version": REPORT_VERSION,
        "generated_at": iso_utc(at),
        "title": f"Case report {case.case_key}",
        "draft_notice": DRAFT_NOTICE,
        "case": {"case_id": case.case_id, "case_key": case.case_key, "status": case.status,
                 "status_label": STATUS_LABELS_EN.get(Status(case.status), case.status), "critical": case.critical,
                 "severity": case.severity, "product_family": case.product_family, "rule_sets": case.rule_sets,
                 "authority_labels": case.authority_labels, "authority_note": "labels only - nothing is sent",
                 "route_unverified": case.route_unverified, "rs_notes": case.rs_notes,
                 "awareness_anchor": case.awareness_anchor, "anchor_ref": case.anchor_ref,
                 "analysis_state": pipeline.analysis_state_of(case), "content_version": case.content_version,
                 "content_hash": workflow.content_hash(case_id), "urgency": case.urgency,
                 "metadata": case.metadata, "created_at": case.created_at, "updated_at": case.updated_at},
        "signals": signal_rows,
        "facts": {"rule": rule_facts, "agent": agent_facts,
                  "note": "Rule facts are verbatim extractions; agent facts are drafts whose quotes were checked "
                          "against the letter (unverified quotes are marked)."},
        "implant_and_product": {"registry_checks": registry_checks, "products": products,
                                "matching_proposals": matching},
        "recommendation": recommendation,
        "evidence": {"for": a3_out.get("evidence_for", []) if a3 else [],
                     "against": a3_out.get("evidence_against", []) if a3 else []},
        "uncertainty_and_missing_data": {"uncertainty": a3_out.get("uncertainty", []) if a3 else [],
                                         "missing_data": a3_out.get("missing_data", []) if a3 else []},
        "correlation": {"corpus_version": pipeline.search.corpus_version, "rule": corr_rule,
                        "agent": None if a6 is None else {k: (a6.output or {}).get(k) for k in (
                            "correlations", "contradictions", "open_questions", "no_hits_note", "app_checks")}
                        | {"run_id": a6.run_id, "draft": True},
                        "tool_calls": audit_tool_calls or tool_calls,
                        "note": "Named matches and counts only; a correlation is not proof of causality and no hits "
                                "is never an all-clear."},
        "deadlines": workflow.deadline_overview(case_id, at),
        "vigilance_report_drafts": None if a4 is None else {**(a4.output or {}), "run_id": a4.run_id,
                                                            "status": "draft - not submitted, nothing is sent"},
        "costs": case_costs(store, case_id, platform=pipeline.platform_costs()) | {
            "price_table": {"version": pipeline.prices.version, "date": pipeline.prices.date,
                            "source": pipeline.prices.source}},
        "versions": {**manifest(pipeline.settings.synthetic_data_dir),
                     "agent_runs": [{"run_id": r.run_id, "agent": r.agent_id, "status": r.status,
                                     "agent_version": r.agent_version, "model": r.model,
                                     "model_version": r.model_version, "prompt_version": r.prompt_version,
                                     "signal_id": r.signal_id} for r in runs],
                     "price_table": pipeline.prices.version, "agent_backend": pipeline.runtime.backend},
        "reviewer_decision": {"status": case.status, "summary": summary, "decisions": decisions,
                              "approvals": approvals, "submissions": submissions,
                              "deadline_waiver": case.deadline_waiver},
        "metadata_edits": [asdict(e) for e in store.list_metadata_edits(case_id)],
        "audit": {"records_for_case": store.audit.count(case_id=case_id), "chain": verify.to_dict()},
        "disclaimer": {"de": DISCLAIMER_DE, "en": DISCLAIMER_EN},
    }
    report["completeness"] = {"missing": check_report(report)}
    report["completeness"]["complete"] = not report["completeness"]["missing"]
    return report


def check_report(report: dict[str, Any]) -> list[str]:
    """Mandatory content of check 7 that is missing (empty list = complete report)."""
    missing: list[str] = []
    for section in SECTIONS:
        if report.get(section) in (None, "", [], {}):
            missing.append(section)
    signals = report.get("signals") or []
    if any(not s.get("received_at") or not s.get("source") for s in signals):
        missing.append("signals.received_at/source")
    facts = report.get("facts") or {}
    all_facts = (facts.get("rule") or []) + (facts.get("agent") or [])
    if not any(f.get("quote") for f in all_facts):
        missing.append("facts.source_text")
    product = report.get("implant_and_product") or {}
    if len(product.get("registry_checks") or []) != len(signals):
        missing.append("implant_and_product.registry_checks")
    rec = report.get("recommendation") or {}
    if rec.get("causality") not in CAUSALITY_SCALE:
        missing.append("recommendation.causality")
    if "imdrf_candidates" not in rec or (not rec.get("imdrf_candidates") and not rec.get("imdrf_note")):
        missing.append("recommendation.imdrf_candidates")
    if any(c.get("code") not in IMDRF_CODES or not c.get("attribution") for c in rec.get("imdrf_candidates") or []):
        missing.append("recommendation.imdrf_attribution")
    evidence = report.get("evidence") or {}
    if "for" not in evidence or "against" not in evidence:
        missing.append("evidence.for/against")
    unc = report.get("uncertainty_and_missing_data") or {}
    if not unc.get("uncertainty") or "missing_data" not in unc:
        missing.append("uncertainty_and_missing_data")
    corr = report.get("correlation") or {}
    if not corr.get("corpus_version") or not corr.get("rule"):
        missing.append("correlation.rule_results")
    if not corr.get("agent"):
        missing.append("correlation.agent")
    if not (report.get("deadlines") or {}).get("candidates"):
        missing.append("deadlines.candidates")
    versions = report.get("versions") or {}
    if not versions.get("app") or not versions.get("rules") or not versions.get("corpus"):
        missing.append("versions.app/rules/corpus")
    if not any(r.get("prompt_version") and r.get("model") for r in versions.get("agent_runs") or []):
        missing.append("versions.prompts/models")
    costs = report.get("costs") or {}
    if not (costs.get("variable") or {}).get("per_agent") or not costs.get("price_table"):
        missing.append("costs.per_agent")
    decision = report.get("reviewer_decision") or {}
    if not decision.get("summary"):
        missing.append("reviewer_decision.summary")
    disclaimer = report.get("disclaimer") or {}
    if not disclaimer.get("de") or not disclaimer.get("en"):
        missing.append("disclaimer.de/en")
    return sorted(set(missing))


# --------------------------------------------------------------------------- Markdown
def _md_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_md_escape(c) for c in row) + " |" for row in rows]
    return out


def render_markdown(report: dict[str, Any]) -> str:
    c = report["case"]
    lines = [f"# {report['title']}", "", f"> {report['disclaimer']['de']}", ">", f"> {report['disclaimer']['en']}",
             "", f"**{report['draft_notice']}**", "",
             f"Generated {report['generated_at']} - report version {report['report_version']} - "
             f"completeness: {'complete' if report['completeness']['complete'] else 'INCOMPLETE: ' + ', '.join(report['completeness']['missing'])}",
             "", "## Case", ""]
    lines += _table(["Field", "Value"], [
        ["Case", f"{c['case_key']} ({c['case_id']})"], ["Status", f"{c['status_label']} ({c['status']})"],
        ["Critical", "yes" if c["critical"] else "no"], ["Severity (CRIT-1.0)", c["severity"]],
        ["Product family", c["product_family"]], ["Rule sets", ", ".join(c["rule_sets"])],
        ["Authority labels (labels only)", "; ".join(c["authority_labels"])],
        ["Route unverified", "yes" if c["route_unverified"] else "no"],
        ["Awareness anchor", f"{c['awareness_anchor']} (signal {c['anchor_ref']})"],
        ["Content hash", c["content_hash"]]])
    lines += ["", "## Signals (incl. duplicates and follow-ups)", ""]
    lines += _table(["Ref", "Relation", "Received (UTC)", "Source", "Readable", "Injection flag", "Registry check"],
                    [[s["ref"], s["relation"], s["received_at"], s["channel"], s["readable"],
                      "yes" if s["injection_flag"] else "no", s["registry_check"]] for s in report["signals"]])
    for s in report["signals"]:
        lines += ["", f"### Original text of {s['ref']}", "", "```text", (s["text"] or "(unreadable)").rstrip(), "```"]
    lines += ["", "## Facts with source text", ""]
    lines += _table(["Signal", "Origin", "Fact", "Value", "Quote", "Verified"],
                    [[f["signal_ref"], "rule", f["name"], f["value"], f["quote"], f["verified"]]
                     for f in report["facts"]["rule"]] +
                    [[f["signal_ref"], "agent (draft)", f["name"], f["value"], f.get("quote"),
                      f.get("verification")] for f in report["facts"]["agent"]])
    lines += ["", "## Implant and product data", ""]
    for rc in report["implant_and_product"]["registry_checks"]:
        lines.append(f"- {rc['signal_ref']}: {rc['value']}")
        for e in rc["entries"]:
            lines.append(f"  - {e['reg_id']}: patient {e['patient']}, model {e['model']}, UDI-DI {e['udi_di']}, "
                         f"lot {e['lot']}, implanted {e['implant_date']} ({e['country']})")
    for p in report["implant_and_product"]["products"]:
        lines.append(f"- Product family {p['family']} ({p['name_en']}): " + ", ".join(
            f"{m['model']} {m['component']} (UDI-DI {m['udi_di']})" for m in p["models"]))
    rec = report["recommendation"] or {}
    lines += ["", "## Recommendation (draft - Entwurf)", ""]
    if rec:
        lines += _table(["Item", "Draft"], [
            ["Causality", f"{rec['causality']} (scale: {', '.join(rec['causality_scale'])})"],
            ["Rationale", rec["causality_rationale"]], ["Product defect possible", rec["product_defect_possible"]],
            ["Expected per dossier", f"{rec['expectedness']} - {rec['expectedness_basis']}"],
            ["AI urgency reading", f"{rec['ai_severity']} ({rec['ai_reading_outcome']}; {rec['ai_reading_rule']})"],
            ["FSCA check hint", rec["fsca_check_hint"]], ["Note", rec["causality_note"]]])
        lines += ["", "IMDRF code candidates (pending human review):", ""]
        if rec["imdrf_candidates"]:
            lines += _table(["Code", "Annex", "Term", "Basis", "Quote"],
                            [[x["code"], x["annex"], x["term"], x["basis"], x["quote"]] for x in rec["imdrf_candidates"]])
        else:
            lines.append(f"- {rec['imdrf_note']}")
        lines += ["", f"Attribution: {rec['imdrf_attribution']}"]
        if rec.get("request_info_draft"):
            lines += ["", "Request to the sender (draft - never sent):", ""]
            lines += [f"- {f['field']}: {f['question']} ({f['reason']})" for f in rec["request_info_draft"]["fields"]]
    else:
        lines.append("- no recommendation draft available (analysis pending or failed)")
    lines += ["", "## Evidence for and against", ""]
    lines += [f"- FOR: {e['statement']} [{e.get('source_id')}] {e.get('quote') or ''}".rstrip()
              for e in report["evidence"]["for"]] or ["- FOR: none listed"]
    lines += [f"- AGAINST: {e['statement']} [{e.get('source_id')}] {e.get('quote') or ''}".rstrip()
              for e in report["evidence"]["against"]] or ["- AGAINST: none listed"]
    unc = report["uncertainty_and_missing_data"]
    lines += ["", "## Uncertainty and missing data", ""]
    lines += [f"- {u}" for u in unc["uncertainty"]]
    lines += [f"- Missing: {m}" for m in unc["missing_data"]] or ["- Missing data: none recorded"]
    corr = report["correlation"]
    lines += ["", f"## Correlation (corpus version {corr['corpus_version']})", "", corr["note"], ""]
    for r in corr["rule"]:
        lines.append(f"- CORR-1.0 for {r['signal_ref']}: {r['status']}, hits: " + (", ".join(
            f"{h['ref']} ({'/'.join(h['match_types'])})" for h in r["hits"]) or "none (not an all-clear)"))
    for item in (corr.get("agent") or {}).get("correlations") or []:
        lines.append(f"- A6 draft: {item['relation']} [{item['source_id']}] {item['statement']} "
                     f"Quote: {item.get('quote') or '-'} (verified: {item.get('quote_verified')})")
    lines += ["", "## Deadlines per rule set (DL-1.0)", ""]
    dl = report["deadlines"]
    lines += _table(["Rule set", "Basis", "Days", "Due date", "Status"],
                    [[x["rule_set"], x["basis"], x["days"], x["due_date"], x["label"] or x["status"]]
                     for x in dl["candidates"]])
    drive = dl.get("driving") or {}
    lines += ["", f"Earliest (drives priority): {drive.get('due_date')} ({drive.get('rule_set')}, "
                  f"{drive.get('basis')}); clock: {dl['clock']}"]
    drafts = report.get("vigilance_report_drafts") or {}
    if drafts:
        lines += ["", "## Vigilance report drafts (A4 - not submitted)", ""]
        for d in drafts.get("report_drafts", []):
            lines += [f"### {d['rule_set']}", "", "```text", d["draft_text"], "```"]
    costs = report["costs"]
    lines += ["", "## Costs of the case", ""]
    lines += _table(["Agent", "Runs", "Model", "Input tokens", "Output tokens", "USD"],
                    [[a, v["runs"], v["model"], v["input_tokens"], v["output_tokens"], v["usd"]]
                     for a, v in costs["variable"]["per_agent"].items()])
    lines += ["", f"Variable model costs: {costs['variable']['usd_cents']} USD"
                  f"{' (simulated, offline stand-in)' if costs['variable']['simulated'] else ''}; price table "
                  f"{costs['price_table']['version']} ({costs['price_table']['source']}). Fixed-cost share: "
                  f"{costs['fixed_share'].get('usd')} ({costs['fixed_share'].get('method') or costs['fixed_share'].get('note')})."]
    v = report["versions"]
    lines += ["", "## Versions used", "", f"- App {v['app']}; rules {', '.join(v['rules'].values())}; corpus "
                                         f"{v['corpus']}; price table {v['price_table']}; backend {v['agent_backend']}"]
    lines += [f"- {r['agent']} {r['agent_version']}: model {r['model']} ({r['model_version']}), prompt "
              f"{r['prompt_version']}, run {r['run_id']} {r['status']}" for r in v["agent_runs"]]
    rd = report["reviewer_decision"]
    lines += ["", "## Reviewer decision", "", f"- {rd['summary']}"]
    lines += [f"- {d['ts']}: {d['kind']} by {d['actor_id']} ({d['status_before']} -> {d['status_after']}); reason: "
              f"{d['reason']}{'; code ' + d['reason_code'] if d['reason_code'] else ''}" for d in rd["decisions"]]
    lines += [f"- Approval {a['approval_id']} for content hash {a['content_hash'][:16]}... valid: {a['valid']}"
              for a in rd["approvals"]]
    lines += [f"- Submission recorded (made outside the system): {s['channel_label']} {s['reference']} on "
              f"{s['submitted_on']}, receipt {s['receipt_status']}" for s in rd["submissions"]]
    audit = report["audit"]
    lines += ["", "## Audit", "", f"- {audit['records_for_case']} audit records for this case; chain check: "
                                  f"{'OK' if audit['chain']['ok'] else 'BROKEN at ' + str(audit['chain']['first_break_seq'])}",
              "", "---", "", report["disclaimer"]["de"], "", report["disclaimer"]["en"], ""]
    return "\n".join(lines)


def export_report(pipeline: Any, case_id: str, fmt: str = "json") -> tuple[str, str]:
    """(content, media type) for GET /api/cases/{id}/report?format=json|md."""
    report = build_case_report(pipeline, case_id)
    if fmt == "md":
        return render_markdown(report), "text/markdown; charset=utf-8"
    if fmt == "json":
        return json.dumps(report, ensure_ascii=False, indent=2, default=str), "application/json"
    raise ValueError("format must be json or md")
