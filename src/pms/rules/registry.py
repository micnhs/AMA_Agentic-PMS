"""REG-1.0 - deterministic registry check (concept section 6.3; design element D-04).

Compares patient pseudonym, lot and UDI-DI of a message with the fictional implant
registry (``data/synthetic/registry/registry.json``). Result values:

* ``MATCH:<reg ids>`` - every given identifier agrees with the identified entries;
* ``MISMATCH:<reg ids> (<field and value details>)`` - entries identified but at least
  one identifier disagrees; every difference is shown with field and value;
* ``NOT_FOUND (<details>)`` - identifiers given but no registry entry identified;
* ``NO_IDENTIFIERS`` - the message carries no patient pseudonym, lot or UDI-DI.

Nothing is ever dropped silently: unmatched input stays visible (Goal Card QUALITY
"unknown stays unknown ... unreadable or unmatched input made visible", source GC-UNK;
03-scenario.md line 1369, SCN-NULL). The check never corrects data; a human does.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..knowledge import ProductCatalogue, RegistryEntry
from ..versions import REG

MATCH = "MATCH"
MISMATCH = "MISMATCH"
NOT_FOUND = "NOT_FOUND"
NO_IDENTIFIERS = "NO_IDENTIFIERS"


@dataclass(frozen=True)
class Identifiers:
    patients: tuple[str, ...] = ()
    lots: tuple[str, ...] = ()
    udi_dis: tuple[str, ...] = ()
    models: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.patients or self.lots or self.udi_dis)


@dataclass(frozen=True)
class RegistryCheck:
    status: str
    value: str
    entry_ids: tuple[str, ...]
    details: tuple[str, ...]
    entries: tuple[RegistryEntry, ...] = field(default_factory=tuple)
    rule_version: str = REG

    @property
    def matched(self) -> bool:
        return self.status == MATCH

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "value": self.value,
            "entry_ids": list(self.entry_ids),
            "details": list(self.details),
            "rule_version": self.rule_version,
        }


def _udi_detail(udi: str, entry: RegistryEntry, catalogue: ProductCatalogue | None) -> str:
    owner = catalogue.by_udi(udi) if catalogue is not None else None
    if owner is not None and owner.model != entry.model:
        return f"UDI-DI {udi} belongs to {owner.model} {owner.component}"
    if owner is None:
        return f"UDI-DI {udi} not in product catalogue, registry UDI-DI {entry.udi_di}"
    return f"UDI-DI {udi} differs from registry UDI-DI {entry.udi_di}"


def _lot_detail(lot: str, entry: RegistryEntry, registry: Sequence[RegistryEntry]) -> str:
    holders = [e.reg_id for e in registry if e.lot == lot]
    if not holders:
        return f"lot {lot} not in registry, registry lot {entry.lot}"
    return f"lot {lot} registered for {','.join(holders)}, registry lot {entry.lot}"


def check(ids: Identifiers, registry: Sequence[RegistryEntry], catalogue: ProductCatalogue | None = None) -> RegistryCheck:
    """Run REG-1.0 for one message."""
    if ids.empty:
        return RegistryCheck(NO_IDENTIFIERS, NO_IDENTIFIERS, (), ())

    details: list[str] = []
    if ids.patients:
        entries: list[RegistryEntry] = []
        for patient in ids.patients:
            found = [e for e in registry if e.patient == patient]
            if not found:
                details.append(f"patient {patient} not in registry")
            entries.extend(found)
    else:
        # No patient reference: identify entries by device identifiers only.
        entries = [
            e for e in registry
            if (not ids.lots or e.lot in ids.lots) and (not ids.udi_dis or e.udi_di in ids.udi_dis)
        ]
        if not entries:
            if ids.lots:
                details.extend(f"lot {lot} not in registry" for lot in ids.lots if all(e.lot != lot for e in registry))
            if ids.udi_dis:
                details.extend(f"UDI-DI {u} not in registry" for u in ids.udi_dis if all(e.udi_di != u for e in registry))
            if not details:
                details.append("lot and UDI-DI do not belong to one registry entry")

    if not entries:
        return RegistryCheck(NOT_FOUND, f"{NOT_FOUND} ({'; '.join(details)})" if details else NOT_FOUND, (), tuple(details))

    entry_lots = {e.lot for e in entries}
    entry_udis = {e.udi_di for e in entries}
    for lot in ids.lots:
        if lot not in entry_lots:
            details.append(_lot_detail(lot, entries[0], registry))
    for udi in ids.udi_dis:
        if udi not in entry_udis:
            details.append(_udi_detail(udi, entries[0], catalogue))
    if ids.models and not any(e.model in ids.models for e in entries):
        details.append(f"model {', '.join(ids.models)} does not match registry model {entries[0].model}")

    entry_ids = tuple(sorted({e.reg_id for e in entries}))
    if details:
        return RegistryCheck(MISMATCH, f"{MISMATCH}:{','.join(entry_ids)} ({'; '.join(details)})", entry_ids,
                             tuple(details), tuple(entries))
    return RegistryCheck(MATCH, f"{MATCH}:{','.join(entry_ids)}", entry_ids, (), tuple(entries))


def lookup(registry: Sequence[RegistryEntry], *, patient: str | None = None, lot: str | None = None,
           udi_di: str | None = None, model: str | None = None, limit: int = 25) -> list[RegistryEntry]:
    """Read-only registry lookup for tools and views (exact-match filters, AND-combined)."""
    if not any((patient, lot, udi_di, model)):
        raise ValueError("at least one filter (patient, lot, udi_di, model) is required")
    out = [
        e for e in registry
        if (patient is None or e.patient == patient) and (lot is None or e.lot == lot)
        and (udi_di is None or e.udi_di == udi_di) and (model is None or e.model == model)
    ]
    return out[: max(1, min(limit, 50))]
