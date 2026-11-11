"""Agent catalogue A1-A6 (concept 6.2), versioned prompts, output schemas and the closed lists.

Agents recommend, humans decide. Each agent stays at or below the autonomy level of Case
Study 5; every output is a draft. A5 (light trend view) is plain code without a model
(:mod:`pms.agents.trend`). Models (ADR-002, decision Q-10): gpt-6-astra for A3 and A6,
gpt-6.1-sol for A1, A2 and A4 (fallback gpt-6.1-sol for all via configuration).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import AgentSpec

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

#: Model versions of the deployments (concept 6.9; infra/models.json).
MODEL_VERSIONS: dict[str, str] = {"gpt-6-astra": "2026-09-03", "gpt-6.1-sol": "2026-09-29"}
DEPLOYMENT_TYPE = "Data Zone Standard (EU)"
DEFAULT_MODELS_BY_ROLE: dict[str, str] = {"analysis": "gpt-6-astra", "general": "gpt-6.1-sol"}

#: Causality scale (decision Q-2; MDCG 2020-10/1 Rev. 1 section 9; "not assessable" -> possible).
CAUSALITY_SCALE: tuple[str, ...] = ("not related", "possible", "probable", "causal")
CAUSALITY_NOTE = ("Draft on the 4-level scale of MDCG 2020-10/1 Rev. 1 section 9 (used by analogy; 'not assessable' "
                  "-> possible); not clinically validated.")

AI_SEVERITIES: tuple[str, ...] = ("DEATH", "PUBLIC_HEALTH_THREAT", "SERIOUS_DETERIORATION", "NOT_SERIOUS", "UNKNOWN")

#: The ONLY IMDRF codes of the demo (input 03-scenario.md SRC-014; P2_NOTES). Never invent others.
IMDRF_CODES: dict[str, dict[str, str]] = {
    "A0401": {"annex": "A", "annex_title": "Medical device problem", "term": "Break",
              "definition": "Unintended damage or breakage of device material."},
    "F1905": {"annex": "F", "annex_title": "Health impact", "term": "Device Revision or Replacement",
              "definition": "Corrective invasive procedure with revision or replacement."},
}
IMDRF_ATTRIBUTION = (
    "IMDRF Adverse Event Terminology, release 2026 (Annexes A and F dated 2026-03-23), https://www.imdrf.org/node/460. "
    "Only the two codes used in this demo are included. IMDRF terms of use restrict use to non-commercial research "
    "or teaching unless authorised (OI-009).")
IMDRF_CANDIDATE_STATUS = "candidate - pending human review; a shared code proves no causality"
IMDRF_NO_CANDIDATE_NOTE = "no candidate from the demo list"

FACT_NAMES: tuple[str, ...] = (
    "patient_reference", "device_model", "product_family", "lot", "udi_di", "implant_date", "implant_site",
    "event_date", "event", "outcome", "incident_location", "sender_reference",
)
CORRELATION_RELATIONS: tuple[str, ...] = ("agreement", "contradiction", "context")


# --------------------------------------------------------------------------- schema helpers (strict JSON schema)
def _s(max_len: int = 2000, *, nullable: bool = False, enum: tuple[str, ...] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": ["string", "null"] if nullable else "string", "maxLength": max_len}
    if enum is not None:
        schema["enum"] = list(enum) + ([None] if nullable else [])
    return schema


def _i(*, nullable: bool = False) -> dict[str, Any]:
    return {"type": ["integer", "null"] if nullable else "integer", "minimum": 0}


def _a(items: dict[str, Any], max_items: int = 40) -> dict[str, Any]:
    return {"type": "array", "items": items, "maxItems": max_items}


def _o(props: dict[str, Any], *, nullable: bool = False) -> dict[str, Any]:
    return {"type": ["object", "null"] if nullable else "object", "properties": props, "required": list(props),
            "additionalProperties": False}


_EVIDENCE = _o({"statement": _s(600), "source_id": _s(80, nullable=True), "quote": _s(600, nullable=True)})

A1_SCHEMA = _o({
    "facts": _a(_o({"name": _s(40, enum=FACT_NAMES), "value": _s(300, nullable=True), "quote": _s(600, nullable=True),
                    "line": _i(nullable=True)}), 60),
    "language": _s(5, nullable=True),
    "summary": _s(600),
    "ai_severity": _s(30, enum=AI_SEVERITIES),
    "urgency_note": _s(400),
    "injection_note": _s(400, nullable=True),
})
A2_SCHEMA = _o({
    "product_family": _s(60, nullable=True),
    "models": _a(_s(30), 10),
    "registry_check": _o({"status": _s(20, enum=("MATCH", "MISMATCH", "NOT_FOUND", "NO_IDENTIFIERS")),
                          "value": _s(600)}),
    "proposed_assignment": _a(_o({"registry_entry": _s(20), "patient": _s(20, nullable=True),
                                  "model": _s(30, nullable=True), "lot": _s(40, nullable=True),
                                  "udi_di": _s(20, nullable=True), "implant_date": _s(10, nullable=True),
                                  "country": _s(10, nullable=True)}), 10),
    "deviations": _a(_o({"detail": _s(400), "source": _s(40)}), 10),
    "proposal_note": _s(800),
})
A6_SCHEMA = _o({
    "correlations": _a(_o({"source_id": _s(80), "relation": _s(20, enum=CORRELATION_RELATIONS),
                           "statement": _s(600), "quote": _s(600, nullable=True)}), 30),
    "contradictions": _a(_o({"statement": _s(600), "source_id": _s(80, nullable=True)}), 20),
    "open_questions": _a(_s(400), 20),
    "no_hits_note": _s(400),
})
A3_SCHEMA = _o({
    "causality": _s(20, enum=CAUSALITY_SCALE),
    "causality_rationale": _s(1200),
    "product_defect_possible": _s(10, enum=("yes", "no", "unclear")),
    "expectedness": _s(12, enum=("expected", "unexpected", "unknown")),
    "expectedness_basis": _s(600, nullable=True),
    "ai_severity": _s(30, enum=AI_SEVERITIES),
    "evidence_for": _a(_EVIDENCE, 20),
    "evidence_against": _a(_EVIDENCE, 20),
    "uncertainty": _a(_s(400), 20),
    "missing_data": _a(_s(200), 20),
    "imdrf_candidates": _a(_o({"code": _s(10, enum=tuple(IMDRF_CODES)), "basis": _s(400),
                               "quote": _s(600, nullable=True)}), 5),
    "fsca_check_hint": _s(400, nullable=True),
    "request_info_draft": _o({"fields": _a(_o({"field": _s(60), "question": _s(300), "reason": _s(300)}), 12)},
                             nullable=True),
    "needs_more_evidence": {"type": "boolean"},
})
A4_SCHEMA = _o({
    "report_drafts": _a(_o({"rule_set": _s(40), "authority_label": _s(200), "form_label": _s(120),
                            "driving_due_date": _s(10, nullable=True), "deadline_basis": _s(60, nullable=True),
                            "deadline_days": _i(nullable=True), "draft_text": _s(6000),
                            "missing_mandatory_data": _a(_s(200), 20)}), 6),
    "deadline_note": _s(600),
})

# --------------------------------------------------------------------------- the agents
A1 = AgentSpec(
    code="A1", agent_id="A1-signal-ingestion", name="Signal Ingestion",
    description="Reads the extracted letter text and returns structured facts, each with a verbatim quote.",
    agent_version="A1-1.0.0", prompt_file="a1_signal_ingestion.md", model_role="general",
    autonomy="L4 per Case Study 5 (reads and structures; no decision)",
    limits="No decision; quotes are checked against the source text by code; missing values stay empty (unknown); "
           "own urgency suspicion may only raise urgency.",
    tools=(), output_schema=A1_SCHEMA, foundry_name="pms-a1-signal-ingestion", may_raise_urgency=True,
    reasoning_effort="low")
A2 = AgentSpec(
    code="A2", agent_id="A2-product-matching", name="Product and implant matching",
    description="Proposes the product and implant assignment and explains registry deviations (uses REG-1.0).",
    agent_version="A2-1.0.0", prompt_file="a2_product_matching.md", model_role="general",
    autonomy="below L4 (proposal only)",
    limits="The assignment stays a proposal; a registry deviation is always visible.",
    tools=("get_registry_entry", "get_dossier_item"), output_schema=A2_SCHEMA,
    foundry_name="pms-a2-product-matching", reasoning_effort="low")
A3 = AgentSpec(
    code="A3", agent_id="A3-medical-analysis", name="Medical analysis and causality",
    description="Drafts causality (4-level MDCG scale), product-defect assessment, evidence for and against, "
                "uncertainty, missing data and IMDRF code candidates from the demo list.",
    agent_version="A3-1.0.0", prompt_file="a3_medical_analysis.md", model_role="analysis",
    autonomy="L2 (recommendation)",
    limits="Recommendation only; may only raise urgency; never decides reportability.",
    tools=("get_dossier_item", "get_past_case", "search_corpus", "list_case_signals"), output_schema=A3_SCHEMA,
    foundry_name="pms-a3-medical-analysis", may_raise_urgency=True, reasoning_effort="medium")
A4 = AgentSpec(
    code="A4", agent_id="A4-vigilance-reporting", name="Vigilance reporting draft",
    description="Drafts a report text per rule set and a deadline overview taken from DL-1.0 and RS-1.0.",
    agent_version="A4-1.0.0", prompt_file="a4_vigilance_reporting.md", model_role="general",
    autonomy="below L3 (drafts only)",
    limits="No submission and nothing is sent; deadlines come exclusively from rule code.",
    tools=(), output_schema=A4_SCHEMA, foundry_name="pms-a4-vigilance-reporting", reasoning_effort="low")
A6 = AgentSpec(
    code="A6", agent_id="A6-evidence-correlation", name="Evidence and correlation",
    description="Derives targeted read-only searches from the findings of A1-A3 and correlates hits, agreements "
                "and contradictions with quote and source ID.",
    agent_version="A6-1.0.0", prompt_file="a6_evidence_correlation.md", model_role="analysis",
    autonomy="L2 (recommendation)",
    limits="Read-only tools through app code only, no internet; at most 2 rounds and 8 tool calls; may only raise "
           "urgency; 'no hits' is not an all-clear; no statistics.",
    tools=("search_corpus", "get_registry_entry", "get_dossier_item", "get_past_case", "list_case_signals"),
    output_schema=A6_SCHEMA, foundry_name="pms-a6-evidence-correlation", reasoning_effort="medium")

AGENTS: dict[str, AgentSpec] = {spec.code: spec for spec in (A1, A2, A3, A4, A6)}
PIPELINE_ORDER: tuple[str, ...] = ("A1", "A2", "A6", "A3", "A4")

A5_INFO: dict[str, Any] = {
    "code": "A5", "agent_id": "A5-trend-view", "name": "Light trend view", "model": None,
    "agent_version": "A5-1.0.0", "autonomy": "below L4 (display only)",
    "limits": "No statistical statement, no signal score; interpretation by humans only.",
}


# --------------------------------------------------------------------------- prompts
@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    version: str
    agent: str
    text: str
    sha256: str

    @property
    def prompt_version(self) -> str:
        return f"{self.prompt_id}@{self.version}"


def _parse_prompt(raw: str, file: str) -> Prompt:
    if not raw.startswith("---\n"):
        raise ValueError(f"prompt {file} has no front matter")
    head, body = raw[4:].split("\n---\n", 1)
    meta = dict(line.split(":", 1) for line in head.strip().splitlines())
    meta = {k.strip(): v.strip() for k, v in meta.items()}
    text = body.strip() + "\n"
    return Prompt(meta["prompt_id"], meta["version"], meta["agent"], text,
                  hashlib.sha256(text.encode("utf-8")).hexdigest())


def load_prompt(spec: AgentSpec) -> Prompt:
    path = PROMPT_DIR / spec.prompt_file
    prompt = _parse_prompt(path.read_text(encoding="utf-8"), spec.prompt_file)
    if prompt.agent != spec.code:
        raise ValueError(f"prompt {spec.prompt_file} belongs to {prompt.agent}, not {spec.code}")
    return prompt


def prompt_manifest() -> dict[str, Any]:
    return json.loads((PROMPT_DIR / "manifest.json").read_text(encoding="utf-8"))


def model_version_of(deployment: str) -> str:
    return MODEL_VERSIONS.get(deployment, "unknown (deployment default)")


def agent_catalogue(settings: Any | None = None) -> list[dict[str, Any]]:
    """Versions, models and limits of every agent (governance view, reports)."""
    out = []
    for code in PIPELINE_ORDER:
        spec = AGENTS[code]
        prompt = load_prompt(spec)
        model = settings.model_for(spec.model_role) if settings is not None else None
        out.append({
            "code": spec.code, "agent_id": spec.agent_id, "name": spec.name, "agent_version": spec.agent_version,
            "prompt_version": prompt.prompt_version, "prompt_sha256": prompt.sha256, "model_role": spec.model_role,
            "model_deployment": model, "model_version": model_version_of(model) if model else None,
            "autonomy": spec.autonomy, "limits": spec.limits, "tools": list(spec.tools),
            "foundry_name": spec.foundry_name,
        })
    out.append({**A5_INFO, "prompt_version": None, "model_deployment": None, "tools": []})
    return out
