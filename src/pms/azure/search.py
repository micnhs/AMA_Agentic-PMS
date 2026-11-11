"""Knowledge search on Azure AI Search behind the offline search contract (ADR-007; INFRA_NOTES 5.3).

The index ``AZURE_SEARCH_INDEX`` holds exactly the documents of the offline SQLite FTS5 index (the
fictional corpus plus the dossier side-effect items, see :class:`pms.knowledge.KnowledgeIndex`). The app
creates and fills it itself with its Entra identity (no keys) - at ``python -m pms seed --index-only``,
on the worker start and lazily before the first query of a process; a sync is idempotent (documents are
compared by content hash).

Queries keep the offline semantics:

* the same :class:`pms.knowledge.SearchQuery` validation (allow-list, no addresses, at most 8 words);
* exact filters (source type, family, model, lot, event tag) run as an OData filter on the service and
  are re-checked locally together with the date filters, so filter-only queries - and therefore the
  deterministic CORR-1.0 correlation - return the same hits in the same order (by ref) as offline;
* text queries use the service's full-text search (custom analyzer: standard tokenizer, lowercase, ASCII
  folding ~ FTS5 unicode61 remove_diacritics) and are re-checked locally with the offline phrase
  semantics (AND for ``match_all``, else OR), ranked by the service score; ranks can differ from FTS5
  bm25, the matched set does not.

Azure SDK imports (azure-search-documents, azure-identity, requests for the REST fallback) are lazy.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from typing import Any, Callable

from ..audit import canonical_json
from ..knowledge import MAX_TEXT_TOKENS, ReferenceData, SearchHit, SearchQuery
from .errors import AzureAdapterError, status_code
from .identity import auth_header

log = logging.getLogger("pms.azure.search")

ANALYZER = "pms_folding"
MAX_TOP = 1000
SEARCH_SCOPE = "https://search.azure.com/.default"
REST_API_VERSION = "2024-07-01"
SELECT_FIELDS: tuple[str, ...] = ("key", "ref", "source_type", "family", "title", "body", "citation", "status",
                                  "item_date", "models", "lots", "tags", "corpus_version", "content_sha")
_TOKEN_RE = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)  # the token rule of pms.knowledge
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)  # FTS5 unicode61: letters and digits, everything else separates


class SearchIndexError(AzureAdapterError):
    """The knowledge index is missing or could not be created/filled."""


# --------------------------------------------------------------------------- documents and index definition
def doc_key(ref: str) -> str:
    """Document key: refs contain ':' (not allowed in keys) - URL-safe base64 is injective and allowed."""
    return base64.urlsafe_b64encode(ref.encode("utf-8")).decode("ascii")


def _document(ref: str, source_type: str, family: str | None, title: str, body: str, citation: str, status: str,
              item_date: str | None, models: Any, lots: Any, tags: Any, corpus_version: str) -> dict[str, Any]:
    doc = {"ref": ref, "source_type": source_type, "family": family, "title": title, "body": body,
           "citation": citation, "status": status, "item_date": item_date, "models": list(models),
           "lots": list(lots), "tags": list(tags), "corpus_version": corpus_version}
    doc["content_sha"] = hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()
    doc["key"] = doc_key(ref)
    return doc


def index_documents(ref_data: ReferenceData) -> list[dict[str, Any]]:
    """The documents of the offline index (same fields as ``KnowledgeIndex._build``)."""
    version = ref_data.corpus_version
    docs = [_document(i.item_id, i.item_type, i.family, i.title, i.body, i.citation, i.status, i.item_date,
                      i.models, i.lots, i.tags, version) for i in ref_data.corpus]
    docs += [_document(d.ref, "dossier_item", d.family, f"{d.item_id}: {d.text}", d.text, d.citation,
                       "product dossier (fictional)", None, (), (), (), version) for d in ref_data.dossier_items]
    return docs


def index_definition(name: str) -> dict[str, Any]:
    """Index definition in the REST shape (also converted to SDK models)."""
    def field(field_name: str, field_type: str = "Edm.String", *, key: bool = False, searchable: bool = False,
              filterable: bool = False, sortable: bool = False, facetable: bool = False) -> dict[str, Any]:
        out = {"name": field_name, "type": field_type, "key": key, "searchable": searchable,
               "filterable": filterable, "sortable": sortable, "facetable": facetable, "retrievable": True}
        if searchable:
            out["analyzer"] = ANALYZER
        return out

    collection = "Collection(Edm.String)"
    return {
        "name": name,
        "fields": [
            field("key", key=True, filterable=True),
            field("ref", filterable=True, sortable=True),
            field("source_type", filterable=True, facetable=True),
            field("family", filterable=True, facetable=True),
            field("models", collection, filterable=True),
            field("lots", collection, filterable=True),
            field("tags", collection, filterable=True),
            field("title", searchable=True),
            field("body", searchable=True),
            field("citation"),
            field("status"),
            field("item_date", filterable=True, sortable=True),
            field("corpus_version", filterable=True),
            field("content_sha"),
        ],
        "analyzers": [{"@odata.type": "#Microsoft.Azure.Search.CustomAnalyzer", "name": ANALYZER,
                       "tokenizer": "standard_v2", "tokenFilters": ["lowercase", "asciifolding"]}],
    }


def sdk_index(definition: dict[str, Any]) -> Any:
    """SDK model of :func:`index_definition` (azure-search-documents field helpers)."""
    from azure.search.documents.indexes.models import (  # type: ignore[import-not-found]
        CustomAnalyzer, SearchableField, SearchIndex, SimpleField,
    )

    fields = []
    for f in definition["fields"]:
        if f["searchable"]:
            fields.append(SearchableField(name=f["name"], type=f["type"], analyzer_name=f["analyzer"]))
        else:
            fields.append(SimpleField(name=f["name"], type=f["type"], key=f["key"], filterable=f["filterable"],
                                      sortable=f["sortable"], facetable=f["facetable"]))
    analyzers = [CustomAnalyzer(name=a["name"], tokenizer_name=a["tokenizer"], token_filters=list(a["tokenFilters"]))
                 for a in definition["analyzers"]]
    return SearchIndex(name=definition["name"], fields=fields, analyzers=analyzers)


# --------------------------------------------------------------------------- query helpers
def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def odata_filter(query: SearchQuery) -> str:
    """Exact filters for the service (dates are checked locally; the service returns a superset)."""
    parts = [f"search.in(source_type, {_literal(','.join(query.source_types))}, ',')"]
    if query.family is not None:
        parts.append(f"family eq {_literal(query.family)}")
    if query.model is not None:
        parts.append(f"models/any(x: x eq {_literal(query.model)})")
    if query.lot is not None:
        parts.append(f"lots/any(x: x eq {_literal(query.lot)})")
    if query.event_tag is not None:
        parts.append(f"tags/any(x: x eq {_literal(query.event_tag)})")
    return " and ".join(parts)


def _fold(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)).lower()


def _words(text: str | None) -> list[str]:
    return _WORD_RE.findall(_fold(text or ""))


def _contains(words: list[str], phrase: list[str]) -> bool:
    n = len(phrase)
    return n > 0 and any(words[i:i + n] == phrase for i in range(len(words) - n + 1))


def query_tokens(query: SearchQuery) -> list[str]:
    return _TOKEN_RE.findall(query.text)[:MAX_TEXT_TOKENS] if query.text is not None else []


def matches(doc: dict[str, Any], query: SearchQuery, tokens: list[str]) -> bool:
    """The offline SQL/FTS5 conditions of :meth:`pms.knowledge.KnowledgeIndex.search` for one document."""
    if doc.get("source_type") not in query.source_types:
        return False
    if query.family is not None and doc.get("family") != query.family:
        return False
    if query.model is not None and query.model not in (doc.get("models") or ()):
        return False
    if query.lot is not None and query.lot not in (doc.get("lots") or ()):
        return False
    if query.event_tag is not None and query.event_tag not in (doc.get("tags") or ()):
        return False
    item_date = doc.get("item_date")
    if query.date_from is not None and item_date is not None and item_date < query.date_from:
        return False
    if query.date_to is not None and item_date is not None and item_date > query.date_to:
        return False
    if tokens:
        title, body = _words(doc.get("title")), _words(doc.get("body"))
        found = [_contains(title, _words(t)) or _contains(body, _words(t)) for t in tokens]
        return all(found) if query.match_all else any(found)
    return True


def snippet(body: str, tokens: list[str], width: int = 12) -> str:
    """Up to ``width`` words of the body around the first match (like FTS5 ``snippet``)."""
    parts = (body or "").split()
    wanted = {w for t in tokens for w in _words(t)}
    hit = next((i for i, part in enumerate(parts) if wanted & set(_words(part))), 0)
    start = max(0, min(hit - 2, len(parts) - width))
    window = parts[start:start + width]
    return ("... " if start > 0 else "") + " ".join(window) + (" ..." if start + width < len(parts) else "")


# --------------------------------------------------------------------------- the adapter
class AzureKnowledgeSearch:
    """Read-only knowledge search on Azure AI Search (:class:`pms.interfaces.KnowledgeSearch`)."""

    def __init__(self, ref_data: ReferenceData, *, endpoint: str, index_name: str,
                 credential_factory: Callable[[], Any] | None = None, client_id: str | None = None,
                 auto_sync: bool = True, http: Any = None, settle_timeout_s: float = 30.0) -> None:
        if not endpoint or not index_name:
            raise SearchIndexError("AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_INDEX must be set")
        self.ref_data = ref_data
        self.corpus_version = ref_data.corpus_version
        self.endpoint = endpoint.rstrip("/")
        self.index_name = index_name
        self.auto_sync = auto_sync
        self.settle_timeout_s = settle_timeout_s
        self._client_id = client_id
        self._credential_factory = credential_factory
        self._http = http
        self._known_tags = set(ref_data.event_terms) | {t for i in ref_data.corpus for t in i.tags}
        self._docs = index_documents(ref_data)
        self._lock = threading.RLock()
        self._ready = False
        self._client: Any = None

    # ------------------------------------------------------------------ clients
    def _credential(self) -> Any:
        if self._credential_factory is not None:
            return self._credential_factory()
        from .identity import get_credential

        return get_credential(self._client_id)

    def _search_client(self) -> Any:
        if self._client is None:
            from azure.search.documents import SearchClient  # type: ignore[import-not-found]

            self._client = SearchClient(self.endpoint, self.index_name, self._credential())
        return self._client

    def _index_client(self) -> Any:
        from azure.search.documents.indexes import SearchIndexClient  # type: ignore[import-not-found]

        return SearchIndexClient(self.endpoint, self._credential())

    def describe(self) -> str:
        return f"Azure AI Search {self.endpoint} index {self.index_name} (corpus {self.corpus_version})"

    # ------------------------------------------------------------------ index maintenance (seed, worker start)
    def _existing(self) -> dict[str, str]:
        out: dict[str, str] = {}
        skip = 0
        while True:
            page = list(self._search_client().search(search_text="*", select=["key", "content_sha"], top=MAX_TOP,
                                                     skip=skip))
            out.update({str(r["key"]): str(r.get("content_sha") or "") for r in page})
            if len(page) < MAX_TOP:
                return out
            skip += MAX_TOP

    def status(self) -> dict[str, Any]:
        """Is the index present and does it hold exactly the current corpus?"""
        try:
            existing = self._existing()
        except Exception as exc:
            if status_code(exc) == 404:
                return {"index": self.index_name, "exists": False, "documents": 0, "current": False}
            raise
        expected = {d["key"]: d["content_sha"] for d in self._docs}
        return {"index": self.index_name, "exists": True, "documents": len(existing),
                "missing_or_changed": sum(1 for k, sha in expected.items() if existing.get(k) != sha),
                "stale": sum(1 for k in existing if k not in expected), "current": existing == expected,
                "corpus_version": self.corpus_version}

    def _put_index_rest(self, definition: dict[str, Any]) -> None:
        import requests  # type: ignore[import-not-found]

        token = self._credential().get_token(SEARCH_SCOPE).token
        session = self._http or requests
        response = session.put(f"{self.endpoint}/indexes/{self.index_name}",
                               params={"api-version": REST_API_VERSION, "allowIndexDowntime": "true"},
                               headers={**auth_header(token), "Content-Type": "application/json"},
                               data=json.dumps(definition), timeout=60)
        if response.status_code not in (200, 201, 204):
            raise SearchIndexError(f"creating the index {self.index_name} failed: HTTP {response.status_code} "
                                   f"{str(getattr(response, 'text', ''))[:300]}")

    def ensure_index(self) -> str:
        """Create or update the index definition (idempotent); recreate it when a field changed incompatibly."""
        definition = index_definition(self.index_name)
        try:
            index = sdk_index(definition)
        except (ImportError, AttributeError, TypeError) as exc:
            log.info("search SDK models not usable (%s) - using the REST API for the index definition", exc)
            self._put_index_rest(definition)
            return "created or updated (REST)"
        client = self._index_client()
        try:
            client.create_or_update_index(index)
            return "created or updated"
        except Exception as exc:
            if status_code(exc) is None and isinstance(exc, (TypeError, AttributeError, ValueError)):
                log.info("search SDK could not serialise the index (%s) - using the REST API", exc)
                self._put_index_rest(definition)
                return "created or updated (REST)"
            if status_code(exc) != 400:
                raise
            log.warning("index %s has an incompatible definition - recreating it from the corpus", self.index_name)
            client.delete_index(self.index_name)
            client.create_or_update_index(index)
            return "recreated"

    def _wait_until(self, expected: dict[str, str]) -> None:
        deadline = time.monotonic() + self.settle_timeout_s
        while self._existing() != expected:
            if time.monotonic() > deadline:
                raise SearchIndexError(f"the index {self.index_name} did not reach the uploaded state within "
                                       f"{self.settle_timeout_s:.0f} s")
            time.sleep(1.0)

    def sync(self, *, force: bool = False) -> dict[str, Any]:
        """Make the index hold exactly the current corpus (upload changed, delete stale documents)."""
        action = self.ensure_index()
        existing = self._existing()
        expected = {d["key"]: d["content_sha"] for d in self._docs}
        upload = [d for d in self._docs if force or existing.get(d["key"]) != d["content_sha"]]
        stale = sorted(k for k in existing if k not in expected)
        client = self._search_client()
        failed: list[str] = []
        for i in range(0, len(upload), 500):
            results = client.merge_or_upload_documents(documents=upload[i:i + 500]) or []
            failed += [str(getattr(r, "key", "?")) for r in results if getattr(r, "succeeded", True) is False]
        if stale:
            client.delete_documents(documents=[{"key": k} for k in stale])
        if failed:
            raise SearchIndexError(f"{len(failed)} documents were not indexed: {failed[:5]}")
        if upload or stale:
            self._wait_until(expected)
        with self._lock:
            self._ready = True
        return {"index": self.index_name, "endpoint": self.endpoint, "index_definition": action,
                "corpus_version": self.corpus_version, "documents": len(self._docs), "uploaded": len(upload),
                "deleted": len(stale), "unchanged": len(self._docs) - len(upload)}

    def ensure_ready(self) -> None:
        """Once per process before the first query: the index must hold the current corpus."""
        if self._ready:
            return
        with self._lock:  # other threads wait: they need the synchronised index as well
            if self._ready:
                return
            state = self.status()
            if not state["current"]:
                if not self.auto_sync:
                    raise SearchIndexError(f"the knowledge index {self.index_name} does not hold corpus "
                                           f"{self.corpus_version}: run 'python -m pms seed --index-only'")
                try:
                    log.info("knowledge index synchronised: %s", self.sync())
                except Exception as exc:
                    if not (state["exists"] and state["documents"]):
                        raise SearchIndexError(f"the knowledge index {self.index_name} is missing and could not be "
                                               f"created: {type(exc).__name__}: {exc}") from exc
                    log.warning("knowledge index %s is not current and could not be synchronised (%s: %s) - "
                                "searching the existing documents", self.index_name, type(exc).__name__, exc)
            self._ready = True

    # ------------------------------------------------------------------ search contract
    def search(self, query: SearchQuery) -> list[SearchHit]:
        """Run a validated read-only query. Raises :class:`pms.knowledge.InvalidQuery` when refused."""
        query.validate(self._known_tags)
        self.ensure_ready()
        tokens = query_tokens(query)
        kwargs: dict[str, Any] = {"filter": odata_filter(query), "top": MAX_TOP, "select": list(SELECT_FIELDS)}
        if tokens:
            kwargs.update(search_text=" ".join('"' + t.replace('"', "") + '"' for t in tokens),
                          search_mode="all" if query.match_all else "any", query_type="simple",
                          search_fields=["title", "body"])
        else:
            kwargs["search_text"] = "*"
        found = [dict(r) for r in self._search_client().search(**kwargs)]
        docs = [d for d in found if matches(d, query, tokens)]
        if tokens:
            docs.sort(key=lambda d: (-float(d.get("@search.score") or 0.0), str(d["ref"])))
        else:
            docs.sort(key=lambda d: str(d["ref"]))
        return [SearchHit(d["ref"], d["source_type"], d.get("family"), d.get("title") or "", d.get("citation") or "",
                          d.get("status") or "",
                          snippet(d.get("body") or "", tokens) if tokens else (d.get("body") or "")[:160])
                for d in docs[:query.limit]]

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # closing never fails the caller
                pass
        self._client = None
