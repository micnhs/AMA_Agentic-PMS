"""INJ-1.0 - prompt-injection detection (concept section 6.3, design element D-13).

Pattern check in DE/EN/FR/IT for instructions addressed to systems: "ignore all previous
instructions", status commands, forwarding addresses, "do not mention" (concept 6.3
INJ-1.0; source: Goal Card DONE WHEN check 11, GC-INJ "prompt-injection letter gets the
same labels as its clean copy and raises a flag").

Letter text is always data, never an instruction:

* a hit only sets a visible flag and causes an audit record - no rule output changes;
* :func:`scan` returns ``masked_text`` in which every paragraph that contains a hit is
  blanked (same length, offsets unchanged). Deterministic extraction (identifiers,
  places, references, event terms, correlation queries) runs on the masked text, so an
  injected instruction cannot steer them. CRIT-1.0 still scans the full raw text
  because it may only raise urgency.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..versions import INJ
from .extract import line_of

#: (pattern id, language, compiled pattern). Patterns are deliberately specific to
#: instructions addressed to an automated reader; ordinary clinical text never matches.
_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (pid, lang, re.compile(rx, re.IGNORECASE))
    for pid, lang, rx in (
        # English
        ("ignore-instructions", "en", r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier|preceding|system)\s+(?:instructions|prompts?|rules|messages)\b"),
        ("addressed-to-ai", "en", r"\b(?:note|message|instruction)s?\s+(?:to|for)\s+(?:any|the|all)?\s*(?:automated|ai|artificial|language[- ]model|llm)\s*(?:assistant|system|agent|model|reader)s?\b"),
        ("role-override", "en", r"\b(?:you\s+are\s+now|act\s+as\s+(?:an?\s+)?(?:system|administrator|admin)|system\s+prompt)\b"),
        ("status-command", "en", r"\b(?:set|change|update|mark)\s+(?:the\s+)?(?:case\s+)?status\s+(?:to|as)\b|\bclassify\s+(?:this|the)\s+case\s+as\b|\bclose\s+(?:this|the)\s+case\b"),
        ("forward-address", "en", r"\b(?:forward|send|e-?mail|transmit)\b[^.\n]{0,80}?[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
        ("conceal", "en", r"\b(?:do\s+not|don't|never)\s+(?:mention|disclose|reveal|report|log)\s+(?:this|that|the\s+(?:note|instruction))\b"),
        # German
        ("ignore-instructions", "de", r"\b(?:ignoriere|ignorieren\s+sie|vergiss|vergessen\s+sie)\s+(?:alle\s+)?(?:vorherigen|bisherigen|obigen|vorigen)\s+(?:anweisungen|instruktionen|regeln)\b"),
        ("addressed-to-ai", "de", r"\bhinweis\s+an\s+(?:den|die|alle|jeden|jedes)\s+(?:automatisierten?\s+|ki-?)?(?:assistenten|systeme?|agenten|sprachmodelle?)\b"),
        ("status-command", "de", r"\b(?:setze|setzen\s+sie|ändere|ändern\s+sie)\s+(?:den\s+)?(?:fall)?status\b|\bstatus\s+auf\s+\S+\s+setzen\b|\bstufe\s+(?:den\s+)?fall\s+als\b"),
        ("forward-address", "de", r"\b(?:leite|leiten\s+sie|sende|senden\s+sie|weiterleiten|schicke)\b[^.\n]{0,80}?[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
        ("conceal", "de", r"\b(?:erwähne|erwähnen\s+sie)\s+(?:dies|das|diesen\s+hinweis)\s+nicht\b|\bnicht\s+erwähnen\b"),
        # French
        ("ignore-instructions", "fr", r"\bignore[zr]?\s+(?:toutes\s+)?les\s+instructions\s+(?:précédentes|antérieures)\b"),
        ("addressed-to-ai", "fr", r"\b(?:note|message)\s+(?:à|pour)\s+(?:tout|l'|le)\s*(?:assistant|système|agent)\s+(?:automatisé|automatique|ia)\b"),
        ("status-command", "fr", r"\b(?:changez|change|définissez|définis|mettez|mets)\s+le\s+statut\b|\bclassez\s+ce\s+cas\s+comme\b"),
        ("forward-address", "fr", r"\b(?:transférez|transfère|envoyez|envoie|transmettez)\b[^.\n]{0,80}?[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
        ("conceal", "fr", r"\bne\s+(?:mentionnez|mentionne|signalez)\s+pas\b"),
        # Italian
        ("ignore-instructions", "it", r"\bignora(?:te)?\s+(?:tutte\s+)?le\s+istruzioni\s+(?:precedenti|sopra)\b"),
        ("addressed-to-ai", "it", r"\b(?:nota|messaggio)\s+per\s+(?:qualsiasi|ogni|l')\s*(?:assistente|sistema|agente)\s+(?:automatico|automatizzato|ia)\b"),
        ("status-command", "it", r"\b(?:imposta|impostate|cambia|cambiate)\s+lo\s+stato\b|\bclassifica(?:te)?\s+(?:questo|il)\s+caso\s+come\b"),
        ("forward-address", "it", r"\b(?:inoltra|inoltrate|invia|inviate)\b[^.\n]{0,80}?[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
        ("conceal", "it", r"\bnon\s+(?:menzionare|menzionate|citare|riportare)\b"),
    )
)


@dataclass(frozen=True)
class InjectionHit:
    pattern_id: str
    language: str
    quote: str
    start: int
    end: int
    line: int


@dataclass(frozen=True)
class InjectionResult:
    flag: bool
    hits: tuple[InjectionHit, ...]
    masked_text: str
    masked_spans: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    rule_version: str = INJ

    def to_dict(self) -> dict:
        return {
            "flag": self.flag,
            "rule_version": self.rule_version,
            "hits": [h.__dict__ for h in self.hits],
            "masked_spans": [list(s) for s in self.masked_spans],
        }


def _paragraph_span(text: str, pos: int) -> tuple[int, int]:
    """Span of the blank-line separated paragraph that contains ``pos``."""
    start = text.rfind("\n\n", 0, pos)
    start = 0 if start == -1 else start + 2
    end = text.find("\n\n", pos)
    end = len(text) if end == -1 else end
    return start, end


def scan(text: str) -> InjectionResult:
    """Detect injected instructions and return the flag, hits and masked text."""
    hits: list[InjectionHit] = []
    for pid, lang, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            hits.append(InjectionHit(pid, lang, m.group(0), m.start(), m.end(), line_of(text, m.start())))
    hits.sort(key=lambda h: (h.start, h.pattern_id))
    spans: list[tuple[int, int]] = []
    for h in hits:
        s, e = _paragraph_span(text, h.start)
        if not spans or s > spans[-1][1]:
            spans.append((s, e))
        else:
            spans[-1] = (spans[-1][0], max(spans[-1][1], e))
    masked = list(text)
    for s, e in spans:
        for i in range(s, e):
            if masked[i] != "\n":
                masked[i] = " "
    return InjectionResult(bool(hits), tuple(hits), "".join(masked), tuple(spans))
