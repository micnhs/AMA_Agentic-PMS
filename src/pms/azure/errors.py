"""Error helpers shared by the Azure adapters (no SDK import needed)."""
from __future__ import annotations


def status_code(exc: BaseException) -> int | None:
    """HTTP status of an azure-core / azure-cosmos / requests error (read defensively)."""
    for candidate in (exc, getattr(exc, "response", None)):
        value = getattr(candidate, "status_code", None)
        if isinstance(value, int):
            return value
    return None


class AzureAdapterError(RuntimeError):
    """An Azure service answered unexpectedly (missing resource, missing permission, API mismatch)."""
