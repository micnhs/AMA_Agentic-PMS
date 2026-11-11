"""Live smoke test against the deployed app (Gate C, checks 13 and 14; T13, T14) - standard library only.

Part 1 (automatic): health, sign-in enforced, case list, upload of a synthetic critical letter (new
fictional case), alert within 60 s and triage status, analysis finished with a complete report,
decision refused for a non-reviewer token, audit chain, cost view.
Part 2 (after the human live check): the smoke case carries a human reviewer decision with reason
(approval bound to a content hash; a recorded submission stops the clock), the audit chain still
verifies and the report shows the decision.

The expected API is BUILD_SPEC 4.3; ``GET /api/cases/{id}`` returns :meth:`pms.pipeline.Pipeline.case_view`.
The smoke letter is synthetic (fictional pseudonym ``P-ZZ-nnnn`` that is not in the registry).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from .agents.specs import IMDRF_CODES
from .cli import ApiError, api_request, token_roles
from .models import iso_utc, parse_utc, utcnow

ALERT_TARGET_S = 60

SMOKE_LETTER = """SYNTHETIC DEMO DOCUMENT – FICTIONAL DATA – SMOKE TEST – NO REAL PATIENTS, PEOPLE, INSTITUTIONS OR PRODUCTS

Smoke Test Clinic (fictional), Department of Orthopaedics
Westshire, England – {date}
Our ref: SMOKE/{stamp}

To: Fictivia Medical Ltd. (fictional), Vigilance Department

Re: Femoral stem fracture followed by death – patient P-ZZ-{num}

Dear Vigilance Team,

Our patient (pseudonym P-ZZ-{num}) received a Fictivia OA-S5 femoral stem in England. The stem fractured,
revision surgery was performed and the patient died a few days later. This letter is a synthetic smoke test.

Yours sincerely,
Consultant (fictional role, no name used in the demo)
"""


def _case_id(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("case_id", "id"):
        if body.get(key):
            return str(body[key])
    nested = body.get("case")
    return str(nested.get("case_id")) if isinstance(nested, dict) and nested.get("case_id") else None


def _case(body: Any) -> dict[str, Any]:
    return body.get("case", body) if isinstance(body, dict) else {}


def run_smoke_test(base_url: str, *, part: int, token: str | None, timeout_s: float = 600.0,
                   previous: Path | None = None, request: Callable[..., tuple[int, Any]] = api_request,
                   sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    started = iso_utc(utcnow())

    def add(name: str, ok: bool, detail: str = "", *, skipped: bool = False, **data: Any) -> None:
        checks.append({"name": name, "ok": bool(ok) and not skipped, "skipped": skipped, "detail": detail, **data})

    def call(method: str, path: str, **kw: Any) -> tuple[int | None, Any]:
        try:
            return request(base_url, method, path, **kw)
        except ApiError as exc:
            return None, str(exc)

    roles = token_roles(token)
    case_id: str | None = None
    if part == 1:
        status, body = call("GET", "/healthz")
        add("healthz without sign-in", status == 200 and isinstance(body, dict) and body.get("status") == "ok",
            f"HTTP {status}", versions=(body or {}).get("versions") if isinstance(body, dict) else None)
        status, _ = call("GET", "/api/cases")
        add("API refuses requests without sign-in", status in (401, 403, 302), f"HTTP {status}")
        status, _ = call("GET", "/api/cases", token=token)
        add("case list with Entra token", status == 200, f"HTTP {status}; roles: {', '.join(roles) or 'unknown'}")

        now = utcnow()
        stamp = now.strftime("%Y%m%d%H%M%S")
        letter = SMOKE_LETTER.format(date=now.strftime("%d %B %Y"), stamp=stamp, num=stamp[-4:])
        t0 = time.monotonic()
        status, body = call("POST", "/api/letters", token=token,
                            body={"filename": f"smoke-test-{stamp}.txt", "text": letter})
        case_id = _case_id(body)
        if status == 403:
            add("upload synthetic critical letter", False, "token lacks an upload role (PMS.Analyst)", skipped=True)
        else:
            add("upload synthetic critical letter", status in (200, 201) and case_id is not None,
                f"HTTP {status}, case {case_id}")
        if case_id:
            alert_ok, latency, view = False, None, {}
            while time.monotonic() - t0 < ALERT_TARGET_S + 30:
                status, view = call("GET", f"/api/cases/{case_id}", token=token)
                sent = [a for a in (view or {}).get("alerts", []) if a.get("status") == "SENT"] if isinstance(
                    view, dict) else []
                if sent:
                    signal = ((view or {}).get("signals") or [{}])[0]
                    try:
                        latency = (parse_utc(sent[0]["sent_at"]) - parse_utc(signal["received_at"])).total_seconds()
                    except (KeyError, TypeError, ValueError):
                        latency = time.monotonic() - t0
                    alert_ok = latency <= ALERT_TARGET_S
                    break
                sleep(2)
            add("critical alert within 60 s", alert_ok, f"latency {latency} s", latency_s=latency)
            add("critical case waits for human triage", _case(view).get("status") == "TRIAGE_ERFORDERLICH",
                f"status {_case(view).get('status')}")
            state = None
            while time.monotonic() - t0 < timeout_s:
                status, view = call("GET", f"/api/cases/{case_id}", token=token)
                state = _case(view).get("analysis_state")
                if state in ("DONE", "FAILED"):
                    break
                sleep(5)
            add("analysis finished", state == "DONE", f"analysis state {state}")
            status, report = call("GET", f"/api/cases/{case_id}/report?format=json", token=token)
            rec = (report or {}).get("recommendation") or {} if isinstance(report, dict) else {}
            complete = isinstance(report, dict) and (report.get("completeness") or {}).get("complete")
            codes = [c.get("code") for c in rec.get("imdrf_candidates", [])]
            add("case report complete with draft recommendation", bool(complete) and rec.get("draft") is True and
                all(c in IMDRF_CODES for c in codes), f"HTTP {status}; causality {rec.get('causality')}; IMDRF {codes}")
            if "PMS.MedicalReviewer" in roles:
                add("decision refused for non-reviewers", False, "token has the medical reviewer role", skipped=True)
            else:
                status, _ = call("POST", f"/api/cases/{case_id}/decision", token=token,
                                 body={"decision": "approve", "reason": "smoke test: must be refused"})
                add("decision refused for non-reviewers", status == 403, f"HTTP {status}")
        status, body = call("GET", "/api/audit/verify", token=token)
        add("audit chain verifies", status == 200 and isinstance(body, dict) and bool(body.get("ok")), f"HTTP {status}")
        status, body = call("GET", "/api/costs", token=token)
        platform = (body or {}).get("platform", {}) if isinstance(body, dict) else {}
        add("cost view answers (actual costs or a clear message)", status == 200 and (
            platform.get("available") or platform.get("message")), f"HTTP {status}")
    else:
        prior = json.loads(previous.read_text(encoding="utf-8")) if previous and previous.exists() else {}
        case_id = prior.get("smoke_case_id")
        if not case_id:
            add("smoke case from part 1", False, "run part 1 first (its result file holds the case id)")
        else:
            status, view = call("GET", f"/api/cases/{case_id}", token=token)
            decisions = (view or {}).get("decisions", []) if isinstance(view, dict) else []
            human = [d for d in decisions if d.get("reason") and d.get("kind") != "record_submission"]
            add("human reviewer decision with reason", bool(human), "; ".join(
                f"{d.get('kind')} by {d.get('actor_id')}" for d in human) or "no decision yet")
            approvals = (view or {}).get("approvals", []) if isinstance(view, dict) else []
            add("approval bound to a content hash", all(a.get("content_hash") for a in approvals) and bool(approvals),
                f"{len(approvals)} approval(s)", skipped=not approvals)
            status, audit = call("GET", f"/api/audit?case_id={case_id}", token=token)
            records = audit if isinstance(audit, list) else (audit or {}).get("records", []) if isinstance(
                audit, dict) else []
            agent_decisions = [r for r in records if r.get("event_type") == "DECISION" and r.get("actor_type") != "human"]
            add("no decision by an agent or the system", not agent_decisions, f"{len(records)} audit records read")
            status, report = call("GET", f"/api/cases/{case_id}/report?format=json", token=token)
            summary = ((report or {}).get("reviewer_decision") or {}).get("summary", "") if isinstance(report, dict) else ""
            add("report shows the reviewer decision", bool(summary) and not summary.startswith("pending"), summary[:120])
            status, body = call("GET", "/api/audit/verify", token=token)
            add("audit chain verifies", status == 200 and isinstance(body, dict) and bool(body.get("ok")),
                f"HTTP {status}")
    relevant = [c for c in checks if not c["skipped"]]
    return {"part": part, "base_url": base_url, "started_utc": started, "finished_utc": iso_utc(utcnow()),
            "smoke_case_id": case_id, "token_roles": roles, "checks": checks,
            "passed": bool(relevant) and all(c["ok"] for c in relevant),
            "note": "synthetic smoke-test data only; nothing is sent to an authority"}

