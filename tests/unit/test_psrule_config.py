"""Static checks of the PSRule for Azure wiring (CI job "Well-Architected check").

The build sandbox has no PowerShell and no PSRule. CI runs #12 and #14 (11 Nov 2026) showed that PSRule
2.9.0 did not apply the suppression groups in .ps-rule/ (also with -Path ./.ps-rule/), so the documented
deviations moved to 'rule.exclude' in ps-rule.yaml, which CI demonstrably reads (cycle 11).
"""
from __future__ import annotations

import copy
import importlib.util
import re
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The 13 findings of CI run #14 (12 rule names; MinReplicas failed for both container apps).
DOCUMENTED_DEVIATIONS = [
    "Azure.AI.PublicAccess", "Azure.AI.PrivateEndpoints", "Azure.ContainerApp.PublicAccess",
    "Azure.ContainerApp.RestrictIngress", "Azure.Cosmos.PublicAccess", "Azure.Storage.Firewall",
    "Azure.ContainerApp.AvailabilityZone", "Azure.Storage.UseReplication", "Azure.Search.QuerySLA",
    "Azure.Search.IndexSLA", "Azure.ACR.MinSku", "Azure.ContainerApp.MinReplicas",
]


def _validator():
    spec = importlib.util.spec_from_file_location("validate_workflows", ROOT / "scripts" / "validate_workflows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exclude_block(text: str) -> str:
    match = re.search(r"^rule:\n  exclude:\n((?:    .*\n)+)", text, re.M)
    if match is None:
        raise AssertionError("ps-rule.yaml has no 'rule.exclude' block")
    return match.group(1)


class PsRuleConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = _validator()
        self.ci_path = ROOT / ".github" / "workflows" / "ci.yml"
        self.ci = self.validator.load_yaml(self.ci_path, use_pyyaml=False)
        self.options = (ROOT / "ps-rule.yaml").read_text(encoding="utf-8")

    def _errors(self, data, path: Path | None = None) -> list[str]:
        errors: list[str] = []
        self.validator.check_workflow(path or self.ci_path, data, errors, [])
        return errors

    def test_excluded_rules_are_exactly_the_documented_deviations(self) -> None:
        excluded = re.findall(r"^    - (\S+)$", _exclude_block(self.options), re.M)
        self.assertEqual(sorted(excluded), sorted(DOCUMENTED_DEVIATIONS))

    def test_every_excluded_rule_has_a_written_reason(self) -> None:
        lines = _exclude_block(self.options).splitlines()
        self.assertTrue(lines[0].strip().startswith("#"), "the first group needs its reason")
        groups = 0
        for previous, line in zip(lines, lines[1:]):
            if line.strip().startswith("- ") and previous.strip().startswith("#"):
                groups += 1
        # public endpoints, one region, AI Search replicas, ACR Basic, replica count
        self.assertEqual(groups, 5)

    def test_readme_lists_every_excluded_rule(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        limits = readme[readme.index("## Limits and deviations"):]
        for rule in DOCUMENTED_DEVIATIONS:
            self.assertIn(f"`{rule}`", limits, rule)

    def test_no_suppression_groups_are_left_behind(self) -> None:
        self.assertFalse((ROOT / ".ps-rule").exists())
        self.assertNotRegex(self.options, r"(?m)^suppression:")

    def test_psrule_calls_use_the_options_file(self) -> None:
        steps = self.ci["jobs"]["psrule"]["steps"]
        calls = [line for step in steps for line in str(step.get("run", "")).splitlines()
                 if re.search(r"\b(?:Assert|Invoke)-PSRule\b", line)]
        self.assertEqual(len(calls), 2)
        for line in calls:
            self.assertIn("-Option ./ps-rule.yaml", line)
            self.assertIn("-Module PSRule.Rules.Azure", line)
        self.assertEqual(self._errors(self.ci), [])
        broken = copy.deepcopy(self.ci)
        for step in broken["jobs"]["psrule"]["steps"]:
            if "run" in step:
                step["run"] = str(step["run"]).replace("-Option ./ps-rule.yaml ", "")
        self.assertTrue(any("must use -Option ./ps-rule.yaml" in e for e in self._errors(broken)))

    def test_a_ps_rule_folder_would_need_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflows = Path(tmp) / ".github" / "workflows"
            workflows.mkdir(parents=True)
            shutil.copy(self.ci_path, workflows / "ci.yml")
            (Path(tmp) / ".ps-rule").mkdir()
            errors = self._errors(self.ci, workflows / "ci.yml")
        self.assertTrue(any("must also pass -Path ./.ps-rule/" in e for e in errors), errors)

    def test_failure_summary_runs_only_after_a_failure(self) -> None:
        steps = {step.get("name", ""): step for step in self.ci["jobs"]["psrule"]["steps"]}
        summary = [step for name, step in steps.items() if name.startswith("List every PSRule finding")]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0].get("if"), "failure()")
        self.assertIn("GITHUB_STEP_SUMMARY", summary[0]["run"])

    def test_lint_runs_shellcheck_and_the_parser_also_after_a_ruff_finding(self) -> None:
        steps = {step.get("name", ""): step for step in self.ci["jobs"]["lint"]["steps"]}
        for name in ("Shell scripts (bash -n and ShellCheck)", "PowerShell scripts (parser check)"):
            self.assertEqual(steps[name].get("if"), "${{ !cancelled() }}", name)

    def test_resource_token_is_the_only_non_sensitive_name(self) -> None:
        match = re.search(r"AZURE_DEPLOYMENT_NONSENSITIVE_PARAMETER_NAMES:\n((?:\s+- \S+\n)+)", self.options)
        self.assertIsNotNone(match)
        self.assertEqual(re.findall(r"- (\S+)", match.group(1)), ["resourceToken"])

    def test_cosmos_uses_the_free_continuous_7_day_backup(self) -> None:
        text = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(encoding="utf-8")
        self.assertRegex(text, r"backupPolicy: \{\s+type: 'Continuous'\s+continuousModeProperties: \{\s+"
                               r"tier: 'Continuous7Days'")


if __name__ == "__main__":
    unittest.main()
