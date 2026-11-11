"""CLI (BUILD_SPEC 4.1): worker health endpoint, offline seed and verify-audit, remote calls with the Entra bearer
token (urllib only), smoke test parts 1 and 2 against a fake API, publish-agents (Azure only)."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from pms import cli
from pms.smoke import run_smoke_test

from ._pipeline_support import cleanup_runs, new_pipeline, work_dir


def tearDownModule() -> None:
    cleanup_runs()


def run_cli(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict("os.environ", env or {}), redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def fake_jwt(roles: list[str]) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc({'roles': roles})}.signature"


class WorkerHealth(unittest.TestCase):
    def test_healthz_answers_without_auth_and_reports_staleness(self) -> None:
        pipeline, _ = new_pipeline("health")
        server = cli.HealthServer(pipeline, host="127.0.0.1", port=0).start()
        try:
            base = f"http://127.0.0.1:{server.port}"
            with urllib.request.urlopen(f"{base}/healthz", timeout=5) as response:
                body = json.loads(response.read())
            self.assertEqual((response.status, body["status"], body["role"]), (200, "ok", "worker"))
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"{base}/api/cases", timeout=5)
            self.assertEqual(ctx.exception.code, 404)
            pipeline.last_loop_monotonic = time.monotonic() - 3600
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"{base}/healthz", timeout=5)
            self.assertEqual(ctx.exception.code, 503)
        finally:
            server.close()

    def test_worker_command_runs_one_iteration(self) -> None:
        code, _, err = run_cli(["worker", "--once", "--port", "0", "--host", "127.0.0.1"],
                               {"DATA_DIR": str(work_dir("worker", create=True))})
        self.assertEqual(code, 0, err)


class LocalCommands(unittest.TestCase):
    def test_seed_is_idempotent_and_verify_audit_passes(self) -> None:
        env = {"DATA_DIR": str(work_dir("cli-seed", create=True))}
        code, out, err = run_cli(["seed"], env)
        self.assertEqual(code, 0, err)
        self.assertIn("18 messages loaded", out)
        code, out, _ = run_cli(["seed"], env)
        self.assertIn("0 messages loaded, 18 already present", out)
        code, out, _ = run_cli(["verify-audit"], env)
        self.assertEqual(code, 0)
        self.assertIn("audit chain OK", out)

    def test_verify_audit_without_database(self) -> None:
        code, _, err = run_cli(["verify-audit"], {"DATA_DIR": str(work_dir("empty"))})
        self.assertEqual(code, 1)
        self.assertIn("seed", err)

    def test_publish_agents_is_azure_only(self) -> None:
        code, _, err = run_cli(["publish-agents"], {"AGENT_BACKEND": "offline"})
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("Azure-only", err)
        code, out, _ = run_cli(["publish-agents", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual([a["code"] for a in json.loads(out)], ["A1", "A2", "A6", "A3", "A4", "A5"])

    @unittest.skipIf(importlib.util.find_spec("pms.web") is not None, "the web app exists")
    def test_serve_without_the_web_module_explains_itself(self) -> None:
        code, _, err = run_cli(["serve"])
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("pms.web.app", err)

    def test_smoke_test_needs_a_base_url(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.main(["smoke-test", "--part", "1"])
        self.assertEqual(ctx.exception.code, 2)


class RemoteCommands(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[dict] = []
        recorder = self.requests

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, body: dict) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                recorder.append({"method": self.command, "path": self.path,
                                 "authorization": self.headers.get("Authorization"),
                                 "body": self.rfile.read(length).decode() if length else None})
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802
                self._answer({"loaded": 18})

            def do_GET(self) -> None:  # noqa: N802
                self._answer({"ok": True, "checked": 10})

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_seed_and_verify_send_the_bearer_token(self) -> None:
        env = {"PMS_API_TOKEN": "test-token-123"}
        self.assertEqual(run_cli(["seed", "--base-url", self.base], env)[0], 0)
        self.assertEqual(run_cli(["verify-audit", "--base-url", self.base], env)[0], 0)
        self.assertEqual([(r["method"], r["path"]) for r in self.requests],
                         [("POST", "/api/seed"), ("GET", "/api/audit/verify")])
        self.assertTrue(all(r["authorization"] == "Bearer test-token-123" for r in self.requests))


class SmokeTest(unittest.TestCase):
    def fake_api(self, *, decisions: bool):
        token_seen = []

        def request(base_url, method, path, *, body=None, token=None, timeout=60):
            token_seen.append(token)
            if path == "/healthz":
                return 200, {"status": "ok", "versions": {"app": "0.1.0"}}
            if token is None:
                return 401, None
            if (method, path) == ("GET", "/api/cases"):
                return 200, []
            if (method, path) == ("POST", "/api/letters"):
                self.letter = body["text"]
                return 201, {"case_id": "CASE-SMOKE", "signal_id": "SIG-1"}
            if path == "/api/cases/CASE-SMOKE":
                return 200, {"case": {"case_id": "CASE-SMOKE", "status": "TRIAGE_ERFORDERLICH",
                                      "analysis_state": "DONE"},
                             "signals": [{"received_at": "2026-11-20T10:00:00Z"}],
                             "alerts": [{"status": "SENT", "sent_at": "2026-11-20T10:00:02.500Z"}],
                             "decisions": [{"kind": "approve", "actor_id": "reviewer@contoso.example",
                                            "reason": "checked"}] if decisions else [],
                             "approvals": [{"content_hash": "abc"}] if decisions else []}
            if path.startswith("/api/cases/CASE-SMOKE/report"):
                return 200, {"completeness": {"complete": True},
                             "recommendation": {"draft": True, "causality": "probable",
                                                "imdrf_candidates": [{"code": "A0401"}]},
                             "reviewer_decision": {"summary": "approve by reviewer@contoso.example"
                                                   if decisions else "pending - no reviewer decision"}}
            if path.endswith("/decision"):
                return 403, {"error": "forbidden"}
            if path.startswith("/api/audit?"):
                return 200, [{"event_type": "DECISION", "actor_type": "human"}]
            if path == "/api/audit/verify":
                return 200, {"ok": True}
            if path == "/api/costs":
                return 200, {"platform": {"available": False, "message": "not available (OI-030)"}}
            raise AssertionError((method, path))

        return request, token_seen

    def test_parts_one_and_two(self) -> None:
        token = fake_jwt(["PMS.Analyst"])
        request, tokens = self.fake_api(decisions=False)
        part1 = run_smoke_test("https://demo.example", part=1, token=token, request=request, sleep=lambda s: None)
        self.assertTrue(part1["passed"], part1["checks"])
        self.assertEqual(part1["smoke_case_id"], "CASE-SMOKE")
        self.assertIn("P-ZZ-", self.letter)  # fictional pseudonym outside the registry
        names = {c["name"]: c for c in part1["checks"]}
        self.assertEqual(names["critical alert within 60 s"]["latency_s"], 2.5)
        self.assertTrue(names["decision refused for non-reviewers"]["ok"])
        previous = work_dir("smoke", create=True) / "smoke-test-result.json"
        previous.write_text(json.dumps(part1), encoding="utf-8")
        request2, _ = self.fake_api(decisions=True)
        part2 = run_smoke_test("https://demo.example", part=2, token=token, previous=previous, request=request2)
        self.assertTrue(part2["passed"], part2["checks"])
        request3, _ = self.fake_api(decisions=False)
        pending = run_smoke_test("https://demo.example", part=2, token=token, previous=previous, request=request3)
        self.assertFalse(pending["passed"])
        self.assertEqual(cli.token_roles(token), ["PMS.Analyst"])


if __name__ == "__main__":
    unittest.main()
