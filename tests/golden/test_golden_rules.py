"""Golden run (Goal Card check 3, concept T03/T21): 100 % match on every rule-based field."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from pms.golden import run_golden

from tests.unit._pipeline_support import cleanup_runs, work_dir

REPO = Path(__file__).resolve().parents[2]


def tearDownModule() -> None:
    cleanup_runs()


class GoldenRun(unittest.TestCase):
    def test_all_rule_based_fields_match_expected_json(self) -> None:
        out = work_dir("golden-rules", create=True) / "golden-test-summary.json"  # this test run only
        passed = run_golden(REPO / "data" / "synthetic", out)
        summary = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(summary["mismatches"], [], "rule-based fields differ from expected.json")
        self.assertTrue(passed)
        self.assertEqual(summary["matches"], summary["fields_compared"])
        # 12 letters x (6 labels + 11 rule expectations + case grouping) + 6 seeds x 2 labels
        self.assertEqual(summary["fields_compared"], 12 * 18 + 6 * 2)
        self.assertEqual(summary["input_integrity"]["problems"], [])
        self.assertTrue(summary["audit_chain"]["ok"])
        self.assertNotIn("causality_range", summary["per_field"])


if __name__ == "__main__":
    unittest.main()
