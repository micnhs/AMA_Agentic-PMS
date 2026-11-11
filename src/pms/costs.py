"""Cost monitoring (concept 6.7, ADR-010, R-17; tests T27, T28; D-35, D-36).

* Model cost per agent run = token usage (input, cached input, output incl. reasoning) x the dated
  price table ``config/pricing.json`` (Decimal arithmetic, exact to the cent).
* Cost per case = measured variable model costs of all agent runs of the case, plus - shown
  separately - an allocated share of the fixed platform costs (platform cost month to date / number
  of cases).
* Platform costs month to date and the next-month forecast come from the Azure Cost Management
  Query and Forecast REST APIs (api-version 2026-06-01, resource group scope, managed identity with
  Cost Management Reader). Without a forecast the app shows a labelled simple projection from the
  14-day daily average; without API access a clear message instead of an error (OI-030).
* Offline, platform costs and forecast are simulated and labelled "simulated".

Cost values are kept apart from safety and trend views; they are no statement about products (K-19).
"""
from __future__ import annotations

import calendar
import datetime as _dt
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable

from .agents.base import TokenUsage
from .interfaces import CaseStore
from .models import iso_utc, parse_utc

COST_API_VERSION = "2026-06-01"
MANAGEMENT_ENDPOINT = "https://management.azure.com"
MANAGEMENT_SCOPE = "https://management.azure.com/.default"
MILLION = Decimal(1_000_000)
USD6 = Decimal("0.000001")
CENT = Decimal("0.01")
BUDGET_THRESHOLDS = (50, 80, 100)
NOT_AVAILABLE_MESSAGE = ("The Cost Management APIs are not available for this subscription or identity (OI-030): "
                         "only token-based model costs are shown.")
SIMULATED_LABEL = "simulated - offline demo values, not measured"
#: Indicative fixed platform cost per day for the simulated offline view (concept section 14, 30-day month).
SIMULATED_DAILY_USD: dict[str, Decimal] = {
    "Azure AI Search (Basic)": Decimal("2.4577"),
    "Azure Container Apps": Decimal("0.3667"),
    "Azure Container Registry (Basic)": Decimal("0.1670"),
    "Log Analytics and Application Insights": Decimal("0.3333"),
    "Cosmos DB, Storage, Key Vault, Communication Services": Decimal("0.1000"),
}


def _money(value: Decimal) -> float:
    return float(value.quantize(USD6, rounding=ROUND_HALF_UP))


def _cents(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- price table
@dataclass(frozen=True)
class ModelPrice:
    model: str
    input: Decimal
    cached_input: Decimal | None
    output: Decimal
    model_version: str | None


@dataclass(frozen=True)
class PriceTable:
    version: str
    date: str
    source: str
    currency: str
    unit: str
    notes: tuple[str, ...]
    models: dict[str, ModelPrice] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "PriceTable":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        models = {
            name: ModelPrice(name, Decimal(str(m["input"])),
                             None if m.get("cached_input") is None else Decimal(str(m["cached_input"])),
                             Decimal(str(m["output"])), m.get("model_version"))
            for name, m in raw["models"].items()
        }
        return cls(str(raw["version"]), str(raw["date"]), str(raw["source"]), str(raw.get("currency", "USD")),
                   str(raw.get("unit", "USD per 1M tokens")), tuple(raw.get("notes", [])), models)

    def describe(self) -> dict[str, Any]:
        return {"version": self.version, "date": self.date, "source": self.source, "currency": self.currency,
                "unit": self.unit, "notes": list(self.notes),
                "models": {n: {"input": float(p.input), "cached_input": None if p.cached_input is None else float(
                    p.cached_input), "output": float(p.output), "model_version": p.model_version}
                    for n, p in self.models.items()}}

    def estimate_exact(self, model: str | None, usage: TokenUsage) -> tuple[Decimal | None, dict[str, Any]]:
        price = self.models.get(model or "")
        if price is None:
            return None, {"note": f"no price for model {model!r} in price table {self.version}: cost unknown"}
        cached = min(usage.cached_input_tokens, usage.input_tokens)
        uncached = usage.input_tokens - cached
        cached_price = price.cached_input if price.cached_input is not None else price.input
        parts = {
            "input": Decimal(uncached) * price.input / MILLION,
            "cached_input": Decimal(cached) * cached_price / MILLION,
            "output": Decimal(usage.output_tokens) * price.output / MILLION,
        }
        note = None if price.cached_input is not None or not cached else (
            "cached input price unknown - cached tokens charged at the input price")
        return sum(parts.values(), Decimal(0)), {"breakdown": {k: _money(v) for k, v in parts.items()}, "note": note}

    def estimate(self, model: str | None, usage: TokenUsage, *, simulated: bool) -> dict[str, Any]:
        total, info = self.estimate_exact(model, usage)
        return {
            "usd": _money(total) if total is not None else 0.0,
            "usd_known": total is not None,
            "model": model,
            "price_table_version": self.version,
            "price_date": self.date,
            "tokens": usage.to_dict(),
            "simulated": simulated,
            **info,
        }


# --------------------------------------------------------------------------- model and case costs
def _month(ts: str) -> str:
    return ts[:7]


def model_costs(store: CaseStore, *, month: str | None = None) -> dict[str, Any]:
    """Model costs by agent and by model (all records, or those of ``month`` = YYYY-MM)."""
    per_agent: dict[str, dict[str, Any]] = {}
    per_model: dict[str, dict[str, Any]] = {}
    total = Decimal(0)
    simulated = False
    for rec in store.list_costs():
        if month is not None and _month(rec.ts) != month:
            continue
        value = Decimal(str(rec.cost_usd))
        total += value
        simulated = simulated or rec.simulated
        for key, bucket in ((rec.agent_id, per_agent), (rec.model or "unknown", per_model)):
            b = bucket.setdefault(key, {"runs": 0, "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0,
                                        "usd": Decimal(0)})
            b["runs"] += 1
            b["input_tokens"] += rec.input_tokens
            b["cached_tokens"] += rec.cached_tokens
            b["output_tokens"] += rec.output_tokens
            b["usd"] += value
    fmt = lambda bucket: {k: {**v, "usd": _money(v["usd"])} for k, v in sorted(bucket.items())}  # noqa: E731
    return {"total_usd": _money(total), "per_agent": fmt(per_agent), "per_model": fmt(per_model),
            "simulated": simulated, "month": month,
            "label": "measured token usage x dated price table" + (" (offline stand-in: fixed token numbers, "
                                                                   "simulated)" if simulated else "")}


def case_costs(store: CaseStore, case_id: str, *, platform: dict[str, Any] | None = None,
               case_count: int | None = None) -> dict[str, Any]:
    """Variable model costs of one case plus a separately shown share of the fixed platform costs."""
    per_agent: dict[str, dict[str, Any]] = {}
    total = Decimal(0)
    versions: set[str] = set()
    simulated = False
    for rec in store.list_costs(case_id):
        value = Decimal(str(rec.cost_usd))
        total += value
        simulated = simulated or rec.simulated
        if rec.price_table_version:
            versions.add(rec.price_table_version)
        b = per_agent.setdefault(rec.agent_id, {"runs": 0, "model": rec.model, "input_tokens": 0, "cached_tokens": 0,
                                                "output_tokens": 0, "usd": Decimal(0)})
        b["runs"] += 1
        b["input_tokens"] += rec.input_tokens
        b["cached_tokens"] += rec.cached_tokens
        b["output_tokens"] += rec.output_tokens
        b["usd"] += value
    fixed: dict[str, Any] = {"available": False, "usd": None,
                             "note": "platform costs not available - no fixed-cost share shown"}
    if platform and platform.get("available") and platform.get("month_to_date"):
        count = case_count if case_count is not None else max(1, len(store.list_cases()))
        mtd = Decimal(str(platform["month_to_date"]["total_usd"]))
        share = mtd / Decimal(max(1, count))
        fixed = {"available": True, "usd": _money(share), "usd_cents": _cents(share), "cases": count,
                 "method": f"platform cost month to date / number of cases ({count})",
                 "simulated": bool(platform.get("simulated")), "label": "allocated fixed-cost share (estimate)"}
    return {
        "case_id": case_id,
        "variable": {"usd": _money(total), "usd_cents": _cents(total), "simulated": simulated,
                     "per_agent": {k: {**v, "usd": _money(v["usd"]), "usd_cents": _cents(v["usd"])}
                                   for k, v in sorted(per_agent.items())},
                     "label": "measured model costs of all agent runs of the case"},
        "fixed_share": fixed,
        "price_table_versions": sorted(versions),
    }


# --------------------------------------------------------------------------- platform costs (Cost Management)
class CostApiError(RuntimeError):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(f"Cost Management API error {status}: {message}")
        self.status = status


def _http_post(url: str, body: dict[str, Any], token: str, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed Azure endpoint
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise CostApiError(exc.code, exc.read().decode("utf-8", errors="replace")[:300]) from exc
    except urllib.error.URLError as exc:
        raise CostApiError(None, str(exc.reason)) from exc


def azure_token_provider(settings: Any) -> Callable[[], str]:
    """Managed identity (AZURE_CLIENT_ID) or the default credential chain; lazy import."""
    def provider() -> str:
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential  # type: ignore[import-not-found]

        credential = (ManagedIdentityCredential(client_id=settings.azure_client_id) if settings.azure_client_id
                      else DefaultAzureCredential())
        return credential.get_token(MANAGEMENT_SCOPE).token
    return provider


def _rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    props = response.get("properties", {})
    names = [c.get("name") for c in props.get("columns", [])]
    return [dict(zip(names, row)) for row in props.get("rows", [])]


def _cost_value(row: dict[str, Any]) -> Decimal:
    for key in ("Cost", "PreTaxCost", "CostUSD", "totalCost"):
        if row.get(key) is not None:
            return Decimal(str(row[key]))
    return Decimal(0)


def _next_month(now: _dt.datetime) -> tuple[_dt.date, _dt.date]:
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return _dt.date(year, month, 1), _dt.date(year, month, calendar.monthrange(year, month)[1])


class CostManagementClient:
    """Minimal REST client (urllib) for the Cost Management Query and Forecast APIs."""

    def __init__(self, scope: str, *, token_provider: Callable[[], str],
                 http: Callable[[str, dict[str, Any], str], dict[str, Any]] | None = None,
                 api_version: str = COST_API_VERSION) -> None:
        self.scope = "/" + scope.strip("/")
        self.token_provider = token_provider
        self.http = http or _http_post
        self.api_version = api_version

    def _post(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        url = (f"{MANAGEMENT_ENDPOINT}{self.scope}/providers/Microsoft.CostManagement/{operation}"
               f"?api-version={self.api_version}")
        return self.http(url, body, self.token_provider())

    def month_to_date_by_service(self) -> dict[str, Any]:
        body = {"type": "ActualCost", "timeframe": "MonthToDate",
                "dataset": {"granularity": "None",
                            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                            "grouping": [{"type": "Dimension", "name": "ServiceName"}]}}
        rows = _rows(self._post("query", body))
        by_service = {str(r.get("ServiceName") or "other"): _cost_value(r) for r in rows}
        currency = next((r.get("Currency") for r in rows if r.get("Currency")), "USD")
        total = sum(by_service.values(), Decimal(0))
        return {"total_usd": _money(total), "by_service": {k: _money(v) for k, v in sorted(by_service.items())},
                "currency": currency}

    def daily_last_days(self, now: _dt.datetime, days: int = 14) -> list[Decimal]:
        start = (now - _dt.timedelta(days=days)).date()
        end = (now - _dt.timedelta(days=1)).date()
        body = {"type": "ActualCost", "timeframe": "Custom",
                "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"},
                "dataset": {"granularity": "Daily", "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}}}}
        return [_cost_value(r) for r in _rows(self._post("query", body))]

    def forecast_next_month(self, now: _dt.datetime) -> dict[str, Any]:
        first, last = _next_month(now)
        body = {"type": "ActualCost", "timeframe": "Custom",
                "timePeriod": {"from": f"{first}T00:00:00Z", "to": f"{last}T23:59:59Z"},
                "dataset": {"granularity": "Daily", "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}}},
                "includeActualCost": False, "includeFreshPartialCost": False}
        rows = _rows(self._post("forecast", body))
        if not rows:
            raise CostApiError(None, "forecast returned no rows")
        total = sum((_cost_value(r) for r in rows), Decimal(0))
        return {"total_usd": _money(total), "from": str(first), "to": str(last),
                "source": f"Azure Cost Management Forecast API ({self.api_version})", "label": "forecast (estimate)"}


def projection_from_daily(daily: list[Decimal], now: _dt.datetime, *, label: str) -> dict[str, Any]:
    first, last = _next_month(now)
    average = (sum(daily, Decimal(0)) / Decimal(len(daily))) if daily else Decimal(0)
    total = average * Decimal((last - first).days + 1)
    return {"total_usd": _money(total), "from": str(first), "to": str(last), "daily_average_usd": _money(average),
            "days_used": len(daily), "source": label,
            "label": "simple projection from the 14-day daily average (estimate)"}


def simulated_platform_costs(now: _dt.datetime) -> dict[str, Any]:
    """Offline: deterministic, labelled simulated platform costs and projection."""
    day = now.day
    by_service = {name: rate * Decimal(day) for name, rate in SIMULATED_DAILY_USD.items()}
    total = sum(by_service.values(), Decimal(0))
    daily = [sum(SIMULATED_DAILY_USD.values(), Decimal(0))] * 14
    return {
        "available": True, "simulated": True, "label": SIMULATED_LABEL,
        "month_to_date": {"total_usd": _money(total), "by_service": {k: _money(v) for k, v in by_service.items()},
                          "currency": "USD", "from": f"{now.date().replace(day=1)}", "to": str(now.date())},
        "forecast_next_month": projection_from_daily(daily, now, label="simulated 14-day daily average"),
        "data_as_of": iso_utc(now), "source": "simulated offline values (indicative fixed costs of concept 14)",
    }


def platform_costs(settings: Any, now: _dt.datetime, *, client: CostManagementClient | None = None) -> dict[str, Any]:
    """Platform costs month to date and next-month forecast (Azure), or simulated values offline."""
    if not settings.is_azure:
        return simulated_platform_costs(now)
    if client is None:
        if not settings.cost_scope:
            return {"available": False, "simulated": False, "message": NOT_AVAILABLE_MESSAGE,
                    "error": "COST_SCOPE is not set"}
        client = CostManagementClient(settings.cost_scope, token_provider=azure_token_provider(settings))
    try:
        mtd = client.month_to_date_by_service()
    except Exception as exc:  # unsupported subscription type, missing role, no network ...
        return {"available": False, "simulated": False, "message": NOT_AVAILABLE_MESSAGE,
                "error": f"{type(exc).__name__}: {exc}"[:400]}
    try:
        forecast = client.forecast_next_month(now)
    except Exception as exc:
        try:
            forecast = projection_from_daily(client.daily_last_days(now), now,
                                             label=f"Cost Management Query API daily costs (forecast unavailable: "
                                                   f"{type(exc).__name__})")
        except Exception as exc2:
            forecast = {"available": False, "message": f"no forecast available: {type(exc2).__name__}"}
    return {"available": True, "simulated": False, "label": "actual costs (data delay up to about one day, A-15)",
            "month_to_date": {**mtd, "from": f"{now.date().replace(day=1)}", "to": str(now.date())},
            "forecast_next_month": forecast, "data_as_of": iso_utc(now),
            "source": f"Azure Cost Management Query API ({client.api_version}), scope {client.scope}"}


def budget_status(budget_usd: float, spent_usd: float, forecast_usd: float | None) -> dict[str, Any]:
    budget = Decimal(str(budget_usd))
    spent = Decimal(str(spent_usd))
    percent = float((spent / budget * 100).quantize(Decimal("0.1"))) if budget else None
    reached = [t for t in BUDGET_THRESHOLDS if percent is not None and percent >= t]
    forecast_percent = (float((Decimal(str(forecast_usd)) / budget * 100).quantize(Decimal("0.1")))
                        if budget and forecast_usd is not None else None)
    return {"budget_usd": float(budget), "spent_usd": float(spent), "percent_used": percent,
            "thresholds": list(BUDGET_THRESHOLDS), "thresholds_reached": reached,
            "forecast_percent": forecast_percent,
            "forecast_over_budget": forecast_percent is not None and forecast_percent >= 100,
            "note": "Budget alerts at 50, 80 and 100 % actual and 100 % forecast are configured in Azure (Bicep)."}


def cost_overview(store: CaseStore, settings: Any, prices: PriceTable, now: _dt.datetime, *,
                  platform: dict[str, Any] | None = None) -> dict[str, Any]:
    """Data of the cost view (/costs, GET /api/costs)."""
    now = parse_utc(now)
    platform = platform if platform is not None else platform_costs(settings, now)
    models_all = model_costs(store)
    models_month = model_costs(store, month=now.strftime("%Y-%m"))
    cases = store.list_cases()
    per_case = [case_costs(store, c.case_id, platform=platform, case_count=len(cases)) | {"case_key": c.case_key}
                for c in cases]
    platform_mtd = platform["month_to_date"]["total_usd"] if platform.get("available") else 0.0
    forecast = (platform.get("forecast_next_month") or {}).get("total_usd") if platform.get("available") else None
    spent = float(Decimal(str(platform_mtd)) + Decimal(str(models_month["total_usd"])))
    return {
        "generated_at": iso_utc(now),
        "currency": "USD",
        "price_table": prices.describe(),
        "platform": platform,
        "models_month_to_date": models_month,
        "models_all_time": models_all,
        "budget": budget_status(settings.budget_usd, spent, forecast),
        "cases": per_case,
        "notes": [
            "Model costs = measured token usage x dated price table; platform costs from Azure Cost Management.",
            "Forecasts and projections are estimates with their source.",
            "Cost values are separate from safety and trend views and are no statement about products (K-19).",
        ],
    }
