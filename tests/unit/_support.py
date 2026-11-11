"""Shared helpers for the domain unit tests (offline, standard library only).

Receipt times below are the golden-set receipt times of the synthetic letters (concept
section 11.4); unit tests hard-code them so that only the golden comparator reads
expected.json.
"""
from __future__ import annotations

import functools
from pathlib import Path

from pms.knowledge import KnowledgeIndex, ReferenceData
from pms.models import Actor, ActorType, FixedClock, Role, SignalInput
from pms.rules.engine import RuleEngine
from pms.store import Store

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data" / "synthetic"
LETTERS = DATA / "letters"

RECEIVED_AT = {
    "L01": "2026-11-16T08:15:00Z",
    "L02": "2026-11-16T09:30:00Z",
    "L03": "2026-11-16T10:05:00Z",
    "L04": "2026-11-16T11:20:00Z",
    "L05": "2026-11-16T13:40:00Z",
    "L06": "2026-11-16T14:55:00Z",
    "L07": "2026-11-17T08:10:00Z",
    "L08": "2026-11-17T09:45:00Z",
    "L09": "2026-11-17T11:00:00Z",
    "L10": "2026-11-23T09:00:00Z",
    "L11": "2026-11-18T10:30:00Z",
    "L12": "2026-11-18T15:00:00Z",
}

ANALYST = Actor("analyst-1", roles=frozenset({Role.ANALYST}))
REVIEWER = Actor("reviewer-1", roles=frozenset({Role.MEDICAL_REVIEWER}))
REGULATORY = Actor("regulatory-1", roles=frozenset({Role.REGULATORY}))
VIEWER = Actor("viewer-1", roles=frozenset({Role.VIEWER}))
OPERATOR = Actor("operator-1", roles=frozenset({Role.OPERATOR}))
ANONYMOUS = Actor.anonymous()
AGENT = Actor("A3-medical-analysis", actor_type=ActorType.AGENT)
SYSTEM = Actor.system()
AGENT_META = {"agent_version": "A3-1.0", "model": "offline-stand-in", "model_version": "offline-1.0",
              "prompt_version": "A3-prompt-1.0"}


def letter_text(letter_id: str) -> str:
    path = next(LETTERS.glob(f"{letter_id}_*.txt"))
    return path.read_text(encoding="utf-8")


def letter_signal(letter_id: str, received_at: str | None = None) -> SignalInput:
    return SignalInput(text=letter_text(letter_id), received_at=received_at or RECEIVED_AT[letter_id],
                       source="upload", ref=letter_id, filename=f"{letter_id}.txt")


@functools.lru_cache(maxsize=1)
def ref_data() -> ReferenceData:
    return ReferenceData.load(DATA)


@functools.lru_cache(maxsize=1)
def knowledge_index() -> KnowledgeIndex:
    return KnowledgeIndex(ref_data())


def new_engine(at: str = "2026-11-16T08:00:00Z", alert_sink=None) -> tuple[Store, RuleEngine, FixedClock]:
    """Fresh in-memory store and rule engine with a fixed demo clock."""
    clock = FixedClock(at)
    store = Store(":memory:", clock=clock)
    engine = RuleEngine(store, ref_data(), knowledge_index(), clock=clock, alert_sink=alert_sink)
    return store, engine, clock


def process(engine: RuleEngine, clock: FixedClock, letter_id: str, received_at: str | None = None):
    """Process one golden letter at its receipt time (demo clock = receipt time)."""
    at = received_at or RECEIVED_AT[letter_id]
    clock.set(at)
    return engine.process_signal(letter_signal(letter_id, at), now=clock.now())
