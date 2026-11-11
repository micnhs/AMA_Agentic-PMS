"""Web application: pages and JSON API of BUILD_SPEC 4.3 on top of :class:`pms.pipeline.Pipeline`.

* :class:`WebApp` is a WSGI application. :func:`serve` runs it with the standard library
  (``http.server.ThreadingHTTPServer`` + ``wsgiref``) for ``python -m pms serve``; ``application`` is the
  same app for a WSGI server (``gunicorn pms.web.app:application``), built from the environment on the
  first request.
* Authentication: :mod:`pms.auth` (local test users offline, Microsoft Entra principal in Azure).
  Authorisation: ONLY the domain (:meth:`pms.workflow.Workflow.authorize` and the pipeline/workflow
  actions, which authorise and audit themselves). Refusals answer 401 (not signed in) or 403 (role) with
  the reason; every refused attempt is recorded in the audit trail (ACCESS_DENIED).
* State changes are POST only, with JSON or a form from our own pages (same-origin check, see
  :mod:`pms.web.router`). HTML forms are answered with a redirect back to the page (post/redirect/get).
* Nothing here sends anything to an authority: a recorded submission is a record of something a person
  did outside the system.

Importing this module checks the configuration: with ``APP_ENV=azure`` and ``LOCAL_TEST_USERS=true``
(or any other invalid setting) the import fails, so a WSGI server refuses to start (concept 7.3).
"""
from __future__ import annotations

import logging
import signal
import threading
from dataclasses import asdict, is_dataclass
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlunsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

from .. import auth
from ..config import Settings, load_settings
from ..governance import governance_view
from ..intake import UploadRejected
from ..models import STATUS_LABELS_EN, Status
from ..reports import build_case_report, export_report
from ..versions import APP_VERSION
from ..workflow import Action, ApprovalRequired, InvalidTransition, PermissionDenied, ReasonRequired
from . import views
from .markup import url_part
from .router import HttpError, Request, Response, Router, Upload, check_same_origin

log = logging.getLogger("pms.web")
access_log = logging.getLogger("pms.web.access")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES: dict[str, str] = {"app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "same-origin"),
    ("Content-Security-Policy", CSP),
)
AUDIT_PAGE_DEFAULT_LIMIT = 300
DEMO_COOKIE_ATTRIBUTES = "Path=/; HttpOnly; SameSite=Strict"


def _plain(value: Any) -> Any:
    return asdict(value) if is_dataclass(value) and not isinstance(value, type) else value


def _safe_next(value: Any) -> str:
    text = str(value or "").strip()
    if not text.startswith("/") or text.startswith("//") or "\\" in text or any(c in text for c in "\r\n\t"):
        return "/"
    return text


class WebApp:
    """WSGI application around one :class:`pms.pipeline.Pipeline`."""

    def __init__(self, pipeline: Any) -> None:
        pipeline.settings.validate_startup()  # refuses APP_ENV=azure with LOCAL_TEST_USERS=true
        self.pipeline = pipeline
        self.settings: Settings = pipeline.settings
        self.workflow = pipeline.workflow
        self.router = Router()
        self._static_cache: dict[str, bytes] = {}
        self._register()

    # ------------------------------------------------------------------ routes
    def _register(self) -> None:
        r = self.router
        r.add("GET", "/healthz", self.healthz, public=True)
        r.add("GET", "/static/{name}", self.static, public=True)
        r.add("GET", "/favicon.ico", self.favicon, public=True)
        r.add("GET", "/login", self.login_page, public=True)
        r.add("POST", "/login", self.login, public=True)
        r.add("POST", "/logout", self.logout, public=True)
        r.add("GET", "/", self.page_dashboard)
        r.add("GET", "/cases/{case_id}", self.page_case)
        r.add("GET", "/rejected", self.page_rejected)
        r.add("GET", "/unreadable", self.page_unreadable)
        r.add("GET", "/trend", self.page_trend)
        r.add("GET", "/costs", self.page_costs)
        r.add("GET", "/governance", self.page_governance)
        r.add("GET", "/audit", self.page_audit)
        r.add("GET", "/api/me", self.api_me)
        r.add("POST", "/api/letters", self.api_upload)
        r.add("GET", "/api/cases", self.api_cases)
        r.add("GET", "/api/cases/{case_id}", self.api_case)
        r.add("GET", "/api/cases/{case_id}/progress", self.api_progress)
        r.add("GET", "/api/cases/{case_id}/report", self.api_report)
        r.add("POST", "/api/cases/{case_id}/signals", self.api_add_signal)
        r.add("POST", "/api/cases/{case_id}/metadata", self.api_metadata)
        r.add("POST", "/api/cases/{case_id}/triage", self.api_triage)
        r.add("POST", "/api/cases/{case_id}/review", self.api_review)
        r.add("POST", "/api/cases/{case_id}/urgency", self.api_urgency)
        r.add("POST", "/api/cases/{case_id}/decision", self.api_decision)
        r.add("POST", "/api/cases/{case_id}/anticipated", self.api_anticipated)
        r.add("POST", "/api/cases/{case_id}/reanalysis", self.api_reanalysis)
        r.add("POST", "/api/cases/{case_id}/submission", self.api_submission)
        r.add("POST", "/api/findings/{finding_id}", self.api_finding)
        r.add("GET", "/api/rejected", self.api_rejected)
        r.add("GET", "/api/unreadable", self.api_unreadable)
        r.add("GET", "/api/trend", self.api_trend)
        r.add("GET", "/api/governance", self.api_governance)
        r.add("GET", "/api/audit", self.api_audit)
        r.add("GET", "/api/audit/verify", self.api_audit_verify)
        r.add("GET", "/api/costs", self.api_costs)
        r.add("GET", "/api/outbox", self.api_outbox)
        r.add("POST", "/api/seed", self.api_seed)

    # ------------------------------------------------------------------ WSGI
    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        request = Request(environ)
        try:
            response = self.dispatch(request)
        except HttpError as exc:
            response = self.error_response(request, exc)
        except Exception:  # never leak a traceback to the client; the log has it
            log.exception("unhandled error in %s %s", request.method, request.path)
            response = self.error_response(request, HttpError(500, "internal error - see the server log",
                                                              error="internal_error"))
        headers = [("Content-Type", response.content_type), ("Content-Length", str(len(response.body)))]
        headers += response.headers
        headers += list(SECURITY_HEADERS)
        if not any(name.lower() == "cache-control" for name, _ in headers):
            headers.append(("Cache-Control", "no-store"))
        start_response(response.status_line, headers)
        return [b""] if request.method == "HEAD" else [response.body]

    def dispatch(self, request: Request) -> Response:
        route, params = self.router.resolve(request.method, request.path)
        request.params = params
        request.identity = auth.authenticate(request.header, request.cookies, self.settings)
        if request.method == "POST":
            check_same_origin(request)
        if not route.public and request.method in ("GET", "HEAD") and not request.identity.authenticated:
            raise HttpError(401, request.identity.problem or "not signed in", error="unauthenticated")
        return route.handler(request)

    # ------------------------------------------------------------------ helpers
    def _source(self, request: Request) -> str:
        """Audit source of a human action: ``ui`` for our forms, ``api`` for JSON clients."""
        return "ui" if request.is_form else "api"

    def _ctx(self, request: Request, active: str = "") -> views.PageContext:
        identity = request.identity
        actor = identity.actor if identity is not None else None
        return views.PageContext(
            identity=identity, settings=self.settings, now=self.pipeline.now(), active=active,
            done=request.arg("done"),
            can=(lambda action: self.workflow.can(actor, action)) if actor is not None else (lambda action: False))

    def _denied(self, request: Request, exc: PermissionDenied) -> HttpError:
        identity = request.identity
        if identity is None or not identity.authenticated:
            problem = identity.problem if identity is not None else "not signed in"
            return HttpError(401, f"Refused: {exc} - {problem}. The attempt is recorded in the audit trail.",
                             error="unauthenticated", back=self._back(request))
        roles = ", ".join(identity.role_labels) or "no PMS role"
        return HttpError(403, f"Refused: {exc} (your roles: {roles}). Only the roles of the fixed role matrix may "
                              f"do this; the attempt is recorded in the audit trail (ACCESS_DENIED).",
                         error="permission_denied", back=self._back(request))

    def _back(self, request: Request) -> str:
        case_id = request.params.get("case_id")
        if case_id:
            return f"/cases/{url_part(case_id)}"
        return "/"

    def _call(self, request: Request, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a domain call and translate refusals into HTTP errors with the domain's reason."""
        back = self._back(request)
        try:
            return fn(*args, **kwargs)
        except PermissionDenied as exc:
            raise self._denied(request, exc) from exc
        except ReasonRequired as exc:
            raise HttpError(400, f"Refused: {exc}.", error="reason_required", back=back) from exc
        except ApprovalRequired as exc:
            raise HttpError(409, f"Refused: {exc}.", error="approval_required", back=back) from exc
        except InvalidTransition as exc:
            raise HttpError(409, f"Refused: {exc}.", error="invalid_transition", back=back) from exc
        except UploadRejected as exc:
            status = 413 if "larger than" in str(exc) else 400
            raise HttpError(status, f"Upload refused: {exc}.", error="upload_rejected", back=back) from exc
        except KeyError as exc:
            message = exc.args[0] if exc.args else "not found"
            raise HttpError(404, f"Not found: {message}.", error="not_found", back=back) from exc
        except ValueError as exc:
            raise HttpError(400, f"Invalid value: {exc}.", error="invalid_value", back=back) from exc

    def _require(self, request: Request, action: Action = Action.VIEW, *, case_id: str | None = None) -> None:
        self._call(request, self.workflow.authorize, request.actor, action, case_id=case_id,
                   source="api" if request.is_api else "ui")

    def _case(self, request: Request, case_id: str) -> Any:
        return self._call(request, self.pipeline.store.get_case, case_id)

    def _case_json(self, case: Any) -> dict[str, Any]:
        data = _plain(case)
        try:
            data["status_label"] = STATUS_LABELS_EN[Status(data["status"])]
        except (KeyError, ValueError):
            data["status_label"] = data.get("status")
        return data

    def _done(self, request: Request, case_id: str, key: str, payload: dict[str, Any], *, status: int = 200,
              anchor: str = "") -> Response:
        if request.is_form:
            return Response.redirect(f"/cases/{url_part(case_id)}?done={key}{anchor}")
        return Response.json(payload, status)

    @staticmethod
    def _text(fields: dict[str, Any], name: str) -> str:
        value = fields.get(name)
        if value is None:
            return ""
        if not isinstance(value, (str, int, float)):
            raise HttpError(400, f"field '{name}' must be text", error="invalid_value")
        return str(value)

    def _letter(self, request: Request) -> dict[str, Any]:
        """Keyword arguments of :meth:`Pipeline.ingest_letter` from a JSON body or an upload form."""
        fields, files = request.data()
        received_at = self._text(fields, "received_at").strip() or None
        filename = self._text(fields, "filename").strip() or None
        upload: Upload | None = files.get("file")
        if upload is not None and (upload.content or upload.filename):
            return {"filename": upload.filename or filename, "content": upload.content, "received_at": received_at}
        if request.is_json and "text" in fields:
            text = fields["text"]
            if not isinstance(text, str):
                raise HttpError(400, "'text' must be a string", error="invalid_value")
            return {"filename": filename, "text": text, "received_at": received_at}
        if request.is_json and fields.get("content_base64") is not None:
            return {"filename": filename, "content_base64": self._text(fields, "content_base64"),
                    "received_at": received_at}
        text = self._text(fields, "text")
        if text.strip():
            return {"filename": filename or "pasted-letter.txt", "text": text, "received_at": received_at}
        raise HttpError(400, "A letter needs a file, 'text' or 'content_base64'.", error="invalid_upload",
                        back=self._back(request))

    # ------------------------------------------------------------------ errors
    def error_response(self, request: Request, exc: HttpError) -> Response:
        identity = request.identity
        wants_html = (not request.is_api) or (request.method == "POST" and request.is_form)
        if not wants_html:
            response = Response.json({"error": exc.error, "reason": exc.reason, "status": exc.status}, exc.status)
        else:
            ctx = views.PageContext(identity=identity, settings=self.settings, now=self.pipeline.now())
            if exc.status == 401 and request.method in ("GET", "HEAD"):
                next_url = request.path + (f"?{request.environ.get('QUERY_STRING')}"
                                           if request.environ.get("QUERY_STRING") else "")
                markup = views.login_page(ctx, next_url=_safe_next(next_url), problem=exc.reason)
            else:
                try:
                    title = HTTPStatus(exc.status).phrase
                except ValueError:
                    title = "Error"
                markup = views.error_page(ctx, exc.status, title, exc.reason, back=exc.back or self._back(request))
            response = Response.html(markup, exc.status)
        if exc.status == 401:
            challenge = 'Bearer realm="pms-agent-demo"' if self.settings.is_azure else \
                'X-Demo-User realm="pms-agent-demo local test users"'
            response.headers.append(("WWW-Authenticate", challenge))
        response.headers.extend(exc.headers)
        return response

    # ------------------------------------------------------------------ public endpoints
    def healthz(self, request: Request) -> Response:
        try:
            body = self.pipeline.healthz()
        except Exception as exc:  # the probe answers even when something is broken
            log.exception("health check failed")
            return Response.json({"status": "error", "reason": type(exc).__name__}, 503)
        return Response.json(body)

    def static(self, request: Request) -> Response:
        name = request.params["name"]
        if name not in STATIC_FILES:
            raise HttpError(404, "no such static file")
        if name not in self._static_cache:
            self._static_cache[name] = (STATIC_DIR / name).read_bytes()
        return Response(200, self._static_cache[name], STATIC_FILES[name], [("Cache-Control", "no-cache")])

    def favicon(self, request: Request) -> Response:
        return Response(204, b"", "text/plain; charset=utf-8", [("Cache-Control", "max-age=86400")])

    def login_page(self, request: Request) -> Response:
        ctx = self._ctx(request)
        return Response.html(views.login_page(ctx, next_url=_safe_next(request.arg("next") or "/")))

    def login(self, request: Request) -> Response:
        if not auth.local_test_users_enabled(self.settings):
            raise HttpError(404, "There is no local sign-in in this mode"
                                 + (" - sign in with Microsoft Entra ID." if self.settings.is_azure else "."))
        user = auth.local_test_user(self._text(request.data()[0], "user"))
        if user is None:
            raise HttpError(400, f"Unknown demo user. Known: {', '.join(auth.LOCAL_TEST_USERS)}.", back="/login")
        response = Response.redirect(_safe_next(request.field("next")))
        response.headers.append(("Set-Cookie", f"{auth.DEMO_USER_COOKIE}={user.name}; {DEMO_COOKIE_ATTRIBUTES}"))
        return response

    def logout(self, request: Request) -> Response:
        if self.settings.is_azure:
            return Response.redirect("/.auth/logout?post_logout_redirect_uri=/")
        response = Response.redirect("/login")
        response.headers.append(("Set-Cookie", f"{auth.DEMO_USER_COOKIE}=; Max-Age=0; {DEMO_COOKIE_ATTRIBUTES}"))
        return response

    # ------------------------------------------------------------------ pages
    def page_dashboard(self, request: Request) -> Response:
        self._require(request)
        data = self.pipeline.dashboard()
        return Response.html(views.dashboard_page(self._ctx(request, "dashboard"), data, self.pipeline.trend_view()))

    def page_case(self, request: Request) -> Response:
        case_id = request.params["case_id"]
        self._require(request, case_id=case_id)
        self._case(request, case_id)
        view = self.pipeline.case_view(case_id)
        report = build_case_report(self.pipeline, case_id)
        findings = [_plain(f) for f in self.pipeline.store.list_findings(case_id=case_id)]
        return Response.html(views.case_page(self._ctx(request, "dashboard"), view, report, findings))

    def page_rejected(self, request: Request) -> Response:
        self._require(request)
        return Response.html(views.rejected_page(self._ctx(request, "rejected"), self.pipeline.rejected_view()))

    def page_unreadable(self, request: Request) -> Response:
        self._require(request)
        return Response.html(views.unreadable_page(self._ctx(request, "unreadable"),
                                                   self.pipeline.unreadable_unmatched_view()))

    def page_trend(self, request: Request) -> Response:
        self._require(request)
        return Response.html(views.trend_page(self._ctx(request, "trend"), self.pipeline.trend_view()))

    def page_costs(self, request: Request) -> Response:
        self._require(request, Action.VIEW_COSTS_GOVERNANCE)
        return Response.html(views.costs_page(self._ctx(request, "costs"), self.pipeline.cost_overview()))

    def page_governance(self, request: Request) -> Response:
        self._require(request, Action.VIEW_COSTS_GOVERNANCE)
        findings = [_plain(f) for f in self.pipeline.store.list_findings()]
        return Response.html(views.governance_page(self._ctx(request, "governance"), governance_view(self.pipeline),
                                                   findings))

    def _audit_query(self, request: Request) -> tuple[str | None, str | None, int | None]:
        case_id = (request.arg("case_id") or "").strip() or None
        event_type = (request.arg("event_type") or "").strip() or None
        raw_limit = (request.arg("limit") or "").strip()
        try:
            limit = int(raw_limit) if raw_limit else None
        except ValueError as exc:
            raise HttpError(400, "limit must be a number") from exc
        if limit is not None and limit < 1:
            raise HttpError(400, "limit must be at least 1")
        return case_id, event_type, limit

    def page_audit(self, request: Request) -> Response:
        self._require(request)
        case_id, event_type, limit = self._audit_query(request)
        limit = limit or AUDIT_PAGE_DEFAULT_LIMIT
        records = self.pipeline.audit_records(case_id=case_id, event_type=event_type)
        latest = list(reversed(records[-limit:]))
        return Response.html(views.audit_page(self._ctx(request, "audit"), latest, self.pipeline.verify_audit(),
                                              total=len(records), case_id=case_id, event_type=event_type,
                                              limit=limit))

    # ------------------------------------------------------------------ read API
    def api_me(self, request: Request) -> Response:
        identity = request.identity
        actor = identity.actor
        return Response.json({
            "actor_id": actor.actor_id, "display_name": actor.display_name, "roles": sorted(actor.roles),
            "role_labels": identity.role_labels, "method": identity.method, "authenticated": actor.authenticated,
            "unmapped_roles": list(identity.unmapped_roles), "app_env": self.settings.app_env,
            "permissions": {a.value: self.workflow.can(actor, a) for a in Action},
        })

    def api_cases(self, request: Request) -> Response:
        self._require(request)
        return Response.json(self.pipeline.dashboard())

    def api_case(self, request: Request) -> Response:
        case_id = request.params["case_id"]
        self._require(request, case_id=case_id)
        self._case(request, case_id)
        return Response.json(self.pipeline.case_view(case_id))

    def api_progress(self, request: Request) -> Response:
        case_id = request.params["case_id"]
        self._require(request, case_id=case_id)
        case = self._case(request, case_id)
        data = self._case_json(case)
        return Response.json({"case_id": case.case_id, "status": case.status, "status_label": data["status_label"],
                              "critical": case.critical, "analysis_state": case.analysis_state,
                              "content_version": case.content_version, "updated_at": case.updated_at,
                              "progress": self.pipeline.case_progress(case_id)})

    def api_report(self, request: Request) -> Response:
        case_id = request.params["case_id"]
        self._require(request, case_id=case_id)
        self._case(request, case_id)
        fmt = (request.arg("format") or "json").strip().lower()
        content, media_type = self._call(request, export_report, self.pipeline, case_id, fmt)
        ext = "md" if fmt == "md" else "json"
        safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in case_id)
        response = Response(200, content.encode("utf-8"), media_type if "charset" in media_type
                            else f"{media_type}; charset=utf-8")
        response.headers.append(("Content-Disposition", f'inline; filename="case-report-{safe_name}.{ext}"'))
        return response

    def api_rejected(self, request: Request) -> Response:
        self._require(request)
        return Response.json({"rejected": self.pipeline.rejected_view()})

    def api_unreadable(self, request: Request) -> Response:
        self._require(request)
        return Response.json({"inputs": self.pipeline.unreadable_unmatched_view()})

    def api_trend(self, request: Request) -> Response:
        self._require(request)
        return Response.json(self.pipeline.trend_view())

    def api_governance(self, request: Request) -> Response:
        self._require(request, Action.VIEW_COSTS_GOVERNANCE)
        return Response.json(governance_view(self.pipeline))

    def api_audit(self, request: Request) -> Response:
        self._require(request)
        case_id, event_type, limit = self._audit_query(request)
        records = self.pipeline.audit_records(case_id=case_id, event_type=event_type, limit=limit)
        return Response.json({"records": records, "count": len(records),
                              "note": "append-only; there is no API to change or delete audit records"})

    def api_audit_verify(self, request: Request) -> Response:
        self._require(request)
        return Response.json(self.pipeline.verify_audit())

    def api_costs(self, request: Request) -> Response:
        self._require(request, Action.VIEW_COSTS_GOVERNANCE)
        return Response.json(self.pipeline.cost_overview())

    def api_outbox(self, request: Request) -> Response:
        self._require(request)
        if self.settings.is_azure:
            raise HttpError(404, "The local outbox exists only in the offline mode; in Azure alerts are e-mails to "
                                 "the configured demo recipients.")
        return Response.json({"entries": self.pipeline.outbox_entries(),
                              "note": "local outbox - nothing is sent, not to an authority and not by e-mail"})

    # ------------------------------------------------------------------ actions
    def api_upload(self, request: Request) -> Response:
        source = "upload" if request.is_form else "api"
        # Authorise before the upload is decoded, so that a refusal never depends on the content.
        self._call(request, self.workflow.authorize, request.actor, Action.UPLOAD, source=source)
        letter = self._letter(request)
        result = self._call(request, self.pipeline.ingest_letter, actor=request.actor, source=source, **letter)
        payload = {**result.to_dict(), "case_url": f"/cases/{url_part(result.case_id)}"}
        return self._done(request, result.case_id, "upload", payload, status=201)

    def api_add_signal(self, request: Request) -> Response:
        case_id = request.params["case_id"]
        source = "upload" if request.is_form else "api"
        self._call(request, self.workflow.authorize, request.actor, Action.UPLOAD, case_id=case_id, source=source)
        self._case(request, case_id)
        letter = self._letter(request)
        result = self._call(request, self.pipeline.add_signal, case_id, request.actor, source=source, **letter)
        payload = {**result.to_dict(), "case_url": f"/cases/{url_part(result.case_id)}"}
        return self._done(request, case_id, "signal", payload, status=201, anchor="#signals")

    def api_metadata(self, request: Request) -> Response:
        case_id = request.params["case_id"]
        fields = request.data()[0]
        field_name = self._text(fields, "field").strip()
        new_value = fields.get("new_value")
        if request.is_form:
            new_value = (new_value or "").strip() or None  # empty means unknown
        edit = self._call(request, self.pipeline.edit_metadata, case_id, request.actor, field_name, new_value,
                          self._text(fields, "reason"), source=self._source(request))
        case = self.pipeline.store.get_case(case_id)
        return self._done(request, case_id, "metadata", {"edit": _plain(edit), "case": self._case_json(case)},
                          anchor="#metadata-edits")

    def api_triage(self, request: Request) -> Response:
        case_id = request.params["case_id"]
        reason = self._text(request.data()[0], "reason")
        case = self._call(request, self.pipeline.take_triage, case_id, request.actor, reason or None,
                          source=self._source(request))
        return self._done(request, case_id, "triage", {"case": self._case_json(case)})

    def api_review(self, request: Request) -> Response:
        case_id = request.params["case_id"]
        reason = self._text(request.data()[0], "reason")
        case = self._call(request, self.pipeline.start_review, case_id, request.actor, reason or None,
                          source=self._source(request))
        return self._done(request, case_id, "review", {"case": self._case_json(case)})

    def api_urgency(self, request: Request) -> Response:
        case_id = request.params["case_id"]
        fields = request.data()[0]
        direction = (self._text(fields, "direction") or self._text(fields, "action")).strip().lower()
        reason = self._text(fields, "reason")
        if direction == "raise":
            case = self._call(request, self.pipeline.raise_urgency, case_id, request.actor, reason or None,
                              source=self._source(request))
        elif direction == "lower":
            case = self._call(request, self.pipeline.lower_urgency, case_id, request.actor, reason,
                              source=self._source(request))
        else:
            raise HttpError(400, "direction must be 'raise' or 'lower'", error="invalid_value",
                            back=self._back(request))
        return self._done(request, case_id, f"urgency-{direction}", {"case": self._case_json(case)})

    def api_decision(self, request: Request) -> Response:
        case_id = request.params["case_id"]
        fields = request.data()[0]
        decision = self._text(fields, "decision").strip()
        if decision not in views.DECISION_ACTIONS:
            raise HttpError(400, f"decision must be one of {', '.join(views.DECISION_ACTIONS)}",
                            error="invalid_value", back=self._back(request))
        reason_code = self._text(fields, "reason_code").strip().upper() or None
        result = self._call(request, self.pipeline.decide, case_id, request.actor, decision,
                            self._text(fields, "reason"), reason_code=reason_code, source=self._source(request))
        case = self.pipeline.store.get_case(case_id)
        return self._done(request, case_id, "decision", {"decision": decision, "result": _plain(result),
                                                          "case": self._case_json(case)}, anchor="#decisions")

    def api_anticipated(self, request: Request) -> Response:
        case_id = request.params["case_id"]
        added = self._call(request, self.pipeline.classify_anticipated, case_id, request.actor,
                           self._text(request.data()[0], "reason"), source=self._source(request))
        return self._done(request, case_id, "anticipated", {"added_deadlines": added}, anchor="#deadlines")

    def api_reanalysis(self, request: Request) -> Response:
        case_id = request.params["case_id"]
        source = self._source(request)
        self._call(request, self.workflow.authorize, request.actor, Action.RERUN_ANALYSIS, case_id=case_id,
                   source=source)
        self._case(request, case_id)
        reason = self._text(request.data()[0], "reason")
        job = self._call(request, self.pipeline.request_reanalysis, case_id, request.actor, reason or None,
                         source=source)
        return self._done(request, case_id, "reanalysis", {"job": _plain(job)}, status=202, anchor="#progress")

    def api_submission(self, request: Request) -> Response:
        case_id = request.params["case_id"]
        fields = request.data()[0]
        submission = self._call(
            request, self.pipeline.record_submission, case_id, request.actor,
            channel_label=self._text(fields, "channel_label"), reference=self._text(fields, "reference"),
            submitted_on=self._text(fields, "submitted_on").strip(),
            receipt_status=self._text(fields, "receipt_status").strip() or "unconfirmed",
            reason=self._text(fields, "reason") or None, source=self._source(request))
        case = self.pipeline.store.get_case(case_id)
        return self._done(request, case_id, "submission", {
            "submission": _plain(submission), "case": self._case_json(case),
            "note": "recorded only - this system never sends anything to an authority"}, status=201,
            anchor="#decisions")

    def api_finding(self, request: Request) -> Response:
        finding_id = request.params["finding_id"]
        fields = request.data()[0]
        finding = self._call(request, self.workflow.update_finding_status, finding_id, request.actor,
                             self._text(fields, "status").strip(), self._text(fields, "reason"),
                             source=self._source(request))
        if request.is_form:
            return Response.redirect("/governance?done=finding#findings")
        return Response.json({"finding": _plain(finding)})

    def api_seed(self, request: Request) -> Response:
        result = self._call(request, self.pipeline.seed_demo, request.actor)
        if request.is_form:
            return Response.redirect("/?done=seed")
        return Response.json(result)


# --------------------------------------------------------------------------- standard-library server
class _RequestHandler(WSGIRequestHandler):
    """wsgiref handler with logging, and without header names containing '_' (they would collide with the
    canonical headers in the WSGI environ, e.g. a forged X_MS_CLIENT_PRINCIPAL)."""

    server_version = f"pms-agent-demo/{APP_VERSION}"

    def get_environ(self) -> dict[str, Any]:
        for name in list(self.headers.keys()):
            if "_" in name:
                del self.headers[name]
        return super().get_environ()

    def address_string(self) -> str:
        return str(self.client_address[0])

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature of the base class
        access_log.info("%s %s", self.address_string(), format % args)


class PMSHTTPServer(ThreadingHTTPServer, WSGIServer):
    """``http.server.ThreadingHTTPServer`` serving a WSGI application (one thread per request)."""

    daemon_threads = True


def make_server(host: str, port: int, app: Callable[..., Iterable[bytes]]) -> PMSHTTPServer:
    server = PMSHTTPServer((host, port), _RequestHandler)
    server.set_app(app)
    return server


def create_app(settings: Settings | None = None, *, start_worker: bool | None = None) -> WebApp:
    """WebApp with a pipeline from ``settings`` (default: environment); refuses unsafe settings."""
    settings = settings or load_settings()
    settings.validate_startup()
    from ..pipeline import build_pipeline

    pipeline = build_pipeline(settings)
    app = WebApp(pipeline)
    if start_worker if start_worker is not None else settings.worker_mode == "inprocess":
        pipeline.start_worker()
    return app


def _seed(pipeline: Any) -> None:
    result = pipeline.seed_demo()
    loaded = result["loaded"]
    alerts = sum(len(item.get("alerts") or []) for item in loaded)
    critical = sum(1 for item in loaded if item.get("critical"))
    log.info("demo data: %d messages loaded, %d already present; %d critical signals, %d alerts in the outbox",
             len(loaded), len(result["skipped_already_loaded"]), critical, alerts)


def serve(host: str = "127.0.0.1", port: int = 8000, seed: bool = False, settings: Settings | None = None,
          auto_port: bool = False) -> None:
    """``python -m pms serve``: web app plus (locally) the in-process analysis worker, until Ctrl+C/SIGTERM.

    ``auto_port`` (offline only, when no port was given): if the default port is busy, the next free port
    (up to +99) is used and printed, so the README quick start works on machines where 8000 is taken.
    """
    settings = settings or load_settings()
    settings.validate_startup()
    from ..pipeline import build_pipeline

    pipeline = build_pipeline(settings)
    app = WebApp(pipeline)
    server: PMSHTTPServer | None = None
    try:
        server = _bind(host, port, app, auto_port and not settings.is_azure)  # bind first: fail before loading
        if seed:
            _seed(pipeline)
        if settings.worker_mode == "inprocess":
            pipeline.start_worker()
        else:
            log.info("WORKER_MODE=separate: analysis jobs and alert retries run in the worker container")
        bound = server.server_address[1]
        shown = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
        mode = "offline - local test users, synthetic data only" if not settings.is_azure else \
            "Azure - Microsoft Entra ID sign-in"
        url = urlunsplit(("http", f"{shown}:{bound}", "/", "", ""))
        print(f"PMS agent demo ({mode}) listening on {url} - stop with Ctrl+C", flush=True)
        _install_sigterm(server)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("stopping (Ctrl+C)")
    finally:
        if server is not None:
            server.server_close()
        pipeline.close()


def _bind(host: str, port: int, app: "WebApp", auto_port: bool) -> "PMSHTTPServer":
    """Bind the server; with ``auto_port`` fall back to the next free port when the requested one is busy."""
    import errno

    last_error: OSError | None = None
    for candidate in range(port, port + (100 if auto_port else 1)):
        try:
            server = make_server(host, candidate, app)
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES) or not auto_port:
                raise
            last_error = exc
            continue
        if candidate != port:
            print(f"Port {port} is in use - using port {candidate} instead.", flush=True)
        return server
    raise OSError(f"no free port between {port} and {port + 99}: {last_error}")


def _install_sigterm(server: PMSHTTPServer) -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def stop(*_: Any) -> None:
        threading.Thread(target=server.shutdown, name="pms-web-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, stop)


# --------------------------------------------------------------------------- WSGI entry point (gunicorn)
class _LazyApplication:
    """``gunicorn pms.web.app:application`` - the pipeline is built on the first request of each worker."""

    def __init__(self) -> None:
        self._app: WebApp | None = None
        self._lock = threading.Lock()

    def get(self) -> WebApp:
        if self._app is None:
            with self._lock:
                if self._app is None:
                    self._app = create_app()
        return self._app

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        return self.get()(environ, start_response)


def _refuse_unsafe_start() -> None:
    """Fail the import with unsafe settings (e.g. APP_ENV=azure with LOCAL_TEST_USERS=true): a WSGI server
    that loads ``pms.web.app:application`` then refuses to start."""
    load_settings()


_refuse_unsafe_start()
application = _LazyApplication()
