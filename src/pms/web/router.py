"""HTTP plumbing (standard library only): request parsing, responses, routing and the same-origin
check that protects every state-changing request against cross-site request forgery.

CSRF design: state changes are only possible with ``POST``; the body must be JSON (a cross-site HTML
form cannot send ``application/json``, and cross-origin ``fetch`` with JSON needs a CORS preflight that
this app never grants) or a form posted by one of our own pages (``Origin``/``Referer`` must name this
host; a form without either header is refused). Any other content type is refused (415), and
``Sec-Fetch-Site: cross-site`` is always refused. The local demo-user cookie is ``SameSite=Strict``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

MAX_BODY_BYTES = 8 * 1024 * 1024  # 5 MiB letters (intake limit) as base64 JSON fit
JSON_TYPE = "application/json"
FORM_TYPE = "application/x-www-form-urlencoded"
MULTIPART_TYPE = "multipart/form-data"
DEFAULT_PORTS = {"http": "80", "https": "443"}


class HttpError(Exception):
    """An HTTP error with a reason that is shown to the caller (JSON or HTML)."""

    def __init__(self, status: int, reason: str, *, error: str | None = None,
                 headers: list[tuple[str, str]] | None = None, back: str | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.error = error or HTTPStatus(status).phrase.lower().replace(" ", "_")
        self.headers = headers or []
        self.back = back


@dataclass(frozen=True)
class Upload:
    filename: str | None
    content: bytes
    content_type: str


class Request:
    """One WSGI request."""

    def __init__(self, environ: dict[str, Any]) -> None:
        self.environ = environ
        self.method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        raw_path = environ.get("PATH_INFO") or "/"
        try:  # WSGI: PATH_INFO is URL-decoded, then latin-1 ("bytes as str")
            raw_path = raw_path.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        self.path = raw_path if raw_path.startswith("/") else "/" + raw_path
        self.query = parse_qs(environ.get("QUERY_STRING") or "", keep_blank_values=True)
        self.params: dict[str, str] = {}
        self.identity: Any = None
        self._body: bytes | None = None
        self._data: tuple[dict[str, Any], dict[str, Upload]] | None = None
        self._cookies: dict[str, str] | None = None

    # ------------------------------------------------------------------ headers, query, cookies
    def header(self, name: str) -> str | None:
        key = name.upper().replace("-", "_")
        if key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            value = self.environ.get(key)
        else:
            value = self.environ.get("HTTP_" + key)
        return str(value) if value not in (None, "") else None

    def arg(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[-1] if values else default

    @property
    def cookies(self) -> dict[str, str]:
        if self._cookies is None:
            jar = SimpleCookie()
            try:
                jar.load(self.environ.get("HTTP_COOKIE") or "")
            except CookieError:
                jar = SimpleCookie()
            self._cookies = {k: m.value for k, m in jar.items()}
        return self._cookies

    @property
    def content_type(self) -> str:
        return (self.header("Content-Type") or "").split(";", 1)[0].strip().lower()

    @property
    def is_form(self) -> bool:
        return self.content_type in (FORM_TYPE, MULTIPART_TYPE)

    @property
    def is_json(self) -> bool:
        return self.content_type == JSON_TYPE

    @property
    def is_api(self) -> bool:
        return self.path.startswith("/api/") or self.path == "/healthz"

    @property
    def actor(self) -> Any:
        return self.identity.actor if self.identity is not None else None

    # ------------------------------------------------------------------ body
    def body(self) -> bytes:
        if self._body is None:
            try:
                length = int(self.environ.get("CONTENT_LENGTH") or 0)
            except ValueError as exc:
                raise HttpError(400, "invalid Content-Length header") from exc
            if length < 0:
                raise HttpError(400, "invalid Content-Length header")
            if length > MAX_BODY_BYTES:
                raise HttpError(413, f"request body larger than {MAX_BODY_BYTES} bytes")
            stream = self.environ.get("wsgi.input")
            self._body = stream.read(length) if (length and stream is not None) else b""
        return self._body

    def data(self) -> tuple[dict[str, Any], dict[str, Upload]]:
        """Fields and uploaded files of a JSON, url-encoded or multipart body."""
        if self._data is not None:
            return self._data
        body = self.body()
        ctype = self.content_type
        if ctype == JSON_TYPE:
            try:
                value = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, ValueError) as exc:
                raise HttpError(400, f"the request body is not valid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise HttpError(400, "the JSON body must be an object")
            self._data = (value, {})
        elif ctype == FORM_TYPE:
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HttpError(400, "form data must be UTF-8") from exc
            fields = {k: v[-1] for k, v in parse_qs(text, keep_blank_values=True).items()}
            self._data = (fields, {})
        elif ctype == MULTIPART_TYPE:
            self._data = parse_multipart(body, self.header("Content-Type") or "")
        elif not body:
            self._data = ({}, {})
        else:
            raise HttpError(415, f"unsupported content type {ctype or '(none)'}: send JSON or a form")
        return self._data

    def field(self, name: str, default: Any = None) -> Any:
        return self.data()[0].get(name, default)


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, Any], dict[str, Upload]]:
    """multipart/form-data with the email parser (the cgi module is deprecated)."""
    if "boundary=" not in content_type:
        raise HttpError(400, "multipart body without boundary")
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1", errors="replace")
    message = BytesParser(policy=policy.HTTP).parsebytes(head + body)
    if not message.is_multipart():
        raise HttpError(400, "malformed multipart body")
    fields: dict[str, Any] = {}
    files: dict[str, Upload] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files[str(name)] = Upload(filename=filename, content=payload, content_type=part.get_content_type())
        else:
            charset = part.get_content_charset() or "utf-8"
            try:
                fields[str(name)] = payload.decode(charset)
            except (LookupError, UnicodeDecodeError):
                fields[str(name)] = payload.decode("utf-8", errors="replace")
    return fields, files


# --------------------------------------------------------------------------- same-origin check (CSRF)
def _host_port(netloc: str, scheme: str | None) -> tuple[str, str | None]:
    netloc = netloc.strip().lower()
    if netloc.startswith("["):  # IPv6 literal
        host, _, rest = netloc[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else None
    else:
        host, _, port_text = netloc.partition(":")
        port = port_text or None
    if port is None and scheme in DEFAULT_PORTS:
        port = DEFAULT_PORTS[scheme]
    return host, port


def _request_hosts(request: Request) -> list[tuple[str, str | None]]:
    hosts = []
    for value in (request.header("Host"), request.header("X-Forwarded-Host")):
        if value:
            hosts.append(_host_port(value.split(",")[0], None))
    if not hosts:
        hosts.append(_host_port(f"{request.environ.get('SERVER_NAME', '')}:{request.environ.get('SERVER_PORT', '')}",
                                None))
    return hosts


def same_origin(source: str, request: Request) -> bool:
    parts = urlsplit(source)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    host, port = _host_port(parts.netloc, parts.scheme)
    for req_host, req_port in _request_hosts(request):
        # A Host header without a port (TLS terminated by the ingress) matches the origin's default port.
        if host == req_host and (req_port is None or req_port == port):
            return True
    return False


def check_same_origin(request: Request) -> None:
    """Refuse cross-site state-changing requests (see module docstring)."""
    site = (request.header("Sec-Fetch-Site") or "").lower()
    if site == "cross-site":
        raise HttpError(403, "cross-site request refused (CSRF protection)", error="cross_origin")
    if request.content_type not in (JSON_TYPE, FORM_TYPE, MULTIPART_TYPE):
        raise HttpError(415, "state-changing requests must send JSON (application/json) or a form of this app",
                        error="unsupported_media_type")
    source = request.header("Origin") or request.header("Referer")
    if source is None:
        if request.content_type != JSON_TYPE:
            raise HttpError(403, "form submission without Origin or Referer header refused (CSRF protection)",
                            error="cross_origin")
        return  # a non-browser JSON client (scripts, CLI)
    if not same_origin(source, request):
        raise HttpError(403, "cross-origin request refused (CSRF protection)", error="cross_origin")


# --------------------------------------------------------------------------- responses
@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def html(cls, markup: str, status: int = 200) -> "Response":
        return cls(status, str(markup).encode("utf-8"), "text/html; charset=utf-8")

    @classmethod
    def json(cls, data: Any, status: int = 200) -> "Response":
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        return cls(status, body, "application/json; charset=utf-8")

    @classmethod
    def text(cls, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> "Response":
        return cls(status, text.encode("utf-8"), content_type)

    @classmethod
    def redirect(cls, location: str, status: int = 303) -> "Response":
        if not location.startswith("/") or location.startswith("//"):
            location = "/"
        return cls(status, b"", "text/plain; charset=utf-8", [("Location", location)])

    @property
    def status_line(self) -> str:
        try:
            phrase = HTTPStatus(self.status).phrase
        except ValueError:
            phrase = "Unknown"
        return f"{self.status} {phrase}"


# --------------------------------------------------------------------------- routing
Handler = Callable[[Request], Response]


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    pattern: re.Pattern[str]
    handler: Handler
    public: bool


class Router:
    """``/cases/{case_id}`` style routes; ``{name}`` matches one path segment."""

    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add(self, method: str, path: str, handler: Handler, *, public: bool = False) -> None:
        regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", re.escape(path).replace(r"\{", "{").replace(r"\}", "}"))
        self.routes.append(Route(method.upper(), path, re.compile(regex), handler, public))

    def resolve(self, method: str, path: str) -> tuple[Route, dict[str, str]]:
        allowed: set[str] = set()
        for route in self.routes:
            match = route.pattern.fullmatch(path)
            if match is None:
                continue
            if route.method == method or (method == "HEAD" and route.method == "GET"):
                return route, match.groupdict()
            allowed.add(route.method)
        if allowed:
            raise HttpError(405, f"method {method} not allowed here", headers=[("Allow", ", ".join(sorted(allowed)))])
        raise HttpError(404, f"no page or API at {path}")
