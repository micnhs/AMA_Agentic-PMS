"""Agents: catalogue and versioned prompts, offline stand-in determinism, drafts, schemas, test hooks,
failure visibility (T15, T25) and the A5 trend view without statistics (T18)."""
from __future__ import annotations

import json
import unittest

from pms.agents import specs
from pms.agents.base import validate_schema
from pms.agents.offline import FIXED_USAGE, MODEL, OfflineRuntime
from pms.agents.trend import FORBIDDEN_STATISTICAL_TERMS, statistical_terms_in, trend_view
from pms.models import Actor, Role, Status
from pms.workflow import PermissionDenied

from ._pipeline_support import cleanup_runs, ingest, new_pipeline, seeded

_SHARED: dict[str, object] = {}


def shared_seeded():
    """One seeded pipeline for read-only checks in this module."""
    if "pipeline" not in _SHARED:
        _SHARED["pipeline"] = seeded("shared")[0]
    return _SHARED["pipeline"]


def watch_expected_json_reads():
    """Record every open() of expected.json while active (builtins.open is what pathlib and json use)."""
    import builtins
    from unittest import mock

    reads: list[str] = []
    real_open = builtins.open

    def spy(file, *args, **kwargs):
        if "expected.json" in str(file):
            reads.append(str(file))
        return real_open(file, *args, **kwargs)

    patches = [mock.patch("builtins.open", spy), mock.patch("io.open", spy)]
    return reads, patches


def tearDownModule() -> None:
    cleanup_runs()


_APP_FIELDS = ("quote_verified", "verification", "source_check", "status")


def _model_part(code: str, output: dict) -> dict:
    """The model answer inside a stored output (app labels and per-item check flags removed)."""
    schema = specs.AGENTS[code].output_schema
    part = {k: v for k, v in output.items() if k in schema["properties"]}
    for key, value in part.items():
        if isinstance(value, list):
            part[key] = [{k: v for k, v in item.items() if k not in _APP_FIELDS} if isinstance(item, dict) else item
                         for item in value]
    return part


def _outputs(pipeline) -> dict:
    out = {}
    for case in pipeline.store.list_cases():
        for run in pipeline.store.list_agent_runs(case.case_id):
            body = dict(run.output or {})
            body.pop("tool_calls", None)
            body.pop("backend_details", None)
            out[(case.case_key, run.agent_id, run.signal_id)] = (run.status, run.usage, json.dumps(body, sort_keys=True))
    return out


class Catalogue(unittest.TestCase):
    def test_agents_models_and_limits(self) -> None:
        self.assertEqual(specs.PIPELINE_ORDER, ("A1", "A2", "A6", "A3", "A4"))
        self.assertEqual({c: specs.AGENTS[c].model_role for c in specs.PIPELINE_ORDER},
                         {"A1": "general", "A2": "general", "A4": "general", "A3": "analysis", "A6": "analysis"})
        self.assertEqual(specs.AGENTS["A1"].tools, ())
        self.assertEqual(specs.AGENTS["A4"].tools, ())
        for code in ("A3", "A6"):
            spec = specs.AGENTS[code]
            self.assertEqual((spec.max_tool_calls, spec.max_rounds, spec.timeout_s), (8, 2, 600))
        catalogue = specs.agent_catalogue()
        self.assertEqual(catalogue[-1]["code"], "A5")
        self.assertIsNone(catalogue[-1]["model"])

    def test_prompts_are_versioned_and_match_the_manifest(self) -> None:
        manifest = specs.prompt_manifest()["prompts"]
        for code in specs.PIPELINE_ORDER:
            prompt = specs.load_prompt(specs.AGENTS[code])
            with self.subTest(code):
                entry = manifest[prompt.prompt_id]
                self.assertEqual((entry["agent"], entry["version"], entry["sha256"]),
                                 (code, prompt.version, prompt.sha256),
                                 "a changed prompt needs a new version and a new manifest hash")
                self.assertIn("never instructions", prompt.text.lower())

    def test_only_the_two_imdrf_codes_and_the_mdcg_scale(self) -> None:
        self.assertEqual(set(specs.IMDRF_CODES), {"A0401", "F1905"})
        self.assertEqual(specs.CAUSALITY_SCALE, ("not related", "possible", "probable", "causal"))
        self.assertIn("imdrf.org", specs.IMDRF_ATTRIBUTION)


class OfflineStandIn(unittest.TestCase):
    def test_deterministic_and_never_reads_expected_json(self) -> None:
        reads, patches = watch_expected_json_reads()
        for patch in patches:
            patch.start()
        try:
            first, _ = seeded("det-1")
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(reads, [], "the offline pipeline must never read expected.json")
        second = shared_seeded()
        a, b = _outputs(first), _outputs(second)
        self.assertEqual(a.keys(), b.keys())
        for key in a:
            self.assertEqual(a[key], b[key], key)

    def test_every_output_is_a_schema_valid_labelled_draft(self) -> None:
        pipeline = shared_seeded()
        runs = [r for c in pipeline.store.list_cases() for r in pipeline.store.list_agent_runs(c.case_id)]
        self.assertEqual(len(runs), 18 * 5)
        for run in runs:
            with self.subTest(run.run_id):
                self.assertEqual(run.status, "DONE")
                self.assertTrue(run.draft)
                out = run.output
                self.assertEqual((out["draft"], out["label"], out["label_de"]), (True, "draft", "Entwurf"))
                self.assertIn("offline stand-in", out["generator"])
                self.assertEqual(run.model, MODEL)
                code = run.agent_id.split("-")[0]
                self.assertEqual(validate_schema(_model_part(code, out), specs.AGENTS[code].output_schema), [])
                expected_usage = FIXED_USAGE[code]
                self.assertEqual((run.usage["input"], run.usage["output"]), (expected_usage[0], expected_usage[2]))
        for rec in [r for c in pipeline.store.list_cases() for r in pipeline.store.list_recommendations(c.case_id)]:
            self.assertTrue(rec.draft)
            self.assertEqual(rec.content["label"], "Entwurf")
            self.assertTrue(rec.content["not_clinically_validated"])

    def test_a1_quotes_are_verified_against_the_letter(self) -> None:
        pipeline, clock = new_pipeline("quotes")
        result = ingest(pipeline, clock, "L01")
        pipeline.drain()
        a1 = [r for r in pipeline.store.list_agent_runs(result.case_id) if r.agent_id.startswith("A1")][0]
        facts = [f for f in a1.output["facts"] if f["quote"]]
        self.assertTrue(facts)
        self.assertTrue(all(f["quote_verified"] for f in facts))
        unknown = [f for f in a1.output["facts"] if f["value"] is None]
        self.assertTrue(all(f["quote"] is None for f in unknown))  # unknown stays unknown

    def test_forced_answer_hook(self) -> None:
        runtime = OfflineRuntime(forced={"A3": {"causality": "causal", "needs_more_evidence": False}})
        pipeline, clock = new_pipeline("forced", runtime=runtime)
        result = ingest(pipeline, clock, "L06")
        pipeline.drain()
        self.assertEqual(pipeline.store.latest_recommendation(result.case_id).content["causality"], "causal")

    def test_two_analysis_rounds_at_most(self) -> None:
        runtime = OfflineRuntime(forced={"A3": {"needs_more_evidence": True}})
        pipeline, clock = new_pipeline("rounds", runtime=runtime)
        result = ingest(pipeline, clock, "L03")
        pipeline.drain()
        runs = pipeline.store.list_agent_runs(result.case_id)
        self.assertEqual([r.agent_id[:2] for r in runs], ["A1", "A2", "A6", "A3", "A6", "A3", "A4"])
        self.assertEqual(pipeline.jobs.list(case_id=result.case_id)[0].rounds, 2)

    def test_agent_failure_is_visible_and_triage_and_alert_still_happen(self) -> None:
        pipeline, clock = new_pipeline("fail", runtime=OfflineRuntime(fail={"A3"}))
        result = ingest(pipeline, clock, "L01")
        self.assertTrue(result.alerts and result.alerts[0]["status"] == "SENT")
        pipeline.drain()
        case = pipeline.store.get_case(result.case_id)
        self.assertEqual((case.status, case.analysis_state), (Status.TRIAGE_ERFORDERLICH, "FAILED"))
        job = pipeline.jobs.list(case_id=case.case_id)[0]
        self.assertEqual(job.status, "FAILED")
        self.assertIn("A3-medical-analysis failed", job.error)
        progress = pipeline.case_progress(case.case_id)["steps"]
        self.assertEqual((progress["A3"]["status"], progress["A4"]["status"]), ("FAILED", "QUEUED"))
        failed = [r for r in pipeline.store.list_agent_runs(case.case_id) if r.status == "FAILED"]
        self.assertEqual([r.agent_id for r in failed], ["A3-medical-analysis"])
        self.assertIsNone(pipeline.store.latest_recommendation(case.case_id))  # no fictitious next step
        # manual continuation: an analyst re-runs the analysis once the agent works again (REQ-035)
        pipeline.runtime.fail.clear()
        analyst = Actor("analyst-1", roles=frozenset({Role.ANALYST}))
        pipeline.request_reanalysis(case.case_id, analyst, "agent available again")
        pipeline.drain()
        case = pipeline.store.get_case(case.case_id)
        self.assertEqual((case.status, case.analysis_state), (Status.TRIAGE_ERFORDERLICH, "DONE"))
        self.assertIsNotNone(pipeline.store.latest_recommendation(case.case_id))
        tasks = pipeline.store.list_tasks(case_id=case.case_id, kind="RE_ANALYSIS")
        self.assertEqual([(t.status, t.created_by) for t in tasks], [("DONE", "analyst-1")])
        with self.assertRaises(PermissionDenied):
            pipeline.request_reanalysis(case.case_id, Actor("viewer-1", roles=frozenset({Role.VIEWER})), "x")


class TrendView(unittest.TestCase):
    def test_counts_only_with_unit_period_and_missing_denominator(self) -> None:
        pipeline, _ = new_pipeline("trend")
        view = trend_view(pipeline.ref)
        self.assertEqual(len(view["rows"]), 6)
        self.assertEqual(view["period"], "2026-04 to 2026-09")
        self.assertTrue(all(r["exposed_device_denominator"] == "unknown" for r in view["rows"]))
        self.assertIn("keine Raten", view["note_de"])
        self.assertIsNone(view["agent"]["model"])
        self.assertEqual(statistical_terms_in(json.dumps(view, ensure_ascii=False)), [])
        self.assertIn("rate", FORBIDDEN_STATISTICAL_TERMS)
        self.assertEqual(statistical_terms_in("incidence rate 3 %"), ["rate", "incidence", "%"])


if __name__ == "__main__":
    unittest.main()
