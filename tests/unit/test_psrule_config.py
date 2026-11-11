"""Static checks of the PSRule for Azure wiring (CI job "Well-Architected check").

The build sandbox has no PowerShell and no PSRule; CI run #12 (11 Nov 2026) showed that the suppression
groups in .ps-rule/ were ignored because Assert-PSRule ran with -Module but without -Path (cycle 10).
"""
from __future__ import annotations

import copy
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _validator():
    spec = importlib.util.spec_from_file_location("validate_workflows", ROOT / "scripts" / "validate_workflows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PsRuleConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = _validator()
        self.ci_path = ROOT / ".github" / "workflows" / "ci.yml"
        self.ci = self.validator.load_yaml(self.ci_path, use_pyyaml=False)

    def _errors(self, data) -> list[str]:
        errors: list[str] = []
        self.validator.check_workflow(self.ci_path, data, errors, [])
        return errors

    def test_every_psrule_call_loads_the_suppression_groups(self) -> None:
        steps = self.ci["jobs"]["psrule"]["steps"]
        calls = [line for step in steps for line in str(step.get("run", "")).splitlines()
                 if re.search(r"\b(?:Assert|Invoke)-PSRule\b", line)]
        self.assertEqual(len(calls), 2)
        for line in calls:
            self.assertIn("-Module PSRule.Rules.Azure", line)
            self.assertIn("-Path ./.ps-rule/", line)
        self.assertEqual(self._errors(self.ci), [])

    def test_validator_refuses_psrule_without_path(self) -> None:
        broken = copy.deepcopy(self.ci)
        for step in broken["jobs"]["psrule"]["steps"]:
            if "run" in step:
                step["run"] = str(step["run"]).replace(" -Path ./.ps-rule/", "")
        errors = self._errors(broken)
        self.assertTrue(any("must also pass -Path ./.ps-rule/" in error for error in errors), errors)

    def test_failure_summary_runs_only_after_a_failure(self) -> None:
        steps = {step.get("name", ""): step for step in self.ci["jobs"]["psrule"]["steps"]}
        summary = [step for name, step in steps.items() if name.startswith("List every PSRule finding")]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0].get("if"), "failure()")
        self.assertIn("GITHUB_STEP_SUMMARY", summary[0]["run"])

    def test_suppression_groups_name_current_rules_with_a_reason(self) -> None:
        text = (ROOT / ".ps-rule" / "waf-deviations.Rule.yaml").read_text(encoding="utf-8")
        groups = [part for part in text.split("\n---\n")[1:] if part.strip()]
        self.assertEqual(len(groups), 7)
        for group in groups:
            self.assertRegex(group.lstrip(), r"^# Synopsis: \S")
            self.assertIn("kind: SuppressionGroup", group)
        # Names reported by PSRule.Rules.Azure 1.39.0 in CI run #12 (the old Azure.Cognitive.* names are gone).
        for rule in ("Azure.AI.PublicAccess", "Azure.AI.PrivateEndpoints", "Azure.Storage.Firewall",
                     "Azure.Storage.UseReplication", "Azure.Cosmos.PublicAccess", "Azure.Search.QuerySLA",
                     "Azure.Search.IndexSLA", "Azure.ACR.MinSku", "Azure.AppInsights.LocalAuth"):
            self.assertIn(f"    - {rule}\n", text)
        self.assertNotIn("Azure.Cognitive.", text.split("\n---\n", 1)[1])
        # Not suppressed: fixed in the templates or in the configuration instead.
        self.assertNotIn("Azure.Cosmos.ContinuousBackup", text)
        self.assertNotIn("Azure.Deployment.SecureParameter", text)

    def test_resource_token_is_the_only_non_sensitive_name(self) -> None:
        text = (ROOT / "ps-rule.yaml").read_text(encoding="utf-8")
        match = re.search(r"AZURE_DEPLOYMENT_NONSENSITIVE_PARAMETER_NAMES:\n((?:\s+- \S+\n)+)", text)
        self.assertIsNotNone(match)
        self.assertEqual(re.findall(r"- (\S+)", match.group(1)), ["resourceToken"])

    def test_cosmos_uses_the_free_continuous_7_day_backup(self) -> None:
        text = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(encoding="utf-8")
        self.assertRegex(text, r"backupPolicy: \{\s+type: 'Continuous'\s+continuousModeProperties: \{\s+"
                               r"tier: 'Continuous7Days'")


if __name__ == "__main__":
    unittest.main()
