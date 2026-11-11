"""Server-rendered pages (concept 9): dashboard with the critical queue first, case view, rejected,
unreadable and unmatched inputs, trend, costs, governance, audit trail, sign-in and error pages.

Pure functions from view data (dicts built by :class:`pms.pipeline.Pipeline`, :mod:`pms.reports`,
:mod:`pms.governance`) to escaped HTML. Who may do what is never decided here: forms are shown with
the roles the domain permission matrix allows (:data:`pms.workflow.PERMISSIONS`), and every submitted
action is authorised by :meth:`pms.workflow.Workflow.authorize` on the server. Every AI output is
labelled "Draft (Entwurf)"; decisions are never shown by colour alone.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import auth
from ..agents.specs import CAUSALITY_SCALE, IMDRF_ATTRIBUTION
from ..agents.trend import NOTE_DE, NOTE_EN
from ..audit import EVENT_TYPES
from ..config import Settings
from ..models import CLOSED_STATUSES, REJECT_REASON_CODES, STATUS_LABELS_EN, Status, iso_utc
from ..pipeline import STEPS
from ..reports import DISCLAIMER_DE, DISCLAIMER_EN
from ..versions import APP_VERSION
from ..workflow import EDITABLE_FIELDS, HUMAN_TRANSITIONS, PERMISSIONS, RECEIPT_STATUSES, Action
from .markup import Markup, dl, el, join, json_text, table, url_part

APP_TITLE = "PMS agent demo"
DISCLAIMER_NOTICE = ("Synthetic data only. This demo illustrates workflows; it makes no compliance claim and gives no "
                     "regulatory, legal or medical advice. Agents recommend, people decide - every AI output is a draft "
                     "(Entwurf). Nothing is ever sent to an authority; authorities appear only as labels.")
NAV: tuple[tuple[str, str, str], ...] = (
    ("dashboard", "/", "Dashboard"),
    ("rejected", "/rejected", "Rejected"),
    ("unreadable", "/unreadable", "Unreadable & unmatched"),
    ("trend", "/trend", "Trend"),
    ("costs", "/costs", "Costs"),
    ("governance", "/governance", "Governance"),
    ("audit", "/audit", "Audit trail"),
)
STEP_LABELS: dict[str, str] = {
    "A1": "A1 Signal ingestion", "A2": "A2 Product matching", "CORR": "CORR-1.0 correlation (rule code)",
    "A6": "A6 Evidence & correlation", "A3": "A3 Medical analysis", "A4": "A4 Vigilance report draft",
}
STEP_SHORT: dict[str, str] = {"A1": "A1", "A2": "A2", "CORR": "CORR", "A6": "A6", "A3": "A3", "A4": "A4"}
STEP_STATUS_TEXT: dict[str, str] = {
    "DONE": "✓ done", "RUNNING": "▶ running", "QUEUED": "… queued", "FAILED": "✗ failed",
    "NOT_QUEUED": "– not started", "SKIPPED": "– skipped",
}
BASIS_LABELS: dict[str, str] = {
    "DEATH": "death", "PUBLIC_HEALTH_THREAT": "serious public health threat",
    "SERIOUS_DETERIORATION": "serious deterioration",
    "SERIOUS_DETERIORATION_ANTICIPATED": "serious deterioration, classified as anticipated",
    "PROVISIONAL_REPORTABILITY_UNCLEAR": "provisional - reportability unclear",
}
DEADLINE_STATUS_TEXT: dict[str, str] = {
    "OPEN": "open", "OVERDUE": "OVERDUE", "STOPPED_BY_SUBMISSION": "stopped - submission recorded by a person",
    "WAIVED_BY_HUMAN": "entfällt laut menschlicher Entscheidung (not applicable according to a human decision)",
    "SUPERSEDED_BY_HUMAN": "superseded by a human classification (still shown)",
}
DECISION_LABELS: dict[str, str] = {
    "approve": "Approve this version", "reject": "Reject (reason code R1-R6)", "request_info": "Request information",
    "close": "Close the case", "non_reportable": "Close as non-reportable",
}
DECISION_ACTIONS: dict[str, Action] = {
    "approve": Action.APPROVE, "reject": Action.REJECT, "request_info": Action.REQUEST_INFO, "close": Action.CLOSE,
    "non_reportable": Action.CLOSE_NON_REPORTABLE,
}
ACTION_LABELS: dict[Action, str] = {
    Action.VIEW: "view cases and signals", Action.UPLOAD: "upload letters and add signals",
    Action.EDIT_METADATA: "edit metadata (reason)", Action.RERUN_ANALYSIS: "re-run the analysis",
    Action.TAKE_TRIAGE: "take over triage", Action.RAISE_URGENCY: "raise urgency",
    Action.LOWER_URGENCY: "lower urgency (reason)", Action.START_REVIEW: "take over the medical review",
    Action.APPROVE: "approve", Action.REJECT: "reject", Action.REQUEST_INFO: "request information",
    Action.CLOSE: "close", Action.CLOSE_NON_REPORTABLE: "close as non-reportable",
    Action.CLASSIFY_ANTICIPATED: "classify a deterioration as anticipated",
    Action.RECORD_SUBMISSION: "record a submission (approved version)",
    Action.VIEW_COSTS_GOVERNANCE: "view costs and governance", Action.VIEW_TRACES: "read traces",
    Action.MANAGE_FINDINGS: "withdraw findings, close process findings", Action.LOAD_DEMO_DATA: "load demo data",
    Action.MODIFY_AUDIT: "change audit records",
}
DONE_MESSAGES: dict[str, str] = {
    "upload": "Letter received. The fixed rule code ran before any agent; a critical result already created the "
              "triage task and the alert. The analysis runs in the background.",
    "signal": "Signal added to the case. Any approval of the case is void; the analysis of the new signal is queued.",
    "metadata": "Metadata changed. The edit is logged with user, old and new value and reason.",
    "triage": "Triage taken over by a person.",
    "review": "Medical review taken over.",
    "urgency-raise": "Urgency raised.",
    "urgency-lower": "Urgency lowered by the medical reviewer (with reason).",
    "decision": "Decision recorded with reason.",
    "anticipated": "Deterioration classified as anticipated; the 15-day candidate is added, the 10-day one stays visible.",
    "reanalysis": "Re-analysis queued.",
    "submission": "Submission recorded (it was made outside this system - nothing was sent). The deadline clock stopped.",
    "finding": "Finding updated.",
    "seed": "Demo data loaded (already loaded messages are skipped).",
}


@dataclass
class PageContext:
    identity: auth.Identity | None
    settings: Settings
    now: _dt.datetime
    active: str = ""
    done: str | None = None
    can: Callable[[Action], bool] = field(default=lambda action: False)


# --------------------------------------------------------------------------- small helpers
def status_label(status: str | None) -> str:
    try:
        return STATUS_LABELS_EN[Status(status)]
    except (ValueError, KeyError):
        return str(status)


def status_badge(status: str | None) -> Markup:
    return el("span", status_label(status), " ", el("code", status), class_=f"badge status status-{str(status).lower()}")


def critical_badge(critical: bool, severity: str | None = None) -> Markup:
    if not critical:
        return el("span", "not critical", class_="badge noncritical")
    return el("span", "CRITICAL", f" ({severity})" if severity else "", class_="badge critical")


def draft_label(text: str = "Draft (Entwurf)") -> Markup:
    return el("span", text, class_="draft-label", title="AI output - a draft until a person decides")


def unknown(value: Any) -> Any:
    return value if value not in (None, "", []) else el("span", "unknown", class_="unknown")


def yes_no(value: Any) -> str:
    return "yes" if value else "no"


def fmt_duration(seconds: float) -> str:
    total = abs(int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"


def countdown(seconds: Any, *, id_: str | None = None) -> Markup:
    if seconds is None:
        return el("span", "no open deadline", class_="countdown none")
    value = int(seconds)
    overdue = value < 0
    text = f"OVERDUE by {fmt_duration(value)}" if overdue else fmt_duration(value)
    return el("span", text, class_="countdown overdue" if overdue else "countdown", data_countdown=True,
              data_seconds=value, id=id_, role="timer")


def case_link(case_id: str, label: str | None = None) -> Markup:
    return el("a", label or case_id, href=f"/cases/{url_part(case_id)}", class_="case-link")


def allowed_roles(actions: list[Action]) -> list[str]:
    roles = {r for a in actions for r in PERMISSIONS.get(a, frozenset())}
    return [auth.ROLE_LABELS.get(r, r) for r in sorted(roles)] or ["nobody"]


def flash(ctx: PageContext) -> Markup | None:
    message = DONE_MESSAGES.get(ctx.done or "")
    return el("p", message, class_="flash", role="status", id="flash") if message else None


def section(section_id: str, title: Any, *children: Any, **attributes: Any) -> Markup:
    return el("section", el("h2", title), *children, id=section_id, **attributes)


def hidden(name: str, value: Any) -> Markup:
    return el("input", type="hidden", name=name, value=value)


def label(text: str, control: Markup, hint: str | None = None) -> Markup:
    return el("label", el("span", text, class_="label-text"), control, el("small", hint) if hint else None)


# --------------------------------------------------------------------------- layout
def _user_box(ctx: PageContext) -> Markup:
    identity = ctx.identity
    if identity is None or not identity.authenticated:
        return el("div", el("a", "Sign in", href="/login"), class_="user")
    actor = identity.actor
    roles = ", ".join(identity.role_labels) or "no PMS role"
    if ctx.settings.is_azure:
        switch = el("a", "Sign out", href="/.auth/logout?post_logout_redirect_uri=/", class_="button-link")
    else:
        switch = el("form", el("button", "Switch user", type="submit", class_="secondary"), method="post",
                    action="/logout", class_="inline", id="form-logout")
    return el("div", "Signed in as ", el("strong", actor.actor_id, class_="user-id"), f" ({roles})", " ", switch,
              class_="user", data_user=actor.actor_id)


def layout(ctx: PageContext, title: str, *content: Any, page: str = "") -> Markup:
    nav = el("nav", [el("a", text, href=href, class_="active" if key == ctx.active else None,
                        aria_current="page" if key == ctx.active else None) for key, href, text in NAV],
             aria_label="Main")
    mode = ("offline demo (local test users)" if not ctx.settings.is_azure else "Azure (Microsoft Entra ID sign-in)")
    head = join(
        Markup("<!doctype html>"),
        Markup('<html lang="en">'),
        el("head", el("meta", charset="utf-8"), el("meta", name="viewport", content="width=device-width, initial-scale=1"),
           el("title", f"{title} - {APP_TITLE}"),
           el("link", rel="stylesheet", href=f"/static/app.css?v={APP_VERSION}"),
           el("script", src=f"/static/app.js?v={APP_VERSION}", defer=True)),
    )
    body = el(
        "body",
        el("div", el("strong", "Disclaimer: "), DISCLAIMER_NOTICE, el("br"),
           el("span", DISCLAIMER_DE, lang="de"), id="disclaimer", class_="disclaimer", role="note"),
        el("header", el("a", APP_TITLE, href="/", class_="brand"), nav, _user_box(ctx), class_="topbar"),
        el("main", *content, id="main"),
        el("footer", f"{APP_TITLE} {APP_VERSION} - {mode} - synthetic data only - server time {iso_utc(ctx.now)}. ",
           DISCLAIMER_EN),
        data_page=page, data_server_now=iso_utc(ctx.now),
    )
    return join(head, body, Markup("</html>"))


# --------------------------------------------------------------------------- sign-in and errors
def login_page(ctx: PageContext, *, next_url: str = "/", problem: str | None = None) -> Markup:
    parts: list[Any] = [el("h1", "Sign in")]
    if problem:
        parts.append(el("p", problem, class_="problem", id="sign-in-problem"))
    if ctx.settings.is_azure:
        target = f"/.auth/login/aad?post_login_redirect_uri={url_part(next_url)}"
        parts += [el("p", "People sign in only with Microsoft Entra ID. Rights come only from the app role in your "
                          "token: PMS.Viewer, PMS.Analyst, PMS.Regulatory, PMS.MedicalReviewer or PMS.Operator."),
                  el("p", el("a", "Sign in with Microsoft Entra ID", href=target, class_="button-link",
                             id="entra-sign-in"))]
    elif not ctx.settings.local_test_users:
        parts.append(el("p", "Local test users are switched off (LOCAL_TEST_USERS=false). Start the offline demo "
                             "with LOCAL_TEST_USERS=true to pick a demo user."))
    else:
        buttons = []
        for user in auth.LOCAL_TEST_USERS.values():
            may = [ACTION_LABELS[a] for a in Action if user.role in PERMISSIONS.get(a, frozenset())
                   and a not in (Action.VIEW, Action.VIEW_COSTS_GOVERNANCE)]
            buttons.append(el("li", el("button", el("strong", user.name), " - ",
                                       auth.ROLE_LABELS[user.role], type="submit", name="user", value=user.name,
                                       class_="user-choice", id=f"pick-{user.name}"),
                              el("small", "may also: " + (", ".join(may) if may else "read only (cases, costs, "
                                                                                       "governance)"))))
        parts += [
            el("p", "Offline demo: pick a built-in test user (one per role). The choice is kept in a cookie in this "
                    "browser. Scripts can send the header ", el("code", "X-Demo-User: <user>"), " instead. Test users "
                    "exist only in the offline mode; the app refuses to start in Azure with test users switched on."),
            el("form", hidden("next", next_url), el("ul", buttons, class_="user-list"), method="post",
               action="/login", id="form-login"),
        ]
    return layout(ctx, "Sign in", *parts, page="login")


def error_page(ctx: PageContext, status: int, title: str, reason: str, *, back: str | None = None) -> Markup:
    parts = [el("h1", f"{status} - {title}"),
             el("p", reason, class_="reason", id="error-reason")]
    if status == 403:
        parts.append(el("p", "Permissions come from the fixed role matrix of the domain (concept 7.3). Refused "
                             "attempts are recorded in the audit trail (ACCESS_DENIED).", class_="hint"))
    parts.append(el("p", el("a", "Back", href=back or "/", id="error-back")))
    return layout(ctx, f"{status} {title}", el("section", *parts, id="error", class_="error", data_status=status),
                  page="error")


# --------------------------------------------------------------------------- dashboard
def step_status(info: dict[str, Any] | None, job: dict[str, Any] | None) -> str:
    """Status of one step; a job that waits in the queue has no per-step progress yet."""
    if info and info.get("status"):
        return str(info["status"])
    job_status = (job or {}).get("status")
    if job_status in ("QUEUED", "RUNNING"):
        return "QUEUED"
    if job_status == "SKIPPED":
        return "SKIPPED"
    return "NOT_QUEUED"


def _step_cell(step: str, info: dict[str, Any] | None, job: dict[str, Any] | None = None) -> Markup:
    status = step_status(info, job)
    title = STEP_LABELS[step]
    if info and info.get("error"):
        title += f" - error: {info['error']}"
    return Markup(el("td", STEP_STATUS_TEXT.get(status, status.lower()), class_=f"step step-{status.lower()}",
                     data_step=step, data_status=status, title=title))


def _case_rows(rows: list[dict[str, Any]]) -> tuple[list[list[Any]], list[dict[str, Any]]]:
    out, attributes = [], []
    for r in rows:
        steps = (r.get("progress") or {}).get("steps") or {}
        job = (r.get("progress") or {}).get("latest_job")
        status_cell = Markup(el("td", el("span", r["status_label"], class_="status-label"), " ",
                                el("code", r["status"]),
                                el("div", "waiting for a person to take over the triage", class_="waiting")
                                if r["status"] == Status.TRIAGE_ERFORDERLICH else None,
                                class_="status"))
        flags = []
        if r.get("injection_flag"):
            flags.append(el("span", "injection flag (INJ-1.0)", class_="flag"))
        for check in sorted({c for c in r.get("registry_checks") or [] if c and c != "MATCH"}):
            flags.append(el("span", f"registry {check}", class_="flag"))
        if r.get("route_unverified"):
            flags.append(el("span", "route unverified", class_="flag"))
        tasks = [t["kind"] for t in r.get("open_tasks") or []]
        deadline = (join(r["driving_due_date"], " ", el("small", r.get("driving_rule_set") or ""))
                    if r.get("driving_due_date") else el("span", "none", class_="unknown"))
        out.append([
            Markup(el("td", case_link(r["case_id"], r["case_key"]), class_="case")),
            status_cell,
            critical_badge(r["critical"], r["severity"]),
            join(", ".join(r.get("rule_sets") or []) or "unknown", el("br"),
                 el("small", "; ".join(r.get("authority_labels") or []))),
            deadline,
            Markup(el("td", countdown(r.get("countdown_seconds")), class_="countdown-cell")),
            *[_step_cell(step, steps.get(step), job) for step in STEPS],
            r.get("signals"),
            join(", ".join(sorted(set(tasks))) or "-", " ", flags),
        ])
        classes = ["case-row"]
        if r.get("overdue"):
            classes.append("overdue")
        if r.get("critical"):
            classes.append("critical")
        attributes.append({"class_": " ".join(classes), "data_case_id": r["case_id"], "data_status": r["status"],
                           "data_critical": "true" if r.get("critical") else "false",
                           "data_overdue": "true" if r.get("overdue") else "false"})
    return out, attributes


CASE_TABLE_HEADERS = ["Case", "Status", "Critical (CRIT-1.0)", "Rule sets and authority labels",
                      "Earliest deadline (DL-1.0)", "Countdown", *[STEP_SHORT[s] for s in STEPS], "Signals",
                      "Open tasks and flags"]


def case_table(rows: list[dict[str, Any]], table_id: str, empty: str) -> Markup:
    body, attributes = _case_rows(rows)
    return table(CASE_TABLE_HEADERS, body, empty=empty, row_attrs=attributes, id=table_id, class_="cases")


def _upload_form(action: str, form_id: str, button: str) -> Markup:
    return el(
        "form",
        label("Letter file (.txt or .pdf)", el("input", type="file", name="file",
                                               accept=".txt,.pdf,text/plain,application/pdf")),
        label("or paste the text", el("textarea", name="text", rows="6")),
        label("File name for pasted text", el("input", name="filename", placeholder="letter.txt")),
        label("Received at (UTC, optional)", el("input", name="received_at", placeholder="2026-11-16T10:05:00Z"),
              "Default: now. With a time zone, e.g. Z."),
        el("button", button, type="submit"),
        method="post", action=action, enctype="multipart/form-data", id=form_id, class_="action-form",
    )


def action_block(ctx: PageContext, key: str, title: str, actions: list[Action], form: Markup, *,
                 case_status: str | None = None, note: str | None = None) -> Markup:
    permitted = any(ctx.can(a) for a in actions)
    transitions = [HUMAN_TRANSITIONS[a][0] for a in actions if a in HUMAN_TRANSITIONS]
    applicable = case_status is None or not transitions or any(Status(case_status) in t for t in transitions)
    hints: list[Any] = [el("span", "Allowed for: " + ", ".join(allowed_roles(actions)), class_="allowed")]
    if not permitted:
        hints.append(el("span", "Your role may not do this - the server refuses and records the attempt.",
                        class_="not-permitted"))
    if not applicable:
        hints.append(el("span", f"Not available in the current status ({status_label(case_status)}).",
                        class_="not-applicable"))
    if note:
        hints.append(el("span", note, class_="note"))
    return el("details", el("summary", title), el("p", hints, class_="hint"), form, class_="action",
              id=f"action-{key}", open=permitted and applicable,
              data_permitted="true" if permitted else "false")


def dashboard_page(ctx: PageContext, data: dict[str, Any], trend: dict[str, Any]) -> Markup:
    counts = data["counts"]
    closed_states = {s.value for s in CLOSED_STATUSES}
    critical = data["critical_queue"]
    others = [r for r in data["cases"] if r["status"] not in closed_states]
    closed = [r for r in data["cases"] if r["status"] in closed_states]
    earliest = next((r for r in critical if r.get("countdown_seconds") is not None), None)
    earliest_box = (el("p", "Earliest deadline in the critical queue: ", case_link(earliest["case_id"],
                                                                                   earliest["case_key"]),
                       f" - due {earliest['driving_due_date']} ({earliest.get('driving_rule_set')}) - ",
                       countdown(earliest["countdown_seconds"], id_="earliest-countdown"), class_="earliest")
                    if earliest else el("p", "No open deadline in the critical queue.", class_="earliest"))
    summary = el("ul", [
        el("li", el("strong", counts["cases"]), " cases"),
        el("li", el("strong", counts["critical"]), " in the critical queue"),
        el("li", el("strong", counts["overdue"]), " overdue"),
        el("li", el("strong", counts["analysis_pending"]), " analyses queued or running"),
        el("li", el("a", f"{len(data.get('rejected') or [])} rejected", href="/rejected")),
        el("li", el("a", f"{len(data.get('unreadable_unmatched') or [])} unreadable or unmatched inputs",
                    href="/unreadable")),
    ], class_="counts", id="counts")
    a5 = section("a5-trend", "A5 Light trend view",
                 el("p", "Plain code without a model: ", f"{len(trend.get('rows') or [])} synthetic monthly rows "
                         f"({trend.get('period') or 'no period'}), counts only. ", NOTE_EN, " ",
                    el("a", "Open the trend view", href="/trend")), class_="a5")
    upload = section("upload", "Upload a letter", action_block(
        ctx, "upload", "Upload a letter (synthetic data only)", [Action.UPLOAD],
        _upload_form("/api/letters", "form-upload", "Upload - the rule code runs before any agent"),
        note="Critical detection (CRIT-1.0) runs before the analysis: a critical letter gets a triage task and an "
             "alert at once."))
    seed = section("demo-data", "Demo data", action_block(
        ctx, "seed", "Load the demo data (12 letters, 6 seed messages)", [Action.LOAD_DEMO_DATA],
        el("form", el("button", "Load demo data", type="submit"), method="post", action="/api/seed",
           id="form-seed", class_="action-form"),
        note="Idempotent: messages that are already loaded are skipped."))
    legend = el("p", "Agent progress per case (latest analysis job): ",
                ", ".join(STEP_LABELS[s] for s in STEPS),
                ". Symbols: ✓ done, ▶ running, … queued, ✗ failed, – not started. A5 (trend) has no per-case run.",
                class_="legend")
    content = [
        el("h1", "Dashboard"), flash(ctx), summary,
        el("p", "", id="refresh-notice", class_="refresh-notice", hidden=True),
        section("critical-queue", "Critical queue",
                el("p", "Critical cases come first, sorted by the earliest deadline; overdue cases are marked "
                        "OVERDUE. Only a person moves a critical case on (triage); agents may only raise urgency.",
                   class_="hint"),
                earliest_box, case_table(critical, "critical-table", "No critical case."), class_="critical-queue"),
        section("other-cases", "Other current cases", case_table(others, "cases-table", "No other open case.")),
    ]
    if closed:
        content.append(section("closed-cases", "Closed cases", case_table(closed, "closed-table", "None.")))
    content += [legend, a5, upload, seed]
    return layout(ctx, "Dashboard", *content, page="dashboard")


# --------------------------------------------------------------------------- case page
def _summary(case: dict[str, Any], view: dict[str, Any], report: dict[str, Any]) -> Markup:
    urgency = case.get("urgency") or {}
    approvals = view.get("approvals") or []
    valid = [a for a in approvals if a.get("valid")]
    if valid:
        a = valid[-1]
        approval = join("valid approval of version ", str(a["content_version"]), " by ", a["reviewer_id"], " at ",
                        a["ts"], " (content hash ", el("code", a["content_hash"][:16] + "…"), ")")
    elif approvals:
        a = approvals[-1]
        approval = join("no valid approval - the last one was voided at ", a.get("voided_at") or "?", ": ",
                        a.get("voided_reason") or "")
    else:
        approval = "no approval yet"
    route = ("yes - the authority route for this incident state is unverified; check it before any submission"
             if case.get("route_unverified") else "no")
    metadata = case.get("metadata") or {}
    rows = [
        ("Status", status_badge(case["status"])),
        ("Critical", join(critical_badge(case["critical"], case.get("severity")),
                          el("ul", [el("li", r) for r in urgency.get("reasons") or []]) if urgency.get("reasons")
                          else None)),
        ("Product family", case.get("product_family")),
        ("Rule sets (RS-1.0)", ", ".join(case.get("rule_sets") or [])),
        ("Authority labels", join("; ".join(case.get("authority_labels") or []) or None,
                                  el("small", " (labels only - nothing is sent)"))),
        ("Route unverified", route),
        ("Incident states", ", ".join(case.get("incident_states") or [])),
        ("Rule-set notes (RS-1.0)", el("ul", [el("li", n) for n in case.get("rs_notes") or []], id="rs-notes")
         if case.get("rs_notes") else "none"),
        ("Awareness anchor (first receipt)", join(case.get("awareness_anchor") or "unknown",
                                                  f" (signal {case.get('anchor_ref')})" if case.get("anchor_ref")
                                                  else "")),
        ("Analysis", join(el("span", case.get("analysis_state"), id="analysis-summary"),
                          el("small", " - a new analysis is queued or running; the drafts below are from the "
                                      "previous run until it finishes")
                          if case.get("analysis_state") in ("PENDING", "RUNNING") and view.get("recommendation")
                          else None)),
        ("Content version", join(str(case.get("content_version")), " - hash ",
                                 el("code", (report.get("case") or {}).get("content_hash", "")[:16] + "…"))),
        ("Approval", approval),
        ("Edited metadata", el("ul", [el("li", f"{k}: ", unknown(v)) for k, v in sorted(metadata.items())])
         if metadata else "none"),
        ("Case report", join(el("a", "JSON", href=f"/api/cases/{url_part(case['case_id'])}/report?format=json"),
                             " · ",
                             el("a", "Markdown", href=f"/api/cases/{url_part(case['case_id'])}/report?format=md"))),
    ]
    return section("summary", "Case summary", dl(rows, class_="summary"))


def _actions(ctx: PageContext, case: dict[str, Any]) -> Markup:
    cid = url_part(case["case_id"])
    status = case["status"]
    field_options = [el("option", f, value=f) for f in EDITABLE_FIELDS]
    decision_options = [el("option", text, value=key) for key, text in DECISION_LABELS.items()]
    code_options = [el("option", "- no code (only for reject: required) -", value="")] + [
        el("option", f"{code} - {text}", value=code) for code, text in REJECT_REASON_CODES.items()]
    blocks = [
        action_block(ctx, "add-signal", "Add a signal to this case", [Action.UPLOAD],
                     _upload_form(f"/api/cases/{cid}/signals", "form-add-signal", "Add signal"),
                     note="The rule code links it (duplicate or follow-up); a new signal voids an approval."),
        action_block(ctx, "metadata", "Edit metadata (reason required)", [Action.EDIT_METADATA], el(
            "form",
            label("Field", el("select", field_options, name="field")),
            label("New value", el("input", name="new_value"), "Empty means unknown."),
            label("Reason (required)", el("textarea", name="reason", rows="2", required=True)),
            el("button", "Save change", type="submit"),
            method="post", action=f"/api/cases/{cid}/metadata", id="form-metadata", class_="action-form"),
            note="Logged with user, old and new value; any change after approval voids the approval."),
        action_block(ctx, "triage", "Take over the triage", [Action.TAKE_TRIAGE], el(
            "form", label("Note (optional)", el("textarea", name="reason", rows="2")),
            el("button", "Take over the triage", type="submit"),
            method="post", action=f"/api/cases/{cid}/triage", id="form-triage", class_="action-form"),
            case_status=status, note="Only a person moves a critical case on."),
        action_block(ctx, "review", "Take over the medical review", [Action.START_REVIEW], el(
            "form", label("Note (optional)", el("textarea", name="reason", rows="2")),
            el("button", "Take over the review", type="submit"),
            method="post", action=f"/api/cases/{cid}/review", id="form-review", class_="action-form"),
            case_status=status),
        action_block(ctx, "decision", "Medical reviewer decision (reason required)",
                     list(DECISION_ACTIONS.values()), el(
                         "form",
                         label("Decision", el("select", decision_options, name="decision")),
                         label("Reason code (reject only)", el("select", code_options, name="reason_code")),
                         label("Reason (required)", el("textarea", name="reason", rows="3", required=True)),
                         el("button", "Record decision", type="submit"),
                         method="post", action=f"/api/cases/{cid}/decision", id="form-decision",
                         class_="action-form"),
                     case_status=status,
                     note="An approval binds to exactly the current content version."),
        action_block(ctx, "urgency-raise", "Raise urgency", [Action.RAISE_URGENCY], el(
            "form", hidden("direction", "raise"), label("Reason (optional)", el("textarea", name="reason", rows="2")),
            el("button", "Raise urgency", type="submit"),
            method="post", action=f"/api/cases/{cid}/urgency", id="form-urgency-raise", class_="action-form")),
        action_block(ctx, "urgency-lower", "Lower urgency (medical reviewer, reason required)",
                     [Action.LOWER_URGENCY], el(
                         "form", hidden("direction", "lower"),
                         label("Reason (required)", el("textarea", name="reason", rows="2", required=True)),
                         el("button", "Lower urgency", type="submit"),
                         method="post", action=f"/api/cases/{cid}/urgency", id="form-urgency-lower",
                         class_="action-form"),
                     note="An AI answer such as 'not serious' never lowers urgency - only this human step does."),
        action_block(ctx, "anticipated", "Classify a serious deterioration as anticipated",
                     [Action.CLASSIFY_ANTICIPATED], el(
                         "form", label("Reason (required)", el("textarea", name="reason", rows="2", required=True)),
                         el("button", "Classify as anticipated", type="submit"),
                         method="post", action=f"/api/cases/{cid}/anticipated", id="form-anticipated",
                         class_="action-form")),
        action_block(ctx, "reanalysis", "Re-run the analysis", [Action.RERUN_ANALYSIS], el(
            "form", label("Reason (optional)", el("textarea", name="reason", rows="2")),
            el("button", "Re-run the analysis", type="submit"),
            method="post", action=f"/api/cases/{cid}/reanalysis", id="form-reanalysis", class_="action-form"),
            note="Voids an existing approval."),
        action_block(ctx, "submission", "Record a submission made outside the system", [Action.RECORD_SUBMISSION], el(
            "form",
            label("Channel label", el("input", name="channel_label", required=True,
                                      placeholder="e.g. national authority portal (label only)")),
            label("Reference", el("input", name="reference", required=True)),
            label("Submitted on", el("input", name="submitted_on", type="date", required=True)),
            label("Receipt status", el("select", [el("option", s, value=s) for s in RECEIPT_STATUSES],
                                       name="receipt_status")),
            label("Note (optional)", el("textarea", name="reason", rows="2")),
            el("button", "Record submission - nothing is sent", type="submit"),
            method="post", action=f"/api/cases/{cid}/submission", id="form-submission", class_="action-form"),
            case_status=status,
            note="Approved version only. This records a submission made elsewhere and stops the deadline clock; "
                 "the system never sends anything, not even simulated."),
    ]
    return section("actions", "Actions", el("p", "Every action is checked by the domain role matrix on the server and "
                                                 "written to the audit trail.", class_="hint"), blocks)


def _progress(view: dict[str, Any]) -> Markup:
    progress = view.get("progress") or {}
    steps = progress.get("steps") or {}
    job = progress.get("latest_job")
    rows, attributes = [], []
    for step in STEPS:
        info = steps.get(step) or {}
        status = step_status(info, job)
        reading = info.get("ai_reading")
        rows.append([
            STEP_LABELS[step],
            Markup(el("td", STEP_STATUS_TEXT.get(status, status.lower()), class_=f"step step-{status.lower()}",
                      data_step=step, data_status=status)),
            info.get("round"), info.get("tool_calls") if step != "CORR" else info.get("hits"),
            join(reading, f" ({info.get('ai_reading_outcome')})" if info.get("ai_reading_outcome") else "")
            if reading else "",
            info.get("error") or "",
        ])
        attributes.append({"data_step": step})
    job = progress.get("latest_job")
    job_text = (f"Latest job {job['job_id']}: {job['status']} ({job['reason']}); rounds {job['rounds']}; enqueued "
                f"{job['enqueued_at']}; finished {job.get('finished_at') or '-'}"
                + (f"; error: {job['error']}" if job.get("error") else "")) if job else "No analysis job yet."
    return section(
        "progress", "Agent progress",
        el("p", f"Analysis state: {progress.get('analysis_state')}. {job_text}", class_="hint", id="analysis-state"),
        el("p", "", id="progress-notice", class_="refresh-notice", hidden=True),
        table(["Step", "Status", "Round", "Tool calls / hits", "AI urgency reading (may only raise)", "Error"], rows,
              row_attrs=attributes, id="progress-table"),
        el("p", "A5 light trend view is a portfolio view without a model - see ", el("a", "Trend", href="/trend"), ".",
           class_="hint"),
        data_case_id=(view.get("case") or {}).get("case_id"), data_analysis_state=progress.get("analysis_state"),
        data_status=(view.get("case") or {}).get("status"),
    )


def _deadlines(overview: dict[str, Any], case: dict[str, Any]) -> Markup:
    drive = overview.get("driving") or {}
    drive_key = (drive.get("rule_set"), drive.get("basis")) if drive else None
    rows, attributes = [], []
    for c in overview.get("candidates") or []:
        is_drive = drive_key == (c["rule_set"], c["basis"]) and c["status"] in ("OPEN", "OVERDUE")
        status_text = c.get("label") or DEADLINE_STATUS_TEXT.get(c["status"], c["status"])
        rows.append([
            c["rule_set"], BASIS_LABELS.get(c["basis"], c["basis"]), c["days"], c["start_date"], c["due_date"],
            c.get("due_end_local"), status_text,
            Markup(el("td", countdown(c.get("seconds_remaining")))) if c["status"] in ("OPEN", "OVERDUE")
            else "-",
            el("strong", "earliest - drives priority", class_="driving") if is_drive else "",
            ", ".join(c.get("sources") or []),
        ])
        classes = ["deadline-row"] + (["overdue"] if c["status"] == "OVERDUE" else []) + (
            ["driving"] if is_drive else [])
        attributes.append({"class_": " ".join(classes), "data_rule_set": c["rule_set"], "data_status": c["status"],
                           "data_driving": "true" if is_drive else "false"})
    waiver = overview.get("waiver")
    notes = [el("p", f"Clock: {overview.get('clock')}. Day 1 is the day after the first receipt; follow-ups never "
                     "reset the deadline; the clock stops only when a person records a submission.", class_="hint")]
    if waiver:
        notes.append(el("p", f"{waiver.get('label')} - {waiver.get('by')}: {waiver.get('reason')} ({waiver.get('at')})",
                        class_="waiver"))
    if case.get("route_unverified"):
        notes.append(el("p", "Route unverified: the authority route for this incident state is not verified "
                             "(e.g. France, ANSM). The deadline is shown; check the route before any submission.",
                        class_="route-unverified", id="route-unverified"))
    return section("deadlines", "Deadlines per rule set (DL-1.0, RS-1.0)", *notes, table(
        ["Rule set", "Basis", "Days", "Start (day 1)", "Due date", "Due end (local)", "Status", "Countdown",
         "Priority", "Sources"], rows, row_attrs=attributes, id="deadlines-table",
        empty="No deadline candidate (unknown reportability stays visible as a task)."))


def _letter_text(signal: dict[str, Any]) -> Markup:
    text = signal.get("text") or ""
    spans = sorted(tuple(s) for s in (signal.get("injection") or {}).get("masked_spans") or [])
    parts: list[Any] = []
    pos = 0
    for start, end in spans:
        start, end = max(start, pos), min(end, len(text))
        if start >= end:
            continue
        parts.append(text[pos:start])
        parts.append(el("mark", text[start:end], class_="withheld",
                        title="INJ-1.0: this paragraph is withheld from the agents (letter content is data)"))
        pos = end
    parts.append(text[pos:])
    return el("pre", parts, class_="letter-text")


def _relation(signal: dict[str, Any]) -> str:
    link = signal.get("link") or {}
    kind = link.get("kind") or signal.get("relation") or "unknown"
    target = link.get("target_ref")
    text = {"NEW_CASE": "first signal of the case", "DUPLICATE": "duplicate", "FOLLOW_UP": "follow-up"}.get(kind, kind)
    if target:
        text += f" of {target}"
    if link.get("matched_on"):
        text += f" (matched on {', '.join(link['matched_on'])})"
    return text


def _crit_text(crit: dict[str, Any]) -> Markup:
    """Severity, then each matched term with its group and line (the words that made the signal critical)."""
    matched = [f"{i.get('group')}: '{i.get('match')}' (line {i.get('line')})" for i in crit.get("indicators") or []]
    if not matched:
        matched = list(crit.get("groups") or [])
    return join(crit.get("severity") or "unknown", " - matched: " + "; ".join(matched) if matched else None)


def _signals(view: dict[str, Any]) -> Markup:
    articles = []
    signals = view.get("signals") or []
    for s in signals:
        inj = s.get("injection") or {}
        reg = s.get("registry") or {}
        crit = s.get("crit") or {}
        missing = (s.get("completeness") or {}).get("missing") or []
        facts = dl([
            ("Relation (LINK-1.0)", _relation(s)),
            ("Received (UTC)", s.get("received_at")),
            ("Source / channel", f"{s.get('source')} - {s.get('channel')}"),
            ("File", s.get("filename")),
            ("Language", s.get("language")),
            ("Readable", yes_no(s.get("readable"))),
            ("Critical indicators (CRIT-1.0)", _crit_text(crit)),
            ("Registry check (REG-1.0)", reg.get("value") or reg.get("status")),
            ("Injection flag (INJ-1.0)", el("strong", "FLAGGED - the marked paragraph is withheld from the agents",
                                             class_="flag") if inj.get("flag") else "no"),
            ("Deadline not reset", yes_no(s.get("deadline_not_reset"))),
            ("Missing data", ", ".join(missing) if missing else "none"),
            ("SHA-256", el("code", (s.get("text_sha256") or "")[:16] + "…")),
        ], class_="signal-facts")
        body = (_letter_text(s) if s.get("readable") else
                el("p", "Unreadable: no text could be extracted. The input stays visible with a task; nothing is "
                        "dropped.", class_="unreadable"))
        articles.append(el("article", el("h3", f"Signal {s.get('ref')} ", el("small", _relation(s))), facts,
                           el("h4", "Original letter"), body, class_="signal", data_signal_id=s.get("signal_id"),
                           data_ref=s.get("ref"), data_relation=s.get("relation"),
                           data_injection="true" if inj.get("flag") else "false"))
    originals = view.get("originals") or []
    extra = el("p", "Stored originals: ", ", ".join(o["name"] for o in originals), class_="hint") if originals else None
    return section("signals", f"Signals and original letters ({len(signals)})",
                   el("p", "All signals of the case, including duplicates and follow-ups. Letter text is shown as "
                           "received (escaped); it is data, never an instruction.", class_="hint"), articles, extra)


def _facts(report: dict[str, Any]) -> Markup:
    facts = report.get("facts") or {}
    rule_rows = [[f.get("signal_ref"), f.get("name"), unknown(f.get("value")), el("q", f["quote"]) if f.get("quote")
                  else "", f.get("line"), yes_no(f.get("verified"))] for f in facts.get("rule") or []]
    agent_rows = [[f.get("signal_ref"), f.get("name"), unknown(f.get("value")), el("q", f["quote"]) if f.get("quote")
                   else "", f.get("line"), f.get("verification") or ""] for f in facts.get("agent") or []]
    return section(
        "facts", "Facts with source quotes",
        el("h3", "Rule extraction (verbatim)"),
        table(["Signal", "Fact", "Value", "Source quote", "Line", "Verified"], rule_rows, id="rule-facts",
              empty="No rule facts."),
        el("h3", "A1 facts ", draft_label()),
        table(["Signal", "Fact", "Value", "Source quote", "Line", "Quote check (app code)"], agent_rows,
              id="agent-facts", empty="No A1 draft yet."),
        el("p", facts.get("note") or "", class_="hint"))


def _registry(report: dict[str, Any]) -> Markup:
    data = report.get("implant_and_product") or {}
    rows = []
    for rc in data.get("registry_checks") or []:
        entries = rc.get("entries") or []
        detail = [el("li", f"{e.get('reg_id')}: patient {e.get('patient')}, model {e.get('model')}, UDI-DI "
                           f"{e.get('udi_di')}, lot {e.get('lot')}, implanted {e.get('implant_date')} "
                           f"({e.get('country')})") for e in entries]
        rows.append([rc.get("signal_ref"), rc.get("value") or rc.get("status"),
                     el("ul", detail) if detail else "no registry entry",
                     "; ".join(str(d) for d in rc.get("details") or [])])
    products = [el("li", f"{p.get('family')} ({p.get('name_en')}): ",
                   ", ".join(f"{m['model']} {m['component']} (UDI-DI {m['udi_di']})" for m in p.get("models") or [])
                   or "no model identified") for p in data.get("products") or []]
    proposals = []
    for m in data.get("matching_proposals") or []:
        assignment = "; ".join(f"{a.get('registry_entry')} {a.get('patient')} {a.get('model')} lot {a.get('lot')}"
                               for a in m.get("proposed_assignment") or []) or "no assignment"
        deviations = "; ".join(d.get("detail", "") for d in m.get("deviations") or []) or "none"
        proposals.append([m.get("signal_ref"), (m.get("registry_check") or {}).get("value"), assignment, deviations,
                          m.get("proposal_note")])
    return section(
        "registry", "Implant, product and registry data",
        table(["Signal", "Registry check (REG-1.0)", "Registry entries", "Details"], rows, id="registry-table"),
        el("h3", "Products"), el("ul", products) if products else el("p", "No product identified.", class_="empty"),
        el("h3", "A2 matching proposal ", draft_label()),
        table(["Signal", "Registry check", "Proposed assignment", "Deviations", "Note"], proposals,
              id="matching-table", empty="No A2 draft yet."))


def _evidence_rows(items: list[dict[str, Any]]) -> list[list[Any]]:
    return [[e.get("statement"), e.get("source_id"), el("q", e["quote"]) if e.get("quote") else "",
             {True: "verified", False: "NOT verified", None: "-"}.get(e.get("quote_verified"), "-")] for e in items]


def _recommendation(report: dict[str, Any]) -> Markup:
    rec = report.get("recommendation")
    if not rec:
        return section("recommendation", join("Recommendation ", draft_label()),
                       el("p", "No draft yet - the analysis is pending or failed. The rule results above are "
                               "already complete.", class_="empty"))
    candidates = rec.get("imdrf_candidates") or []
    imdrf_rows = [[c.get("code"), f"Annex {c.get('annex')} - {c.get('annex_title')}", c.get("term"),
                   c.get("definition"), c.get("basis"), el("q", c["quote"]) if c.get("quote") else "",
                   c.get("status")] for c in candidates]
    imdrf = el("section",
               el("h3", "IMDRF code candidates ", draft_label("candidates - pending human review")),
               table(["Code", "Annex", "Term", "Definition", "Basis", "Quote", "Status"], imdrf_rows,
                     id="imdrf-table", empty=rec.get("imdrf_note") or "no candidate from the demo list"),
               el("p", "Attribution: ", rec.get("imdrf_attribution") or IMDRF_ATTRIBUTION, class_="attribution"),
               id="imdrf")
    evidence = report.get("evidence") or {}
    unc = report.get("uncertainty_and_missing_data") or {}
    request_info = rec.get("request_info_draft")
    request_block = None
    if request_info:
        request_block = join(el("h3", "Request to the sender ", draft_label("draft - never sent")),
                             el("ul", [el("li", f"{f.get('field')}: {f.get('question')} ({f.get('reason')})")
                                       for f in request_info.get("fields") or []]))
    scale = " | ".join(CAUSALITY_SCALE)
    return section(
        "recommendation", join("Recommendation ", draft_label()),
        el("p", rec.get("generator") or "", " - ", "agents recommend; only the medical reviewer decides.",
           class_="hint"),
        dl([
            ("Causality (4-level scale)", join(el("strong", rec.get("causality")), el("small", f" scale: {scale}"))),
            ("Rationale", rec.get("causality_rationale")),
            ("Product defect possible", rec.get("product_defect_possible")),
            ("Expected per dossier", join(rec.get("expectedness"), " - ", rec.get("expectedness_basis") or "")),
            ("AI urgency reading", join(rec.get("ai_severity"), f" ({rec.get('ai_reading_outcome')})"
                                        if rec.get("ai_reading_outcome") else "", " - ", rec.get("ai_reading_rule"))),
            ("FSCA check hint", rec.get("fsca_check_hint") or "none"),
            ("Note", rec.get("causality_note")),
        ], class_="recommendation", id="recommendation-facts"),
        imdrf,
        el("h3", "Evidence for"), table(["Statement", "Source", "Quote", "Quote check"],
                                       _evidence_rows(evidence.get("for") or []), id="evidence-for",
                                       empty="None listed."),
        el("h3", "Evidence against"), table(["Statement", "Source", "Quote", "Quote check"],
                                           _evidence_rows(evidence.get("against") or []), id="evidence-against",
                                           empty="None listed."),
        el("h3", "Uncertainty"),
        el("ul", [el("li", u) for u in unc.get("uncertainty") or []]) if unc.get("uncertainty")
        else el("p", "None recorded.", class_="empty"),
        el("h3", "Missing data"),
        el("ul", [el("li", m) for m in unc.get("missing_data") or []]) if unc.get("missing_data")
        else el("p", "None recorded.", class_="empty"),
        request_block,
        data_draft="true")


def _correlation(report: dict[str, Any], findings: list[dict[str, Any]]) -> Markup:
    corr = report.get("correlation") or {}
    rule_rows = []
    for r in corr.get("rule") or []:
        hits = r.get("hits") or []
        rule_rows.append([r.get("signal_ref"), r.get("status"), ", ".join(r.get("event_tags") or []),
                          el("ul", [el("li", f"{h.get('ref')} ({h.get('source_type')}; "
                                             f"{'/'.join(h.get('match_types') or [])}): {h.get('title')}")
                                    for h in hits]) if hits else "no hits - not an all-clear",
                          json_text(r.get("counts")), r.get("corpus_version")])
    agent = corr.get("agent") or {}
    agent_rows = [[c.get("relation"), c.get("source_id"), c.get("statement"),
                   el("q", c["quote"]) if c.get("quote") else "",
                   {True: "verified", False: "NOT verified", None: "-"}.get(c.get("quote_verified"), "-")]
                  for c in agent.get("correlations") or []]
    approved = [[f.get("finding_id"), f.get("status"), f.get("summary"), f.get("reviewer_id"), f.get("reason"),
                 f.get("updated_at")] for f in findings]
    return section(
        "correlation", f"Evidence and correlation (corpus {corr.get('corpus_version')})",
        el("p", corr.get("note") or "", class_="hint"),
        el("h3", "CORR-1.0 (rule code: named matches and counts, no rates)"),
        table(["Signal", "Status", "Event tags", "Hits", "Counts", "Corpus"], rule_rows, id="corr-rule",
              empty="Not run yet."),
        el("h3", "A6 correlation ", draft_label()),
        table(["Relation", "Source ID", "Statement", "Quote", "Quote check"], agent_rows, id="corr-agent",
              empty=("A6 ran and named no correlation for this case - this is not an all-clear." if agent
                     else "No A6 draft yet.")),
        el("p", "Contradictions: ", "; ".join(str(c.get("statement")) for c in agent.get("contradictions") or [])
           or "none listed", class_="hint") if agent else None,
        el("p", "Open questions: ", "; ".join(str(q) for q in agent.get("open_questions") or []) or "none listed",
           class_="hint") if agent else None,
        el("h3", "Approved findings (separate from drafts)"),
        table(["Finding", "Status", "Summary", "Reviewer", "Reason", "Updated"], approved, id="approved-findings",
              empty="No approved finding for this case."),
    )


def _report_drafts(report: dict[str, Any]) -> Markup:
    drafts = report.get("vigilance_report_drafts") or {}
    items = []
    for d in drafts.get("report_drafts") or []:
        items.append(el("article", el("h3", f"{d.get('rule_set')} - {d.get('authority_label')} ",
                                      draft_label("draft - not submitted, nothing is sent")),
                        el("p", f"Form: {d.get('form_label')}; due {d.get('driving_due_date')} "
                                f"({d.get('deadline_basis')}, {d.get('deadline_days')} days, from rule code)"),
                        el("pre", d.get("draft_text") or "", class_="draft-text"),
                        el("p", "Missing mandatory data: ", ", ".join(d.get("missing_mandatory_data") or []) or "none",
                           class_="hint"), class_="report-draft"))
    return section("report-drafts", join("A4 vigilance report drafts ", draft_label()),
                   el("p", drafts.get("deadline_note") or "", class_="hint") if drafts else None,
                   items or el("p", "No A4 draft yet.", class_="empty"))


def _decisions(view: dict[str, Any]) -> Markup:
    decisions = [[d["ts"], d["kind"], d["actor_id"], f"{d['status_before']} → {d['status_after']}",
                  d.get("reason") or "", join(d.get("reason_code") or "", f" ({REJECT_REASON_CODES.get(d['reason_code'])})"
                                              if d.get("reason_code") else "")] for d in view.get("decisions") or []]
    approvals = [[a["ts"], a["approval_id"], a["content_version"], el("code", a["content_hash"][:16] + "…"),
                  a["reviewer_id"], a["reason"], "valid" if a["valid"] else f"voided {a.get('voided_at')}: "
                  f"{a.get('voided_reason')}"] for a in view.get("approvals") or []]
    submissions = [[s["ts"], s["channel_label"], s["reference"], s["submitted_on"], s["receipt_status"],
                    s["recorded_by"], s["approval_id"]] for s in view.get("submissions") or []]
    return section(
        "decisions", "Human decisions, approvals and recorded submissions",
        table(["Time (UTC)", "Decision", "By", "Status", "Reason", "Reason code"], decisions, id="decisions-table",
              empty="No human decision yet (only the medical reviewer decides)."),
        el("h3", "Approvals (bound to a content hash)"),
        table(["Time (UTC)", "Approval", "Version", "Content hash", "Reviewer", "Reason", "State"], approvals,
              id="approvals-table", empty="No approval."),
        el("h3", "Submissions recorded by a person (made outside the system)"),
        table(["Time (UTC)", "Channel label", "Reference", "Submitted on", "Receipt", "Recorded by", "Approval"],
              submissions, id="submissions-table", empty="None - nothing is ever sent by this system."))


def _metadata_edits(view: dict[str, Any]) -> Markup:
    rows, attributes = [], []
    for e in view.get("metadata_edits") or []:
        rows.append([e["ts"], Markup(el("td", e["user_id"], class_="user")), e["field"],
                     Markup(el("td", json_text(e.get("old_value")) or "unknown", class_="old-value")),
                     Markup(el("td", json_text(e.get("new_value")) or "unknown", class_="new-value")), e["reason"]])
        attributes.append({"class_": "metadata-edit", "data_field": e["field"], "data_edit_id": e["edit_id"]})
    return section("metadata-edits", "Metadata edits",
                   table(["Time (UTC)", "User", "Field", "Old value", "New value", "Reason"], rows,
                         row_attrs=attributes, id="metadata-table", empty="No metadata edit."))


def _tasks_alerts(view: dict[str, Any]) -> Markup:
    tasks = [[t["kind"], t["status"], t["title"], t["created_at"], t.get("created_by"),
              f"{t.get('closed_by') or ''} {t.get('close_reason') or ''}".strip()] for t in view.get("tasks") or []]
    alerts = [[a["alert_id"], a["status"], a.get("channel"), a["reason"], a["created_at"], a.get("sent_at"),
               (a.get("detail") or {}).get("latency_s")] for a in view.get("alerts") or []]
    return section(
        "tasks", "Tasks and alerts",
        table(["Kind", "Status", "Title", "Created", "By", "Closed by / reason"], tasks, id="tasks-table",
              empty="No task."),
        el("h3", "Alerts (local outbox or demo e-mail; case ID, reason and link only)"),
        table(["Alert", "Status", "Channel", "Reason", "Created", "Sent", "Latency (s)"], alerts, id="alerts-table",
              empty="No alert."))


def _agent_runs(view: dict[str, Any]) -> Markup:
    rows = []
    for r in view.get("agent_runs") or []:
        usage = r.get("usage") or {}
        rows.append([r["run_id"], r["agent_id"], r["status"], r.get("signal_id"), r.get("model"),
                     r.get("model_version"), r.get("prompt_version"), r.get("agent_version"),
                     f"{usage.get('input', 0)} / {usage.get('output', 0)}", r.get("finished_at"), r.get("error") or ""])
    return section("agent-runs", join("Agent runs ", draft_label("every output is a draft")),
                   table(["Run", "Agent", "Status", "Signal", "Model", "Model version", "Prompt", "Agent version",
                          "Tokens in / out", "Finished", "Error"], rows, id="agent-runs-table",
                         empty="No agent run yet."))


def audit_table(records: list[dict[str, Any]], table_id: str) -> Markup:
    rows, attributes = [], []
    for r in records:
        versions = [v for v in (r.get("agent_version"), r.get("model"), r.get("prompt_version")) if v]
        versions += list(r.get("rule_versions") or [])
        rows.append([r.get("seq"), r.get("ts_utc"), r.get("event_type"), r.get("actor_type"),
                     Markup(el("td", r.get("actor_id"), class_="actor")), r.get("source"),
                     case_link(r["case_id"]) if r.get("case_id") else "",
                     Markup(el("td", json_text(r.get("before")), class_="before")),
                     Markup(el("td", json_text(r.get("after")), class_="after")),
                     Markup(el("td", r.get("reason") or "", class_="reason")),
                     Markup(el("td", ", ".join(versions), class_="versions"))])
        attributes.append({"class_": "audit-row", "data_event": r.get("event_type"), "data_seq": r.get("seq")})
    return table(["Seq", "Time (UTC)", "Event", "Actor type", "Actor", "Source", "Case", "Before", "After", "Reason",
                  "Versions"], rows, row_attrs=attributes, id=table_id, empty="No audit record.")


def case_page(ctx: PageContext, view: dict[str, Any], report: dict[str, Any],
              findings: list[dict[str, Any]]) -> Markup:
    case = view["case"]
    callout = None
    if case["status"] == Status.TRIAGE_ERFORDERLICH:
        callout = el("p", el("strong", "Critical - waiting for a person to take over the triage. "),
                     "The system never moves a critical case on by itself; agents and rules may only raise urgency.",
                     class_="callout critical", id="triage-callout")
    records = view.get("audit") or []
    content = [
        el("p", el("a", "← Dashboard", href="/"), class_="crumbs"),
        el("h1", f"Case {case['case_key']} ", status_badge(case["status"]), " ",
           critical_badge(case["critical"], case.get("severity"))),
        flash(ctx), callout,
        _summary(case, view, report),
        _actions(ctx, case),
        _progress(view),
        _deadlines(view.get("deadlines") or {}, case),
        _signals(view),
        _facts(report),
        _registry(report),
        _recommendation(report),
        _correlation(report, findings),
        _report_drafts(report),
        _decisions(view),
        _metadata_edits(view),
        _tasks_alerts(view),
        _agent_runs(view),
        section("audit-history", f"Audit history ({len(records)} records)",
                el("p", "Append-only, SHA-256 hash chain; there is no way to change or delete records.",
                   class_="hint"), audit_table(records, "audit-table")),
    ]
    return layout(ctx, f"Case {case['case_key']}", *content, page="case")


# --------------------------------------------------------------------------- lists
def rejected_page(ctx: PageContext, rows: list[dict[str, Any]]) -> Markup:
    body = [[case_link(r["case_id"], r["case_key"]), Markup(el("td", el("strong", r.get("reason_code") or "?"),
                                                                class_="reason-code")),
             r.get("reason_code_label") or "", r.get("reason"), r.get("reviewer"), r.get("ts"),
             status_label(r.get("current_status"))] for r in rows]
    attributes = [{"class_": "rejected-row", "data_case_id": r["case_id"], "data_reason_code": r.get("reason_code")}
                  for r in rows]
    legend = el("ul", [el("li", el("strong", code), f" - {text}") for code, text in REJECT_REASON_CODES.items()],
                id="reason-codes", class_="legend")
    return layout(ctx, "Rejected", el("h1", "Rejected recommendations"),
                  el("p", "Rejections by the medical reviewer with reason code and reason. Reason codes:"), legend,
                  section("rejected", "Rejections",
                          table(["Case", "Reason code", "Meaning", "Reason", "Reviewer", "Time (UTC)",
                                 "Current status"], body, row_attrs=attributes, id="rejected-table",
                                empty="No rejected recommendation yet.")),
                  page="rejected")


def unreadable_page(ctx: PageContext, rows: list[dict[str, Any]]) -> Markup:
    body = [[r["kind"], r["status"], r["title"], case_link(r["case_id"]) if r.get("case_id") else "",
             r.get("signal_ref"), r.get("filename"), r.get("registry_check"), r.get("created_at")] for r in rows]
    attributes = [{"class_": "input-row", "data_kind": r["kind"], "data_status": r["status"]} for r in rows]
    return layout(ctx, "Unreadable and unmatched inputs", el("h1", "Unreadable and unmatched inputs"),
                  el("p", "Inputs whose text could not be extracted (UNREADABLE) or whose product or patient could "
                          "not be matched with the registry (UNMATCHED). Nothing is dropped: each one stays visible "
                          "with a task until a person handles it. Missing evidence is not an all-clear.",
                     class_="hint"),
                  section("inputs", "Inputs", table(["Kind", "Task status", "Task", "Case", "Signal", "File",
                                                     "Registry check", "Created"], body, row_attrs=attributes,
                                                    id="inputs-table", empty="None.")),
                  page="unreadable")


def trend_page(ctx: PageContext, trend: dict[str, Any]) -> Markup:
    columns = trend.get("columns") or []
    rows = [[unknown(row.get(c["key"])) for c in columns] for row in trend.get("rows") or []]
    agent = trend.get("agent") or {}
    return layout(ctx, "Trend", el("h1", trend.get("title") or "Light trend view"), section(
        "trend", "Monthly counts",
        dl([("Unit", trend.get("unit")), ("Period", trend.get("period")), ("Denominator", trend.get("denominator")),
            ("Source", trend.get("source")), ("Synthetic", yes_no(trend.get("synthetic"))),
            ("Produced by", f"{agent.get('code')} {agent.get('name')} ({agent.get('agent_version')}) - no model")]),
        el("p", el("strong", NOTE_EN), " ", el("span", NOTE_DE, lang="de"), class_="trend-note", id="trend-note"),
        table([c["label"] for c in columns], rows, id="trend-table", empty="No rows."),
        el("p", agent.get("limits") or "", class_="hint")), page="trend")


# --------------------------------------------------------------------------- costs and governance
def _money(value: Any) -> str:
    try:
        return f"{float(value):,.2f} USD"
    except (TypeError, ValueError):
        return "unknown"


def costs_page(ctx: PageContext, data: dict[str, Any]) -> Markup:
    platform = data.get("platform") or {}
    prices = data.get("price_table") or {}
    budget = data.get("budget") or {}
    if platform.get("available"):
        mtd = platform.get("month_to_date") or {}
        forecast = platform.get("forecast_next_month") or {}
        platform_block = join(
            el("p", el("strong", platform.get("label") or ""), f" - source: {platform.get('source')}; data as of "
                                                                f"{platform.get('data_as_of')}", class_="hint"),
            table(["Service", "Month to date"], [[k, _money(v)] for k, v in (mtd.get("by_service") or {}).items()]
                  + [[el("strong", "Total"), el("strong", _money(mtd.get("total_usd")))]], id="platform-table"),
            el("p", f"Forecast next month ({forecast.get('from')} to {forecast.get('to')}): "
                    f"{_money(forecast.get('total_usd'))} - {forecast.get('label') or forecast.get('message') or ''}"
                    f" ({forecast.get('source') or ''})", id="forecast"))
    else:
        platform_block = el("p", platform.get("message") or "Platform costs are not available.", class_="problem",
                            id="platform-unavailable")
    models = data.get("models_month_to_date") or {}
    agent_rows = [[name, v["runs"], v["input_tokens"], v["cached_tokens"], v["output_tokens"], _money(v["usd"])]
                  for name, v in (models.get("per_agent") or {}).items()]
    model_rows = [[name, v["runs"], v["input_tokens"], v["cached_tokens"], v["output_tokens"], _money(v["usd"])]
                  for name, v in (models.get("per_model") or {}).items()]
    case_rows = [[case_link(c["case_id"], c.get("case_key")), _money((c.get("variable") or {}).get("usd")),
                  "simulated" if (c.get("variable") or {}).get("simulated") else "measured",
                  _money((c.get("fixed_share") or {}).get("usd")) if (c.get("fixed_share") or {}).get("available")
                  else (c.get("fixed_share") or {}).get("note")] for c in data.get("cases") or []]
    price_rows = [[name, p.get("input"), p.get("cached_input"), p.get("output"), p.get("model_version")]
                  for name, p in (prices.get("models") or {}).items()]
    return layout(
        ctx, "Costs", el("h1", "Costs"),
        el("p", "Cost values are kept apart from safety and trend views and make no statement about products.",
           class_="hint"),
        section("platform-costs", "Platform costs (month to date) and forecast", platform_block),
        section("model-costs", f"Model costs month to date ({models.get('month')})",
                el("p", models.get("label") or "", class_="hint"),
                el("p", "Total: ", el("strong", _money(models.get("total_usd"))), id="model-total"),
                el("h3", "Per agent"), table(["Agent", "Runs", "Input tokens", "Cached", "Output tokens", "Cost"],
                                             agent_rows, id="per-agent-table", empty="No agent run this month."),
                el("h3", "Per model"), table(["Model", "Runs", "Input tokens", "Cached", "Output tokens", "Cost"],
                                             model_rows, id="per-model-table", empty="No agent run this month.")),
        section("budget", "Budget",
                dl([("Budget", _money(budget.get("budget_usd"))), ("Spent (platform + models)",
                                                                   _money(budget.get("spent_usd"))),
                    ("Used", f"{budget.get('percent_used')} %"), ("Thresholds reached",
                                                                  ", ".join(f"{t} %" for t in budget.get(
                                                                      "thresholds_reached") or []) or "none"),
                    ("Forecast", f"{budget.get('forecast_percent')} % of the budget"), ("Note", budget.get("note"))])),
        section("case-costs", "Costs per case",
                table(["Case", "Model costs", "Basis", "Allocated fixed-cost share"], case_rows, id="case-costs-table",
                      empty="No case yet.")),
        section("price-table", f"Price table {prices.get('version')} ({prices.get('date')})",
                el("p", f"{prices.get('unit')} - source: {prices.get('source')}", class_="hint"),
                table(["Model", "Input", "Cached input", "Output", "Model version"], price_rows, id="prices-table"),
                el("ul", [el("li", n) for n in prices.get("notes") or []])),
        el("ul", [el("li", n) for n in data.get("notes") or []], class_="hint"),
        page="costs")


def governance_page(ctx: PageContext, data: dict[str, Any], findings: list[dict[str, Any]]) -> Markup:
    versions = data.get("versions") or {}
    agents = versions.get("agents") or []
    agent_rows = [[a.get("code"), a.get("name"), a.get("agent_version"), a.get("prompt_version"),
                   a.get("model_deployment"), a.get("model_version"), a.get("autonomy"), a.get("limits")]
                  for a in agents]
    models = data.get("model_deployments") or {}
    latency = data.get("alert_latency") or {}
    worker = data.get("worker") or {}
    audit = data.get("audit") or {}
    open_decisions = data.get("open_human_decisions") or {}
    deadlines = data.get("deadlines") or {}
    evaluation = data.get("evaluation") or {}
    finding_rows, finding_attrs = [], []
    for f in findings:
        options = {"approved_assessment": ["withdrawn"], "process_finding": ["Closed"]}.get(f.get("kind"), [])
        form = (el("form", el("select", [el("option", o, value=o) for o in options], name="status"),
                   el("input", name="reason", required=True, placeholder="reason (required)"),
                   el("button", "Update", type="submit"), method="post",
                   action=f"/api/findings/{url_part(f['finding_id'])}", class_="inline-form")
                if options and f.get("status") not in ("withdrawn", "Closed") else "")
        finding_rows.append([f.get("finding_id"), f.get("kind"), f.get("ref") or "", f.get("case_id") or "",
                             f.get("status"), f.get("summary"), f.get("reason") or "", form])
        finding_attrs.append({"class_": "finding-row", "data_finding_id": f.get("finding_id")})
    rules = data.get("rule_sets") or []
    return layout(
        ctx, "Governance", el("h1", "Governance and operations"), flash(ctx),
        section("versions", "Versions",
                dl([("App", versions.get("app")), ("Rules", ", ".join((versions.get("rules") or {}).values())),
                    ("Corpus", versions.get("corpus")), ("Price table", versions.get("price_table")),
                    ("Agent backend", versions.get("agent_backend"))]),
                table(["Agent", "Name", "Version", "Prompt", "Model deployment", "Model version", "Autonomy",
                       "Limits"], agent_rows, id="agents-table"),
                table(["Rule", "Purpose"], [[r["rule"], r["purpose"]] for r in rules], id="rules-table")),
        section("models", "Model deployments",
                dl([("Analysis (A3, A6)", f"{(models.get('analysis') or {}).get('deployment')} "
                                          f"({(models.get('analysis') or {}).get('model_version')})"),
                    ("General (A1, A2, A4)", f"{(models.get('general') or {}).get('deployment')} "
                                             f"({(models.get('general') or {}).get('model_version')})"),
                    ("Deployment type", models.get("deployment_type")), ("Fallback", models.get("fallback")),
                    ("Runtime", json_text(models.get("runtime")))])),
        section("evaluation", "Evaluation status", dl([(k, json_text(v)) for k, v in evaluation.items()])),
        section("open-decisions", "Open human decisions",
                table(["Status", "Cases"], [[status_label(k), v] for k, v in open_decisions.items()],
                      id="open-decisions-table"),
                el("p", f"Deadlines: {deadlines.get('overdue')} overdue, {deadlines.get('due_within_48h')} due within "
                        "48 hours.", id="deadline-counts")),
        section("audit-status", "Audit trail",
                dl([("Chain check", el("strong", "OK" if audit.get("ok") else "BROKEN", id="audit-ok")),
                    ("Records", audit.get("records")), ("Missing mandatory fields", audit.get("missing_fields")),
                    ("Head hash", el("code", audit.get("head_hash"))),
                    ("First break", audit.get("first_break_seq") or "none")]),
                el("p", el("a", "Open the audit trail", href="/audit"))),
        section("operations", "Operations",
                dl([("Alert latency", f"{latency.get('sent')} of {latency.get('alerts')} alerts sent; "
                                      f"{latency.get('within_target')} within {latency.get('target_seconds')} s; "
                                      f"max {latency.get('max_seconds')} s"),
                    ("Worker", f"mode {worker.get('mode')}; heartbeat {worker.get('heartbeat')}; pending jobs "
                               f"{worker.get('pending_jobs')}; last audit check {worker.get('last_audit_verify')}"),
                    ("Preview features", f"{'on' if (data.get('preview_features') or {}).get('enabled') else 'off'} - "
                                         f"{(data.get('preview_features') or {}).get('note')}"),
                    ("Tracing", json_text(data.get("tracing"))),
                    ("Policy and budget", json_text(data.get("policy_and_budget"))),
                    ("Content safety", json_text(data.get("content_safety"))),
                    ("Links", json_text(data.get("links")))])),
        section("findings", "Findings",
                el("p", "Withdrawing an approved finding or closing a process finding needs the medical reviewer and "
                        "a reason; the server decides.", class_="hint"),
                table(["Finding", "Kind", "Ref", "Case", "Status", "Summary", "Reason", "Action"], finding_rows,
                      row_attrs=finding_attrs, id="findings-table", empty="No finding.")),
        page="governance")


def audit_page(ctx: PageContext, records: list[dict[str, Any]], verify: dict[str, Any], *, total: int,
               case_id: str | None, event_type: str | None, limit: int) -> Markup:
    options = [el("option", "all events", value="")] + [
        el("option", e, value=e, selected=(e == event_type)) for e in sorted(EVENT_TYPES)]
    filters = el("form", label("Case", el("input", name="case_id", value=case_id or "")),
                 label("Event type", el("select", options, name="event_type")),
                 label("Show the latest", el("input", name="limit", value=str(limit), type="number", min="1",
                                             max="5000")),
                 el("button", "Filter", type="submit"), method="get", action="/audit", id="form-audit-filter",
                 class_="filter-form")
    ok = verify.get("ok")
    return layout(
        ctx, "Audit trail", el("h1", "Audit trail"),
        section("chain", "Hash chain check",
                el("p", el("strong", "Chain OK" if ok else "Chain BROKEN", class_="ok" if ok else "broken",
                           id="chain-status"),
                   f" - {verify.get('checked')} records checked; head {verify.get('head_hash')}"
                   + ("" if ok else f"; first break at record {verify.get('first_break_seq')}: {verify.get('reason')}")),
                el("p", "Records are only appended; there is no API to change or delete them. Editing a stored record "
                        "breaks the chain.", class_="hint")),
        filters,
        section("records", f"Records (latest {len(records)} of {total})", audit_table(records, "audit-table")),
        page="audit")
