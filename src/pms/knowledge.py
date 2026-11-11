"""Fictional reference data and the offline knowledge index (SQLite FTS5).

Everything loaded here is synthetic and versioned in ``data/synthetic`` (golden set
v1.0, concept section 11 and appendix F). Two parts:

* :class:`ReferenceData` - read-only loader for the product catalogue (``products.json``
  plus the model tables of the dossiers), the dossier side-effect items (section 8),
  the fictional implant registry, the knowledge corpus (literature, technical
  documents, past cases, process findings) and the DE/EN/FR/IT event-term dictionary.
* :class:`KnowledgeIndex` - an in-memory SQLite FTS5 index behind a small, validated
  search contract (:class:`SearchQuery`). The deterministic correlation CORR-1.0 uses
  exact filter queries; agent A6 may use the same contract as a read-only tool with a
  short free-text query (concept 6.5, ADR-007: "offline SQLite FTS5 behind the same
  search contract" as Azure AI Search).

Search results never mean "all clear": an empty result only says that the fictional
corpus holds no matching item (concept 3.4, 6.5).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .versions import default_data_dir

# --------------------------------------------------------------------------- records


@dataclass(frozen=True)
class ProductFamily:
    family_id: str
    name_en: str
    name_de: str
    brand_prefix: str  # e.g. "Fictivia CB" - first two words of the English name


@dataclass(frozen=True)
class CatalogueModel:
    family: str
    model: str  # model code, e.g. "OA-S5"
    udi_di: str  # GTIN-14 without the "(01)" application identifier
    component: str  # English component name, e.g. "stem" or "aortic bioprosthesis"
    description: str  # English description from the dossier model table


@dataclass(frozen=True)
class ProductCatalogue:
    families: dict[str, ProductFamily]
    models: dict[str, CatalogueModel]

    def by_udi(self, udi_di: str) -> CatalogueModel | None:
        for model in self.models.values():
            if model.udi_di == udi_di:
                return model
        return None

    def family_of_model(self, model_code: str) -> str | None:
        model = self.models.get(model_code)
        return model.family if model else None


@dataclass(frozen=True)
class RegistryEntry:
    reg_id: str
    patient: str
    family: str
    model: str
    udi_di: str
    lot: str
    serial: str | None
    implant_date: str | None
    site: str | None
    country: str | None  # ISO 3166 code; GB nations as GB-ENG/GB-SCT/GB-WLS/GB-NIR
    market: str | None
    placed_on_gb_market: str | None = None


@dataclass(frozen=True)
class DossierItem:
    """One listed undesirable side-effect of a product dossier (section 8)."""

    family: str
    item_id: str  # e.g. "CV-04"
    text: str  # side-effect description without the frequency
    line: int  # 1-based line number in the dossier file
    file: str  # repository-relative path

    @property
    def ref(self) -> str:
        return f"DOS:{self.family}:{self.item_id}"

    @property
    def citation(self) -> str:
        return f"{self.file}, section 8, item {self.item_id}, line {self.line}"


@dataclass(frozen=True)
class CorpusItem:
    item_id: str
    item_type: str  # literature | technical_document | past_case | process_finding
    file: str
    family: str | None
    models: tuple[str, ...]
    lots: tuple[str, ...]
    tags: tuple[str, ...]
    title: str
    status: str
    text: str  # full source text (used for verbatim quote checks)
    body: str  # main content used for full-text search
    item_date: str | None = None  # ISO date (literature: year start; past case: received)

    @property
    def citation(self) -> str:
        return f"{self.file} ({self.item_type}: {self.title})"


@dataclass
class ReferenceData:
    """Read-only fictional reference data (golden set v1.0)."""

    data_dir: Path
    catalogue: ProductCatalogue
    registry: tuple[RegistryEntry, ...]
    dossier_items: tuple[DossierItem, ...]
    corpus: tuple[CorpusItem, ...]
    event_terms: dict[str, tuple[str, ...]]
    corpus_version: str
    trend: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, data_dir: str | Path | None = None) -> "ReferenceData":
        base = Path(data_dir) if data_dir is not None else default_data_dir()
        catalogue, dossier_items = _load_products(base)
        registry = _load_registry(base)
        index = json.loads((base / "knowledge" / "knowledge_index.json").read_text(encoding="utf-8"))
        event_terms = {str(k): tuple(str(t) for t in v) for k, v in index.get("event_terms", {}).items()}
        corpus = _load_corpus(base, index)
        trend_path = base / "trend" / "monthly_counts.json"
        trend = json.loads(trend_path.read_text(encoding="utf-8")) if trend_path.exists() else {}
        return cls(
            data_dir=base,
            catalogue=catalogue,
            registry=tuple(registry),
            dossier_items=tuple(dossier_items),
            corpus=tuple(corpus),
            event_terms=event_terms,
            corpus_version=str(index.get("corpus_version", "unknown")),
            trend=trend,
        )

    def corpus_item(self, item_id: str) -> CorpusItem | None:
        return next((i for i in self.corpus if i.item_id == item_id), None)

    def dossier_item(self, ref: str) -> DossierItem | None:
        return next((d for d in self.dossier_items if d.ref == ref), None)

    def registry_entry(self, reg_id: str) -> RegistryEntry | None:
        return next((e for e in self.registry if e.reg_id == reg_id), None)

    def seed_signals(self) -> list[Any]:
        """The six unchanged demo messages of ``seeds/seeds.json`` as :class:`pms.models.SignalInput`
        (source "seed") with their structured metadata; LINK-1.0 honours the metadata."""
        from .models import SignalInput

        data = json.loads((self.data_dir / "seeds" / "seeds.json").read_text(encoding="utf-8"))
        keys = ("message_id", "case_id", "kind", "previous_message_id", "duplicate_of_message_id",
                "product_family", "market", "event_date", "reporter_label")
        out = []
        for record in data.get("seeds", []):
            out.append(SignalInput(text=record["text"], received_at=record["received_at"], source="seed",
                                   ref=record["message_id"], metadata={k: record.get(k) for k in keys if k in record}))
        return out

    def process_findings(self) -> list[CorpusItem]:
        return [i for i in self.corpus if i.item_type == "process_finding"]

    def source_text(self, ref: str) -> str | None:
        """Verbatim source text for a correlation reference (for quote verification)."""
        if ref.startswith("DOS:"):
            item = self.dossier_item(ref)
            return item.text if item else None
        if ref.startswith("REG:"):
            entry = self.registry_entry(ref[4:])
            return json.dumps(entry.__dict__, ensure_ascii=False) if entry else None
        item = self.corpus_item(ref)
        return item.text if item else None


# --------------------------------------------------------------------------- loaders
_REL_PREFIX = "data/synthetic"
_DOSSIER_TABLE_ROW = re.compile(r"^\|\s*([A-Z]{2}-[A-Z0-9]+)(?:\s*\(([^)]*)\))?\s*\|\s*\(01\)(\d{14})\s*\|")
_DOSSIER_ITEM = re.compile(r"^-\s*([A-Z]{2}-\d{2}):\s*(.+?)\s*$")
_SIZE_SUFFIX = re.compile(r"\s*(?:size\s*\d+|\d+\s*mm)\s*$", re.IGNORECASE)


def _rel(path: Path, base: Path) -> str:
    return f"{_REL_PREFIX}/{path.relative_to(base).as_posix()}"


def _load_products(base: Path) -> tuple[ProductCatalogue, list[DossierItem]]:
    products = json.loads((base / "products" / "products.json").read_text(encoding="utf-8"))
    families: dict[str, ProductFamily] = {}
    models: dict[str, CatalogueModel] = {}
    dossier_items: list[DossierItem] = []
    for family_id, fam in products.items():
        name_en = str(fam.get("name_en", family_id))
        brand = " ".join(name_en.split()[:2])
        families[family_id] = ProductFamily(family_id, name_en, str(fam.get("name_de", "")), brand)
        # Generic noun of the family, e.g. "aortic bioprosthesis" (used when the dossier
        # model table gives no component description).
        family_noun = re.sub(r"\(.*?\)", "", name_en).replace(brand, "").strip().lower()
        dossier_path = base / "products" / f"{family_id}_dossier.md"
        table: dict[str, tuple[str, str]] = {}
        if dossier_path.exists():
            lines = dossier_path.read_text(encoding="utf-8").splitlines()
            in_section_8 = False
            for lineno, line in enumerate(lines, start=1):
                row = _DOSSIER_TABLE_ROW.match(line)
                if row:
                    table[row.group(1)] = ((row.group(2) or "").strip(), row.group(3))
                if line.startswith("## "):
                    in_section_8 = line.startswith("## 8")
                    continue
                if in_section_8:
                    item = _DOSSIER_ITEM.match(line)
                    if item:
                        text = re.split(r"\s+[–-]\s+frequency\b", item.group(2), maxsplit=1)[0]
                        dossier_items.append(
                            DossierItem(family_id, item.group(1), text.strip(), lineno, _rel(dossier_path, base))
                        )
        for entry in fam.get("models", []):
            code = str(entry["model"]).split(" (")[0].strip()
            description, _udi = table.get(code, ("", ""))
            component = _SIZE_SUFFIX.sub("", description).strip() if description else family_noun
            models[code] = CatalogueModel(
                family=family_id,
                model=code,
                udi_di=str(entry["udi_di"]),
                component=component or family_noun,
                description=description,
            )
    return ProductCatalogue(families, models), dossier_items


def _load_registry(base: Path) -> list[RegistryEntry]:
    data = json.loads((base / "registry" / "registry.json").read_text(encoding="utf-8"))
    out = []
    for e in data.get("entries", []):
        out.append(
            RegistryEntry(
                reg_id=e["reg_id"],
                patient=e["patient"],
                family=e["family"],
                model=e["model"],
                udi_di=e["udi_di"],
                lot=e["lot"],
                serial=e.get("serial"),
                implant_date=e.get("implant_date"),
                site=e.get("site"),
                country=e.get("country"),
                market=e.get("market"),
                placed_on_gb_market=e.get("placed_on_gb_market"),
            )
        )
    return out


def _markdown_body(text: str) -> str:
    """Content after the first second-level heading (summary or excerpt)."""
    parts = re.split(r"^## .*$", text, maxsplit=1, flags=re.MULTILINE)
    return parts[1].strip() if len(parts) > 1 else text.strip()


def _load_corpus(base: Path, index: dict[str, Any]) -> list[CorpusItem]:
    past_cases_path = base / "knowledge" / "past_cases" / "past_cases.json"
    past = json.loads(past_cases_path.read_text(encoding="utf-8")) if past_cases_path.exists() else {}
    by_id: dict[str, dict[str, Any]] = {c["id"]: c for c in past.get("cases", [])}
    by_id.update({f["id"]: f for f in past.get("process_findings", [])})
    items: list[CorpusItem] = []
    for raw in index.get("items", []):
        item_type = raw["type"]
        rel_file = str(raw["file"])
        path = base / rel_file.removeprefix(_REL_PREFIX + "/")
        item_date: str | None = None
        if item_type in ("literature", "technical_document"):
            text = path.read_text(encoding="utf-8")
            body = _markdown_body(text)
            year = re.search(r"^- Journal:.*?(\d{4})\s*$", text, flags=re.MULTILINE)
            item_date = f"{year.group(1)}-01-01" if year else None
        else:
            record = by_id.get(raw["id"], {})
            fields = ("event", "finding", "finding_status", "reporting", "summary", "status")
            text = " ".join(str(record[k]) for k in fields if record.get(k))
            body = text
            item_date = record.get("received")
        items.append(
            CorpusItem(
                item_id=raw["id"],
                item_type=item_type,
                file=rel_file,
                family=raw.get("family"),
                models=tuple(raw.get("models", [])),
                lots=tuple(raw.get("lots", [])),
                tags=tuple(raw.get("tags", [])),
                title=str(raw.get("title", "")),
                status=str(raw.get("status", "")),
                text=text,
                body=body,
                item_date=item_date,
            )
        )
    return items


# --------------------------------------------------------------------------- search contract
CORPUS_SOURCE_TYPES: tuple[str, ...] = ("literature", "technical_document", "past_case", "process_finding")
ALLOWED_SOURCE_TYPES: tuple[str, ...] = CORPUS_SOURCE_TYPES + ("dossier_item",)
MAX_LIMIT = 50
MAX_TEXT_CHARS = 120
MAX_TEXT_TOKENS = 8
_MODEL_RE = re.compile(r"^[A-Z]{2}-[A-Z0-9]{1,6}$")
_LOT_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,30}[A-Z0-9]$")
_FAMILY_RE = re.compile(r"^[a-z0-9-]{3,40}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOKEN_RE = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)


class InvalidQuery(ValueError):
    """A search parameter is outside the allow-list (the call is refused, not widened)."""


@dataclass(frozen=True)
class SearchQuery:
    """Read-only search request (concept 6.5: source type, family, model, lot, event term,
    period, short search text). Unknown or malformed parameters are refused."""

    source_types: tuple[str, ...] = CORPUS_SOURCE_TYPES
    family: str | None = None
    model: str | None = None
    lot: str | None = None
    event_tag: str | None = None
    text: str | None = None
    match_all: bool = False  # AND instead of OR for the text tokens
    date_from: str | None = None
    date_to: str | None = None
    limit: int = 10

    def validate(self, known_tags: Iterable[str] = ()) -> None:
        if not self.source_types or any(t not in ALLOWED_SOURCE_TYPES for t in self.source_types):
            raise InvalidQuery(f"source_types must be a non-empty subset of {ALLOWED_SOURCE_TYPES}")
        if self.family is not None and not _FAMILY_RE.match(self.family):
            raise InvalidQuery("family has an invalid format")
        if self.model is not None and not _MODEL_RE.match(self.model):
            raise InvalidQuery("model has an invalid format")
        if self.lot is not None and not _LOT_RE.match(self.lot):
            raise InvalidQuery("lot has an invalid format")
        tags = set(known_tags)
        if self.event_tag is not None and tags and self.event_tag not in tags:
            raise InvalidQuery("event_tag is not in the event-term dictionary or corpus tags")
        for name in ("date_from", "date_to"):
            value = getattr(self, name)
            if value is not None and not _DATE_RE.match(value):
                raise InvalidQuery(f"{name} must be YYYY-MM-DD")
        if not isinstance(self.limit, int) or not 1 <= self.limit <= MAX_LIMIT:
            raise InvalidQuery(f"limit must be between 1 and {MAX_LIMIT}")
        if self.text is not None:
            if len(self.text) > MAX_TEXT_CHARS:
                raise InvalidQuery(f"text longer than {MAX_TEXT_CHARS} characters")
            if "@" in self.text or "://" in self.text or "www." in self.text.lower():
                raise InvalidQuery("text must not contain addresses or links")
            tokens = _TOKEN_RE.findall(self.text)
            if not tokens:
                raise InvalidQuery("text has no searchable words")
            if len(tokens) > MAX_TEXT_TOKENS:
                raise InvalidQuery(f"text has more than {MAX_TEXT_TOKENS} words")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_types": list(self.source_types),
            "family": self.family,
            "model": self.model,
            "lot": self.lot,
            "event_tag": self.event_tag,
            "text": self.text,
            "match_all": self.match_all,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class SearchHit:
    ref: str
    source_type: str
    family: str | None
    title: str
    citation: str
    status: str
    snippet: str


class KnowledgeIndex:
    """Offline index of the fictional corpus and the dossier side-effect items.

    Uses an in-memory SQLite database (no files, rebuilt from the versioned corpus at
    start-up). Thread-safe for concurrent read calls.
    """

    def __init__(self, ref_data: ReferenceData) -> None:
        self.ref_data = ref_data
        self.corpus_version = ref_data.corpus_version
        self._lock = threading.Lock()
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._build()
        self._known_tags = set(ref_data.event_terms) | {t for i in ref_data.corpus for t in i.tags}

    def _build(self) -> None:
        db = self._db
        db.executescript(
            """
            CREATE TABLE docs (ref TEXT PRIMARY KEY, source_type TEXT NOT NULL, family TEXT,
                               title TEXT, body TEXT, citation TEXT, status TEXT, item_date TEXT);
            CREATE TABLE doc_models (ref TEXT NOT NULL, model TEXT NOT NULL);
            CREATE TABLE doc_lots (ref TEXT NOT NULL, lot TEXT NOT NULL);
            CREATE TABLE doc_tags (ref TEXT NOT NULL, tag TEXT NOT NULL);
            CREATE VIRTUAL TABLE docs_fts USING fts5(ref UNINDEXED, title, body,
                                                     tokenize='unicode61 remove_diacritics 2');
            """
        )
        for item in self.ref_data.corpus:
            self._insert(item.item_id, item.item_type, item.family, item.title, item.body, item.citation,
                         item.status, item.item_date, item.models, item.lots, item.tags)
        for d in self.ref_data.dossier_items:
            self._insert(d.ref, "dossier_item", d.family, f"{d.item_id}: {d.text}", d.text, d.citation,
                         "product dossier (fictional)", None, (), (), ())
        db.commit()

    def _insert(self, ref: str, source_type: str, family: str | None, title: str, body: str, citation: str,
                status: str, item_date: str | None, models: Sequence[str], lots: Sequence[str],
                tags: Sequence[str]) -> None:
        db = self._db
        db.execute("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?)",
                   (ref, source_type, family, title, body, citation, status, item_date))
        db.executemany("INSERT INTO doc_models VALUES (?,?)", [(ref, m) for m in models])
        db.executemany("INSERT INTO doc_lots VALUES (?,?)", [(ref, lot) for lot in lots])
        db.executemany("INSERT INTO doc_tags VALUES (?,?)", [(ref, t) for t in tags])
        db.execute("INSERT INTO docs_fts (ref, title, body) VALUES (?,?,?)", (ref, title, body))

    @staticmethod
    def _fts_expression(text: str, match_all: bool) -> str:
        tokens = _TOKEN_RE.findall(text)[:MAX_TEXT_TOKENS]
        quoted = ['"' + t.replace('"', "") + '"' for t in tokens]
        return (" AND " if match_all else " OR ").join(quoted)

    def search(self, query: SearchQuery) -> list[SearchHit]:
        """Run a validated read-only query. Raises :class:`InvalidQuery` when refused."""
        query.validate(self._known_tags)
        where = [f"d.source_type IN ({','.join('?' * len(query.source_types))})"]
        params: list[Any] = list(query.source_types)
        if query.family is not None:
            where.append("d.family = ?")
            params.append(query.family)
        if query.model is not None:
            where.append("EXISTS (SELECT 1 FROM doc_models m WHERE m.ref = d.ref AND m.model = ?)")
            params.append(query.model)
        if query.lot is not None:
            where.append("EXISTS (SELECT 1 FROM doc_lots l WHERE l.ref = d.ref AND l.lot = ?)")
            params.append(query.lot)
        if query.event_tag is not None:
            where.append("EXISTS (SELECT 1 FROM doc_tags t WHERE t.ref = d.ref AND t.tag = ?)")
            params.append(query.event_tag)
        if query.date_from is not None:
            where.append("(d.item_date IS NULL OR d.item_date >= ?)")
            params.append(query.date_from)
        if query.date_to is not None:
            where.append("(d.item_date IS NULL OR d.item_date <= ?)")
            params.append(query.date_to)
        if query.text is not None:
            sql = (
                "SELECT d.ref, d.source_type, d.family, d.title, d.citation, d.status, "
                "snippet(docs_fts, 2, '', '', ' ... ', 12) FROM docs_fts JOIN docs d ON d.ref = docs_fts.ref "
                f"WHERE docs_fts MATCH ? AND {' AND '.join(where)} ORDER BY bm25(docs_fts), d.ref LIMIT ?"
            )
            params = [self._fts_expression(query.text, query.match_all)] + params + [query.limit]
        else:
            sql = (
                "SELECT d.ref, d.source_type, d.family, d.title, d.citation, d.status, substr(d.body, 1, 160) "
                f"FROM docs d WHERE {' AND '.join(where)} ORDER BY d.ref LIMIT ?"
            )
            params.append(query.limit)
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [SearchHit(*row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
