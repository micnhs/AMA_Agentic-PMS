"""CORR-1.0 - deterministic correlation index (concept 6.3 and 6.5; design element D-27).

Event terms in DE/EN/FR/IT (dictionary ``event_terms`` of
``data/synthetic/knowledge/knowledge_index.json``) assign event groups to a letter. The
code then searches the fictional knowledge corpus, the product dossiers, the registry
and all other cases for

* the same lot,
* the same model,
* the same product family with the same event group,
* listed side-effects of the product dossier that belong to the event group,
* further registry patients with the same lot,

and shows every hit with its source ID (expected.json ``conventions.correlation``:
"lot, model, family + event term, registry"). Only counts and named matches, no rates,
scores or significance (Goal Card QUALITY; conflict K-15). A match can only raise
urgency (a hint to the human); "no hits" never means "harmless" (concept 3.4, 6.5).

Queries are built only from allowed structured fields (lot, model, family, event group);
letter text never becomes a query, so an injected instruction cannot steer a search
(test T21). Source: change request R-15 of 10 Nov 2026 (DEC-R15); 01-vision.md VIS-003.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..knowledge import CORPUS_SOURCE_TYPES, KnowledgeIndex, RegistryEntry, SearchQuery
from ..versions import CORR
from .extract import line_of, sentence_at

#: Event group -> keyword queries on the listed side-effects (section 8) of the product
#: dossier. All words of one query must occur in the side-effect text. Sources:
#: data/synthetic/products/demo-cardio-b_dossier.md lines 44-52 and
#: data/synthetic/products/demo-ortho-a_dossier.md lines 48-60. The clinical relation of
#: radiolucent lines to loosening/migration follows LIT-008 ("rarely precede loosening
#: ... migration need closer follow-up"); loosening presenting as instability or
#: dislocation follows LIT-009 (fictional corpus, data/synthetic/knowledge/literature).
DOSSIER_QUERIES: dict[str, tuple[str, ...]] = {
    "leaflet tear": ("leaflet tear",),  # CV-01 structural valve deterioration (leaflet tear)
    "endocarditis": ("endocarditis",),  # CV-04 prosthetic valve endocarditis
    "stem fracture": ("fracture stem",),  # OR-12 fracture of implant components (ceramic head, stem)
    "liner dissociation": ("dissociation",),  # not listed: candidate for "unexpected"
    "valve thrombosis": ("valve thrombosis",),  # CV-02 valve thrombosis
    "squeaking": ("squeaking",),  # OR-11 noise (squeaking)
    "ceramic head fracture": ("fracture ceramic head",),  # OR-12
    "radiolucent line": ("loosening", "migration"),  # OR-04 aseptic loosening / migration
    "cup loosening": ("loosening", "migration", "dislocation", "instability"),  # OR-04, OR-01
}

STATUS_RAN = "RAN"
STATUS_NOT_RUN = "NOT_RUN_NO_IDENTIFIERS"

NOTE_NO_ALL_CLEAR = ("Named matches and counts only; no rates or scores. A correlation is not proof of "
                     "causality, and no hits never means all-clear.")
NOTE_NOT_RUN = "No identifiers (lot, model or product family): correlation cannot run; missing data stay visible."

# Match types
SAME_LOT = "same_lot"
SAME_MODEL = "same_model"
FAMILY_EVENT = "family_event_term"
DOSSIER_SIDE_EFFECT = "dossier_listed_side_effect"
REGISTRY_SAME_LOT = "registry_same_lot"


@dataclass(frozen=True)
class EventTag:
    tag: str
    term: str
    quote: str
    line: int


def tag_events(text: str, dictionary: Mapping[str, Sequence[str]]) -> list[EventTag]:
    """Event groups whose dictionary terms occur in ``text`` (case-insensitive, substring
    so that German compounds match). Dictionary order; first hit per group."""
    tags: list[EventTag] = []
    for tag, terms in dictionary.items():
        best: tuple[int, str] | None = None
        for term in terms:
            m = re.search(re.escape(term), text, re.IGNORECASE)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), m.group(0))
        if best is not None:
            tags.append(EventTag(tag, best[1], sentence_at(text, best[0]), line_of(text, best[0])))
    return tags


@dataclass(frozen=True)
class CaseProfile:
    """Correlation-relevant view of another case (for the search across all cases)."""

    case_id: str
    case_key: str
    family: str | None
    models: tuple[str, ...]
    lots: tuple[str, ...]
    event_tags: tuple[str, ...]


@dataclass(frozen=True)
class CorrelationHit:
    ref: str
    source_type: str
    match_types: tuple[str, ...]
    citation: str
    title: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "match_types": list(self.match_types)}


@dataclass(frozen=True)
class CorrelationResult:
    status: str
    event_tags: tuple[str, ...]
    keys: dict[str, Any]
    hits: tuple[CorrelationHit, ...]
    counts: dict[str, int]
    queries: tuple[dict[str, Any], ...]
    note: str
    corpus_version: str
    rule_version: str = CORR

    @property
    def refs(self) -> list[str]:
        return [h.ref for h in self.hits]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "event_tags": list(self.event_tags),
            "keys": dict(self.keys),
            "hits": [h.to_dict() for h in self.hits],
            "counts": dict(self.counts),
            "queries": [dict(q) for q in self.queries],
            "note": self.note,
            "corpus_version": self.corpus_version,
            "rule_version": self.rule_version,
        }


class _Collector:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.hits: dict[str, dict[str, Any]] = {}

    def add(self, ref: str, source_type: str, match_type: str, citation: str, title: str, status: str) -> None:
        if ref not in self.hits:
            self.order.append(ref)
            self.hits[ref] = {"source_type": source_type, "match_types": [], "citation": citation,
                              "title": title, "status": status}
        if match_type not in self.hits[ref]["match_types"]:
            self.hits[ref]["match_types"].append(match_type)

    def result(self) -> tuple[CorrelationHit, ...]:
        return tuple(
            CorrelationHit(ref, h["source_type"], tuple(h["match_types"]), h["citation"], h["title"], h["status"])
            for ref, h in ((r, self.hits[r]) for r in self.order)
        )


def correlate(
    *,
    event_tags: Sequence[str],
    family: str | None,
    models: Sequence[str],
    lots: Sequence[str],
    index: KnowledgeIndex,
    registry: Sequence[RegistryEntry] = (),
    own_registry_ids: Sequence[str] = (),
    cases: Sequence[CaseProfile] = (),
    own_case_id: str | None = None,
    dictionary: Mapping[str, Sequence[str]] | None = None,
) -> CorrelationResult:
    """Run CORR-1.0 for one signal. Extra hits are fine; missing ones are a defect."""
    keys = {"family": family, "models": list(models), "lots": list(lots)}
    if not (family or models or lots):
        return CorrelationResult(STATUS_NOT_RUN, tuple(event_tags), keys, (), {"hits": 0}, (), NOTE_NOT_RUN,
                                 index.corpus_version)

    col = _Collector()
    queries: list[dict[str, Any]] = []

    def run(query: SearchQuery, match_type: str) -> None:
        queries.append({"index": "knowledge", **query.as_dict()})
        for hit in index.search(query):
            col.add(hit.ref, hit.source_type, match_type, hit.citation, hit.title, hit.status)

    for lot in lots:
        run(SearchQuery(source_types=CORPUS_SOURCE_TYPES, lot=lot, limit=50), SAME_LOT)
    for model in models:
        run(SearchQuery(source_types=CORPUS_SOURCE_TYPES, model=model, limit=50), SAME_MODEL)
    if family:
        for tag in event_tags:
            run(SearchQuery(source_types=CORPUS_SOURCE_TYPES, family=family, event_tag=tag, limit=50), FAMILY_EVENT)
        for tag in event_tags:
            texts = DOSSIER_QUERIES.get(tag) or tuple((dictionary or {}).get(tag, ()))
            for text in texts:
                run(SearchQuery(source_types=("dossier_item",), family=family, text=text, match_all=True, limit=50),
                    DOSSIER_SIDE_EFFECT)

    own = set(own_registry_ids)
    same_lot_entries = [e for e in registry if e.lot in set(lots) and e.reg_id not in own]
    if lots:
        queries.append({"index": "registry", "lots": list(lots), "exclude": sorted(own)})
    for e in same_lot_entries:
        col.add(f"REG:{e.reg_id}", "registry_entry", REGISTRY_SAME_LOT,
                f"data/synthetic/registry/registry.json, entry {e.reg_id}",
                f"{e.model} lot {e.lot}, implanted {e.implant_date} ({e.country})", "fictional registry")
    same_model_count = sum(1 for e in registry if e.model in set(models) and e.reg_id not in own)

    if cases:
        queries.append({"index": "cases", "lots": list(lots), "models": list(models), "family": family,
                        "event_tags": list(event_tags), "exclude_case": own_case_id})
    for c in cases:
        if c.case_id == own_case_id:
            continue
        ref = f"CASE:{c.case_key}"
        title = f"case {c.case_key}"
        if set(c.lots) & set(lots):
            col.add(ref, "case", SAME_LOT, f"case {c.case_key}", title, "case in this system")
        if set(c.models) & set(models):
            col.add(ref, "case", SAME_MODEL, f"case {c.case_key}", title, "case in this system")
        if family and c.family == family and set(c.event_tags) & set(event_tags):
            col.add(ref, "case", FAMILY_EVENT, f"case {c.case_key}", title, "case in this system")

    hits = col.result()
    counts: dict[str, int] = {"hits": len(hits), "registry_same_lot": len(same_lot_entries),
                              "registry_same_model": same_model_count}
    for h in hits:
        counts[h.source_type] = counts.get(h.source_type, 0) + 1
    return CorrelationResult(STATUS_RAN, tuple(event_tags), keys, hits, counts, tuple(queries), NOTE_NO_ALL_CLEAR,
                             index.corpus_version)
