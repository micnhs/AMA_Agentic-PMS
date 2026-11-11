"""``bash scripts/run-local.sh`` end to end (README quick start, Goal Card check 9 support): the offline demo starts
with the demo data, answers /healthz without sign-in and the dashboard for a demo user, and stops cleanly on
SIGTERM. The script runs on a free port with its own DATA_DIR (``var/ui-scratch-*``, outside ``var/test-runs``,
which other test modules remove as a whole) that the test deletes afterwards."""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest import mock
from wsgiref.util import setup_testing_defaults

from pms.web import app as web_app

from tests.unit._pipeline_support import REPO

START_TIMEOUT_S = 90


def remove_tree(path: Path, attempts: int = 20) -> None:
    """rmtree with retries: the shared workspace file system sometimes fails an unlink transiently."""
    for _ in range(attempts):
        try:
            shutil.rmtree(path)
        except OSError:
            pass
        if not path.exists():
            return
        time.sleep(0.5)


def scratch_dir(test: unittest.TestCase, name: str) -> Path:
    path = REPO / "var" / f"ui-scratch-{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    test.addCleanup(remove_tree, path)
    return path


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get(url: str, user: str | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"X-Demo-User": user} if user else {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - local test server
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


@unittest.skipIf(shutil.which("bash") is None or os.name == "nt", "needs bash and POSIX signals")
class RunLocalScript(unittest.TestCase):
    def test_run_local_serves_the_seeded_demo_and_stops_on_sigterm(self) -> None:
        port = free_port()
        data_dir = scratch_dir(self, "run-local")
        log_path = data_dir / "server.log"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AZURE_", "COSMOS_", "ACS_", "FOUNDRY_"))}
        env["DATA_DIR"] = str(data_dir)
        with open(log_path, "w", encoding="utf-8") as log:
            process = subprocess.Popen(["bash", "scripts/run-local.sh", "--port", str(port)], cwd=REPO, env=env,
                                       stdout=log, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + START_TIMEOUT_S
            health = None
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    status, body = get(f"{base}/healthz")
                    if status == 200:
                        health = json.loads(body)
                        break
                except OSError:
                    pass
                time.sleep(0.5)
            self.assertIsNotNone(health, log_path.read_text(encoding="utf-8")[-2000:])
            self.assertEqual(health["status"], "ok")
            self.assertEqual(health["app_env"], "local")
            status, page = get(f"{base}/", user="reviewer")
            self.assertEqual(status, 200)
            self.assertIn('id="critical-queue"', page)
            self.assertIn('id="disclaimer"', page)
            self.assertEqual(get(f"{base}/")[0], 401, "no demo user: the sign-in page with the user picker")
            status, cases = get(f"{base}/api/cases", user="viewer")
            self.assertEqual((status, json.loads(cases)["counts"]["cases"]), (200, 14))
            self.assertTrue(list((data_dir / "outbox").glob("*.json")), "critical alerts in the local outbox")
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        self.assertEqual(process.returncode, 0, log_path.read_text(encoding="utf-8")[-2000:])
        output = log_path.read_text(encoding="utf-8")
        self.assertIn(f"listening on http://127.0.0.1:{port}", output)
        self.assertIn("18 messages loaded", output)


class WsgiEntryPoint(unittest.TestCase):
    """``gunicorn pms.web.app:application``: the app is built from the environment on the first request."""

    def call(self, application: object, path: str, user: str | None = None) -> tuple[int, str]:
        environ: dict = {}
        setup_testing_defaults(environ)
        environ.update(PATH_INFO=path, REQUEST_METHOD="GET", HTTP_HOST="127.0.0.1:8000")
        if user:
            environ["HTTP_X_DEMO_USER"] = user
        captured: dict = {}
        body = b"".join(application(environ, lambda status, headers: captured.update(status=status)))
        return int(captured["status"].split()[0]), body.decode("utf-8")

    def test_application_is_built_lazily_from_the_environment(self) -> None:
        self.assertTrue(callable(web_app.application))
        env = {"APP_ENV": "local", "LOCAL_TEST_USERS": "true", "AGENT_BACKEND": "offline", "WORKER_MODE": "separate",
               "DATA_DIR": str(scratch_dir(self, "wsgi-entry"))}
        lazy = web_app._LazyApplication()
        with mock.patch.dict(os.environ, env):
            status, body = self.call(lazy, "/healthz")
            self.assertEqual((status, json.loads(body)["status"]), (200, "ok"))
            self.assertEqual(self.call(lazy, "/api/cases")[0], 401)
            self.assertEqual(self.call(lazy, "/", user="viewer")[0], 200)
        self.assertIs(lazy.get(), lazy.get())
        lazy.get().pipeline.close()


if __name__ == "__main__":
    unittest.main()
