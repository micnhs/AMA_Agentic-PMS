"""Read-only function tools for agents A2, A3 and A6 (concept 6.5, 13; D-31; tests T21, T26).

The application - not the model - validates every argument against an allow-list, executes
the call read-only and writes exactly one ``TOOL_CALL`` audit record per call attempt (refused
calls included). Limits per agent run: at most 8 calls, 2 rounds and 5 calls per round
(assumption A-12). There is no tool that writes, sends or decides, and no internet access.

Tools: ``search_corpus`` (knowledge search contract, SQLite FTS5 offline / Azure AI Search),
``get_registry_entry``, ``get_dossier_item``, ``get_past_case`` (fictional past cases and
human-approved findings only - drafts are never returned) and ``list_case_signals`` (signals of
the analysed case only, without letter text).
"""
from __future__ import annotations

import datetime as _dt
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .. import audit as A
from ..interfaces import CaseStore, KnowledgeSearch
from ..knowledge import ALLOWED_SOURCE_TYPES, CORPUS_SOURCE_TYPES, InvalidQuery, ReferenceData, SearchQuery
from ..models import Clock, SystemClock
from ..rules.correlation import NOTE_NO_ALL_CLEAR
from .base import ToolCallRecord

MAX_CALLS = 8
MAX_ROUNDS = 2
MAX_CALLS_PER_ROUND = 5
MAX_SEARCH_LIMIT = 20
EXCERPT_CHARS = 900


class ToolError(ValueError):
    """A tool call was refused (invalid argument or limit)."""


class ToolLimitExceeded(ToolError):
    pass


@dataclass(frozen=True)
class Param:
    kind: str  # str | int | list
    description: str
    pattern: str | None = None
    enum: tuple[str, ...] | None = None
    max_len: int = 120
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: dict[str, Param]
    one_of_required: tuple[str, ...] = ()

    def json_schema(self) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for name, p in self.params.items():
            if p.kind == "list":
                item: dict[str, Any] = {"type": "string"}
                if p.enum:
                    item["enum"] = list(p.enum)
                props[name] = {"type": "array", "items": item, "description": p.description}
            else:
                schema: dict[str, Any] = {"type": "integer" if p.kind == "int" else "string",
                                          "description": p.description}
                if p.enum:
                    schema["enum"] = list(p.enum)
                if p.pattern:
                    schema["pattern"] = p.pattern
                props[name] = schema
        return {"type": "object", "properties": props, "additionalProperties": False}


_FAMILY = r"^[a-z0-9-]{3,40}$"
_MODEL = r"^[A-Z]{2}-[A-Z0-9]{1,6}$"
_LOT = r"^[A-Z0-9][A-Z0-9-]{2,30}[A-Z0-9]$"
_DATE = r"^\d{4}-\d{2}-\d{2}$"

TOOL_SPECS: dict[str, ToolSpec] = {
    "search_corpus": ToolSpec(
        "search_corpus",
        "Read-only search in the fictional knowledge corpus (literature, technical documents, past cases, process "
        "findings, dossier side-effects). Structured filters only plus an optional short text (max 8 words, no "
        "addresses or links). No hits is never an all-clear.",
        {
            "source_types": Param("list", "subset of " + ", ".join(ALLOWED_SOURCE_TYPES), enum=ALLOWED_SOURCE_TYPES),
            "family": Param("str", "product family id, e.g. demo-ortho-a", pattern=_FAMILY, max_len=40),
            "model": Param("str", "model code, e.g. OA-S5", pattern=_MODEL, max_len=12),
            "lot": Param("str", "lot number", pattern=_LOT, max_len=40),
            "event_tag": Param("str", "event group from the event-term dictionary", max_len=40),
            "text": Param("str", "short search text (max 8 words)", max_len=120),
            "date_from": Param("str", "YYYY-MM-DD", pattern=_DATE, max_len=10),
            "date_to": Param("str", "YYYY-MM-DD", pattern=_DATE, max_len=10),
            "limit": Param("int", f"1-{MAX_SEARCH_LIMIT}", minimum=1, maximum=MAX_SEARCH_LIMIT),
        },
        one_of_required=("family", "model", "lot", "event_tag", "text"),
    ),
    "get_registry_entry": ToolSpec(
        "get_registry_entry",
        "Read-only lookup in the fictional implant registry by entry id, patient pseudonym, lot, UDI-DI or model "
        "(exact match, combined with AND).",
        {
            "reg_id": Param("str", "registry entry id, e.g. REG-0002", pattern=r"^REG-\d{4}$", max_len=8),
            "patient": Param("str", "patient pseudonym, e.g. P-GB-2208", pattern=r"^P-[A-Z]{2}-\d{4}$", max_len=9),
            "lot": Param("str", "lot number", pattern=_LOT, max_len=40),
            "udi_di": Param("str", "UDI-DI (14 digits, without (01))", pattern=r"^\d{14}$", max_len=14),
            "model": Param("str", "model code", pattern=_MODEL, max_len=12),
        },
        one_of_required=("reg_id", "patient", "lot", "udi_di", "model"),
    ),
    "get_dossier_item": ToolSpec(
        "get_dossier_item",
        "Read-only: one listed undesirable side-effect of a fictional product dossier (section 8).",
        {
            "ref": Param("str", "dossier reference, e.g. DOS:demo-ortho-a:OR-12",
                         pattern=r"^DOS:[a-z0-9-]{3,40}:[A-Z]{2}-\d{2}$", max_len=60),
            "family": Param("str", "product family id", pattern=_FAMILY, max_len=40),
            "item_id": Param("str", "item id, e.g. OR-12", pattern=r"^[A-Z]{2}-\d{2}$", max_len=6),
        },
        one_of_required=("ref", "item_id"),
    ),
    "get_past_case": ToolSpec(
        "get_past_case",
        "Read-only: a fictional past case with its human-approved assessment (H-001 ...) or an approved finding of "
        "this system (FND-00001 ...). Drafts are never returned.",
        {
            "case_ref": Param("str", "past case id, e.g. H-003", pattern=r"^H-\d{3}$", max_len=5),
            "finding_id": Param("str", "approved finding id, e.g. FND-00004", pattern=r"^FND-\d{5}$", max_len=9),
        },
        one_of_required=("case_ref", "finding_id"),
    ),
    "list_case_signals": ToolSpec(
        "list_case_signals",
        "Read-only: the signals of the analysed case (reference, receipt time, relation, rule results) without "
        "letter text.",
        {"case_id": Param("str", "id of the analysed case", pattern=r"^[A-Za-z0-9._:-]{1,60}$", max_len=60)},
        one_of_required=("case_id",),
    ),
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LINK_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def redact(value: Any) -> Any:
    """Arguments as stored in the audit trail: addresses and links removed, strings truncated."""
    if isinstance(value, str):
        return _LINK_RE.sub("[redacted link]", _EMAIL_RE.sub("[redacted address]", value))[:200]
    if isinstance(value, list):
        return [redact(v) for v in value[:20]]
    if isinstance(value, dict):
        return {str(k)[:40]: redact(v) for k, v in list(value.items())[:20]}
    return value


def validate_arguments(spec: ToolSpec, arguments: Any) -> dict[str, Any]:
    """Allow-list validation in app code (never trusts the model). Raises :class:`ToolError`."""
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")
    clean: dict[str, Any] = {}
    for name, value in arguments.items():
        if value is None:
            continue
        param = spec.params.get(name)
        if param is None:
            raise ToolError(f"unknown parameter {name!r} for {spec.name}")
        if param.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError(f"{name} must be an integer")
            if (param.minimum is not None and value < param.minimum) or (
                    param.maximum is not None and value > param.maximum):
                raise ToolError(f"{name} must be between {param.minimum} and {param.maximum}")
        elif param.kind == "list":
            if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
                raise ToolError(f"{name} must be a non-empty list of strings")
            if param.enum and any(v not in param.enum for v in value):
                raise ToolError(f"{name} must be a subset of {list(param.enum)}")
        else:
            if not isinstance(value, str) or not value.strip():
                raise ToolError(f"{name} must be a non-empty string")
            if len(value) > param.max_len:
                raise ToolError(f"{name} is longer than {param.max_len} characters")
            if _EMAIL_RE.search(value) or _LINK_RE.search(value) or "@" in value:
                raise ToolError(f"{name} must not contain addresses or links")
            if param.pattern and not re.match(param.pattern, value):
                raise ToolError(f"{name} has an invalid format")
            if param.enum and value not in param.enum:
                raise ToolError(f"{name} must be one of {list(param.enum)}")
        clean[name] = value
    if spec.one_of_required and not any(k in clean for k in spec.one_of_required):
        raise ToolError(f"{spec.name} needs at least one of {', '.join(spec.one_of_required)}")
    return clean


class ToolBox:
    """Validated, audited, limited read-only tools for one agent run."""

    def __init__(self, *, store: CaseStore, ref: ReferenceData, search: KnowledgeSearch, case_id: str,
                 signal_id: str | None, agent_id: str, agent_meta: dict[str, Any], allowed: Iterable[str],
                 clock: Clock | None = None, max_calls: int = MAX_CALLS, max_rounds: int = MAX_ROUNDS,
                 max_calls_per_round: int | None = MAX_CALLS_PER_ROUND,
                 now: Callable[[], _dt.datetime] | None = None) -> None:
        self.store = store
        self.ref = ref
        self.search_backend = search
        self.case_id = case_id
        self.signal_id = signal_id
        self.agent_id = agent_id
        self.agent_meta = dict(agent_meta)
        self.allowed = tuple(allowed)
        unknown = [t for t in self.allowed if t not in TOOL_SPECS]
        if unknown:
            raise ValueError(f"unknown tools {unknown}")
        self.clock = clock or SystemClock()
        self._now = now or self.clock.now
        self.max_calls = max_calls
        self.max_rounds = max_rounds
        self.max_calls_per_round = max_calls_per_round
        self.round = 0
        self._round_calls = 0
        self.calls: list[ToolCallRecord] = []
        self.refused_over_limit = 0

    # ------------------------------------------------------------------ limits
    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - len(self.calls))

    def can_begin_round(self) -> bool:
        return self.round < self.max_rounds and self.remaining > 0

    def begin_round(self) -> int:
        if self.round >= self.max_rounds:
            raise ToolLimitExceeded(f"at most {self.max_rounds} rounds per run")
        self.round += 1
        self._round_calls = 0
        return self.round

    def definitions(self) -> list[dict[str, Any]]:
        return [{"name": TOOL_SPECS[n].name, "description": TOOL_SPECS[n].description,
                 "parameters": TOOL_SPECS[n].json_schema()} for n in self.allowed]

    # ------------------------------------------------------------------ calls
    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Validate, execute read-only and audit one call. Refusals return ``{"error": ...}``."""
        started = time.perf_counter()
        if self.round == 0:
            self.begin_round()
        args = dict(arguments or {})
        over_limit = None
        if len(self.calls) >= self.max_calls:
            over_limit = f"tool call limit reached ({self.max_calls} per run)"
        elif self.max_calls_per_round is not None and self._round_calls >= self.max_calls_per_round:
            over_limit = f"tool call limit reached ({self.max_calls_per_round} per round)"
        if over_limit is not None:
            self.refused_over_limit += 1
            self._audit(name, args, ok=False, error=over_limit, result_count=0, source_ids=[], call_no=None)
            return {"error": over_limit}
        call_no = len(self.calls) + 1
        self._round_calls += 1
        error: str | None = None
        result: dict[str, Any] = {}
        clean: dict[str, Any] = {}
        try:
            if name not in self.allowed:
                raise ToolError(f"tool {name!r} is not available to {self.agent_id}")
            clean = validate_arguments(TOOL_SPECS[name], args)
            result = getattr(self, f"_tool_{name}")(**clean)
        except (ToolError, InvalidQuery) as exc:
            error = str(exc)
        source_ids = list(result.get("source_ids", [])) if error is None else []
        seq = self._audit(name, clean if error is None else args, ok=error is None, error=error,
                          result_count=int(result.get("count", 0)) if error is None else 0, source_ids=source_ids,
                          call_no=call_no)
        self.calls.append(ToolCallRecord(
            call_no=call_no, round=self.round, tool=name, arguments=redact(clean if error is None else args),
            ok=error is None, error=error, result_count=int(result.get("count", 0)) if error is None else 0,
            source_ids=source_ids, audit_seq=seq, duration_ms=round((time.perf_counter() - started) * 1000, 2)))
        if error is not None:
            return {"error": error}
        return result

    def _audit(self, name: str, args: dict[str, Any], *, ok: bool, error: str | None, result_count: int,
               source_ids: list[str], call_no: int | None) -> int | None:
        meta = self.agent_meta
        record = self.store.audit.append(
            event_type=A.TOOL_CALL, source="agent", actor_type="agent", actor_id=self.agent_id,
            case_id=self.case_id, signal_id=self.signal_id,
            before={"tool": name, "arguments": redact(args)},
            after={"ok": ok, "error": error, "results": result_count, "call_no": call_no, "round": self.round,
                   "read_only": True},
            agent_version=meta.get("agent_version"), model=meta.get("model"), model_version=meta.get("model_version"),
            prompt_version=meta.get("prompt_version"), corpus_version=self.search_backend.corpus_version,
            source_ids=source_ids, ts=self._now(),
        )
        return record.get("seq")

    # ------------------------------------------------------------------ tool implementations (read-only)
    def _excerpt(self, source_id: str) -> str | None:
        text = self.ref.source_text(source_id)
        return text[:EXCERPT_CHARS] if text else None

    def _tool_search_corpus(self, source_types: list[str] | None = None, family: str | None = None,
                            model: str | None = None, lot: str | None = None, event_tag: str | None = None,
                            text: str | None = None, date_from: str | None = None, date_to: str | None = None,
                            limit: int = 10) -> dict[str, Any]:
        if family is not None and family not in self.ref.catalogue.families:
            raise ToolError("family is not a known product family")
        query = SearchQuery(source_types=tuple(source_types or CORPUS_SOURCE_TYPES + ("dossier_item",)),
                            family=family, model=model, lot=lot, event_tag=event_tag, text=text,
                            date_from=date_from, date_to=date_to, limit=limit)
        hits = self.search_backend.search(query)
        items = [{"source_id": h.ref, "source_type": h.source_type, "family": h.family, "title": h.title,
                  "citation": h.citation, "status": h.status, "snippet": h.snippet, "excerpt": self._excerpt(h.ref)}
                 for h in hits]
        return {"hits": items, "count": len(items), "source_ids": [h["source_id"] for h in items],
                "corpus_version": self.search_backend.corpus_version, "note": NOTE_NO_ALL_CLEAR}

    def _tool_get_registry_entry(self, reg_id: str | None = None, patient: str | None = None,
                                 lot: str | None = None, udi_di: str | None = None,
                                 model: str | None = None) -> dict[str, Any]:
        entries = [e for e in self.ref.registry
                   if (reg_id is None or e.reg_id == reg_id) and (patient is None or e.patient == patient)
                   and (lot is None or e.lot == lot) and (udi_di is None or e.udi_di == udi_di)
                   and (model is None or e.model == model)][:10]
        rows = [{"source_id": f"REG:{e.reg_id}", "reg_id": e.reg_id, "patient": e.patient, "family": e.family,
                 "model": e.model, "udi_di": e.udi_di, "lot": e.lot, "serial": e.serial,
                 "implant_date": e.implant_date, "site": e.site, "country": e.country, "market": e.market}
                for e in entries]
        return {"entries": rows, "count": len(rows), "source_ids": [r["source_id"] for r in rows],
                "note": "fictional registry; no entry found is not an all-clear"}

    def _tool_get_dossier_item(self, ref: str | None = None, family: str | None = None,
                               item_id: str | None = None) -> dict[str, Any]:
        if ref is None:
            if family is None:
                raise ToolError("item_id needs family")
            ref = f"DOS:{family}:{item_id}"
        item = self.ref.dossier_item(ref)
        if item is None:
            return {"item": None, "count": 0, "source_ids": [], "note": "no such dossier item"}
        return {"item": {"source_id": item.ref, "family": item.family, "item_id": item.item_id, "text": item.text,
                         "citation": item.citation, "listed": True},
                "count": 1, "source_ids": [item.ref]}

    def _tool_get_past_case(self, case_ref: str | None = None, finding_id: str | None = None) -> dict[str, Any]:
        if case_ref is not None:
            item = self.ref.corpus_item(case_ref)
            if item is None or item.item_type != "past_case":
                return {"past_case": None, "count": 0, "source_ids": [], "note": "no such past case"}
            return {"past_case": {"source_id": item.item_id, "title": item.title, "family": item.family,
                                  "models": list(item.models), "lots": list(item.lots), "tags": list(item.tags),
                                  "text": item.text, "citation": item.citation, "status": item.status,
                                  "fictional": True},
                    "count": 1, "source_ids": [item.item_id]}
        try:
            finding = self.store.get_finding(str(finding_id))
        except KeyError:
            return {"past_case": None, "count": 0, "source_ids": [], "note": "no such finding"}
        if finding.kind != "approved_assessment" or finding.status != "approved":
            return {"past_case": None, "count": 0, "source_ids": [],
                    "note": "only human-approved findings are available; drafts are never returned"}
        return {"past_case": {"source_id": finding.finding_id, "case_id": finding.case_id,
                              "summary": finding.summary, "reviewer": finding.reviewer_id,
                              "approved_at": finding.created_at, "content_hash": finding.content_hash},
                "count": 1, "source_ids": [finding.finding_id]}

    def _tool_list_case_signals(self, case_id: str) -> dict[str, Any]:
        if case_id != self.case_id:
            raise ToolError("only the signals of the analysed case are available")
        rows = []
        for s in self.store.list_signals(case_id):
            rows.append({"signal_id": s.signal_id, "ref": s.ref, "received_at": s.received_at, "source": s.source,
                         "relation": s.relation, "readable": s.readable, "language": s.language,
                         "crit_severity": s.crit.get("severity"), "registry_check": s.registry.get("value"),
                         "event_tags": [t["tag"] for t in s.extraction.get("event_tags", [])],
                         "injection_flag": bool(s.injection.get("flag"))})
        return {"signals": rows, "count": len(rows), "source_ids": []}
