"""Deterministic offline stand-in for agents A1-A4 and A6 (concept 6.8; ADR-001 "Offline-Stand-in").

No model call and no network. Each answer is derived from deterministic features that the
application passes in (rule results, registry check, CORR-1.0 hits, extracted quotes) and from the
read-only tools; it never reads ``expected.json``. Answers are clearly labelled
"offline stand-in" and carry fixed token numbers, so cost tests are exact (T27).

The causality heuristic is a demo stand-in, NOT clinically validated: device-material events
(fracture, tear, dissociation, noise) and lot clusters -> "probable"; events with typical patient
contribution (infection, thrombosis, loosening, radiolucent lines) -> "possible"; no device event
term with an identified device -> "not related"; not assessable -> "possible" (MDCG 2020-10/1).
Its urgency reading for a letter without device event term is "NOT_SERIOUS" (deliberately the
wrong-but-plausible answer of concept 12 for L07); the application never lets it lower urgency.

Test hooks: ``OfflineRuntime(forced={"A3": {"ai_severity": "NOT_SERIOUS"}})`` overrides fields of an
answer, ``fail={"A3"}`` makes a run fail (T25).
"""
from __future__ import annotations

import copy
import re
import time
from typing import Any, Iterable, Mapping

from ..models import utcnow
from ..rules.correlation import NOTE_NO_ALL_CLEAR
from ..rules.extract import header_end as _header_end
from ..rules.extract import line_of as _line_of
from ..rules.extract import sentence_at as _sentence_around
from .base import AgentError, AgentInput, AgentResult, AgentRuntime, AgentSpec, TokenUsage
from .specs import CAUSALITY_NOTE, DEFAULT_MODELS_BY_ROLE, IMDRF_CODES, load_prompt
from .tools import ToolBox

MODEL = "offline-stand-in"
MODEL_VERSION = "offline-stand-in-1.0.0"
LABEL = "offline stand-in (deterministic, no model call)"

#: Fixed token numbers per agent run (T27: costs per agent and case are exact to the cent).
FIXED_USAGE: dict[str, tuple[int, int, int, int]] = {  # input, cached input, output (incl. reasoning), reasoning
    "A1": (2000, 0, 500, 0),
    "A2": (1500, 0, 400, 0),
    "A6": (4000, 0, 1000, 0),
    "A3": (5000, 0, 1500, 500),
    "A4": (3000, 0, 1200, 0),
}

_SERIOUS = ("DEATH", "PUBLIC_HEALTH_THREAT", "SERIOUS_DETERIORATION")
_DEVICE_MATERIAL_EVENTS = frozenset({"stem fracture", "ceramic head fracture", "leaflet tear", "liner dissociation",
                                     "squeaking"})
_BREAK_EVENTS = frozenset({"stem fracture", "ceramic head fracture", "leaflet tear"})  # IMDRF A0401 candidates
_REVISION_RE = re.compile(r"revision|re-?operation|redo|explant|wechsel|réintervention|reprise|révision|"
                          r"reintervento|espiant", re.IGNORECASE)
_OUTCOME_RE = re.compile(
    r"\b(?:recover(?:ed|ing)|discharged|stable|resolved|entlassen|Entlassung|stabil|erholt|beschwerdefrei|"
    r"favorable|sortie|guéri|dimess[oa]|stabile|guarit[oa])\b", re.IGNORECASE)
#: Statements that weigh against a device cause (offline heuristic; DE/EN/FR/IT).
_AGAINST_PATTERNS: tuple[tuple[str, str], ...] = (
    ("device described as functioning normally", r"regelrecht funktionierend\w*|ohne Auffälligkeiten|unauffällig\w*|"
                                                 r"functioning normally|unremarkable|sans anomalie|"
                                                 r"fonctionnement normal|regolare funzionamento|senza anomalie"),
    ("alternative diagnosis or cause reported", r"appendizitis|appendicitis|pulmonary embolism|Lungenembolie|"
                                                r"embolie pulmonaire|embolia polmonare"),
    ("patient-related factor reported", r"\bBMI\b|dental|Zahnbehandlung|zahnärztlich\w*|soins dentaires|"
                                        r"cure dentali|Antikoagulation|anticoagulation"),
)
_FIELD_QUESTIONS: dict[str, tuple[str, str]] = {
    "product_family": ("Which implant family was used (manufacturer and product)?", "needed for product matching"),
    "device_model_or_version": ("Which model and size were implanted?", "registry check and product matching"),
    "lot_or_udi": ("Please provide the lot number and the UDI-DI from the implant record.",
                   "registry check (REG-1.0) and lot correlation"),
    "patient_reference": ("Please provide the patient pseudonym used in your records.",
                          "registry check and duplicate detection"),
    "patient_exposure": ("Was a patient exposed to the device, and how?", "reportability assessment"),
    "event_date": ("On which date did the event occur?", "timeline for the causality assessment"),
    "outcome": ("What is the current outcome for the patient?", "severity and causality assessment"),
    "cause": ("Has a cause been identified (for example by explant analysis)?", "causality assessment"),
    "market": ("In which country did the event occur?", "rule set and authority label (RS-1.0)"),
    "event_description": ("Please describe the event.", "the letter text is missing"),
}
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")


def _merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def pick_quote(text: str | None, needles: Iterable[str] = (), max_len: int = 300) -> str | None:
    """A verbatim sentence of ``text`` that contains one of ``needles`` (else the first sentence)."""
    if not text:
        return None
    sentences = [m.group(0).strip() for m in _SENTENCE_RE.finditer(text)]
    sentences = [s for s in sentences if len(s) > 12 and not s.startswith(("#", "-", "|", "SYNTHETIC", "FICTIONAL"))]
    lowered = [n.lower() for n in needles if n]
    for s in sentences:
        if any(n in s.lower() for n in lowered):
            return s[:max_len]
    return sentences[0][:max_len] if sentences else None


class OfflineRuntime(AgentRuntime):
    """Deterministic stand-in behind the common agent interface."""

    backend = "offline"

    def __init__(self, *, forced: Mapping[str, Mapping[str, Any]] | None = None, fail: Iterable[str] = (),
                 models_by_role: Mapping[str, str] | None = None) -> None:
        self.forced: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (forced or {}).items()}
        self.fail = set(fail)
        self.models_by_role = dict(models_by_role or DEFAULT_MODELS_BY_ROLE)

    # ------------------------------------------------------------------ test hooks
    def force(self, code: str, **fields: Any) -> None:
        self.forced.setdefault(code, {}).update(fields)

    def clear(self) -> None:
        self.forced.clear()
        self.fail.clear()

    def describe(self) -> dict[str, Any]:
        return {"backend": self.backend, "model": MODEL, "model_version": MODEL_VERSION, "label": LABEL,
                "forced": sorted(self.forced), "fail": sorted(self.fail)}

    # ------------------------------------------------------------------ interface
    def run(self, spec: AgentSpec, agent_input: AgentInput, tools: ToolBox) -> AgentResult:
        started = utcnow()
        t0 = time.perf_counter()
        prompt = load_prompt(spec)
        output: dict[str, Any] | None
        try:
            if spec.code in self.fail:
                raise AgentError("forced failure (test hook): the agent run failed deliberately")
            output = getattr(self, f"_{spec.code.lower()}")(agent_input, tools)
            if spec.code in self.forced:
                output = _merge(output, self.forced[spec.code])
            status, error = "DONE", None
        except Exception as exc:  # a failed run stays visible; it never produces a fictitious next step
            output, status, error = None, "FAILED", f"{type(exc).__name__}: {exc}"
        usage = TokenUsage(*FIXED_USAGE[spec.code])
        return AgentResult(
            code=spec.code, agent_id=spec.agent_id, status=status, output=output, backend=self.backend, model=MODEL,
            model_version=MODEL_VERSION, prompt_version=prompt.prompt_version, agent_version=spec.agent_version,
            usage=usage, tool_calls=list(tools.calls), started_at=started, finished_at=utcnow(),
            duration_ms=(time.perf_counter() - t0) * 1000, error=error,
            priced_model=self.models_by_role.get(spec.model_role), details={"generator": LABEL})

    # ------------------------------------------------------------------ A1 signal ingestion
    def _a1(self, inp: AgentInput, tools: ToolBox) -> dict[str, Any]:
        p = inp.payload
        rr = p["rule_results"]
        ex = rr["extraction"]
        text = inp.letter_text or ""
        facts: list[dict[str, Any]] = []

        def add(name: str, value: Any, quote: str | None, line: int | None) -> None:
            facts.append({"name": name, "value": None if value is None else str(value), "quote": quote,
                          "line": line})

        for name, key in (("patient_reference", "patient_refs"), ("device_model", "models"), ("lot", "lots"),
                          ("udi_di", "udi_dis"), ("implant_date", "implant_dates"), ("implant_site", "implant_sites"),
                          ("event_date", "event_dates"), ("sender_reference", "sender_refs")):
            for ev in ex.get(key, []):
                add(name, ev["value"], ev["quote"], ev["line"])
        for ev in ex.get("families", [])[:1]:
            add("product_family", ev["value"], ev["quote"], ev["line"])
        for tag in rr.get("event_tags", []):
            add("event", tag["tag"], tag["quote"], tag["line"])
        if not rr.get("event_tags"):
            indicator = next(iter(rr["crit"]["indicators"]), None)
            if indicator is not None:
                add("event", f"event as described ({indicator['group'].lower().replace('_', ' ')} indicator)",
                    indicator["quote"], indicator["line"])
            else:
                start = _header_end(text)
                body = text[start:] if start < len(text) else text
                quote = pick_quote(body.split("\n", 1)[-1] if start < len(text) else body)
                if quote and quote in text:
                    add("event", "event as described (no event term from the dictionary)", quote,
                        _line_of(text, text.find(quote)))
        death = next((i for i in rr["crit"]["indicators"] if i["group"] == "DEATH"), None)
        if death is not None:
            add("outcome", "death", death["quote"], death["line"])
        else:
            m = _OUTCOME_RE.search(text)
            if m:
                add("outcome", m.group(0), _sentence_around(text, m.start()), _line_of(text, m.start()))
        loc = ex.get("incident_location")
        if loc:
            add("incident_location", ", ".join(loc["states"]), loc["quote"], loc["line"])
        present = {f["name"] for f in facts}
        for name in ("patient_reference", "device_model", "lot", "event_date", "event", "outcome"):
            if name not in present:
                add(name, None, None, None)  # unknown stays unknown

        severity = rr["crit"]["severity"]
        ai = severity if severity in _SERIOUS else ("UNKNOWN" if severity == "UNKNOWN" else "NOT_SERIOUS")
        events = ", ".join(t["tag"] for t in rr.get("event_tags", [])) or "no device event term found"
        outcome = next((f["value"] for f in facts if f["name"] == "outcome" and f["value"]), "unknown")
        return {
            "facts": facts,
            "language": p["signal"].get("language"),
            "summary": (f"Letter {p['signal']['ref']}: event group(s) {events}; outcome {outcome}; registry check "
                        f"{rr['registry_check']['status']}. Offline stand-in draft."),
            "ai_severity": ai,
            "urgency_note": "Second reading may only raise urgency; the rule result CRIT-1.0 stays in force.",
            "injection_note": ("INJ-1.0 flagged text addressed to automated systems; it was treated as data and not "
                               "followed.") if p["signal"].get("injection_flag") else None,
        }

    # ------------------------------------------------------------------ A2 product and implant matching
    def _a2(self, inp: AgentInput, tools: ToolBox) -> dict[str, Any]:
        p = inp.payload
        reg = p["rule_results"]["registry_check"]
        ex = p["rule_results"]["extraction"]
        entries: list[dict[str, Any]] = []
        for reg_id in reg.get("entry_ids", [])[:4]:
            entries += tools.call("get_registry_entry", {"reg_id": reg_id}).get("entries", [])
        if not entries and ex.get("patient_refs"):
            entries += tools.call("get_registry_entry", {"patient": ex["patient_refs"][0]["value"]}).get("entries", [])
        family = (ex["families"][0]["value"] if ex.get("families") else None) or (
            entries[0]["family"] if entries else None) or p["case"].get("product_family")
        models = list(dict.fromkeys([e["value"] for e in ex.get("models", [])] or [e["model"] for e in entries]))
        status = reg["status"]
        ids = ", ".join(e["reg_id"] for e in entries)
        notes = {
            "MATCH": f"Proposal: the letter refers to registry entr{'ies' if len(entries) > 1 else 'y'} {ids}; "
                     "patient pseudonym, lot and UDI-DI agree with the registry (REG-1.0). A human confirms.",
            "MISMATCH": f"Proposal with deviations ({ids}): the deviations below stay visible; a human decides which "
                        "value is correct. Nothing was corrected automatically.",
            "NOT_FOUND": "No registry entry identified for the given identifiers; no assignment proposed "
                         "(unknown stays unknown).",
            "NO_IDENTIFIERS": "The letter carries no patient pseudonym, lot or UDI-DI: no assignment is possible; "
                              "the missing identifiers are listed for a request to the sender.",
        }
        return {
            "product_family": family,
            "models": models,
            "registry_check": {"status": status, "value": reg["value"]},
            "proposed_assignment": [
                {"registry_entry": e["reg_id"], "patient": e["patient"], "model": e["model"], "lot": e["lot"],
                 "udi_di": e["udi_di"], "implant_date": e["implant_date"], "country": e["country"]}
                for e in entries] if status in ("MATCH", "MISMATCH") else [],
            "deviations": [{"detail": d, "source": "REG-1.0"} for d in reg.get("details", [])],
            "proposal_note": notes.get(status, "Registry check result unknown."),
        }

    # ------------------------------------------------------------------ A6 evidence and correlation
    def _a6(self, inp: AgentInput, tools: ToolBox) -> dict[str, Any]:
        p = inp.payload
        ident = p["identifiers"]
        family, models, lots, tags = ident.get("family"), ident.get("models", []), ident.get("lots", []), p["event_tags"]
        found: dict[str, dict[str, Any]] = {}

        def collect(result: dict[str, Any], how: str, items_key: str = "hits") -> None:
            for hit in result.get(items_key, []) or []:
                entry = found.setdefault(hit["source_id"], {"hit": hit, "how": []})
                if how not in entry["how"]:
                    entry["how"].append(how)

        if not (family or models or lots):
            return {
                "correlations": [], "contradictions": [],
                "open_questions": ["No lot, model or product family is known: no search was possible; the missing "
                                   "identifiers stay visible.", "No hits is not an all-clear."],
                "no_hits_note": NOTE_NO_ALL_CLEAR,
            }
        tools.begin_round()
        for tag in tags[:2]:
            if family:
                collect(tools.call("search_corpus", {"family": family, "event_tag": tag, "limit": 10}),
                        f"family_event:{tag}")
        for lot in lots[:1]:
            collect(tools.call("search_corpus", {"lot": lot, "limit": 10}), "same_lot")
        for model in models[:2]:
            collect(tools.call("search_corpus", {"model": model, "limit": 10}), "same_model")
        corr_hits = p["corr_1_0"].get("hits", [])
        if tools.can_begin_round():
            tools.begin_round()
            past = [ref for ref in found if ref.startswith("H-")][:2]
            for ref in past:
                res = tools.call("get_past_case", {"case_ref": ref})
                if res.get("past_case"):
                    found[ref]["detail"] = res["past_case"]
            for hit in [h for h in corr_hits if h["ref"].startswith("DOS:")][:2]:
                res = tools.call("get_dossier_item", {"ref": hit["ref"]})
                if res.get("item"):
                    found.setdefault(hit["ref"], {"hit": {"source_id": hit["ref"], "source_type": "dossier_item",
                                                          "title": hit["title"], "excerpt": res["item"]["text"]},
                                                  "how": ["dossier_listed_side_effect"]})
            for hit in [h for h in corr_hits if h["ref"].startswith("REG:")][:1]:
                res = tools.call("get_registry_entry", {"reg_id": hit["ref"][4:]})
                for row in res.get("entries", []):
                    found.setdefault(row["source_id"], {"hit": {"source_id": row["source_id"],
                                                                "source_type": "registry_entry",
                                                                "title": f"{row['model']} lot {row['lot']}",
                                                                "excerpt": row["lot"]},
                                                        "how": ["registry_same_lot"]})

        needles = list(tags) + list(lots) + list(models)
        correlations = []
        for source_id, entry in found.items():
            hit, how = entry["hit"], entry["how"]
            stype = hit.get("source_type")
            text = (entry.get("detail") or {}).get("text") or hit.get("excerpt")
            quote = hit.get("excerpt") if stype == "registry_entry" else pick_quote(text, needles)
            event_how = [h for h in how if h.startswith("family_event:")]
            if stype == "dossier_item":
                relation, statement = "agreement", ("Event group listed as a known side-effect in the product dossier "
                                                    "(expected per dossier).")
            elif stype == "registry_entry":
                relation, statement = "agreement", "Further registry patient with the same lot (count, not a rate)."
            elif ("same_lot" in how or "same_model" in how) and event_how:
                relation = "agreement"
                statement = (f"Same {'lot' if 'same_lot' in how else 'model'} with the same event group "
                             f"({event_how[0].split(':', 1)[1]}).")
            elif event_how:
                relation, statement = "context", f"Fictional {stype} on the same event group ({event_how[0][13:]})."
            else:
                relation, statement = "context", f"Same {'lot' if 'same_lot' in how else 'model'}, other topic."
            correlations.append({"source_id": source_id, "relation": relation, "statement": statement,
                                 "quote": quote})
        for hit in corr_hits:
            if hit["ref"].startswith("CASE:") and hit["ref"] not in found:
                correlations.append({"source_id": hit["ref"], "relation": "context",
                                     "statement": f"Other case in this system with the same "
                                                  f"{'/'.join(t.replace('_', ' ') for t in hit['match_types'])}; "
                                                  "a human reviews a possible pattern (counts only).",
                                     "quote": None})
        contradictions = []
        reg_status = (p.get("a2") or {}).get("registry_status")
        if reg_status == "MISMATCH":
            contradictions.append({"statement": "Letter identifiers contradict the registry (REG-1.0): "
                                                + "; ".join((p.get("a2") or {}).get("deviations", [])),
                                   "source_id": None})
        listed = [h for h in corr_hits if "dossier_listed_side_effect" in h.get("match_types", [])]
        if tags and family and not listed:
            contradictions.append({"statement": f"Event group(s) {', '.join(tags)} not listed as a side-effect in "
                                                "the dossier: candidate for 'unexpected' (human decision).",
                                   "source_id": None})
        open_questions = [f"Missing in the letter: {m}" for m in p.get("missing_data", [])]
        if not found:
            open_questions.append("No corpus hits for the derived searches - this is not an all-clear.")
        return {"correlations": correlations, "contradictions": contradictions, "open_questions": open_questions,
                "no_hits_note": NOTE_NO_ALL_CLEAR}

    # ------------------------------------------------------------------ A3 medical analysis and causality
    def _a3(self, inp: AgentInput, tools: ToolBox) -> dict[str, Any]:
        p = inp.payload
        signals = p["signals"]
        tags: list[str] = []
        tag_quotes: dict[str, tuple[str, str, int]] = {}
        for s in signals:
            for t in s["event_tags"]:
                if t["tag"] not in tags:
                    tags.append(t["tag"])
                    tag_quotes[t["tag"]] = (s["ref"], t["quote"], t["line"])
        statuses = [s["registry_check"]["status"] for s in signals]
        identified = any(st in ("MATCH", "MISMATCH") for st in statuses) or any(s.get("models") for s in signals)
        mismatch_only = "MISMATCH" in statuses and "MATCH" not in statuses
        corr_hits = [h for s in p["corr_1_0"] for h in s.get("hits", [])]
        listed = list(dict.fromkeys(h["ref"] for h in corr_hits if "dossier_listed_side_effect" in h["match_types"]))
        lot_cluster = [h for h in corr_hits if {"same_lot", "registry_same_lot"} & set(h["match_types"])]
        severity = p["case"]["rule_severity"]
        pht = severity == "PUBLIC_HEALTH_THREAT" or any(
            "PUBLIC_HEALTH_THREAT" in s["crit"]["groups"] for s in signals)
        material = [t for t in tags if t in _DEVICE_MATERIAL_EVENTS]

        if not tags and not identified:
            causality, defect = "possible", "unclear"
            rationale = "Not assessable: no device identifiers and no device event term (not assessable -> possible)."
        elif mismatch_only:
            causality, defect = "possible", "unclear"
            rationale = "Device identity is uncertain because the identifiers contradict the registry (REG-1.0)."
        elif not tags:
            causality, defect = "not related", "no"
            rationale = "No device-related event term was found; the reported event points to another cause."
        elif pht or (lot_cluster and tags):
            causality, defect = "probable", "yes"
            rationale = "Device event group together with several events in the same lot (named matches, no rate)."
        elif material:
            causality, defect = "probable", "yes"
            rationale = f"Device-material event group ({', '.join(material)}) reported for an identified device."
        else:
            causality, defect = "possible", "unclear"
            rationale = (f"Event group ({', '.join(tags)}) with typical patient contribution; a device contribution "
                         "cannot be excluded.")
        rationale += " Offline stand-in heuristic; not clinically validated."

        if listed:
            expectedness = "expected"
            basis = "Listed side-effect(s) in the product dossier: " + ", ".join(listed)
        elif tags and p["case"].get("product_family"):
            expectedness, basis = "unexpected", "Event group not listed in section 8 of the product dossier."
        else:
            expectedness, basis = "unknown", None

        if not tags and not identified:
            ai = "UNKNOWN"
        elif not tags:
            ai = "NOT_SERIOUS"
        else:
            ai = severity if severity in _SERIOUS else "NOT_SERIOUS"

        evidence_for: list[dict[str, Any]] = []
        for tag in tags:
            ref, quote, line = tag_quotes[tag]
            evidence_for.append({"statement": f"The letter describes the device event group '{tag}'.",
                                 "source_id": f"LTR:{ref}:{line}", "quote": quote})
        for ref in listed[:2]:
            item = tools.call("get_dossier_item", {"ref": ref}).get("item")
            evidence_for.append({"statement": "Listed side-effect of the product: a known device-related risk.",
                                 "source_id": ref, "quote": item["text"] if item else None})
        a6 = p.get("a6") or {}
        for c in a6.get("correlations", []):
            if c["relation"] == "agreement" and not c["source_id"].startswith("DOS:"):
                evidence_for.append({"statement": c["statement"], "source_id": c["source_id"], "quote": c["quote"]})
        for s in signals:
            if s["registry_check"]["status"] == "MATCH":
                evidence_for.append({"statement": "Device identity confirmed by the registry check (REG-1.0).",
                                     "source_id": "REG-1.0", "quote": None})
                break

        evidence_against: list[dict[str, Any]] = []
        for letter in inp.letters:
            for label, pattern in _AGAINST_PATTERNS:
                m = re.search(pattern, letter.text, re.IGNORECASE)
                if m:
                    evidence_against.append({"statement": f"{label[0].upper()}{label[1:]}.",
                                             "source_id": f"LTR:{letter.ref}:{_line_of(letter.text, m.start())}",
                                             "quote": _sentence_around(letter.text, m.start())})
        if mismatch_only or "MISMATCH" in statuses:
            evidence_against.append({"statement": "Identifiers contradict the registry; device identity uncertain.",
                                     "source_id": "REG-1.0", "quote": None})
        for c in a6.get("contradictions", []):
            evidence_against.append({"statement": c["statement"], "source_id": c["source_id"], "quote": None})
        if not tags:
            evidence_against.append({"statement": "No device-related event term was found in the letter(s).",
                                     "source_id": None, "quote": None})

        missing = list(p.get("missing_data", []))
        uncertainty = [
            "Offline stand-in: deterministic heuristic from rule features and corpus hits; not clinically validated.",
            CAUSALITY_NOTE,
            "Correlations are named matches and counts only; they are not proof of causality.",
        ]
        if missing:
            uncertainty.append("Missing data limit the assessment: " + ", ".join(missing) + ".")
        if p["case"].get("rs_notes"):
            uncertainty.append("Rule-set notes apply: " + "; ".join(p["case"]["rs_notes"]))

        candidates = []
        for tag in tags:
            if tag in _BREAK_EVENTS:
                ref, quote, _line = tag_quotes[tag]
                candidates.append({"code": "A0401",
                                   "basis": f"Event group '{tag}': {IMDRF_CODES['A0401']['definition'].lower()}",
                                   "quote": quote})
                break
        for s in signals:
            hit = next((i for i in s["crit"]["indicators"] if _REVISION_RE.search(i["match"])), None)
            if hit is not None:
                candidates.append({"code": "F1905", "basis": "Revision, re-operation or explantation reported: "
                                                             + IMDRF_CODES["F1905"]["definition"].lower(),
                                   "quote": hit["quote"]})
                break

        fsca = None
        if tags and (pht or lot_cluster):
            fsca = ("FSCA check (hint for humans): several events with the same lot - a human decides whether a field "
                    "safety corrective action must be assessed.")
        elif material and expectedness == "unexpected":
            fsca = ("FSCA check (hint for humans): unexpected device problem not listed in the dossier - a human "
                    "decides.")
        request = None
        if missing:
            request = {"fields": [{"field": f, "question": _FIELD_QUESTIONS.get(f, (f"Please provide: {f}.", ""))[0],
                                   "reason": _FIELD_QUESTIONS.get(f, ("", "needed for the assessment"))[1]}
                                  for f in missing]}
        return {
            "causality": causality, "causality_rationale": rationale, "product_defect_possible": defect,
            "expectedness": expectedness, "expectedness_basis": basis, "ai_severity": ai,
            "evidence_for": evidence_for, "evidence_against": evidence_against, "uncertainty": uncertainty,
            "missing_data": missing, "imdrf_candidates": candidates, "fsca_check_hint": fsca,
            "request_info_draft": request, "needs_more_evidence": False,
        }

    # ------------------------------------------------------------------ A4 vigilance reporting draft
    def _a4(self, inp: AgentInput, tools: ToolBox) -> dict[str, Any]:
        p = inp.payload
        case = p["case"]
        facts = {f["name"]: f["value"] for f in p.get("a1_facts", []) if f.get("value")}
        a3 = p.get("a3") or {}
        drafts = []
        for idx, rule_set in enumerate(case["rule_sets"]):
            label = case["authority_labels"][idx] if idx < len(case["authority_labels"]) else "unknown"
            mine = [c for c in p["deadlines"] if c["rule_set"] == rule_set and c["status"] in ("OPEN", "OVERDUE")]
            drive = min(mine, key=lambda c: (c["due_end_utc"], c["days"])) if mine else None
            form = ("MORE portal, GB form (label only)" if rule_set.startswith("UK-MDR") else
                    "MORE portal, EU form (label only)" if rule_set == "EU-MDR:NI" else
                    "EU manufacturer incident report form (label only)" if rule_set.startswith("EU-MDR") else
                    "unresolved - a human confirms the rule set")
            codes = ", ".join(c["code"] for c in a3.get("imdrf_candidates", [])) or "none from the demo list"
            text = "\n".join([
                f"DRAFT - not submitted. Rule set {rule_set}; authority label: {label}.",
                f"Case {case['case_key']}; manufacturer awareness {case.get('awareness_anchor')} "
                f"(first receipt, signal {case.get('anchor_ref')}).",
                f"Device: {facts.get('device_model', 'unknown')}, lot {facts.get('lot', 'unknown')}, "
                f"UDI-DI {facts.get('udi_di', 'unknown')}; patient {facts.get('patient_reference', 'unknown')}.",
                f"Event: {facts.get('event', 'unknown')}; outcome: {facts.get('outcome', 'unknown')}; "
                f"event date: {facts.get('event_date', 'unknown')}.",
                f"Causality (draft, pending the medical reviewer): {a3.get('causality', 'not yet assessed')}; "
                f"IMDRF code candidates: {codes}.",
                (f"Deadline from DL-1.0: {drive['due_date']} ({drive['basis']}, {drive['days']} days)."
                 if drive else "Deadline: no open deadline for this rule set."),
                "Nothing is sent by this system; a regulatory user records a submission made outside the system.",
            ])
            drafts.append({"rule_set": rule_set, "authority_label": label, "form_label": form,
                           "driving_due_date": drive["due_date"] if drive else None,
                           "deadline_basis": drive["basis"] if drive else None,
                           "deadline_days": drive["days"] if drive else None, "draft_text": text,
                           "missing_mandatory_data": list(a3.get("missing_data", []))})
        return {"report_drafts": drafts,
                "deadline_note": "Deadlines are copied from rule code DL-1.0 / RS-1.0; the earliest drives priority. "
                                 "No submission and no sending by this system."}
