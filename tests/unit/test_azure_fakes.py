"""FAKE in-memory Azure SDK clients for the Azure adapter tests (no network, no real package, no live call).

``fake_modules()`` returns stand-ins for ``azure.core``, ``azure.identity``, ``azure.cosmos``,
``azure.search.documents``, ``azure.communication.email`` and ``requests``; tests inject them with
``mock.patch.dict(sys.modules, fake_modules(...))``. The fakes follow the documented service behaviour the
adapters rely on: Cosmos DB partitions, ``_etag`` + ``IfNotModified`` (412), duplicate ids and unique keys
(409), a create/read-only role on the audit container (403), integral numbers returned without a decimal
point, the subset of Cosmos SQL the store emits; Azure AI Search OData filters, ``searchMode`` any/all and
paging; the ACS e-mail poller; Blob REST (BlockBlob PUT, container create, list XML). The tests at the end
check these semantics.
"""
from __future__ import annotations

import collections
import enum
import itertools
import json
import re
import sys
import threading
import time
import types
import unicodedata
import unittest
import urllib.parse
import uuid
from typing import Any, Callable
from unittest import mock

AccessToken = collections.namedtuple("AccessToken", "token expires_on")


# =============================================================================== azure.core
class MatchConditions(enum.Enum):
    Unconditionally = 1
    IfNotModified = 2
    IfModified = 3
    IfPresent = 4
    IfMissing = 5


class HttpResponseError(Exception):
    def __init__(self, message: str | None = None, response: Any = None, *, status_code: int | None = None) -> None:
        super().__init__(message or "")
        self.message = message
        self.response = response
        self.status_code = status_code


class ResourceNotFoundError(HttpResponseError):
    pass


class ResourceExistsError(HttpResponseError):
    pass


# =============================================================================== azure.identity
class CredentialLog:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.tokens: list[str] = []


CREDENTIALS = CredentialLog()


class _Credential:
    kind = "credential"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        CREDENTIALS.created.append((self.kind, kwargs))

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        CREDENTIALS.tokens.extend(scopes)
        return AccessToken(f"fake-token-{self.kind}", time.time() + 3600)

    def close(self) -> None:
        pass


class ManagedIdentityCredential(_Credential):
    kind = "managed"


class DefaultAzureCredential(_Credential):
    kind = "default"


# =============================================================================== Cosmos DB
class CosmosHttpResponseError(HttpResponseError):
    def __init__(self, status_code: int | None = None, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(message or f"HTTP {status_code}", status_code=status_code)
        self.sub_status = kwargs.get("sub_status")


class CosmosResourceNotFoundError(CosmosHttpResponseError):
    def __init__(self, status_code: int = 404, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(status_code, message or "Resource Not Found", **kwargs)


class CosmosResourceExistsError(CosmosHttpResponseError):
    def __init__(self, status_code: int = 409, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(status_code, message or "Conflict", **kwargs)


class CosmosAccessConditionFailedError(CosmosHttpResponseError):
    def __init__(self, status_code: int = 412, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(status_code, message or "Precondition Failed", **kwargs)


def _normalise(value: Any) -> Any:
    """Cosmos DB stores numbers as doubles and returns integral values without a decimal point."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 2 ** 53:
        return int(value)
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


_SELECT_RE = re.compile(r'^SELECT\s+(?:TOP\s+(?P<top>\d+)\s+)?(?P<sel>\*|VALUE COUNT\(1\))\s+FROM\s+c'
                        r'(?:\s+WHERE\s+(?P<where>.+?))?'
                        r'(?:\s+ORDER BY\s+c\["(?P<order>\w+)"\]\s+(?P<dir>ASC|DESC))?\s*$', re.S)
_CMP_RE = re.compile(r'^c\["(\w+)"\]\s*(=|!=|>=|<=|>|<)\s*(@\w+)$')
_ARR_RE = re.compile(r'^ARRAY_CONTAINS\((@\w+),\s*c\["(\w+)"\]\)$')


def _same_kind(a: Any, b: Any) -> bool:
    def kind(v: Any) -> str:
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, (int, float)):
            return "number"
        return type(v).__name__
    return kind(a) == kind(b)


def _compile_query(query: str) -> tuple[Callable[[dict[str, Any], dict[str, Any]], bool], dict[str, Any]]:
    m = _SELECT_RE.match(query.strip())
    if not m:
        raise CosmosHttpResponseError(400, f"fake Cosmos SQL cannot parse: {query}")
    preds = []
    for part in (re.split(r"\s+AND\s+", m.group("where")) if m.group("where") else []):
        cmp_m, arr_m = _CMP_RE.match(part.strip()), _ARR_RE.match(part.strip())
        if cmp_m:
            field, op, param = cmp_m.groups()

            def pred(doc: dict, params: dict, field: str = field, op: str = op, param: str = param) -> bool:
                if field not in doc:
                    return False
                a, b = doc[field], params[param]
                if not _same_kind(a, b):
                    return op == "!="
                if op == "=":
                    return a == b
                if op == "!=":
                    return a != b
                if a is None or b is None or isinstance(a, (dict, list)):
                    return False
                return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
            preds.append(pred)
        elif arr_m:
            param, field = arr_m.groups()

            def pred(doc: dict, params: dict, field: str = field, param: str = param) -> bool:
                return field in doc and any(_same_kind(doc[field], v) and doc[field] == v for v in params[param])
            preds.append(pred)
        else:
            raise CosmosHttpResponseError(400, f"fake Cosmos SQL cannot parse condition: {part}")
    info = {"top": int(m.group("top")) if m.group("top") else None, "count": m.group("sel") != "*",
            "order": m.group("order"), "desc": m.group("dir") == "DESC"}
    return (lambda doc, params: all(p(doc, params) for p in preds)), info


class FakeContainer:
    """One container: logical partitions by ``pk``, etags, unique keys, an optional data-plane role."""

    def __init__(self, account: "FakeCosmosAccount", name: str, *, unique_keys: tuple[str, ...] = (),
                 allowed_ops: frozenset[str] | None = None) -> None:
        self.account = account
        self.name = name
        self.unique_keys = unique_keys
        self.allowed_ops = allowed_ops
        self.partitions: dict[str, dict[str, tuple[dict[str, Any], str]]] = {}
        self.exists = True
        self.forbidden = False

    # ------------------------------------------------------------------ helpers
    def _op(self, op: str, **info: Any) -> None:
        self.account.ops.append((self.name, op, info.get("id"), info.get("pk")))
        if not self.exists:
            raise CosmosResourceNotFoundError(message=f"container {self.name} not found", sub_status=1003)
        if self.forbidden or (self.allowed_ops is not None and op not in self.allowed_ops):
            raise CosmosHttpResponseError(403, f"Request blocked by Auth: {op} is not allowed on {self.name}")
        hook = self.account.hooks.get((self.name, op))
        if hook is not None:
            hook(self, info)

    def _store(self, body: dict[str, Any]) -> dict[str, Any]:
        doc = json.loads(json.dumps(body))
        if self.account.normalise_numbers:
            doc = _normalise(doc)
        for key in ("_rid", "_self", "_attachments"):
            doc.pop(key, None)
        doc["_etag"] = f'"{uuid.uuid4().hex}"'
        doc["_ts"] = int(time.time())
        raw = json.dumps(doc)
        self.partitions.setdefault(str(doc["pk"]), {})[str(doc["id"])] = (doc, raw)
        return json.loads(raw)

    def _get(self, item: str, pk: Any) -> tuple[dict[str, Any], str]:
        found = self.partitions.get(str(pk), {}).get(str(item))
        if found is None:
            raise CosmosResourceNotFoundError()
        return found

    def _check_etag(self, current: dict[str, Any], kwargs: dict[str, Any]) -> None:
        if kwargs.get("match_condition") == MatchConditions.IfNotModified and kwargs.get("etag") != current["_etag"]:
            raise CosmosAccessConditionFailedError()

    def all_docs(self) -> list[dict[str, Any]]:
        return [json.loads(raw) for part in self.partitions.values() for _doc, raw in part.values()]

    def raw_doc(self, item: str, pk: Any) -> dict[str, Any]:
        """A copy of the stored document (no operation is logged)."""
        return json.loads(self._get(item, pk)[1])

    def tamper(self, item: str, pk: Any, change: Callable[[dict[str, Any]], None]) -> None:
        """Edit a stored document OUTSIDE the app (an attacker with account access) - tests only."""
        doc = json.loads(self._get(item, pk)[1])
        change(doc)
        self.partitions[str(pk)][str(item)] = (doc, json.dumps(doc))

    # ------------------------------------------------------------------ ContainerProxy API
    def read(self, **kwargs: Any) -> dict[str, Any]:
        self._op("read_container")
        return {"id": self.name, "partitionKey": {"paths": ["/pk"]}}

    def read_item(self, item: str, partition_key: Any, **kwargs: Any) -> dict[str, Any]:
        with self.account.lock:
            self._op("read", id=item, pk=partition_key)
            return json.loads(self._get(item, partition_key)[1])

    def create_item(self, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        with self.account.lock:
            self._op("create", id=body.get("id"), pk=body.get("pk"), body=body)
            part = self.partitions.get(str(body["pk"]), {})
            if str(body["id"]) in part:
                raise CosmosResourceExistsError(message=f"Entity with the specified id already exists: {body['id']}")
            for key in self.unique_keys:
                if any(doc.get(key) == body.get(key) for doc, _raw in part.values()):
                    raise CosmosResourceExistsError(message=f"Unique index constraint violation on /{key}")
            return self._store(body)

    def upsert_item(self, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        with self.account.lock:
            self._op("upsert", id=body.get("id"), pk=body.get("pk"))
            return self._store(body)

    def replace_item(self, item: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        with self.account.lock:
            self._op("replace", id=item, pk=body.get("pk"))
            current, _raw = self._get(item, body.get("pk"))
            self._check_etag(current, kwargs)
            return self._store({**body, "id": item})

    def delete_item(self, item: str, partition_key: Any, **kwargs: Any) -> None:
        with self.account.lock:
            self._op("delete", id=item, pk=partition_key)
            current, _raw = self._get(item, partition_key)
            self._check_etag(current, kwargs)
            del self.partitions[str(partition_key)][str(item)]

    def query_items(self, query: str, parameters: list[dict[str, Any]] | None = None, partition_key: Any = None,
                    enable_cross_partition_query: bool | None = None, **kwargs: Any) -> list[Any]:
        with self.account.lock:
            self._op("query", pk=partition_key)
            if partition_key is None and not enable_cross_partition_query:
                raise CosmosHttpResponseError(400, "Cross partition query is required but disabled")
            cache = self.account.query_cache
            if query not in cache:
                cache[query] = _compile_query(query)
            pred, info = cache[query]
            params = {p["name"]: p["value"] for p in (parameters or [])}
            parts = [self.partitions.get(str(partition_key), {})] if partition_key is not None else list(
                self.partitions.values())
            hits = [(doc, raw) for part in parts for doc, raw in part.values() if pred(doc, params)]
            if info["count"]:
                return [len(hits)]
            if info["order"]:
                hits = [h for h in hits if info["order"] in h[0]]
                hits.sort(key=lambda h: h[0][info["order"]], reverse=info["desc"])
            if info["top"] is not None:
                hits = hits[:info["top"]]
            return [json.loads(raw) for _doc, raw in hits]


class FakeDatabase:
    def __init__(self, account: "FakeCosmosAccount") -> None:
        self.account = account

    def get_container_client(self, name: str) -> FakeContainer:
        return self.account.containers[name]


class FakeCosmosAccount:
    """The account behind one endpoint, with the containers of infra/modules/cosmos.bicep."""

    def __init__(self, endpoint: str = "https://fake-cosmos.documents.azure.com:443/", *,
                 normalise_numbers: bool = True) -> None:
        self.endpoint = endpoint
        self.lock = threading.RLock()
        self.ops: list[tuple[str, str, Any, Any]] = []
        self.hooks: dict[tuple[str, str], Callable[[FakeContainer, dict[str, Any]], None]] = {}
        self.query_cache: dict[str, Any] = {}
        self.normalise_numbers = normalise_numbers
        self.containers = {name: FakeContainer(self, name) for name in ("cases", "signals", "records")}
        self.containers["audit"] = FakeContainer(self, "audit", unique_keys=("seq",),
                                                 allowed_ops=frozenset({"read_container", "create", "read", "query"}))

    def ops_on(self, container: str) -> collections.Counter:
        return collections.Counter(op for name, op, _id, _pk in self.ops if name == container)


class FakeCosmosClient:
    accounts: dict[str, FakeCosmosAccount] = {}
    created: list[tuple[str, Any]] = []

    def __init__(self, url: str, credential: Any = None, **kwargs: Any) -> None:
        self.url, self.credential = url, credential
        FakeCosmosClient.created.append((url, credential))
        self.account = FakeCosmosClient.accounts.setdefault(url, FakeCosmosAccount(url))

    def get_database_client(self, database: str) -> FakeDatabase:
        return FakeDatabase(self.account)


def cosmos_account_for(client_account: FakeCosmosAccount, database: str = "pms") -> Any:
    """A :class:`pms.azure.cosmos_store.CosmosAccount` on a fake account (one per simulated process)."""
    from pms.azure.cosmos_store import CosmosAccount

    client = types.SimpleNamespace(get_database_client=lambda name: FakeDatabase(client_account))
    return CosmosAccount(client_account.endpoint, database, client=client)


# =============================================================================== Azure AI Search
def _fold(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)).lower()


def _analyse(text: str) -> list[str]:
    """standard tokenizer + lowercase + asciifolding (approximation)."""
    return re.findall(r"[^\W_]+", _fold(text or ""))


class _Model:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class SearchIndex(_Model):
    pass


class CustomAnalyzer(_Model):
    pass


def SimpleField(**kwargs: Any) -> _Model:  # noqa: N802 - SDK helper name
    return _Model(searchable=False, **kwargs)


def SearchableField(**kwargs: Any) -> _Model:  # noqa: N802 - SDK helper name
    return _Model(searchable=True, **kwargs)


class IndexingResult(_Model):
    pass


def _split_top(expr: str, sep: str) -> list[str]:
    parts, depth, quote, start, i = [], 0, False, 0, 0
    while i < len(expr):
        ch = expr[i]
        if ch == "'":
            if quote and i + 1 < len(expr) and expr[i + 1] == "'":
                i += 2
                continue
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
        elif not quote and depth == 0 and expr.startswith(sep, i):
            parts.append(expr[start:i])
            start = i + len(sep)
            i = start
            continue
        i += 1
    parts.append(expr[start:])
    return [p.strip() for p in parts]


def _odata_literal(text: str) -> Any:
    text = text.strip()
    if text == "null":
        return None
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    raise ValueError(f"fake OData cannot parse literal {text}")


def odata_predicate(expr: str | None) -> Callable[[dict[str, Any]], bool]:
    if not expr:
        return lambda doc: True
    conds = []
    for part in _split_top(expr, " and "):
        m_in = re.match(r"^search\.in\((\w+),\s*('(?:[^']|'')*'),\s*'(.)'\)$", part)
        m_any = re.match(r"^(\w+)/any\((\w+):\s*\2\s+eq\s+('(?:[^']|'')*')\)$", part)
        m_cmp = re.match(r"^(\w+)\s+(eq|ne|gt|ge|lt|le)\s+(.+)$", part)
        if m_in:
            field, values = m_in.group(1), _odata_literal(m_in.group(2)).split(m_in.group(3))
            conds.append(lambda d, f=field, v=values: d.get(f) in v)
        elif m_any:
            field, value = m_any.group(1), _odata_literal(m_any.group(3))
            conds.append(lambda d, f=field, v=value: v in (d.get(f) or []))
        elif m_cmp:
            field, op, value = m_cmp.group(1), m_cmp.group(2), _odata_literal(m_cmp.group(3))

            def cmp(d: dict, f: str = field, op: str = op, v: Any = value) -> bool:
                a = d.get(f)
                if op == "eq":
                    return a == v
                if op == "ne":
                    return a != v
                if a is None or v is None:
                    return False
                return {"gt": a > v, "ge": a >= v, "lt": a < v, "le": a <= v}[op]
            conds.append(cmp)
        else:
            raise HttpResponseError(f"Invalid expression: {part}", status_code=400)
    return lambda doc: all(c(doc) for c in conds)


class FakeSearchService:
    services: dict[str, "FakeSearchService"] = {}

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.indexes: dict[str, dict[str, Any]] = {}
        self.ops: list[tuple[str, Any]] = []
        self.lock = threading.RLock()
        self.fail_index_create: Exception | list[Exception] | None = None

    @classmethod
    def get(cls, endpoint: str) -> "FakeSearchService":
        return cls.services.setdefault(endpoint.rstrip("/"), FakeSearchService(endpoint.rstrip("/")))

    def define(self, name: str, definition: Any) -> None:
        with self.lock:
            index = self.indexes.setdefault(name, {"docs": {}})
            index["definition"] = definition


class SearchIndexClient:
    def __init__(self, endpoint: str, credential: Any, **kwargs: Any) -> None:
        self.service = FakeSearchService.get(endpoint)
        self.credential = credential

    def create_or_update_index(self, index: Any, **kwargs: Any) -> Any:
        self.service.ops.append(("create_or_update_index", index.name))
        failure = self.service.fail_index_create
        if isinstance(failure, list):  # fail only the next calls
            failure = failure.pop(0) if failure else None
        if failure is not None:
            raise failure
        self.service.define(index.name, index)
        return index

    def get_index(self, name: str, **kwargs: Any) -> Any:
        if name not in self.service.indexes:
            raise ResourceNotFoundError(f"index {name} not found", status_code=404)
        return self.service.indexes[name]["definition"]

    def delete_index(self, index: Any, **kwargs: Any) -> None:
        name = getattr(index, "name", index)
        self.service.ops.append(("delete_index", name))
        self.service.indexes.pop(name, None)


class SearchClient:
    def __init__(self, endpoint: str, index_name: str, credential: Any, **kwargs: Any) -> None:
        self.service = FakeSearchService.get(endpoint)
        self.index_name = index_name
        self.credential = credential
        self.closed = False

    def _index(self) -> dict[str, Any]:
        index = self.service.indexes.get(self.index_name)
        if index is None:
            raise ResourceNotFoundError(f"The index '{self.index_name}' was not found.", status_code=404)
        return index

    def merge_or_upload_documents(self, documents: list[dict[str, Any]], **kwargs: Any) -> list[IndexingResult]:
        with self.service.lock:
            docs = self._index()["docs"]
            self.service.ops.append(("upload", len(documents)))
            for d in documents:
                docs[d["key"]] = {**docs.get(d["key"], {}), **json.loads(json.dumps(d))}
            return [IndexingResult(key=d["key"], succeeded=True, status_code=200) for d in documents]

    def delete_documents(self, documents: list[dict[str, Any]], **kwargs: Any) -> list[IndexingResult]:
        with self.service.lock:
            docs = self._index()["docs"]
            self.service.ops.append(("delete", len(documents)))
            for d in documents:
                docs.pop(d["key"], None)
            return [IndexingResult(key=d["key"], succeeded=True, status_code=200) for d in documents]

    def search(self, search_text: str | None = None, *, filter: str | None = None, top: int | None = None,
               skip: int | None = None, select: list[str] | None = None, search_mode: str = "any",
               search_fields: list[str] | None = None, query_type: str | None = None,
               include_total_count: bool | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        with self.service.lock:
            self.service.ops.append(("search", search_text, filter))
            docs = list(self._index()["docs"].values())
            pred = odata_predicate(filter)
            fields = search_fields or ["title", "body"]
            phrases = [_analyse(p) for p in re.findall(r'"([^"]*)"', search_text or "")] or (
                [] if (search_text or "*").strip() == "*" else [[w] for w in _analyse(search_text or "")])
            out = []
            for doc in docs:
                if not pred(doc):
                    continue
                score = 1.0
                if phrases:
                    found = []
                    score = 0.0
                    for phrase in phrases:
                        hits = 0
                        for weight, f in zip(itertools.count(2, -1), fields):
                            words = _analyse(doc.get(f) or "")
                            n = len(phrase)
                            hits += weight * sum(1 for i in range(len(words) - n + 1) if words[i:i + n] == phrase)
                        found.append(hits > 0)
                        score += hits
                    if not (all(found) if search_mode == "all" else any(found)):
                        continue
                result = {k: v for k, v in doc.items() if select is None or k in select}
                result["@search.score"] = score
                out.append(result)
            out.sort(key=lambda r: -r["@search.score"])
            start = skip or 0
            return out[start:start + (top if top is not None else 50)]

    def close(self) -> None:
        self.closed = True


# =============================================================================== ACS e-mail
class FakeAcs:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.clients: list[tuple[str, Any]] = []
        self.mode = "ok"  # ok | raise | failed | running
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self.calls = 0


ACS = FakeAcs()


class _Poller:
    def __init__(self, status: str) -> None:
        self._status = status

    def result(self, timeout: float | None = None) -> dict[str, Any]:
        return {"id": f"op-{uuid.uuid4().hex[:8]}", "status": self._status,
                "error": {"code": "Failed"} if self._status == "Failed" else None}


class EmailClient:
    def __init__(self, endpoint: str, credential: Any, **kwargs: Any) -> None:
        ACS.clients.append((endpoint, credential))

    def begin_send(self, message: dict[str, Any], **kwargs: Any) -> _Poller:
        ACS.calls += 1
        ACS.entered.set()
        if ACS.gate is not None and not ACS.gate.wait(30):
            raise TimeoutError("fake ACS gate never opened")
        if ACS.mode == "raise":
            raise HttpResponseError("Service unavailable", status_code=503)
        ACS.sent.append(json.loads(json.dumps(message)))
        return _Poller({"ok": "Succeeded", "failed": "Failed", "running": "Running"}[ACS.mode])


# =============================================================================== requests + Blob REST
class RequestException(Exception):
    pass


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class FakeBlobService:
    services: dict[str, "FakeBlobService"] = {}

    def __init__(self, account_url: str) -> None:
        self.account_url = account_url
        self.containers: dict[str, dict[str, tuple[bytes, str, str]]] = {"letters": {}, "reports": {}}
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.fail_next: list[int] = []
        self.page_size = 5000

    @classmethod
    def get(cls, account_url: str) -> "FakeBlobService":
        return cls.services.setdefault(account_url.rstrip("/"), FakeBlobService(account_url.rstrip("/")))

    def handle(self, method: str, path: str, params: dict[str, str], data: bytes | None,
               headers: dict[str, str]) -> FakeResponse:
        self.requests.append((method, path, dict(headers)))
        if not headers.get("Authorization", "").startswith("Bear") or not headers.get("x-ms-version"):
            return FakeResponse(401, headers={"x-ms-error-code": "NoAuthenticationInformation"})
        if self.fail_next:
            return FakeResponse(self.fail_next.pop(0), headers={"x-ms-error-code": "ServerBusy"})
        parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/", 1)]
        container = parts[0]
        if len(parts) == 1:
            if method == "PUT" and params.get("restype") == "container":
                if container in self.containers:
                    return FakeResponse(409, headers={"x-ms-error-code": "ContainerAlreadyExists"})
                self.containers[container] = {}
                return FakeResponse(201)
            if method == "GET" and params.get("comp") == "list":
                if container not in self.containers:
                    return FakeResponse(404, headers={"x-ms-error-code": "ContainerNotFound"})
                names = sorted(self.containers[container])
                start = int(params.get("marker") or 0)
                page = names[start:start + self.page_size]
                blobs = "".join(
                    f"<Blob><Name>{n}</Name><Properties><Last-Modified>{self.containers[container][n][2]}"
                    f"</Last-Modified><Content-Length>{len(self.containers[container][n][0])}</Content-Length>"
                    f"</Properties></Blob>" for n in page)
                marker = str(start + self.page_size) if start + self.page_size < len(names) else ""
                xml = (f'<?xml version="1.0" encoding="utf-8"?><EnumerationResults ContainerName="{container}">'
                       f"<Blobs>{blobs}</Blobs><NextMarker>{marker}</NextMarker></EnumerationResults>")
                return FakeResponse(200, xml.encode("utf-8"))
            return FakeResponse(400)
        name = parts[1]
        if container not in self.containers:
            return FakeResponse(404, headers={"x-ms-error-code": "ContainerNotFound"})
        if method == "PUT":
            if headers.get("x-ms-blob-type") != "BlockBlob":
                return FakeResponse(400, headers={"x-ms-error-code": "MissingRequiredHeader"})
            stamp = "Tue, 10 Nov 2026 21:00:00 GMT"
            self.containers[container][name] = (bytes(data or b""), headers.get("Content-Type", ""), stamp)
            return FakeResponse(201, headers={"Last-Modified": stamp, "ETag": '"0x1"'})
        if method == "GET":
            blob = self.containers[container].get(name)
            if blob is None:
                return FakeResponse(404, headers={"x-ms-error-code": "BlobNotFound"})
            return FakeResponse(200, blob[0], {"Content-Type": blob[1], "Last-Modified": blob[2]})
        return FakeResponse(405)


class FakeHttp:
    """Routes ``requests`` calls to the fake Blob and Search services (anything else is refused)."""

    log: list[tuple[str, str]] = []

    @staticmethod
    def route(method: str, url: str, *, params: dict[str, str] | None = None, data: Any = None,
              headers: dict[str, str] | None = None, timeout: Any = None, **kwargs: Any) -> FakeResponse:
        FakeHttp.log.append((method, url))
        parsed = urllib.parse.urlsplit(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        query = dict(urllib.parse.parse_qsl(parsed.query))
        query.update(params or {})
        if parsed.netloc.endswith(".blob.core.windows.net") or base in FakeBlobService.services:
            payload = data.encode("utf-8") if isinstance(data, str) else data
            return FakeBlobService.get(base).handle(method, parsed.path, query, payload, headers or {})
        if parsed.netloc.endswith(".search.windows.net") and method == "PUT" and parsed.path.startswith("/indexes/"):
            if not (headers or {}).get("Authorization", "").startswith("Bear"):
                return FakeResponse(401)
            definition = json.loads(data)
            FakeSearchService.get(base).define(definition["name"], _Model(rest=True, **definition))
            FakeSearchService.get(base).ops.append(("rest_put_index", definition["name"]))
            return FakeResponse(201, json.dumps(definition).encode("utf-8"))
        raise RequestException(f"fake requests: no network access to {url}")


class Session:
    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        return FakeHttp.route(method, url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> FakeResponse:
        return FakeHttp.route("PUT", url, **kwargs)

    def close(self) -> None:
        pass


def _requests_put(url: str, **kwargs: Any) -> FakeResponse:
    return FakeHttp.route("PUT", url, **kwargs)


# =============================================================================== module factory
def reset_fakes() -> None:
    FakeCosmosClient.accounts.clear()
    FakeCosmosClient.created.clear()
    FakeSearchService.services.clear()
    FakeBlobService.services.clear()
    FakeHttp.log.clear()
    CREDENTIALS.created.clear()
    CREDENTIALS.tokens.clear()
    ACS.reset()


def fake_modules(*, without_search_models: bool = False) -> dict[str, types.ModuleType]:
    """sys.modules entries for the Azure SDK packages and requests (inject with mock.patch.dict)."""
    def module(name: str, **attrs: Any) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        mod.__fake__ = True  # type: ignore[attr-defined]
        return mod

    core_exceptions = module("azure.core.exceptions", HttpResponseError=HttpResponseError,
                             ResourceNotFoundError=ResourceNotFoundError, ResourceExistsError=ResourceExistsError)
    models = module("azure.search.documents.indexes.models", SearchIndex=SearchIndex, CustomAnalyzer=CustomAnalyzer,
                    SimpleField=SimpleField, SearchableField=SearchableField)
    if without_search_models:
        del models.SearchableField
    return {
        "azure": module("azure"),
        "azure.core": module("azure.core", MatchConditions=MatchConditions, exceptions=core_exceptions),
        "azure.core.exceptions": core_exceptions,
        "azure.identity": module("azure.identity", ManagedIdentityCredential=ManagedIdentityCredential,
                                 DefaultAzureCredential=DefaultAzureCredential),
        "azure.cosmos": module("azure.cosmos", CosmosClient=FakeCosmosClient),
        "azure.cosmos.exceptions": module(
            "azure.cosmos.exceptions", CosmosHttpResponseError=CosmosHttpResponseError,
            CosmosResourceNotFoundError=CosmosResourceNotFoundError,
            CosmosResourceExistsError=CosmosResourceExistsError,
            CosmosAccessConditionFailedError=CosmosAccessConditionFailedError),
        "azure.search": module("azure.search"),
        "azure.search.documents": module("azure.search.documents", SearchClient=SearchClient),
        "azure.search.documents.indexes": module("azure.search.documents.indexes", SearchIndexClient=SearchIndexClient),
        "azure.search.documents.indexes.models": models,
        "azure.communication": module("azure.communication"),
        "azure.communication.email": module("azure.communication.email", EmailClient=EmailClient),
        "requests": module("requests", Session=Session, RequestException=RequestException, put=_requests_put),
    }


def patched_modules(**kwargs: Any) -> Any:
    """Context manager: fake SDK modules in sys.modules plus fresh adapter caches."""
    from pms.azure import cosmos_store, identity

    class _Ctx:
        def __enter__(self) -> "_Ctx":
            self._patch = mock.patch.dict(sys.modules, fake_modules(**kwargs))
            self._patch.__enter__()
            cosmos_store.reset_accounts()
            identity.reset_credentials()
            return self

        def __exit__(self, *exc: Any) -> None:
            cosmos_store.reset_accounts()
            identity.reset_credentials()
            self._patch.__exit__(*exc)

    return _Ctx()


class FakeAzureTestCase(unittest.TestCase):
    """Every test runs with the fake SDK modules in sys.modules and fresh fake services."""

    def setUp(self) -> None:
        reset_fakes()
        ctx = patched_modules()
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)


# =============================================================================== self-tests of the fakes
class FakeCosmosSemantics(unittest.TestCase):
    def setUp(self) -> None:
        self.account = FakeCosmosAccount()
        self.records = self.account.containers["records"]

    def test_etag_preconditions_and_duplicate_ids(self) -> None:
        doc = self.records.create_item(body={"id": "a", "pk": "p", "value": 1.0})
        self.assertEqual(doc["value"], 1)  # integral numbers come back without a decimal point
        with self.assertRaises(CosmosResourceExistsError):
            self.records.create_item(body={"id": "a", "pk": "p"})
        self.records.create_item(body={"id": "a", "pk": "other"})  # ids are unique per partition only
        updated = self.records.replace_item("a", {**doc, "value": 2}, etag=doc["_etag"],
                                            match_condition=MatchConditions.IfNotModified)
        with self.assertRaises(CosmosAccessConditionFailedError) as ctx:
            self.records.replace_item("a", {**doc, "value": 3}, etag=doc["_etag"],
                                      match_condition=MatchConditions.IfNotModified)
        self.assertEqual(ctx.exception.status_code, 412)
        self.assertEqual(self.records.read_item("a", partition_key="p")["value"], 2)
        self.assertNotEqual(updated["_etag"], doc["_etag"])

    def test_audit_role_and_unique_seq(self) -> None:
        audit = self.account.containers["audit"]
        audit.create_item(body={"id": "1", "pk": "audit-chain", "seq": 1})
        for call in (lambda: audit.upsert_item(body={"id": "1", "pk": "audit-chain", "seq": 1}),
                     lambda: audit.replace_item("1", {"id": "1", "pk": "audit-chain", "seq": 1}),
                     lambda: audit.delete_item("1", partition_key="audit-chain")):
            with self.assertRaises(CosmosHttpResponseError) as ctx:
                call()
            self.assertEqual(ctx.exception.status_code, 403)
        with self.assertRaises(CosmosResourceExistsError):
            audit.create_item(body={"id": "x", "pk": "audit-chain", "seq": 1})

    def test_query_subset(self) -> None:
        for i, (status, flag) in enumerate([("QUEUED", True), ("RUNNING", False), ("DONE", True)]):
            self.records.create_item(body={"id": f"j{i}", "pk": f"c{i % 2}", "type": "t", "status": status,
                                           "flag": flag, "n": i})
        q = 'SELECT * FROM c WHERE c["type"] = @t AND c["flag"] = @f'
        hits = self.records.query_items(q, parameters=[{"name": "@t", "value": "t"}, {"name": "@f", "value": True}],
                                        enable_cross_partition_query=True)
        self.assertEqual(sorted(h["id"] for h in hits), ["j0", "j2"])
        count = self.records.query_items('SELECT VALUE COUNT(1) FROM c WHERE ARRAY_CONTAINS(@s, c["status"])',
                                         parameters=[{"name": "@s", "value": ["QUEUED", "RUNNING"]}],
                                         enable_cross_partition_query=True)
        self.assertEqual(count, [2])
        top = self.records.query_items('SELECT TOP 1 * FROM c WHERE c["pk"] = @p ORDER BY c["n"] DESC',
                                       parameters=[{"name": "@p", "value": "c0"}], partition_key="c0")
        self.assertEqual(top[0]["id"], "j2")
        with self.assertRaises(CosmosHttpResponseError):
            self.records.query_items('SELECT * FROM c WHERE c["n"] = @n', parameters=[{"name": "@n", "value": 1}])
        # Cosmos equality is type-aware: true is not 1
        self.assertEqual(self.records.query_items('SELECT * FROM c WHERE c["n"] = @n',
                                                  parameters=[{"name": "@n", "value": True}],
                                                  enable_cross_partition_query=True), [])


class FakeSearchSemantics(unittest.TestCase):
    def test_odata_subset_and_search_modes(self) -> None:
        pred = odata_predicate("search.in(source_type, 'literature,past_case', ',') and family eq 'f''x' "
                               "and models/any(x: x eq 'OA-S5')")
        self.assertTrue(pred({"source_type": "past_case", "family": "f'x", "models": ["OA-S5"]}))
        self.assertFalse(pred({"source_type": "dossier_item", "family": "f'x", "models": ["OA-S5"]}))
        with self.assertRaises(HttpResponseError):
            odata_predicate("family zz 'x'")
        service = FakeSearchService("https://s.search.windows.net")
        FakeSearchService.services["https://s.search.windows.net"] = service
        try:
            service.define("i", SearchIndex(name="i", fields=[]))
            client = SearchClient("https://s.search.windows.net", "i", None)
            client.merge_or_upload_documents([{"key": "1", "title": "Stem fracture", "body": "fatigue"},
                                              {"key": "2", "title": "Squeaking", "body": "fracture noise"}])
            self.assertEqual({r["key"] for r in client.search('"fracture" "stem"', search_mode="all")}, {"1"})
            self.assertEqual({r["key"] for r in client.search('"fracture" "stem"', search_mode="any")}, {"1", "2"})
            self.assertEqual(len(client.search("*", top=1, skip=1)), 1)
        finally:
            FakeSearchService.services.clear()


if __name__ == "__main__":
    unittest.main()
