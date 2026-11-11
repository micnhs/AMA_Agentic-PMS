"""Microsoft Agent Framework + Foundry Agent Service adapter (ADR-001 option A, ADR-002; P2_NOTES).

Azure only. Every Agent Framework / Azure SDK import happens lazily inside this module's
functions, so offline code never needs these packages. All MAF calls are isolated here and read
results defensively (clear error messages when an attribute is missing).

Flow per agent run:

1. Build an ``agent_framework.Agent`` with ``FoundryChatClient`` (Responses API, model deployment of
   the agent's role), the versioned prompt as instructions, the agent's read-only tools (local
   callables that go through :class:`pms.agents.tools.ToolBox`: validated, limited, audited) and
   ``default_options`` with reasoning effort and the JSON response schema.
2. Publish it as a Foundry prompt agent only when its specification changed:
   ``project.agents.create_version(agent_name=..., definition=to_prompt_agent(agent))``. The fingerprint
   (:func:`spec_fingerprint`: prompt text hash and version, model deployment, tools, response schema,
   reasoning effort, project endpoint) and the published version are stored per agent in a registry -
   Cosmos DB ``records`` in Azure (:class:`pms.azure.cosmos_store.CosmosAgentRegistry`), so restarts and
   other processes reuse the version; in memory offline.
3. Run the published agent with ``FoundryAgent(project_client=..., agent_name=..., agent_version=...,
   tools=[...])``; read text/value and token usage defensively; at most 10 minutes per run and up to 3
   attempts with exponential backoff for transient errors.

``publish_all`` (CLI ``publish-agents``, azd ``postdeploy``, worker start in Azure) publishes the agents whose
fingerprint changed and reports the others as unchanged.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import logging
import threading
import time
from typing import Any, Awaitable, Callable, Protocol

from ..audit import canonical_json
from ..models import iso_utc, utcnow
from .base import AgentError, AgentInput, AgentResult, AgentRuntime, AgentSpec, TokenUsage, parse_json_text
from .specs import AGENTS, PIPELINE_ORDER, Prompt, load_prompt, model_version_of
from .tools import TOOL_SPECS, ToolBox

log = logging.getLogger("pms.agents.foundry")

TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_TRANSIENT_NAMES = ("Timeout", "RateLimit", "ServiceUnavailable", "ServiceRequest", "Connection", "Throttl")
_UNSUPPORTED_STRICT_KEYS = ("maxLength", "maxItems", "minItems", "minimum", "maximum", "pattern")


class FoundryAdapterError(AgentError):
    """The Agent Framework / Foundry API did not look as expected (version mismatch, missing package)."""


# --------------------------------------------------------------------------- defensive helpers
def _import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise FoundryAdapterError(
            f"{name} is not installed - the Foundry backend needs requirements.txt (agent-framework-core and "
            f"agent-framework-foundry 1.25.0, azure-identity); use AGENT_BACKEND=offline locally") from exc


def _attr(obj: Any, *names: str, what: str | None = None) -> Any:
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return value
    raise FoundryAdapterError(f"{what or type(obj).__name__} has none of the attributes {', '.join(names)} - "
                              "check the agent-framework / azure-ai-projects versions (P2_NOTES)")


def _first(obj: Any, *names: str) -> Any:
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return value
    return None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _run_sync(factory: Callable[[], Awaitable[Any]], timeout: float) -> Any:
    """Run a coroutine from synchronous code (also when an event loop is already running)."""
    async def guarded() -> Any:
        return await asyncio.wait_for(factory(), timeout=timeout)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(guarded())
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(guarded())
        except BaseException as exc:  # re-raised in the caller thread
            box["error"] = exc

    thread = threading.Thread(target=target, name="foundry-run", daemon=True)
    thread.start()
    thread.join(timeout + 5)
    if "error" in box:
        raise box["error"]
    if "value" not in box:
        raise TimeoutError(f"agent run exceeded {timeout} s")
    return box["value"]


def is_transient(exc: BaseException) -> bool:
    status = _first(exc, "status_code", "status")
    if isinstance(status, int) and status in TRANSIENT_STATUS:
        return True
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    return any(part in type(exc).__name__ for part in _TRANSIENT_NAMES)


def strict_schema(schema: Any) -> Any:
    """Response-format variant of an output schema (keywords strict mode may not support removed)."""
    if isinstance(schema, dict):
        return {k: strict_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED_STRICT_KEYS}
    if isinstance(schema, list):
        return [strict_schema(v) for v in schema]
    return schema


def response_format(spec: AgentSpec) -> dict[str, Any]:
    return {"type": "json_schema", "json_schema": {"name": spec.foundry_name.replace("-", "_"),
                                                   "schema": strict_schema(spec.output_schema), "strict": True}}


def read_usage(response: Any) -> TokenUsage:
    """Token usage of a MAF response (input, cached input, output incl. reasoning) - read defensively."""
    usage = _first(response, "usage_details", "usage")
    if usage is None:
        raw = _first(response, "raw_representation")
        usage = _first(raw, "usage") if raw is not None else None
    if usage is None:
        return TokenUsage(reported=False)

    def num(*names: str) -> int:
        value = _first(usage, *names)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    extra = _first(usage, "additional_counts") or {}
    in_details = _first(usage, "input_token_details", "input_tokens_details", "prompt_tokens_details")
    out_details = _first(usage, "output_token_details", "output_tokens_details", "completion_tokens_details")
    cached = num("cached_input_token_count", "cached_input_tokens", "cached_tokens") or (
        int(_first(in_details, "cached_tokens") or 0) if in_details is not None else 0) or int(
        extra.get("openai.cached_input_tokens", 0) if isinstance(extra, dict) else 0)
    reasoning = num("reasoning_token_count", "reasoning_tokens") or (
        int(_first(out_details, "reasoning_tokens") or 0) if out_details is not None else 0) or int(
        extra.get("openai.reasoning_tokens", 0) if isinstance(extra, dict) else 0)
    return TokenUsage(input_tokens=num("input_token_count", "input_tokens", "prompt_tokens"),
                      cached_input_tokens=cached,
                      output_tokens=num("output_token_count", "output_tokens", "completion_tokens"),
                      reasoning_tokens=reasoning, reported=True)


def read_output(response: Any) -> dict[str, Any]:
    value = _first(response, "value")
    if isinstance(value, dict):
        return value
    if value is not None and hasattr(value, "model_dump"):
        return value.model_dump()
    text = _first(response, "text")
    if not text:
        messages = _first(response, "messages") or []
        text = "\n".join(str(_first(m, "text") or "") for m in messages)
    if not text:
        raise FoundryAdapterError("the agent response has neither value nor text")
    data = parse_json_text(str(text))
    if not isinstance(data, dict):
        raise AgentError("the agent answer is not a JSON object")
    return data


def _annotation(param: Any, pydantic_field: Any) -> Any:
    from typing import Annotated, Optional

    base: Any = {"int": int, "list": list[str]}.get(param.kind, str)
    meta = pydantic_field(description=param.description) if pydantic_field is not None else param.description
    return Annotated[Optional[base], meta]


def build_maf_tools(af: Any, names: tuple[str, ...], caller: Callable[[str, dict[str, Any]], dict[str, Any]]) -> list:
    """Local read-only callables for MAF; every call goes through ``caller`` (ToolBox.call)."""
    decorator = _first(af, "tool", "ai_function")
    if decorator is None:
        raise FoundryAdapterError("agent_framework.tool not found (agent-framework-core 1.25.0 expected)")
    try:
        from pydantic import Field as pydantic_field  # type: ignore[import-not-found]
    except ImportError:
        pydantic_field = None
    def make(tool_name: str) -> Callable[..., str]:
        def fn(**kwargs: Any) -> str:
            args = {k: v for k, v in kwargs.items() if v is not None}
            return json.dumps(caller(tool_name, args), ensure_ascii=False, default=str)[:20000]
        return fn

    tools = []
    for name in names:
        spec = TOOL_SPECS[name]
        params = [inspect.Parameter(p, inspect.Parameter.KEYWORD_ONLY, default=None,
                                    annotation=_annotation(param, pydantic_field)) for p, param in spec.params.items()]
        fn = make(name)
        fn.__name__ = name
        fn.__qualname__ = name
        fn.__doc__ = spec.description
        fn.__signature__ = inspect.Signature(params, return_annotation=str)  # type: ignore[attr-defined]
        fn.__annotations__ = {p.name: p.annotation for p in params} | {"return": str}
        decorated = None
        for kwargs in ({"name": name, "description": spec.description, "approval_mode": "never_require"},
                       {"approval_mode": "never_require"}):
            try:
                decorated = decorator(**kwargs)(fn)
                break
            except TypeError:
                continue
        if decorated is None:
            decorated = decorator(fn)
        tools.append(decorated)
    return tools


# --------------------------------------------------------------------------- published versions
class AgentRegistry(Protocol):
    """Published Foundry prompt-agent version per agent name (fingerprint, version, prompt, model)."""

    def get(self, foundry_name: str) -> dict[str, Any] | None: ...
    def put(self, foundry_name: str, record: dict[str, Any]) -> None: ...
    def describe(self) -> str: ...


class MemoryAgentRegistry:
    """Registry for one process (offline and tests)."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, foundry_name: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(foundry_name)
            return dict(record) if record is not None else None

    def put(self, foundry_name: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._records[foundry_name] = dict(record)

    def describe(self) -> str:
        return "in memory (this process only)"


def spec_fingerprint(settings: Any, spec: AgentSpec, prompt: Prompt) -> tuple[str, dict[str, Any]]:
    """SHA-256 over everything that goes into the published prompt agent; a new version only when it changes."""
    material = {
        "foundry_name": spec.foundry_name, "agent_version": spec.agent_version,
        "description": f"{spec.description} [prompt {prompt.prompt_version}]",
        "prompt_version": prompt.prompt_version, "prompt_sha256": prompt.sha256,
        "model": settings.model_for(spec.model_role), "reasoning_effort": spec.reasoning_effort,
        "response_format": response_format(spec),
        "tools": [{"name": n, "description": TOOL_SPECS[n].description, "parameters": TOOL_SPECS[n].json_schema()}
                  for n in spec.tools],
        "endpoint": settings.foundry_project_endpoint,
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest(), material


# --------------------------------------------------------------------------- runtime
class FoundryRuntime(AgentRuntime):
    backend = "foundry"

    def __init__(self, settings: Any, *, credential_factory: Callable[[], Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = 3, backoff_s: float = 2.0,
                 registry: AgentRegistry | None = None) -> None:
        self.settings = settings
        self._credential_factory = credential_factory
        self._sleep = sleep
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self._published: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self._agent_locks: dict[str, threading.Lock] = {}
        self._registry = registry
        self.tracing_status: str | None = None

    @property
    def registry(self) -> AgentRegistry:
        if self._registry is None:
            from .. import runtime as rt

            self._registry = rt.get_agent_registry(self.settings)
        return self._registry

    def _agent_lock(self, foundry_name: str) -> threading.Lock:
        with self._lock:
            return self._agent_locks.setdefault(foundry_name, threading.Lock())

    def describe(self) -> dict[str, Any]:
        registry = self._registry.describe() if self._registry is not None else "not used yet"
        return {"backend": self.backend, "endpoint_configured": bool(self.settings.foundry_project_endpoint),
                "published": {k[0]: v for k, v in self._published.items()}, "registry": registry,
                "tracing": self.tracing_status}

    # ------------------------------------------------------------------ building blocks
    async def _credential(self) -> Any:
        if self._credential_factory is not None:
            return self._credential_factory()
        identity = _import("azure.identity.aio")
        if self.settings.azure_client_id:
            return _attr(identity, "ManagedIdentityCredential")(client_id=self.settings.azure_client_id)
        return _attr(identity, "DefaultAzureCredential")()

    def _make_agent(self, af: Any, aff: Any, spec: AgentSpec, prompt: Prompt, tool_fns: list,
                    credential: Any) -> tuple[Any, Any]:
        if not self.settings.foundry_project_endpoint:
            raise FoundryAdapterError("FOUNDRY_PROJECT_ENDPOINT is not set")
        model = self.settings.model_for(spec.model_role)
        client = _attr(aff, "FoundryChatClient")(project_endpoint=self.settings.foundry_project_endpoint,
                                                 model=model, credential=credential)
        config = _first(client, "function_invocation_configuration")
        if config is not None and hasattr(config, "max_iterations"):
            try:
                config.max_iterations = spec.max_rounds + 1  # tool rounds plus the final answer
            except Exception:  # read-only in some versions; the ToolBox limits still apply
                pass
        agent = _attr(af, "Agent", "ChatAgent")(
            client=client, name=spec.foundry_name,
            description=f"{spec.description} [prompt {prompt.prompt_version}]", instructions=prompt.text,
            tools=tool_fns,
            default_options={"reasoning": {"effort": spec.reasoning_effort}, "response_format": response_format(spec)})
        return agent, client

    async def _configure_tracing(self, *objects: Any) -> str:
        if self.tracing_status is not None:
            return self.tracing_status
        if not self.settings.applicationinsights_connection_string:
            self.tracing_status = "not configured (no Application Insights connection string)"
            return self.tracing_status
        for obj in objects:
            configure = _first(obj, "configure_azure_monitor")
            if configure is not None:
                try:
                    await _maybe_await(configure())
                    self.tracing_status = "Foundry tracing via configure_azure_monitor"
                except Exception as exc:  # tracing never blocks an analysis
                    self.tracing_status = f"tracing not configured: {type(exc).__name__}: {exc}"
                return self.tracing_status
        self.tracing_status = "configure_azure_monitor not available in this agent-framework version"
        return self.tracing_status

    async def _create_version(self, project: Any, aff: Any, agent: Any, spec: AgentSpec, prompt: Prompt) -> str:
        definition = _attr(aff, "to_prompt_agent")(agent)
        create = _attr(_attr(project, "agents", what="AIProjectClient"), "create_version", what="project.agents")
        try:
            result = await _maybe_await(create(agent_name=spec.foundry_name, definition=definition,
                                               description=f"{spec.name} - {prompt.prompt_version}"))
        except TypeError:
            result = await _maybe_await(create(agent_name=spec.foundry_name, definition=definition))
        version = _first(result, "version", "agent_version", "id")
        if version is None:
            raise FoundryAdapterError("create_version returned no version")
        return str(version)

    def _stored_version(self, spec: AgentSpec, fingerprint: str) -> str | None:
        try:
            stored = self.registry.get(spec.foundry_name)
        except Exception as exc:  # the registry is an optimisation: publishing still works without it
            log.warning("could not read the published version of %s: %s: %s", spec.foundry_name,
                        type(exc).__name__, exc)
            return None
        if stored and stored.get("spec_sha256") == fingerprint and stored.get("foundry_version"):
            return str(stored["foundry_version"])
        return None

    def _record_version(self, spec: AgentSpec, prompt: Prompt, fingerprint: str, version: str,
                        previous: str | None) -> str | None:
        record = {"foundry_name": spec.foundry_name, "foundry_version": version, "spec_sha256": fingerprint,
                  "prompt_version": prompt.prompt_version, "prompt_sha256": prompt.sha256,
                  "agent_version": spec.agent_version, "model": self.settings.model_for(spec.model_role),
                  "endpoint": self.settings.foundry_project_endpoint, "previous_version": previous,
                  "published_at": iso_utc(utcnow(), ms=True)}
        try:
            self.registry.put(spec.foundry_name, record)
            return None
        except Exception as exc:  # e.g. a developer sign-in without Cosmos DB write access (postdeploy)
            status = _first(exc, "status_code", "status")
            text = f"{type(exc).__name__}{f' (HTTP {status})' if status else ''}: {exc}"[:300]
            log.warning("published %s version %s but could not record it: %s", spec.foundry_name, version, text)
            return text

    async def _ensure_published(self, project: Any, aff: Any, agent: Any, spec: AgentSpec, prompt: Prompt,
                                *, force: bool = False) -> tuple[str, str, str | None]:
        """(version, action, registry error): reuse the version recorded for this fingerprint, else publish."""
        fingerprint, _ = spec_fingerprint(self.settings, spec, prompt)
        key = (spec.foundry_name, fingerprint)
        with self._agent_lock(spec.foundry_name):
            with self._lock:
                cached = None if force else self._published.get(key)
            if cached is not None:
                return cached, "unchanged", None
            stored = None if force else self._stored_version(spec, fingerprint)
            if stored is not None:
                with self._lock:
                    self._published[key] = stored
                return stored, "unchanged", None
            try:
                previous = (self.registry.get(spec.foundry_name) or {}).get("foundry_version")
            except Exception:
                previous = None
            version = await self._create_version(project, aff, agent, spec, prompt)
            error = self._record_version(spec, prompt, fingerprint, version, previous)
            with self._lock:
                self._published[key] = version
            return version, "published", error

    def forget_published(self, spec: AgentSpec) -> None:
        """Drop the cached version of one agent (e.g. it was deleted in the Foundry portal)."""
        with self._lock:
            for key in [k for k in self._published if k[0] == spec.foundry_name]:
                del self._published[key]

    @staticmethod
    def _foundry_agent(aff: Any, project: Any, name: str, version: str, tool_fns: list) -> Any:
        cls = _attr(aff, "FoundryAgent")
        variants = (
            {"project_client": project, "agent_name": name, "agent_version": version, "tools": tool_fns},
            {"project_client": project, "name": name, "version": version, "tools": tool_fns},
            {"client": project, "agent_name": name, "agent_version": version, "tools": tool_fns},
        )
        errors = []
        for kwargs in variants:
            try:
                return cls(**kwargs)
            except TypeError as exc:
                errors.append(str(exc))
        raise FoundryAdapterError("FoundryAgent signature not recognised: " + " | ".join(errors))

    # ------------------------------------------------------------------ run
    async def _run_async(self, spec: AgentSpec, prompt: Prompt, agent_input: AgentInput,
                         tools: ToolBox) -> tuple[dict[str, Any], TokenUsage, dict[str, Any]]:
        af = _import("agent_framework")
        aff = _import("agent_framework.foundry")
        projects = _import("azure.ai.projects.aio")
        credential = await self._credential()
        try:
            tool_fns = build_maf_tools(af, spec.tools, tools.call)
            agent, client = self._make_agent(af, aff, spec, prompt, tool_fns, credential)
            tracing = await self._configure_tracing(agent, client)
            async with _attr(projects, "AIProjectClient")(endpoint=self.settings.foundry_project_endpoint,
                                                          credential=credential) as project:
                version, _action, _error = await self._ensure_published(project, aff, agent, spec, prompt)
                foundry_agent = self._foundry_agent(aff, project, spec.foundry_name, version, tool_fns)
                try:
                    response = await _maybe_await(_attr(foundry_agent, "run")(agent_input.render()))
                except Exception as exc:
                    if _first(exc, "status_code", "status") != 404:
                        raise
                    # the recorded version no longer exists (deleted in the portal): publish once more
                    log.warning("Foundry agent %s version %s not found - publishing a new version",
                                spec.foundry_name, version)
                    self.forget_published(spec)
                    version, _action, _error = await self._ensure_published(project, aff, agent, spec, prompt,
                                                                            force=True)
                    foundry_agent = self._foundry_agent(aff, project, spec.foundry_name, version, tool_fns)
                    response = await _maybe_await(_attr(foundry_agent, "run")(agent_input.render()))
            output = read_output(response)
            details = {"foundry_agent": spec.foundry_name, "foundry_agent_version": version,
                       "response_id": _first(response, "response_id", "id"),
                       "model_id": _first(response, "model_id", "model"), "tracing": tracing}
            return output, read_usage(response), details
        finally:
            close = _first(credential, "close")
            if close is not None:
                try:
                    await _maybe_await(close())
                except Exception:
                    pass

    def run(self, spec: AgentSpec, agent_input: AgentInput, tools: ToolBox) -> AgentResult:
        started = utcnow()
        t0 = time.perf_counter()
        prompt = load_prompt(spec)
        model = self.settings.model_for(spec.model_role)
        common = dict(code=spec.code, agent_id=spec.agent_id, backend=self.backend, model=model,
                      model_version=model_version_of(model), prompt_version=prompt.prompt_version,
                      agent_version=spec.agent_version, priced_model=model)
        error: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                output, usage, details = _run_sync(lambda: self._run_async(spec, prompt, agent_input, tools),
                                                   timeout=spec.timeout_s)
                return AgentResult(status="DONE", output=output, usage=usage, tool_calls=list(tools.calls),
                                   started_at=started, finished_at=utcnow(),
                                   duration_ms=(time.perf_counter() - t0) * 1000,
                                   details={**details, "attempts": attempt}, **common)
            except Exception as exc:
                error = exc
                if attempt >= self.max_attempts or not is_transient(exc):
                    break
                self._sleep(self.backoff_s * 2 ** (attempt - 1))
        return AgentResult(status="FAILED", output=None, usage=TokenUsage(reported=False),
                           tool_calls=list(tools.calls), started_at=started, finished_at=utcnow(),
                           duration_ms=(time.perf_counter() - t0) * 1000,
                           error=f"{type(error).__name__}: {error}", details={"attempts": attempt}, **common)

    # ------------------------------------------------------------------ explicit publishing (CLI publish-agents)
    def publish_all(self, codes: tuple[str, ...] = PIPELINE_ORDER, *, force: bool = False) -> list[dict[str, Any]]:
        """Publish the agents whose fingerprint changed (``force``: all); unchanged ones keep their version."""
        return _run_sync(lambda: self._publish_all_async(codes, force=force), timeout=600)

    async def _publish_all_async(self, codes: tuple[str, ...], *, force: bool = False) -> list[dict[str, Any]]:
        af = _import("agent_framework")
        aff = _import("agent_framework.foundry")
        projects = _import("azure.ai.projects.aio")
        credential = await self._credential()

        def refuse(name: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"error": "publishing only - tools run inside an analysis"}

        results = []
        try:
            async with _attr(projects, "AIProjectClient")(endpoint=self.settings.foundry_project_endpoint,
                                                          credential=credential) as project:
                for code in codes:
                    spec = AGENTS[code]
                    prompt = load_prompt(spec)
                    agent, _client = self._make_agent(af, aff, spec, prompt, build_maf_tools(af, spec.tools, refuse),
                                                      credential)
                    version, action, error = await self._ensure_published(project, aff, agent, spec, prompt,
                                                                          force=force)
                    entry = {"agent": code, "foundry_name": spec.foundry_name, "foundry_version": version,
                             "action": action, "spec_sha256": spec_fingerprint(self.settings, spec, prompt)[0],
                             "prompt_version": prompt.prompt_version, "model": self.settings.model_for(spec.model_role),
                             "tools": list(spec.tools)}
                    if error:
                        entry["registry_error"] = error
                    results.append(entry)
        finally:
            close = _first(credential, "close")
            if close is not None:
                await _maybe_await(close())
        return results
