"""Read-only agent tools: argument validation in app code, limits (8 calls, 2 rounds, 5 per round) and one
TOOL_CALL audit record per call attempt (concept 6.5, 13; T26)."""
from __future__ import annotations

import unittest

from pms import audit as A
from pms.agents.offline import MODEL, MODEL_VERSION
from pms.agents.tools import ToolBox, ToolLimitExceeded, validate_arguments, TOOL_SPECS, ToolError
from pms.models import Actor, Role

from ._pipeline_support import ingest, new_pipeline

META = {"agent_version": "A6-1.0.0", "model": MODEL, "model_version": MODEL_VERSION,
        "prompt_version": "a6-evidence-correlation@1.0.0"}
ALL_TOOLS = tuple(TOOL_SPECS)


class ToolsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline, clock = new_pipeline("tools")
        self.result = ingest(self.pipeline, clock, "L03")

    def box(self, **kw) -> ToolBox:
        p = self.pipeline
        return ToolBox(store=p.store, ref=p.ref, search=p.search, case_id=self.result.case_id,
                       signal_id=self.result.signal_id, agent_id="A6-evidence-correlation", agent_meta=META,
                       allowed=kw.pop("allowed", ALL_TOOLS), clock=p.clock, **kw)

    def tool_records(self) -> list[dict]:
        return self.pipeline.store.audit.records(event_type=A.TOOL_CALL)


class ArgumentValidation(ToolsTestCase):
    def test_invalid_arguments_are_refused_and_audited(self) -> None:
        box = self.box()
        refused = {
            "unknown parameter": ("search_corpus", {"family": "demo-cardio-b", "url": "x"}),
            "address in text": ("search_corpus", {"text": "send to vigilance-forward@example.org"}),
            "link in text": ("search_corpus", {"text": "see https://example.org"}),
            "bad lot format": ("search_corpus", {"lot": "lot; DROP TABLE"}),
            "unknown family": ("search_corpus", {"family": "real-product"}),
            "no filter at all": ("search_corpus", {"limit": 5}),
            "limit out of range": ("search_corpus", {"family": "demo-cardio-b", "limit": 500}),
            "unknown event term": ("search_corpus", {"event_tag": "anything goes"}),
        }
        for name, (tool, args) in refused.items():
            with self.subTest(name):
                self.assertIn("error", box.call(tool, args))
        self.assertTrue(all(not c.ok for c in box.calls))
        records = self.tool_records()
        self.assertEqual(len(records), len(refused))
        self.assertTrue(all(r["after"]["ok"] is False for r in records))
        self.assertNotIn("example.org", repr(records))  # addresses are redacted in the audit trail

    def test_other_case_and_unavailable_tool_are_refused(self) -> None:
        box = self.box(allowed=("list_case_signals",))
        self.assertIn("error", box.call("list_case_signals", {"case_id": "CASE-OTHER"}))
        self.assertIn("error", box.call("search_corpus", {"family": "demo-cardio-b"}))
        ok = box.call("list_case_signals", {"case_id": self.result.case_id})
        self.assertEqual(ok["count"], 1)
        self.assertNotIn("text", ok["signals"][0])  # no letter text through tools

    def test_validation_helper(self) -> None:
        spec = TOOL_SPECS["get_registry_entry"]
        self.assertEqual(validate_arguments(spec, {"reg_id": "REG-0003"}), {"reg_id": "REG-0003"})
        for bad in ({}, {"reg_id": "REG-3"}, {"patient": "Max Mustermann"}, {"udi_di": 123}):
            with self.subTest(bad), self.assertRaises(ToolError):
                validate_arguments(spec, bad)


class ReadOnlyResults(ToolsTestCase):
    def test_search_registry_dossier_and_past_case(self) -> None:
        box = self.box()
        hits = box.call("search_corpus", {"family": "demo-cardio-b", "event_tag": "leaflet tear"})
        self.assertIn("LIT-001", [h["source_id"] for h in hits["hits"]])
        self.assertIn("not", hits["note"].lower())  # no hits is never an all-clear
        reg = box.call("get_registry_entry", {"lot": "CB21-2607-A"})
        self.assertEqual(reg["count"], 5)  # three patients of the letter plus REG-0013 and REG-0014
        item = box.call("get_dossier_item", {"ref": "DOS:demo-cardio-b:CV-01"})
        self.assertTrue(item["item"]["listed"])
        past = box.call("get_past_case", {"case_ref": "H-001"})
        self.assertEqual(past["past_case"]["source_id"], "H-001")
        records = self.tool_records()
        self.assertEqual(len(records), 4)
        for rec in records:
            self.assertEqual((rec["actor_type"], rec["source"], rec["model"]), ("agent", "agent", MODEL))
            self.assertTrue(rec["corpus_version"])
            self.assertTrue(rec["after"]["read_only"])
        self.assertIn("LIT-001", records[0]["source_ids"])

    def test_only_human_approved_findings_are_returned(self) -> None:
        p = self.pipeline
        p.drain()
        reviewer = Actor("rev", roles=frozenset({Role.MEDICAL_REVIEWER}))
        analyst = Actor("ana", roles=frozenset({Role.ANALYST}))
        p.workflow.take_triage(self.result.case_id, analyst)
        p.workflow.start_review(self.result.case_id, reviewer)
        p.workflow.approve(self.result.case_id, reviewer, "checked")
        finding = p.store.list_findings(kind="approved_assessment")[0]
        box = self.box()
        self.assertEqual(box.call("get_past_case", {"finding_id": finding.finding_id})["count"], 1)
        p.workflow.update_finding_status(finding.finding_id, reviewer, "withdrawn", "superseded")
        self.assertEqual(box.call("get_past_case", {"finding_id": finding.finding_id})["count"], 0)


class Limits(ToolsTestCase):
    def test_eight_calls_per_run_and_five_per_round(self) -> None:
        box = self.box()
        box.begin_round()
        for _ in range(5):
            self.assertNotIn("error", box.call("get_registry_entry", {"reg_id": "REG-0003"}))
        self.assertIn("5 per round", box.call("get_registry_entry", {"reg_id": "REG-0003"})["error"])
        box.begin_round()
        for _ in range(3):
            self.assertNotIn("error", box.call("get_registry_entry", {"reg_id": "REG-0004"}))
        self.assertIn("8 per run", box.call("get_registry_entry", {"reg_id": "REG-0004"})["error"])
        self.assertEqual(len(box.calls), 8)
        self.assertEqual(box.refused_over_limit, 2)
        self.assertEqual(len(self.tool_records()), 10)  # refused attempts are audited too
        with self.assertRaises(ToolLimitExceeded):
            box.begin_round()

    def test_pipeline_runs_stay_within_the_limits(self) -> None:
        self.pipeline.drain()
        for run in self.pipeline.store.list_agent_runs(self.result.case_id):
            calls = run.output.get("tool_calls", [])
            self.assertLessEqual(len(calls), 8)
            self.assertLessEqual(max([c["round"] for c in calls], default=0), 2)
            for rnd in (1, 2):
                self.assertLessEqual(sum(1 for c in calls if c["round"] == rnd), 5)


if __name__ == "__main__":
    unittest.main()
