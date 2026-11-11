"""Microsoft Entra credentials for the Azure adapters (no keys, no connection strings).

Inside Azure Container Apps the user-assigned managed identity is used
(``ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)``, INFRA_NOTES 5). On a developer machine
(for example the azd ``postdeploy`` hook running ``python -m pms seed --index-only``) there is no
managed identity endpoint, so the default credential chain (Azure CLI / azd sign-in) is used.
``azure.identity`` is imported lazily.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

#: HTTP authorization scheme for Microsoft Entra access tokens.
AUTH_SCHEME = "Bearer"


def auth_header(token: str) -> dict[str, str]:
    """``Authorization`` header for an Entra access token (REST calls of the adapters)."""
    return {"Authorization": f"{AUTH_SCHEME} {token}"}


#: Environment variables set by Azure Container Apps / App Service when a managed identity exists.
_HOSTED_MARKERS: tuple[str, ...] = ("IDENTITY_ENDPOINT", "MSI_ENDPOINT")

_lock = threading.Lock()
_cache: dict[tuple[str, str | None], Any] = {}


def running_with_managed_identity(env: dict[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return any(values.get(name) for name in _HOSTED_MARKERS)


def get_credential(client_id: str | None = None) -> Any:
    """One shared synchronous credential per process (token caches are reused)."""
    hosted = running_with_managed_identity()
    key = ("managed" if hosted else "default", client_id if hosted else None)
    with _lock:
        if key in _cache:
            return _cache[key]
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential  # type: ignore[import-not-found]

        if hosted:
            credential = ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
        else:
            credential = DefaultAzureCredential()
        _cache[key] = credential
        return credential


def reset_credentials() -> None:
    """Forget cached credentials (tests)."""
    with _lock:
        _cache.clear()


class BearerToken:
    """Caches an Entra access token for one scope and refreshes it 5 minutes before it expires."""

    def __init__(self, scope: str, credential_factory: Callable[[], Any]) -> None:
        self.scope = scope
        self._credential_factory = credential_factory
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_on = 0.0

    def __call__(self) -> str:
        with self._lock:
            if self._token is None or time.time() > self._expires_on - 300:
                access = self._credential_factory().get_token(self.scope)
                self._token = str(access.token)
                self._expires_on = float(getattr(access, "expires_on", time.time() + 600))
            return self._token
