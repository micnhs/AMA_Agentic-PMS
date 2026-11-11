"""Helpers for the authorisation tests: the web app called directly as a WSGI application (no socket),
offline pipelines in local and Azure mode (no Azure service is touched)."""
from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from pms import runtime as rt
from pms.agents.offline import OfflineRuntime
from pms.config import local_test_settings
from pms.knowledge import KnowledgeIndex
from pms.models import FixedClock
from pms.notify import OutboxNotifier
from pms.pipeline import Pipeline
from pms.store import Store
from pms.telemetry import Telemetry
from pms.web.app import WebApp

from tests.unit._pipeline_support import ingest, new_pipeline, work_dir

HOST = "127.0.0.1:8000"
ORIGIN = f"http://{HOST}"


@dataclass
class Result:
    status: int
    headers: dict[str, str]
    text: str

    def json(self) -> Any:
        return json.loads(self.text)


class WsgiClient:
    def __init__(self, app: WebApp) -> None:
        self.app = app

    def request(self, method: str, path: str, *, user: str | None = None, principal: str | None = None,
                cookie: str | None = None, json_body: Any = None, form: dict[str, Any] | None = None,
                body: bytes | None = None, content_type: str | None = None,
                headers: dict[str, str] | None = None) -> Result:
        path, _, query = path.partition("?")
        environ: dict[str, Any] = {
            "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "8000", "SERVER_PROTOCOL": "HTTP/1.1", "HTTP_HOST": HOST, "wsgi.url_scheme": "http",
            "wsgi.version": (1, 0), "wsgi.errors": io.StringIO(), "wsgi.multithread": True,
            "wsgi.multiprocess": False, "wsgi.run_once": False,
        }
        data = body or b""
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            content_type = content_type or "application/json"
        elif form is not None:
            data = urlencode(form).encode("utf-8")
            content_type = content_type or "application/x-www-form-urlencoded"
            environ["HTTP_ORIGIN"] = ORIGIN
        if content_type:
            environ["CONTENT_TYPE"] = content_type
        environ["CONTENT_LENGTH"] = str(len(data))
        environ["wsgi.input"] = io.BytesIO(data)
        if user:
            environ["HTTP_X_DEMO_USER"] = user
        if principal:
            environ["HTTP_X_MS_CLIENT_PRINCIPAL"] = principal
        if cookie:
            environ["HTTP_COOKIE"] = cookie
        for name, value in (headers or {}).items():
            environ["HTTP_" + name.upper().replace("-", "_")] = value
        captured: dict[str, Any] = {}

        def start_response(status: str, response_headers: list[tuple[str, str]]) -> None:
            captured["status"] = int(status.split()[0])
            captured["headers"] = {k.lower(): v for k, v in response_headers}

        payload = b"".join(self.app(environ, start_response))
        return Result(captured["status"], captured["headers"], payload.decode("utf-8", errors="replace"))

    def post(self, path: str, body: dict[str, Any], **kw: Any) -> Result:
        return self.request("POST", path, json_body=body, **kw)

    def get(self, path: str, **kw: Any) -> Result:
        return self.request("GET", path, **kw)


def local_app(name: str, letters: tuple[str, ...] = ("L06",), *, drain: bool = True) -> tuple[Pipeline, FixedClock,
                                                                                             WsgiClient]:
    pipeline, clock = new_pipeline(name)
    for letter in letters:
        ingest(pipeline, clock, letter)
    if drain:
        pipeline.drain()
    return pipeline, clock, WsgiClient(WebApp(pipeline))


def azure_pipeline(name: str, *, local_test_users: bool = False, at: str = "2026-11-16T08:00:00Z") -> tuple[
        Pipeline, FixedClock]:
    """APP_ENV=azure settings, but every service offline (in-memory store, local index, outbox, stand-in)."""
    settings = local_test_settings(work_dir(name), app_env="azure", local_test_users=local_test_users,
                                   worker_mode="separate")
    clock = FixedClock(at)
    ref = rt.get_reference_data(settings)
    files = rt.MemoryFileStore()
    pipeline = Pipeline(settings, store=Store(":memory:", clock=clock), ref=ref, search=KnowledgeIndex(ref),
                        runtime=OfflineRuntime(models_by_role={"analysis": settings.model_deployment_analysis,
                                                               "general": settings.model_deployment_general}),
                        notifier=OutboxNotifier(files), files=files, telemetry=Telemetry(trace_path=None),
                        clock=clock)
    return pipeline, clock
