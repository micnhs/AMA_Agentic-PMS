"""Runtime configuration from environment variables (BUILD_SPEC 4.2).

Nothing here is a secret: managed identities and Entra tokens are used in Azure, and the
offline mode needs no credentials at all. The app refuses to start when
``APP_ENV=azure`` and ``LOCAL_TEST_USERS=true`` (concept 7.3, ADR-005, Goal Card check 5).
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .versions import REPO_ROOT, default_data_dir

APP_ENVS: tuple[str, ...] = ("local", "azure")
AGENT_BACKENDS: tuple[str, ...] = ("offline", "foundry")
WORKER_MODES: tuple[str, ...] = ("inprocess", "separate")

DEFAULT_MODEL_ANALYSIS = "gpt-6-astra"  # agents A3, A6 (ADR-002, decision Q-10)
DEFAULT_MODEL_GENERAL = "gpt-6.1-sol"  # agents A1, A2, A4
DEFAULT_BUDGET_USD = 250.0
DEFAULT_PORT = 8000

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(RuntimeError):
    """Invalid or unsafe configuration: the process must not start."""


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _text(env: Mapping[str, str], name: str) -> str | None:
    value = (env.get(name) or "").strip()
    return value or None


def _choice(env: Mapping[str, str], name: str, allowed: tuple[str, ...], default: str) -> str:
    value = (env.get(name) or default).strip().lower()
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
    return value


@dataclass(frozen=True)
class Settings:
    """Typed view of the environment. Build it with :meth:`from_env`."""

    app_env: str = "local"
    agent_backend: str = "offline"
    local_test_users: bool = True
    data_dir: Path = Path("var")
    port: int = DEFAULT_PORT
    worker_mode: str = "inprocess"
    foundry_project_endpoint: str | None = None
    model_deployment_analysis: str = DEFAULT_MODEL_ANALYSIS
    model_deployment_general: str = DEFAULT_MODEL_GENERAL
    azure_search_endpoint: str | None = None
    azure_search_index: str | None = None
    cosmos_endpoint: str | None = None
    cosmos_database: str | None = None
    storage_account_url: str | None = None
    acs_endpoint: str | None = None
    acs_sender_address: str | None = None
    alert_recipients: tuple[str, ...] = ()
    azure_client_id: str | None = None
    applicationinsights_connection_string: str | None = None
    cost_scope: str | None = None
    budget_usd: float = DEFAULT_BUDGET_USD
    entra_tenant_id: str | None = None
    entra_app_client_id: str | None = None
    preview_monitoring: bool = False
    public_base_url: str | None = None  # optional, only used for links in alert messages
    synthetic_data_dir: Path = default_data_dir()
    pricing_path: Path = REPO_ROOT / "config" / "pricing.json"

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        app_env = _choice(env, "APP_ENV", APP_ENVS, "local")
        azure = app_env == "azure"
        backend = _choice(env, "AGENT_BACKEND", AGENT_BACKENDS, "foundry" if azure else "offline")
        worker_mode = _choice(env, "WORKER_MODE", WORKER_MODES, "separate" if azure else "inprocess")
        try:
            port = int(env.get("PORT") or DEFAULT_PORT)
            budget = float(env.get("BUDGET_USD") or DEFAULT_BUDGET_USD)
        except ValueError as exc:
            raise ConfigError(f"PORT and BUDGET_USD must be numbers: {exc}") from exc
        recipients = tuple(r.strip() for r in re.split(r"[,;]", env.get("ALERT_RECIPIENTS") or "") if r.strip())
        analysis = _text(env, "MODEL_DEPLOYMENT_ANALYSIS") or DEFAULT_MODEL_ANALYSIS
        general = _text(env, "MODEL_DEPLOYMENT_GENERAL") or DEFAULT_MODEL_GENERAL
        if _flag(env, "MODEL_FALLBACK_ALL_GENERAL", False):
            analysis = general  # documented fallback: gpt-6.1-sol for all agents (ADR-002)
        return cls(
            app_env=app_env,
            agent_backend=backend,
            local_test_users=_flag(env, "LOCAL_TEST_USERS", not azure),
            data_dir=Path(env.get("DATA_DIR") or "var").expanduser().resolve(),
            port=port,
            worker_mode=worker_mode,
            foundry_project_endpoint=_text(env, "FOUNDRY_PROJECT_ENDPOINT"),
            model_deployment_analysis=analysis,
            model_deployment_general=general,
            azure_search_endpoint=_text(env, "AZURE_SEARCH_ENDPOINT"),
            azure_search_index=_text(env, "AZURE_SEARCH_INDEX"),
            cosmos_endpoint=_text(env, "COSMOS_ENDPOINT"),
            cosmos_database=_text(env, "COSMOS_DATABASE"),
            storage_account_url=_text(env, "STORAGE_ACCOUNT_URL"),
            acs_endpoint=_text(env, "ACS_ENDPOINT"),
            acs_sender_address=_text(env, "ACS_SENDER_ADDRESS"),
            alert_recipients=recipients,
            azure_client_id=_text(env, "AZURE_CLIENT_ID"),
            applicationinsights_connection_string=_text(env, "APPLICATIONINSIGHTS_CONNECTION_STRING"),
            cost_scope=_text(env, "COST_SCOPE"),
            budget_usd=budget,
            entra_tenant_id=_text(env, "ENTRA_TENANT_ID"),
            entra_app_client_id=_text(env, "ENTRA_APP_CLIENT_ID"),
            preview_monitoring=_flag(env, "PREVIEW_MONITORING", False),
            public_base_url=_text(env, "PUBLIC_BASE_URL"),
        )

    def with_overrides(self, **values: Any) -> "Settings":
        return replace(self, **values)

    # ------------------------------------------------------------------ checks
    @property
    def is_azure(self) -> bool:
        return self.app_env == "azure"

    def validate_startup(self) -> list[str]:
        """Raise :class:`ConfigError` for unsafe settings; return non-fatal warnings."""
        if self.is_azure and self.local_test_users:
            raise ConfigError(
                "Refusing to start: APP_ENV=azure with LOCAL_TEST_USERS=true. Local test users exist only in the "
                "offline mode; in Azure users sign in with Microsoft Entra ID (concept 7.3, ADR-005).")
        bad = [r for r in self.alert_recipients if not _EMAIL_RE.match(r)]
        if bad:
            raise ConfigError(f"ALERT_RECIPIENTS contains invalid addresses: {', '.join(bad)}")
        warnings: list[str] = []
        if self.agent_backend == "foundry" and not self.foundry_project_endpoint:
            warnings.append("AGENT_BACKEND=foundry but FOUNDRY_PROJECT_ENDPOINT is empty: agent runs will fail visibly.")
        if self.is_azure and self.agent_backend == "offline":
            warnings.append("APP_ENV=azure with AGENT_BACKEND=offline: the deterministic stand-in answers.")
        if self.is_azure and not (self.acs_endpoint and self.acs_sender_address and self.alert_recipients):
            warnings.append("Alert e-mail is not fully configured (ACS_ENDPOINT, ACS_SENDER_ADDRESS, ALERT_RECIPIENTS): "
                            "alerts are written to the outbox folder instead.")
        return warnings

    # ------------------------------------------------------------------ derived paths and models
    @property
    def db_path(self) -> Path:
        return self.data_dir / "pms.sqlite"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def trace_path(self) -> Path:
        return self.data_dir / "traces" / "agent-spans.jsonl"

    @property
    def golden_summary_path(self) -> Path:
        return self.data_dir / "golden-summary.json"

    def model_for(self, model_role: str) -> str:
        """Deployment name for an agent's model role ("analysis" or "general")."""
        if model_role == "analysis":
            return self.model_deployment_analysis
        if model_role == "general":
            return self.model_deployment_general
        raise ValueError(f"unknown model role {model_role!r}")

    def public_dict(self) -> dict[str, Any]:
        """Settings for views and /healthz - connection strings are reduced to 'configured'."""
        data = asdict(self)
        for key in ("data_dir", "synthetic_data_dir", "pricing_path"):
            data[key] = str(data[key])
        data["alert_recipients"] = list(self.alert_recipients)
        data["applicationinsights_connection_string"] = (
            "configured" if self.applicationinsights_connection_string else None)
        return data


def load_settings(env: Mapping[str, str] | None = None, *, validate: bool = True) -> Settings:
    """Settings from the environment; with ``validate`` the startup refusal rules apply."""
    settings = Settings.from_env(env)
    if validate:
        settings.validate_startup()
    return settings


def local_test_settings(data_dir: str | Path, **overrides: Any) -> Settings:
    """Offline settings for tests and the golden run (never Azure)."""
    base = Settings(app_env="local", agent_backend="offline", local_test_users=True,
                    data_dir=Path(data_dir).resolve(), worker_mode="inprocess")
    return replace(base, **overrides)
