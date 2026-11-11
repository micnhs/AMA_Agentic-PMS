"""Idempotent Foundry prompt-agent publishing: a new version only when the agent specification changed; the
published versions are kept per agent (Cosmos DB records in Azure) and reused after a restart. FAKE Agent
Framework, Foundry project client and Cosmos DB (no network, no real package, no live model)."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from pms import runtime as rt
from pms.agents import foundry
from pms.agents.base import AgentInput
from pms.agents.foundry import FoundryRuntime, MemoryAgentRegistry, spec_fingerprint
from pms.agents.specs import AGENTS, PIPELINE_ORDER, load_prompt
from pms.agents.tools import ToolBox
from pms.config import Settings

from ._pipeline_support import cleanup_runs, ingest, new_pipeline
from .test_azure_fakes import FakeAzureTestCase, FakeCosmosAccount, cosmos_account_for

ENDPOINT = "https://fake-account.services.ai.azure.com/api/projects/proj-pms-demo"


def tearDownModule() -> None:
    cleanup_runs()


class Foundry:
    """Recorder of the fake Foundry project."""

    def __init__(self) -> None:
        self.versions: list[dict] = []
        self.runs: list[tuple[str, str]] = []
        self.run_errors: list[BaseException] = []


FOUNDRY = Foundry()


def agent_framework_modules() -> dict[str, types.ModuleType]:
    af = types.ModuleType("agent_framework")
    aff = types.ModuleType("agent_framework.foundry")
    ident = types.ModuleType("azure.identity.aio")
    projects = types.ModuleType("azure.ai.projects.aio")

    def tool(**kwargs):
        return lambda fn: fn

    class FoundryChatClient:
        def __init__(self, *, project_endpoint, model, credential):
            self.model = model

    class Agent:
        def __init__(self, *, client, name, description, instructions, tools, default_options):
            self.client, self.name, self.instructions = client, name, instructions

    def to_prompt_agent(agent):
        return {"name": agent.name, "model": agent.client.model, "instructions": agent.instructions}

    class FoundryAgent:
        def __init__(self, *, project_client, agent_name, agent_version, tools):
            self.agent_name, self.agent_version = agent_name, agent_version

        async def run(self, message):
            if FOUNDRY.run_errors:
                raise FOUNDRY.run_errors.pop(0)
            FOUNDRY.runs.append((self.agent_name, self.agent_version))
            return SimpleNamespace(text=json.dumps({"ok": True}), usage_details=None)

    class Agents:
        async def create_version(self, *, agent_name, definition, description=None):
            FOUNDRY.versions.append({"agent_name": agent_name, "definition": definition})
            return SimpleNamespace(version=str(sum(1 for v in FOUNDRY.versions if v["agent_name"] == agent_name)))

    class AIProjectClient:
        def __init__(self, *, endpoint, credential):
            self.agents = Agents()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Credential:
        def __init__(self, client_id=None):
            pass

        async def close(self):
            pass

    af.tool, af.Agent = tool, Agent
    aff.FoundryChatClient, aff.to_prompt_agent, aff.FoundryAgent = FoundryChatClient, to_prompt_agent, FoundryAgent
    ident.ManagedIdentityCredential = ident.DefaultAzureCredential = Credential
    projects.AIProjectClient = AIProjectClient
    return {"agent_framework": af, "agent_framework.foundry": aff, "azure.identity.aio": ident,
            "azure.ai": types.ModuleType("azure.ai"), "azure.ai.projects": types.ModuleType("azure.ai.projects"),
            "azure.ai.projects.aio": projects}


def settings(**overrides) -> Settings:
    base = Settings(app_env="local", agent_backend="foundry", foundry_project_endpoint=ENDPOINT)
    return dataclasses.replace(base, **overrides)


class Publishing(FakeAzureTestCase):
    def setUp(self) -> None:
        super().setUp()
        FOUNDRY.__init__()
        patcher = mock.patch.dict(sys.modules, agent_framework_modules())
        patcher.start()
        self.addCleanup(patcher.stop)

    def runtime(self, registry, **overrides) -> FoundryRuntime:
        return FoundryRuntime(settings(**overrides), registry=registry, sleep=lambda s: None)

    def test_publishing_twice_creates_each_version_once(self) -> None:
        registry = MemoryAgentRegistry()
        first = self.runtime(registry).publish_all()
        self.assertEqual([(p["agent"], p["action"], p["foundry_version"]) for p in first],
                         [(code, "published", "1") for code in PIPELINE_ORDER])
        second = self.runtime(registry).publish_all()
        self.assertEqual({p["action"] for p in second}, {"unchanged"})
        self.assertEqual([p["spec_sha256"] for p in second], [p["spec_sha256"] for p in first])
        self.assertEqual(len(FOUNDRY.versions), 5)
        forced = self.runtime(registry).publish_all(force=True)
        self.assertEqual({p["foundry_version"] for p in forced}, {"2"})

    def test_only_changed_specifications_are_published_again(self) -> None:
        registry = MemoryAgentRegistry()
        self.runtime(registry).publish_all()
        original = foundry.load_prompt

        def changed(spec):
            prompt = original(spec)
            return dataclasses.replace(prompt, text=prompt.text + "\nNew rule.", sha256="0" * 64) if (
                spec.code == "A3") else prompt

        with mock.patch.object(foundry, "load_prompt", changed):
            result = {p["agent"]: (p["action"], p["foundry_version"]) for p in self.runtime(registry).publish_all()}
        self.assertEqual(result["A3"], ("published", "2"))
        self.assertEqual({k: v for k, v in result.items() if k != "A3"},
                         {code: ("unchanged", "1") for code in PIPELINE_ORDER if code != "A3"})
        moved = {p["agent"]: p["action"] for p in self.runtime(
            registry, model_deployment_general="gpt-6.1-sol-eu2").publish_all()}
        self.assertEqual(moved, {"A1": "published", "A2": "published", "A4": "published", "A6": "unchanged",
                                 "A3": "published"})  # A3: the prompt of the registry is the changed one
        self.assertEqual(registry.get("pms-a1-signal-ingestion")["model"], "gpt-6.1-sol-eu2")

    def test_fingerprint_covers_prompt_model_tools_schema_and_endpoint(self) -> None:
        spec, prompt = AGENTS["A6"], load_prompt(AGENTS["A6"])
        base, material = spec_fingerprint(settings(), spec, prompt)
        self.assertEqual(material["prompt_sha256"], prompt.sha256)
        self.assertEqual([t["name"] for t in material["tools"]], list(spec.tools))
        self.assertTrue(material["response_format"]["json_schema"]["strict"])
        for other in (spec_fingerprint(settings(model_deployment_analysis="other"), spec, prompt)[0],
                      spec_fingerprint(settings(foundry_project_endpoint=ENDPOINT + "2"), spec, prompt)[0],
                      spec_fingerprint(settings(), dataclasses.replace(spec, tools=spec.tools[:1]), prompt)[0],
                      spec_fingerprint(settings(), spec, dataclasses.replace(prompt, sha256="1" * 64))[0]):
            self.assertNotEqual(other, base)
        self.assertEqual(spec_fingerprint(settings(), spec, prompt)[0], base)

    def test_versions_are_kept_in_cosmos_records_and_reused_after_a_restart(self) -> None:
        from pms.azure.cosmos_store import CosmosAgentRegistry

        account = FakeCosmosAccount()
        self.runtime(CosmosAgentRegistry(cosmos_account_for(account))).publish_all()
        docs = [d for d in account.containers["records"].all_docs() if d.get("type") == "foundry_agent_versions"]
        self.assertEqual(len(docs), 5)
        self.assertEqual({d["pk"] for d in docs}, {"foundry-agents"})
        self.assertEqual({d["foundry_version"] for d in docs}, {"1"})
        self.assertTrue(all(len(d["spec_sha256"]) == 64 and d["prompt_version"] for d in docs))
        # restart: a new process with a new runtime object runs an agent without publishing it again
        restarted = self.runtime(CosmosAgentRegistry(cosmos_account_for(account)))
        pipeline, clock = new_pipeline("publish-run", agent_backend="foundry", foundry_project_endpoint=ENDPOINT)
        result = ingest(pipeline, clock, "L06")
        spec = AGENTS["A1"]
        tools = ToolBox(store=pipeline.store, ref=pipeline.ref, search=pipeline.search, case_id=result.case_id,
                        signal_id=result.signal_id, agent_id=spec.agent_id,
                        agent_meta={"agent_version": "x", "model": "m", "model_version": "v", "prompt_version": "p"},
                        allowed=spec.tools)
        run = restarted.run(spec, AgentInput(result.case_id, result.signal_id, {"agent": "A1"}), tools)
        self.assertEqual(run.status, "DONE", run.error)
        self.assertEqual(len(FOUNDRY.versions), 5)
        self.assertEqual(FOUNDRY.runs, [("pms-a1-signal-ingestion", "1")])
        self.assertEqual(run.details["foundry_agent_version"], "1")
        restarted.publish_all()
        self.assertEqual(len(FOUNDRY.versions), 5)
        history = CosmosAgentRegistry(cosmos_account_for(account))
        self.runtime(history).publish_all(force=True)
        self.assertEqual([h["foundry_version"] for h in history.get("pms-a6-evidence-correlation")["history"]], ["1"])

    def test_a_registry_that_cannot_be_written_does_not_stop_publishing(self) -> None:
        from pms.azure.cosmos_store import CosmosAgentRegistry

        account = FakeCosmosAccount()
        account.containers["records"].allowed_ops = frozenset({"read_container", "read", "query", "create"})
        with self.assertLogs("pms.agents.foundry", "WARNING"):
            result = self.runtime(CosmosAgentRegistry(cosmos_account_for(account))).publish_all(codes=("A1",))
        self.assertEqual(result[0]["action"], "published")
        self.assertIn("403", result[0]["registry_error"])

    def test_a_deleted_agent_version_is_published_again_once(self) -> None:
        registry = MemoryAgentRegistry()
        runtime = self.runtime(registry)
        runtime.publish_all(codes=("A1",))
        gone = RuntimeError("agent version not found")
        gone.status_code = 404  # type: ignore[attr-defined]
        FOUNDRY.run_errors = [gone]
        pipeline, clock = new_pipeline("publish-404", agent_backend="foundry", foundry_project_endpoint=ENDPOINT)
        result = ingest(pipeline, clock, "L06")
        spec = AGENTS["A1"]
        tools = ToolBox(store=pipeline.store, ref=pipeline.ref, search=pipeline.search, case_id=result.case_id,
                        signal_id=result.signal_id, agent_id=spec.agent_id,
                        agent_meta={"agent_version": "x", "model": "m", "model_version": "v", "prompt_version": "p"},
                        allowed=spec.tools)
        run = runtime.run(spec, AgentInput(result.case_id, result.signal_id, {"agent": "A1"}), tools)
        self.assertEqual(run.status, "DONE", run.error)
        self.assertEqual(FOUNDRY.runs, [("pms-a1-signal-ingestion", "2")])
        self.assertEqual(registry.get("pms-a1-signal-ingestion")["foundry_version"], "2")
        self.assertEqual(registry.get("pms-a1-signal-ingestion")["previous_version"], "1")

    def test_cli_publish_agents_is_idempotent_with_the_azure_registry(self) -> None:
        from pms.cli import main

        env = {"APP_ENV": "azure", "LOCAL_TEST_USERS": "false", "AGENT_BACKEND": "foundry", "WORKER_MODE": "separate",
               "FOUNDRY_PROJECT_ENDPOINT": ENDPOINT, "COSMOS_ENDPOINT": "https://fake-cli.documents.azure.com:443/",
               "COSMOS_DATABASE": "pms", "AZURE_CLIENT_ID": "mi-client"}
        outputs = []
        with mock.patch.dict(os.environ, env):
            for _ in range(2):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(main(["publish-agents"]), 0)
                outputs.append(json.loads(buffer.getvalue())["published"])
            self.assertIsInstance(rt.get_agent_registry(rt.get_settings()).describe(), str)
        self.assertEqual({p["action"] for p in outputs[0]}, {"published"})
        self.assertEqual({p["action"] for p in outputs[1]}, {"unchanged"})
        self.assertEqual(len(FOUNDRY.versions), 5)


if __name__ == "__main__":
    unittest.main()
