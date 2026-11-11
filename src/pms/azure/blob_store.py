"""Blob Storage file store (:class:`pms.interfaces.FileStore`) for letters, reports and outbox copies.

Uses the Blob REST API through ``requests`` with a Microsoft Entra access token of the app identity
(``https://storage.azure.com/.default``; shared keys are disabled on the account). azure-storage-blob is
not a dependency. Areas map to blob containers: ``letters`` and ``reports`` exist (storage.bicep);
any other area (``outbox``) is created on first use - the role Storage Blob Data Contributor allows it.
Blob URLs carry no SAS token; reading them needs an Entra identity with a data role.
"""
from __future__ import annotations

import datetime as _dt
import email.utils
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Callable

from ..interfaces import StoredFile
from ..models import iso_utc
from .errors import AzureAdapterError
from .identity import BearerToken, auth_header

STORAGE_SCOPE = "https://storage.azure.com/.default"
API_VERSION = "2023-11-03"
DEFAULT_CONTAINERS: dict[str, str] = {"letters": "letters", "reports": "reports", "outbox": "outbox"}
_RETRY_STATUS = (408, 429, 500, 502, 503, 504)


class BlobStoreError(AzureAdapterError):
    """Blob Storage refused or failed a request."""


def _http_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return iso_utc(email.utils.parsedate_to_datetime(value).astimezone(_dt.timezone.utc))
    except (TypeError, ValueError):
        return None


class BlobFileStore:
    """``<account>/<container>/<name>`` with the same name rules as the local folder store."""

    def __init__(self, account_url: str, *, client_id: str | None = None,
                 credential_factory: Callable[[], Any] | None = None, session: Any = None,
                 containers: dict[str, str] | None = None, timeout_s: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not account_url:
            raise BlobStoreError("STORAGE_ACCOUNT_URL is not set")
        self.account_url = account_url.rstrip("/")
        if credential_factory is None:
            from .identity import get_credential

            def credential_factory() -> Any:
                return get_credential(client_id)
        self._token = BearerToken(STORAGE_SCOPE, credential_factory)
        self._session = session
        self._session_lock = threading.Lock()
        self.containers = {**DEFAULT_CONTAINERS, **(containers or {})}
        self.timeout_s = timeout_s
        self._sleep = sleep

    # ------------------------------------------------------------------ HTTP
    def _http(self) -> Any:
        if self._session is None:
            import requests  # type: ignore[import-not-found]

            self._session = requests.Session()
        return self._session

    def _request(self, method: str, url: str, *, params: dict[str, str] | None = None, data: bytes | None = None,
                 headers: dict[str, str] | None = None) -> Any:
        import requests  # type: ignore[import-not-found]

        for attempt in range(3):
            all_headers = {**auth_header(self._token()), "x-ms-version": API_VERSION,
                           "x-ms-date": email.utils.formatdate(usegmt=True), **(headers or {})}
            try:
                with self._session_lock:
                    response = self._http().request(method, url, params=params, data=data, headers=all_headers,
                                                    timeout=self.timeout_s)
            except requests.RequestException as exc:
                if attempt == 2:
                    raise BlobStoreError(f"Blob Storage not reachable: {type(exc).__name__}: {exc}") from exc
                self._sleep(0.5 * 2 ** attempt)
                continue
            if response.status_code in _RETRY_STATUS and attempt < 2:
                self._sleep(0.5 * 2 ** attempt)
                continue
            return response
        raise BlobStoreError("Blob Storage request failed")  # pragma: no cover - the loop returns or raises

    @staticmethod
    def _error(response: Any, what: str) -> BlobStoreError:
        code = response.headers.get("x-ms-error-code") if getattr(response, "headers", None) else None
        hint = " - the app identity needs 'Storage Blob Data Contributor'" if response.status_code == 403 else ""
        return BlobStoreError(f"{what}: HTTP {response.status_code} {code or ''}{hint}".strip())

    # ------------------------------------------------------------------ names
    def _container(self, area: str) -> str:
        if not area or not area.replace("-", "").isalnum():
            raise ValueError("invalid file store area or name")
        return self.containers.get(area, area.lower())

    @staticmethod
    def _check_name(name: str) -> None:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise ValueError("invalid file store area or name")

    def _blob_url(self, container: str, name: str) -> str:
        return f"{self.account_url}/{container}/{urllib.parse.quote(name, safe='-_.~')}"

    def _create_container(self, container: str) -> None:
        response = self._request("PUT", f"{self.account_url}/{container}", params={"restype": "container"})
        if response.status_code not in (201, 409):
            raise self._error(response, f"creating the blob container '{container}' failed")

    # ------------------------------------------------------------------ FileStore protocol
    def put(self, area: str, name: str, data: bytes, *, content_type: str = "application/octet-stream") -> StoredFile:
        container = self._container(area)
        self._check_name(name)
        url = self._blob_url(container, name)
        payload = bytes(data)
        headers = {"x-ms-blob-type": "BlockBlob", "Content-Type": content_type}
        response = self._request("PUT", url, data=payload, headers=headers)
        if response.status_code == 404 and response.headers.get("x-ms-error-code") == "ContainerNotFound":
            self._create_container(container)
            response = self._request("PUT", url, data=payload, headers=headers)
        if response.status_code != 201:
            raise self._error(response, f"writing {area}/{name} failed")
        modified = _http_date(response.headers.get("Last-Modified")) or iso_utc(_dt.datetime.now(_dt.timezone.utc))
        return StoredFile(area=area, name=name, uri=url, size=len(payload), modified_utc=modified)

    def get(self, area: str, name: str) -> bytes:
        container = self._container(area)
        self._check_name(name)
        response = self._request("GET", self._blob_url(container, name))
        if response.status_code == 404:
            raise FileNotFoundError(f"{area}/{name}")
        if response.status_code != 200:
            raise self._error(response, f"reading {area}/{name} failed")
        return bytes(response.content)

    def list(self, area: str) -> list[StoredFile]:
        container = self._container(area)
        out: list[StoredFile] = []
        marker: str | None = None
        while True:
            params = {"restype": "container", "comp": "list", "maxresults": "5000"}
            if marker:
                params["marker"] = marker
            response = self._request("GET", f"{self.account_url}/{container}", params=params)
            if response.status_code == 404:
                return []  # like a missing local folder
            if response.status_code != 200:
                raise self._error(response, f"listing {area} failed")
            root = ET.fromstring(response.content)
            for blob in root.iter("Blob"):
                name = blob.findtext("Name") or ""
                if not name or "/" in name or name.startswith("."):
                    continue
                props = blob.find("Properties")
                size = int((props.findtext("Content-Length") if props is not None else None) or 0)
                modified = _http_date(props.findtext("Last-Modified") if props is not None else None) or ""
                out.append(StoredFile(area=area, name=name, uri=self._blob_url(container, name), size=size,
                                      modified_utc=modified))
            marker = root.findtext("NextMarker") or None
            if not marker:
                break
        return sorted(out, key=lambda f: f.name)

    def describe(self) -> str:
        return f"Azure Blob Storage {self.account_url} (containers {', '.join(sorted(set(self.containers.values())))})"
