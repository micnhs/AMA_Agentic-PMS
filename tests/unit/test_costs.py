"""Cost monitoring: dated price table, cost per agent run and per case exact to the cent (T27), platform costs and
forecast via Cost Management (fake HTTP) with fallbacks, simulated offline values (T28)."""
from __future__ import annotations

import datetime as _dt
import json
import unittest
from decimal import Decimal

from pms import audit as A
from pms.agents.base import TokenUsage
from pms.config import Settings
from pms.costs import (
    COST_API_VERSION, NOT_AVAILABLE_MESSAGE, CostApiError, CostManagementClient, PriceTable, platform_costs,
    simulated_platform_costs,
)
from pms.reports import build_case_report
from pms.versions import REPO_ROOT

from ._pipeline_support import ingest, new_pipeline

NOW = _dt.datetime(2026, 11, 20, 12, 0, tzinfo=_dt.timezone.utc)
#: Offline stand-in tokens x price table 2026-11-10 (USD): A1/A2/A4 on gpt-6.1-sol, A6/A3 on gpt-6-astra.
EXPECTED_PER_AGENT = {"A1-signal-ingestion": Decimal("0.0099"), "A2-product-matching": Decimal("0.0077"),
                      "A6-evidence-correlation": Decimal("0.099"), "A3-medical-analysis": Decimal("0.1375"),
                      "A4-vigilance-reporting": Decimal("0.0198")}
PER_JOB = sum(EXPECTED_PER_AGENT.values(), Decimal(0))  # 0.2739


class PriceTable_(unittest.TestCase):
    def setUp(self) -> None:
        self.prices = PriceTable.load(REPO_ROOT / "config" / "pricing.json")

    def test_dated_table_with_source_and_data_zone_prices(self) -> None:
        raw = json.loads((REPO_ROOT / "config" / "pricing.json").read_text(encoding="utf-8"))
        self.assertEqual((raw["version"], raw["date"], raw["currency"]), ("2026-11-10", "2026-11-10", "USD"))
        self.assertIn("Azure pricing page", raw["source"])
        self.assertTrue(any("OI-031" in n for n in raw["notes"]))
        astra, sol = self.prices.models["gpt-6-astra"], self.prices.models["gpt-6.1-sol"]
        self.assertEqual((astra.input, astra.output, astra.cached_input), (Decimal("11"), Decimal("55"), None))
        self.assertEqual((sol.input, sol.output, sol.cached_input), (Decimal("2.2"), Decimal("11"), None))

    def test_estimates(self) -> None:
        est = self.prices.estimate("gpt-6-astra", TokenUsage(5000, 0, 1500, 500), simulated=True)
        self.assertEqual((est["usd"], est["price_table_version"], est["simulated"]), (0.1375, "2026-11-10", True))
        cached = self.prices.estimate("gpt-6.1-sol", TokenUsage(1_000_000, 400_000, 0), simulated=False)
        self.assertEqual(cached["usd"], 2.2)  # cached price unknown -> charged at the input price
        self.assertIn("cached input price unknown", cached["note"])
        unknown = self.prices.estimate("other-model", TokenUsage(10, 0, 10), simulated=False)
        self.assertFalse(unknown["usd_known"])


class CaseCosts(unittest.TestCase):
    def test_costs_per_agent_and_case_are_exact_to_the_cent(self) -> None:
        pipeline, clock = new_pipeline("costs")
        l01 = ingest(pipeline, clock, "L01")
        pipeline.drain()
        l02 = ingest(pipeline, clock, "L02")
        pipeline.drain()
        ingest(pipeline, clock, "L12")  # duplicate of L02: a second analysis job on the same case
        pipeline.drain()
        report = build_case_report(pipeline, l01.case_id)
        variable = report["costs"]["variable"]
        for agent, usd in EXPECTED_PER_AGENT.items():
            self.assertEqual(Decimal(str(variable["per_agent"][agent]["usd"])), usd, agent)
        self.assertEqual(Decimal(str(variable["usd"])), PER_JOB)
        self.assertEqual(variable["usd_cents"], "0.27")
        self.assertTrue(variable["simulated"])
        self.assertEqual(report["costs"]["price_table"]["version"], "2026-11-10")
        two_jobs = build_case_report(pipeline, l02.case_id)["costs"]["variable"]
        self.assertEqual(Decimal(str(two_jobs["usd"])), 2 * PER_JOB)
        self.assertEqual(two_jobs["usd_cents"], "0.55")
        records = pipeline.store.audit.records(case_id=l01.case_id, event_type=A.AGENT_RUN)
        self.assertEqual(sum(Decimal(str(r["cost_estimate"]["usd"])) for r in records), PER_JOB)
        fixed = report["costs"]["fixed_share"]
        self.assertTrue(fixed["available"] and fixed["simulated"])
        self.assertIn("number of cases", fixed["method"])

    def test_cost_overview_offline_is_simulated_and_labelled(self) -> None:
        pipeline, clock = new_pipeline("overview")
        ingest(pipeline, clock, "L06")
        pipeline.drain()
        view = pipeline.cost_overview()
        self.assertTrue(view["platform"]["simulated"])
        self.assertIn("simulated", view["platform"]["label"])
        self.assertIn("projection", view["platform"]["forecast_next_month"]["label"])
        self.assertEqual(view["budget"]["budget_usd"], 250.0)
        self.assertEqual(view["budget"]["thresholds"], [50, 80, 100])
        self.assertEqual(len(view["cases"]), 1)
        self.assertGreater(view["models_month_to_date"]["total_usd"], 0)
        self.assertIn("A3-medical-analysis", view["models_month_to_date"]["per_agent"])


class PlatformCosts(unittest.TestCase):
    AZURE = Settings(app_env="azure", agent_backend="foundry", local_test_users=False, cost_scope="/subscriptions/s/resourceGroups/rg-demo")

    def fake_http(self, responses: dict):
        calls = []

        def http(url, body, token):
            calls.append((url, body, token))
            for key, value in responses.items():
                if f"/{key}?" in url:
                    if isinstance(value, Exception):
                        raise value
                    return value
            raise AssertionError(url)

        return http, calls

    def test_query_and_forecast(self) -> None:
        http, calls = self.fake_http({
            "query": {"properties": {"columns": [{"name": "Cost"}, {"name": "ServiceName"}, {"name": "Currency"}],
                                     "rows": [[40.5, "Azure AI Search", "USD"], [6.25, "Container Apps", "USD"]]}},
            "forecast": {"properties": {"columns": [{"name": "Cost"}, {"name": "UsageDate"}],
                                        "rows": [[3.0, 20261201]] * 31}}})
        client = CostManagementClient(self.AZURE.cost_scope, token_provider=lambda: "token", http=http)
        view = platform_costs(self.AZURE, NOW, client=client)
        self.assertTrue(view["available"] and not view["simulated"])
        self.assertEqual(view["month_to_date"]["total_usd"], 46.75)
        self.assertEqual(view["month_to_date"]["by_service"]["Azure AI Search"], 40.5)
        self.assertEqual(view["forecast_next_month"]["total_usd"], 93.0)
        self.assertEqual((view["forecast_next_month"]["from"], view["forecast_next_month"]["to"]),
                         ("2026-12-01", "2026-12-31"))
        url = calls[0][0]
        self.assertTrue(url.startswith("https://management.azure.com/subscriptions/s/resourceGroups/rg-demo/"
                                       "providers/Microsoft.CostManagement/query"))
        self.assertIn(f"api-version={COST_API_VERSION}", url)
        self.assertEqual(COST_API_VERSION, "2026-06-01")
        self.assertEqual(calls[0][2], "token")

    def test_forecast_unavailable_falls_back_to_a_labelled_projection(self) -> None:
        daily = {"properties": {"columns": [{"name": "Cost"}], "rows": [[2.0]] * 14}}
        responses = {"forecast": CostApiError(400, "not supported")}
        http, calls = self.fake_http(responses)

        def http2(url, body, token):
            if "/query?" in url:
                if body["timeframe"] == "MonthToDate":
                    return {"properties": {"columns": [{"name": "Cost"}, {"name": "ServiceName"}],
                                           "rows": [[10.0, "Container Apps"]]}}
                return daily
            return http(url, body, token)

        client = CostManagementClient(self.AZURE.cost_scope, token_provider=lambda: "t", http=http2)
        forecast = platform_costs(self.AZURE, NOW, client=client)["forecast_next_month"]
        self.assertEqual(forecast["total_usd"], 62.0)  # 14-day average 2.00 x 31 days
        self.assertIn("simple projection from the 14-day daily average", forecast["label"])

    def test_no_api_access_gives_a_clear_message_not_an_error(self) -> None:
        http, _ = self.fake_http({"query": CostApiError(403, "AuthorizationFailed")})
        client = CostManagementClient(self.AZURE.cost_scope, token_provider=lambda: "t", http=http)
        view = platform_costs(self.AZURE, NOW, client=client)
        self.assertFalse(view["available"])
        self.assertEqual(view["message"], NOT_AVAILABLE_MESSAGE)
        self.assertFalse(platform_costs(Settings(app_env="azure", local_test_users=False), NOW)["available"])

    def test_simulated_values_are_deterministic(self) -> None:
        a, b = simulated_platform_costs(NOW), simulated_platform_costs(NOW)
        self.assertEqual(a, b)
        self.assertEqual(a["month_to_date"]["total_usd"], 68.494)  # 20 days x 3.4247 USD/day (indicative)


if __name__ == "__main__":
    unittest.main()
