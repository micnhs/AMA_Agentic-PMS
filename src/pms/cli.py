"""Command line interface (BUILD_SPEC 4.1): ``python -m pms <command>``.

serve, worker, seed, golden, verify-audit, smoke-test, make-pdfs, publish-agents.

Commands with ``--base-url`` talk to a deployed app with the standard library only (urllib) and send
``Authorization: Bearer $PMS_API_TOKEN`` (the scripts export it; fallback: ``az account
get-access-token``). Nothing in this module sends anything to an authority.
"""
from __future__ import annotations

import argparse
import base64
import importlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import ConfigError, Settings, load_settings
from .models import iso_utc, utcnow
from .versions import manifest

log = logging.getLogger("pms.cli")
EXIT_CONFIG = 2
WORKER_STALE_AFTER_S = 300


def _settings() -> Settings:
    settings = load_settings()
    for warning in settings.validate_startup():
        print(f"WARNING: {warning}", file=sys.stderr)
    return settings


def _logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


# --------------------------------------------------------------------------- HTTP client (stdlib only)
class ApiError(RuntimeError):
    def __init__(self, status: int | None, body: Any) -> None:
        super().__init__(f"HTTP {status}: {str(body)[:300]}")
        self.status = status
        self.body = body


def api_token(*, required: bool = True) -> str | None:
    """PMS_API_TOKEN (scripts export it), else the Azure CLI; never an SDK."""
    token = os.environ.get("PMS_API_TOKEN", "").strip()
    if token:
        return token
    scope = os.environ.get("ENTRA_API_SCOPE") or (
        f"api://{os.environ['ENTRA_APP_CLIENT_ID']}/user_impersonation" if os.environ.get("ENTRA_APP_CLIENT_ID")
        else None)
    if scope:
        try:
            out = subprocess.run(["az", "account", "get-access-token", "--scope", scope, "--query", "accessToken",
                                  "-o", "tsv"], capture_output=True, text=True, timeout=60, check=False)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    if required:
        raise ApiError(None, "no Entra token: set PMS_API_TOKEN (the scripts do this) or run 'az login'")
    return None


def api_request(base_url: str, method: str, path: str, *, body: Any = None, token: str | None = None,
                timeout: float = 60.0) -> tuple[int, Any]:
    url = base_url.rstrip("/") + path
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # report 302 to the login page as such
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - URL given by the operator
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise ApiError(None, f"cannot reach {url}: {exc.reason}") from exc
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text) if text else None
    except ValueError:
        return status, text


def token_roles(token: str | None) -> list[str]:
    """App roles from the token payload (read only - the app validates tokens, the CLI does not)."""
    if not token or token.count(".") < 2:
        return []
    payload = token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, json.JSONDecodeError):
        return []
    return [str(r) for r in claims.get("roles", [])]


# --------------------------------------------------------------------------- worker health endpoint
class HealthServer:
    """``GET /healthz`` for the worker container probes (no authentication, no data)."""

    def __init__(self, pipeline: Any, host: str = "0.0.0.0", port: int = 8000) -> None:
        self.pipeline = pipeline
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.split("?")[0] != "/healthz":
                    self.send_response(404)
                    self.end_headers()
                    return
                status, body = server_ref.status()
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("health %s", fmt % args)

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="pms-worker-health", daemon=True)

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def status(self) -> tuple[int, dict[str, Any]]:
        last = self.pipeline.last_loop_monotonic
        age = None if last is None else time.monotonic() - last
        stale = age is not None and age > WORKER_STALE_AFTER_S
        try:
            pending = self.pipeline.jobs.pending_count()
        except Exception:  # the probe must answer even when the store is busy
            pending = None
        body = {"status": "stale" if stale else "ok", "role": "worker", "worker_id": self.pipeline.worker_id,
                "seconds_since_last_loop": None if age is None else round(age, 1), "pending_jobs": pending,
                "versions": manifest(self.pipeline.settings.synthetic_data_dir), "time_utc": iso_utc(utcnow())}
        return (503 if stale else 200), body

    def start(self) -> "HealthServer":
        self.thread.start()
        return self

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- commands
def cmd_serve(args: argparse.Namespace) -> int:
    try:
        settings = _settings()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        web = importlib.import_module("pms.web.app")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("pms.web"):
            print("The web application (pms.web.app) is not part of this build yet - it is provided by the web "
                  "builder. The services are ready: 'python -m pms seed', 'python -m pms worker', "
                  "'python -m pms golden'.", file=sys.stderr)
            return EXIT_CONFIG
        raise
    _logging()
    auto_port = args.port is None and "PORT" not in os.environ and not settings.is_azure
    web.serve(host=args.host, port=args.port or settings.port, seed=args.seed, settings=settings,
              auto_port=auto_port)
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    try:
        settings = _settings()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    _logging()
    from .pipeline import build_pipeline

    pipeline = build_pipeline(settings)
    health = HealthServer(pipeline, host=args.host, port=args.port if args.port is not None else settings.port).start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:  # not in the main thread (tests)
            pass
    recovered = pipeline.recover()
    log.info("worker %s started (mode %s, health on port %s, re-queued %s stale jobs)", pipeline.worker_id,
             settings.worker_mode, health.port, recovered)
    from . import runtime as rt

    rt.start_azure_setup(settings, pipeline)  # Azure: search index + Foundry agents, idempotent, background
    pipeline.heartbeat(force=True)
    try:
        while not stop.is_set():
            try:
                done = pipeline.run_worker_once(max_jobs=1)["jobs"]
            except Exception:  # the worker keeps running; failures are visible in the log and the cases
                log.exception("worker iteration failed")
                done = 0
            if args.once:
                break
            if not done:
                stop.wait(args.poll_s)
    finally:
        health.close()
        pipeline.close()
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    if getattr(args, "index_only", False):
        return cmd_seed_index(args)
    if args.base_url:
        try:
            status, body = api_request(args.base_url, "POST", "/api/seed", body={}, token=api_token())
        except ApiError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"status": status, "response": body}, indent=2, ensure_ascii=False, default=str))
        return 0 if 200 <= status < 300 else 1
    try:
        settings = _settings()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    from .pipeline import build_pipeline

    pipeline = build_pipeline(settings)
    try:
        result = pipeline.seed_demo()
        analysed = pipeline.drain(timeout=600)
        critical = [c.case_key for c in pipeline.store.list_cases() if c.critical]
        print(f"seed: {len(result['loaded'])} messages loaded, {len(result['skipped_already_loaded'])} already "
              f"present; {analysed} analyses run; {len(critical)} critical cases; database {settings.db_path}")
        for item in result["loaded"]:
            alerts = ", ".join(f"{a['alert_id']} {a['latency_ms']} ms" for a in item["alerts"])
            print(f"  {item['signal_id']} -> {item['case_key']} ({item['relation']}) status {item['status']}"
                  + (f"; alert {alerts}" if alerts else ""))
    finally:
        pipeline.close()
    return 0


def cmd_seed_index(args: argparse.Namespace) -> int:
    """``seed --index-only``: create/fill the Azure AI Search knowledge index from the fictional corpus."""
    try:
        settings = _settings()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    from . import runtime as rt

    try:
        result = rt.seed_search_index(settings, force=bool(getattr(args, "force", False)))
    except Exception as exc:
        print(f"ERROR: building the knowledge index failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"search_index": result}, indent=2, default=str))
    return 0


def cmd_golden(args: argparse.Namespace) -> int:
    from .golden_e2e import print_summary, run_golden_e2e

    summary = run_golden_e2e(args.data_dir, args.out)
    print_summary(summary)
    print(f"summary: {args.out}")
    return 0 if summary["passed"] else 1


def cmd_verify_audit(args: argparse.Namespace) -> int:
    if args.base_url:
        try:
            status, body = api_request(args.base_url, "GET", "/api/audit/verify", token=api_token())
        except ApiError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"status": status, "result": body}, indent=2, default=str))
        return 0 if status == 200 and isinstance(body, dict) and body.get("ok") else 1
    try:
        settings = _settings()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    from . import runtime as rt

    try:
        cosmos = rt.uses_cosmos(settings)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if not cosmos and not settings.db_path.exists():
        print(f"No local database at {settings.db_path} - run 'python -m pms seed' first.", file=sys.stderr)
        return 1

    store = rt.get_store(settings)
    try:
        result = store.audit.verify().to_dict()
    finally:
        store.close()
    print(json.dumps(result, indent=2))
    if result["ok"]:
        print(f"audit chain OK: {result['checked']} records, head {result['head_hash']}")
    else:
        print(f"audit chain BROKEN at record {result['first_break_seq']}: {result['reason']}")
    return 0 if result["ok"] else 1


def cmd_make_pdfs(args: argparse.Namespace) -> int:
    from .intake import make_pdfs

    try:
        written = make_pdfs(args.data_dir)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"wrote {path}")
    return 0


def cmd_publish_agents(args: argparse.Namespace) -> int:
    try:
        settings = _settings()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    from .agents.specs import agent_catalogue

    if args.dry_run:
        print(json.dumps(agent_catalogue(settings), indent=2))
        return 0
    if settings.agent_backend != "foundry" or not settings.foundry_project_endpoint:
        print("publish-agents is Azure-only: set AGENT_BACKEND=foundry and FOUNDRY_PROJECT_ENDPOINT (azd env).",
              file=sys.stderr)
        return EXIT_CONFIG
    from .agents.foundry import FoundryRuntime

    try:
        published = FoundryRuntime(settings).publish_all()
    except Exception as exc:
        print(f"ERROR: publishing the Foundry prompt agents failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"published": published}, indent=2))
    return 0


def cmd_smoke_test(args: argparse.Namespace) -> int:
    from .smoke import run_smoke_test

    try:
        token = api_token()
    except ApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    result = run_smoke_test(args.base_url, part=args.part, token=token, timeout_s=args.timeout,
                            previous=Path(args.out) if args.part == 2 else None)
    Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    for check in result["checks"]:
        print(f"{'PASS' if check['ok'] else ('SKIP' if check.get('skipped') else 'FAIL')}  {check['name']}: "
              f"{check.get('detail', '')}")
    print(f"smoke test part {args.part}: {'passed' if result['passed'] else 'FAILED'} -> {args.out}")
    return 0 if result["passed"] else 1


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pms", description="PMS agent demo (synthetic data only)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="web app (+ in-process worker locally)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--seed", action="store_true", help="load the demo data on start")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("worker", help="background worker (alerts, analysis, deadline sweep) with GET /healthz")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=None, help="health port (default $PORT or 8000)")
    p.add_argument("--poll-s", type=float, default=2.0)
    p.add_argument("--once", action="store_true", help="run one iteration and exit (tests)")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("seed", help="load the 12 golden letters and the 6 seed messages")
    p.add_argument("--base-url", default=None)
    p.add_argument("--index-only", action="store_true",
                   help="Azure: only create/fill the AI Search knowledge index (idempotent; postdeploy)")
    p.add_argument("--force", action="store_true", help="with --index-only: upload every document again")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("golden", help="fresh offline end-to-end golden run with JSON summary")
    p.add_argument("--out", default="var/golden-summary.json")
    p.add_argument("--data-dir", default=None)
    p.set_defaults(func=cmd_golden)

    p = sub.add_parser("verify-audit", help="verify the audit hash chain")
    p.add_argument("--base-url", default=None)
    p.set_defaults(func=cmd_verify_audit)

    p = sub.add_parser("smoke-test", help="live smoke test against the Azure deployment (Gate C)")
    p.add_argument("--base-url", required=True)
    p.add_argument("--part", type=int, choices=(1, 2), required=True)
    p.add_argument("--out", default="smoke-test-result.json")
    p.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for the analysis")
    p.set_defaults(func=cmd_smoke_test)

    p = sub.add_parser("make-pdfs", help="developer helper: regenerate letters/*.pdf from the .txt files")
    p.add_argument("--data-dir", default=None)
    p.set_defaults(func=cmd_make_pdfs)

    p = sub.add_parser("publish-agents", help="Azure: publish/update the Foundry prompt agents (postdeploy)")
    p.add_argument("--dry-run", action="store_true", help="only print the agent catalogue")
    p.set_defaults(func=cmd_publish_agents)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
