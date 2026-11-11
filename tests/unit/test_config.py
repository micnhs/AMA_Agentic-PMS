"""Configuration (BUILD_SPEC 4.2) and the start refusal in Azure with local test users (check 5, T32)."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

from pms import cli
from pms.config import ConfigError, Settings, load_settings


class Settings_(unittest.TestCase):
    def test_local_defaults(self) -> None:
        s = Settings.from_env({})
        self.assertEqual((s.app_env, s.agent_backend, s.local_test_users, s.worker_mode, s.port),
                         ("local", "offline", True, "inprocess", 8000))
        self.assertEqual((s.model_for("analysis"), s.model_for("general")), ("gpt-6-astra", "gpt-6.1-sol"))
        self.assertEqual(s.budget_usd, 250.0)
        self.assertFalse(s.preview_monitoring)

    def test_azure_defaults(self) -> None:
        s = Settings.from_env({"APP_ENV": "azure"})
        self.assertEqual((s.agent_backend, s.local_test_users, s.worker_mode), ("foundry", False, "separate"))

    def test_fallback_model_for_all_agents(self) -> None:
        s = Settings.from_env({"MODEL_FALLBACK_ALL_GENERAL": "true"})
        self.assertEqual(s.model_for("analysis"), "gpt-6.1-sol")

    def test_invalid_values_are_refused(self) -> None:
        for env in ({"APP_ENV": "prod"}, {"AGENT_BACKEND": "openai"}, {"WORKER_MODE": "cron"}, {"PORT": "x"},
                    {"LOCAL_TEST_USERS": "maybe"}):
            with self.subTest(env), self.assertRaises(ConfigError):
                Settings.from_env(env)
        with self.assertRaises(ConfigError):
            load_settings({"ALERT_RECIPIENTS": "not-an-address"})

    def test_public_dict_hides_the_connection_string(self) -> None:
        s = Settings.from_env({"APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=abc"})
        self.assertEqual(s.public_dict()["applicationinsights_connection_string"], "configured")


class StartRefusal(unittest.TestCase):
    ENV = {"APP_ENV": "azure", "LOCAL_TEST_USERS": "true"}

    def test_settings_refuse_azure_with_local_test_users(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_settings(self.ENV)
        self.assertIn("LOCAL_TEST_USERS", str(ctx.exception))
        load_settings({"APP_ENV": "azure", "LOCAL_TEST_USERS": "false"})  # allowed

    def test_serve_and_worker_refuse_to_start(self) -> None:
        for command in ("serve", "worker"):
            with self.subTest(command), mock.patch.dict("os.environ", self.ENV, clear=True):
                err = io.StringIO()
                with redirect_stderr(err):
                    code = cli.main([command])
                self.assertEqual(code, cli.EXIT_CONFIG)
                self.assertIn("Refusing to start", err.getvalue())


if __name__ == "__main__":
    unittest.main()
