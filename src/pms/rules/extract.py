"""Deterministic extraction from raw letter text (DE, EN, FR, IT).

Pure functions, no AI. Each extracted value keeps its verbatim quote, character span
and line number so that facts can be shown with their source text (concept 6.2: "jeweils
mit woertlichem Zitat"; ADR-003). Missing values stay empty = unknown (Goal Card QUALITY
"unknown stays unknown", source GC-UNK; 03-scenario.md line 1369, source SCN-NULL).

The patterns are generic. There is no special case for a letter id or a file name;
identifier formats (patient pseudonym ``P-XX-0000``, GS1 UDI-DI with the "(01)"
application identifier) are the conventions of the fictional registry and products
(``data/synthetic/registry/registry.json``, ``data/synthetic/products/*_dossier.md``).
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from ..knowledge import ProductCatalogue

# --------------------------------------------------------------------------- text helpers


@dataclass(frozen=True)
class Evidence:
    """An extracted value with its verbatim source quote."""

    value: str
    quote: str
    start: int
    end: int
    line: int


def line_of(text: str, pos: int) -> int:
    """1-based line number of a character offset."""
    return text.count("\n", 0, pos) + 1


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[\"'(\[A-ZÄÖÜÀ-Ý0-9-])")


def sentence_span(text: str, pos: int) -> tuple[int, int]:
    """Character span of the sentence (within its line) that contains ``pos``."""
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end]
    offset = 0
    for part in _SENTENCE_SPLIT.split(line):
        idx = line.find(part, offset)
        if idx == -1:
            continue
        s, e = line_start + idx, line_start + idx + len(part)
        offset = idx + len(part)
        if s <= pos < e or (pos >= e and offset >= len(line)):
            return s, e
    return line_start, line_end


def sentence_at(text: str, pos: int) -> str:
    s, e = sentence_span(text, pos)
    return text[s:e].strip()


def evidence(text: str, value: str, start: int, end: int, *, quote: str | None = None) -> Evidence:
    return Evidence(value=value, quote=quote if quote is not None else sentence_at(text, start),
                    start=start, end=end, line=line_of(text, start))


def sentences(text: str) -> list[tuple[int, int, str]]:
    """All sentences as (start, end, text) in document order."""
    out: list[tuple[int, int, str]] = []
    pos = 0
    for line in text.split("\n"):
        offset = 0
        for part in _SENTENCE_SPLIT.split(line):
            idx = line.find(part, offset)
            if idx == -1 or not part.strip():
                continue
            out.append((pos + idx, pos + idx + len(part), part))
            offset = idx + len(part)
        pos += len(line) + 1
    return out


def _dedupe(items: Iterable[Evidence]) -> list[Evidence]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item.value not in seen:
            seen.add(item.value)
            out.append(item)
    return out


# --------------------------------------------------------------------------- language
_STOPWORDS = {
    "de": {"der", "die", "das", "und", "nicht", "mit", "wurde", "ist", "eine", "ein", "zu", "den", "im", "am", "bei"},
    "en": {"the", "and", "of", "with", "was", "is", "to", "in", "on", "patient", "had", "were", "this"},
    "fr": {"le", "la", "les", "et", "de", "des", "une", "un", "du", "pour", "avec", "est", "été", "au"},
    "it": {"il", "lo", "la", "e", "di", "del", "della", "che", "con", "per", "è", "stato", "dei", "nella"},
}


def detect_language(text: str) -> str | None:
    """Rough stop-word based language guess (de, en, fr, it); ``None`` when unclear."""
    words = re.findall(r"[a-zA-ZÀ-ÿ]+", text.lower())
    scores = {lang: sum(1 for w in words if w in sw) for lang, sw in _STOPWORDS.items()}
    best = max(scores.values(), default=0)
    winners = [lang for lang, s in scores.items() if s == best]
    return winners[0] if best > 0 and len(winners) == 1 else None


# --------------------------------------------------------------------------- identifiers
#: Fictional patient pseudonym format of the registry (e.g. "P-DE-7341").
PSEUDONYM_RE = re.compile(r"(?<![A-Za-z0-9-])P-[A-Z]{2}-\d{4}(?![A-Za-z0-9])")
#: GS1 UDI-DI: "(01)" + GTIN-14, or 14 digits right after "UDI-DI".
UDI_RE = re.compile(r"\(01\)\s?(\d{14})(?!\d)|\bUDI(?:-DI)?\s*[:=]?\s*(\d{14})(?!\d)")
#: Lot keyword in DE/EN/FR/IT followed by a code that contains a digit.
LOT_RE = re.compile(
    r"(?i:\b(?:lot|lotto|charge|chargen|lote|batch)\b)(?:[\s:#.-]*(?i:no|nr|n°|number|nummer|numero)\b\.?)?[\s:#]*"
    r"([A-Z0-9](?=[A-Z0-9-]*\d)[A-Z0-9-]*[A-Z0-9])"
)
#: Model code right after the manufacturer brand (catches models missing from the catalogue).
BRAND_MODEL_RE = re.compile(r"\bFictivia\s+([A-Z]{2}-[A-Z]?\d{1,3})\b")

#: Sender or addressee reference ("Unser Zeichen", "Our ref", "N/Réf", "Ns. rif.").
SENDER_REF_RE = re.compile(
    r"(?i:\b(?:unser(?:e)?|ihr(?:e)?)\s+zeichen|\baktenzeichen|\bour\s+ref(?:erence)?\.?|\byour\s+ref(?:erence)?\.?"
    r"|\b[nv]/\s?r[ée]f\.?|\b(?:notre|votre)\s+r[ée]f[ée]rence|\b(?:ns|vs)\.?\s+rif\.?|\b(?:nostro|vostro)\s+riferimento)"
    r"\s*[:.]?\s*([A-Z0-9][A-Za-z0-9/._-]*\d[A-Za-z0-9/._-]*)"
)

_NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10,
    "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
    "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7, "otto": 8, "nove": 9, "dieci": 10,
}
_PATIENT_COUNT_RE = re.compile(
    r"(?i)\b(\d{1,3}|" + "|".join(_NUMBER_WORDS) + r")\s+(?:[\wäöüéèàù-]+\s+)?"
    r"(?:patients|patienten|patientinnen|pazienti|patientes)\b"
)
_SAME_LOT_RE = re.compile(
    r"(?i)\b(?:same\s+(?:lot|batch)|derselben\s+charge|der\s+gleichen\s+charge|selben\s+charge"
    r"|même\s+lot|stesso\s+lotto|medesimo\s+lotto)\b"
)

# --------------------------------------------------------------------------- follow-up markers
#: LINK-1.0 follow-up markers ('Nachtrag', 'follow-up', 'suite à', 'a seguito'; concept 6.3)
#: in the form "marker + reference to an earlier communication". A bare announcement of a
#: future follow-up ("wir melden uns mit einem Nachtrag") is not a marker.
FOLLOW_UP_RE = re.compile(
    r"(?i)\bnachtrag\s+zu(?:m|r)?\b|\bals\s+nachtrag\b|\bergänzung\s+zu(?:m|r)?\s+unser"
    r"|\bfollow[- ]?up\s+(?:to|of|on)\b|\bfurther\s+to\s+(?:our|my)\b|\bfollow[- ]?up\s+report\b"
    r"|\bsuite\s+à\s+(?:notre|votre|mon|ma|nos)\b|\ben\s+complément\s+(?:de|à)\s+(?:notre|votre)\b"
    r"|\ba\s+seguito\s+(?:della|del|di|dello)\s+(?:nostra|nostro|vostra|vostro|mia|mio)\b"
    r"|\bad\s+integrazione\s+(?:della|del)\s+(?:nostra|nostro)\b"
    r"|^\s*(?:betreff|re|subject|objet|oggetto)\s*:\s*(?:nachtrag|follow[- ]?up|suite|seguito)\b",
    re.MULTILINE,
)

# --------------------------------------------------------------------------- dates
_MONTHS = {
    # EN
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    # DE
    "januar": 1, "februar": 2, "märz": 3, "mai": 5, "juni": 6, "juli": 7, "oktober": 10, "dezember": 12,
    # FR
    "janvier": 1, "février": 2, "mars": 3, "avril": 4, "juin": 6, "juillet": 7, "août": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12,
    # IT
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "dicembre": 12,
}
_DATE_PATTERNS = (
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), ("y", "m", "d")),
    (re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{4})\b"), ("d", "m", "y")),
    (re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(\d{4})\b"), ("d", "m", "y")),
    (re.compile(r"(?i)\b(\d{1,2}|1er)\.?\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})\b"), ("d", "M", "y")),
)


def _to_date(parts: dict[str, str]) -> _dt.date | None:
    try:
        day = 1 if parts["d"].lower() == "1er" else int(parts["d"])
        month = _MONTHS[parts["M"].lower()] if "M" in parts else int(parts["m"])
        return _dt.date(int(parts["y"]), month, day)
    except (KeyError, ValueError):
        return None


def find_dates(text: str) -> list[Evidence]:
    found: dict[int, Evidence] = {}
    for pattern, order in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            parts = dict(zip(order, m.groups()))
            date = _to_date(parts)
            if date is not None and m.start() not in found:
                found[m.start()] = evidence(text, date.isoformat(), m.start(), m.end())
    return [found[k] for k in sorted(found)]


_IMPLANT_CONTEXT_RE = re.compile(
    r"(?i)implant|ersatz|endoprothese|trägt\s+seit|erhielt|replacement|remplacement|bénéficié|"
    r"impianto|impiantat|sottopost"
)
#: Relative phrases ("after implantation") are not an implant date.
_RELATIVE_BEFORE_RE = re.compile(r"(?i)(?:after|since|nach|seit\s+der|après|dopo)\s+(?:the\s+|der\s+|l'|la\s+)?$")
#: Dates that only reference earlier correspondence ("Schreiben vom 16.11.2026").
_REFERENCE_DATE_RE = re.compile(
    r"(?i)(?:schreiben|brief|letter|courrier|lettre|lettera|nota|e-?mail)\s+(?:vom|of|dated|du|del|della)\s*$"
)
_SUBJECT_LINE_RE = re.compile(r"(?i)^\s*(?:betreff|re|subject|objet|oggetto)\s*:")
_IMPLANT_DATE_WINDOW = 120  # characters between implant keyword and date within one sentence


def classify_dates(text: str, dates: Sequence[Evidence], letter_date: Evidence | None) -> tuple[list[Evidence], list[Evidence], list[Evidence]]:
    """Split dates into (implant dates, event dates, reference dates).

    An implant date is the date nearest to an implant keyword in the same sentence
    (within a short window). Dates in subject lines or after "letter of" are references
    to earlier correspondence. Everything else is an event-related date.
    """
    implant: list[Evidence] = []
    event: list[Evidence] = []
    reference: list[Evidence] = []
    remaining: list[Evidence] = []
    for d in dates:
        if letter_date is not None and d.start == letter_date.start:
            continue
        s, _ = sentence_span(text, d.start)
        if _SUBJECT_LINE_RE.match(text[s:d.start]) or _REFERENCE_DATE_RE.search(text[max(s, d.start - 40):d.start]):
            reference.append(d)
        else:
            remaining.append(d)
    implant_starts: set[int] = set()
    for d in remaining:
        s, e = sentence_span(text, d.start)
        for m in _IMPLANT_CONTEXT_RE.finditer(text, s, e):
            if _RELATIVE_BEFORE_RE.search(text[max(s, m.start() - 15):m.start()]):
                continue
            same_sentence = [x for x in remaining if s <= x.start < e]
            nearest = min(same_sentence, key=lambda x: abs(x.start - m.start()))
            if abs(nearest.start - m.start()) <= _IMPLANT_DATE_WINDOW:
                implant_starts.add(nearest.start)
    for d in remaining:
        (implant if d.start in implant_starts else event).append(d)
    return implant, event, reference

# --------------------------------------------------------------------------- jurisdictions
#: Incident-state lexicon for RS-1.0 (MDR Art. 92(5), MDR-92-5: authority of the state where the
#: incident occurred; GB and NI separately, GC-GBNI; regulatory analysis p. 13, RA-P13).
#: Country and nation names in DE/EN/FR/IT. Order matters: Northern Ireland is matched
#: before Ireland and masked, so "Ireland" inside "Northern Ireland" never counts as IE.
#: Codes: EU member states as ISO 3166-1 alpha-2; "GB" = England, Scotland, Wales;
#: "NI" = Northern Ireland; "UK" = United Kingdom without nation (GB or NI unclear).
JURISDICTION_LEXICON: tuple[tuple[str, str], ...] = (
    ("NI", r"Northern\s+Ireland|Nord-?[Ii]rland|Irlande\s+du\s+Nord|Irlanda\s+del\s+Nord"),
    ("UK", r"United\s+Kingdom|Vereinigte[nm]?\s+Königreich|Royaume[- ]Uni|Regno\s+Unito|(?<![\w-])U\.?K\.?(?![\w-])"),
    ("GB", r"England|Scotland|Wales|Great\s+Britain|Schottland|Gro(?:ß|ss)britannien|Angleterre|[ÉE]cosse"
           r"|Pays\s+de\s+Galles|Grande[- ]Bretagne|Inghilterra|Scozia|Galles|Gran\s+Bretagna"),
    ("IE", r"Republic\s+of\s+Ireland|Ireland|Irland|Irlande|Irlanda"),
    ("AT", r"Austria|Österreich|Oesterreich|Autriche"),
    ("BE", r"Belgium|Belgien|Belgique|Belgio"),
    ("BG", r"Bulgaria|Bulgarien|Bulgarie"),
    ("HR", r"Croatia|Kroatien|Croatie|Croazia"),
    ("CY", r"Cyprus|Zypern|Chypre|Cipro"),
    ("CZ", r"Czech\s+Republic|Czechia|Tschechien|Tschechische\s+Republik|Tchéquie|République\s+tchèque"
           r"|Repubblica\s+Ceca|Cechia"),
    ("DK", r"Denmark|Dänemark|Danemark|Danimarca"),
    ("EE", r"Estonia|Estland|Estonie"),
    ("FI", r"Finland|Finnland|Finlande|Finlandia"),
    ("FR", r"France|Frankreich|Francia"),
    ("DE", r"Germany|Deutschland|Allemagne|Germania"),
    ("GR", r"Greece|Griechenland|Grèce|Grecia"),
    ("HU", r"Hungary|Ungarn|Hongrie|Ungheria"),
    ("IT", r"Italy|Italien|Italie|Italia"),
    ("LV", r"Latvia|Lettland|Lettonie|Lettonia"),
    ("LT", r"Lithuania|Litauen|Lituanie|Lituania"),
    ("LU", r"Luxembourg|Luxemburg|Lussemburgo"),
    ("MT", r"Malta|Malte"),
    ("NL", r"Netherlands|Niederlande|Pays-Bas|Paesi\s+Bassi|Olanda"),
    ("PL", r"Poland|Polen|Pologne|Polonia"),
    ("PT", r"Portugal|Portogallo"),
    ("RO", r"Romania|Rumänien|Roumanie"),
    ("SK", r"Slovakia|Slowakei|Slovaquie|Slovacchia"),
    ("SI", r"Slovenia|Slowenien|Slovénie"),
    ("ES", r"Spain|Spanien|Espagne|Spagna"),
    ("SE", r"Sweden|Schweden|Suède|Svezia"),
)
_JURISDICTION_RES = tuple((code, re.compile(r"(?<![\w])(?:" + pat + r")(?![\w])", re.IGNORECASE))
                          for code, pat in JURISDICTION_LEXICON)

#: Sentence-level markers that the place of the event is uncertain (EN, DE, FR, IT).
_LOCATION_UNCERTAIN_RE = re.compile(
    r"(?i)\bunsure\b|\bnot\s+sure\b|\bunclear\b|\buncertain\b|\bnot\s+known\b|\bunknown\b"
    r"|\btravel+ing\s+between\b|\bin\s+transit\s+between\b|\beither\b"
    r"|\bunklar\b|\bunsicher\b|\bnicht\s+bekannt\b|\bunbekannt\b|\bnicht\s+sicher\b|\bentweder\b"
    r"|\b(?:auf\s+der\s+reise|unterwegs)\s+zwischen\b"
    r"|\bincertain\b|\bpas\s+sûr\b|\bpas\s+clair\b|\binconnu\b|\bindéterminé\b|\ben\s+voyage\s+entre\b"
    r"|\bincerto\b|\bnon\s+sicuro\b|\bnon\s+chiaro\b|\bsconosciuto\b|\bnon\s+noto\b|\bin\s+viaggio\s+tra\b"
)
#: Explicit statements of the place of the event (A-07 step 1: "Ereignisort aus Brief").
_EVENT_PLACE_RE = re.compile(
    r"(?i)(?:\boccurred|\bhappened|\btook\s+place|\bincident\s+location|\bwhile\s+(?:staying|on\s+holiday)"
    r"|\bereignete\s+sich|\bereignisort|\bwährend\s+eines\s+aufenthalts|\bim\s+urlaub"
    r"|\bsurvenue?|\bs'est\s+produit|\blieu\s+de\s+l'(?:événement|incident)|\blors\s+d'un\s+séjour"
    r"|\bavvenut[oa]|\bverificat[oa]si|\bluogo\s+dell'evento|\bdurante\s+un\s+soggiorno)"
    r"[^.\n]{0,60}$"
)
#: Addressee or salutation line that ends the letter header (sender block).
_HEADER_END_RE = re.compile(
    r"(?im)^\s*(?:(?:an|to|à|a)\s*:|spett\.?le\b|sehr\s+geehrte|dear\b|madame\b|gentili\b|egregi\b|gentile\b)"
)


@dataclass(frozen=True)
class JurisdictionMention:
    code: str  # ISO code, "GB", "NI" or "UK" (GB or NI unclear)
    quote: str
    start: int
    end: int
    line: int

    def states(self) -> tuple[str, ...]:
        """Incident states this mention stands for ("UK" expands to GB and NI)."""
        return ("GB", "NI") if self.code == "UK" else (self.code,)


def find_jurisdictions(text: str) -> list[JurisdictionMention]:
    work = text
    found: list[JurisdictionMention] = []
    for code, pattern in _JURISDICTION_RES:
        for m in pattern.finditer(work):
            found.append(JurisdictionMention(code, text[m.start():m.end()], m.start(), m.end(), line_of(text, m.start())))
        work = pattern.sub(lambda m: "\x00" * (m.end() - m.start()), work)
    return sorted(found, key=lambda j: j.start)


@dataclass(frozen=True)
class LocationFinding:
    """Incident location stated in the letter (RS-1.0 / assumption A-07 step 1)."""

    states: tuple[str, ...]
    basis: str  # LETTER_UNCERTAIN | LETTER_EXPLICIT
    quote: str
    line: int


def incident_location(text: str, mentions: Sequence[JurisdictionMention]) -> LocationFinding | None:
    """Explicit incident location or explicit uncertainty between jurisdictions.

    * A sentence with an uncertainty marker and at least two jurisdictions (a bare "UK"
      counts as GB and NI) yields all of them: "GB oder NI unklar: beide Regelwerke"
      (concept 6.3 RS-1.0; Goal Card GC-GBNI).
    * A jurisdiction preceded by an explicit event-place phrase ("occurred in", "Ereignisort")
      yields that jurisdiction.
    """
    for s, e, sent in sentences(text):
        inside = [m for m in mentions if s <= m.start < e]
        if not inside:
            continue
        states: list[str] = []
        for m in inside:
            for st in m.states():
                if st not in states:
                    states.append(st)
        if _LOCATION_UNCERTAIN_RE.search(sent) and len(states) >= 2:
            return LocationFinding(tuple(states), "LETTER_UNCERTAIN", sent.strip(), line_of(text, s))
    for m in mentions:
        s, _ = sentence_span(text, m.start)
        if _EVENT_PLACE_RE.search(text[s:m.start]):
            return LocationFinding(m.states(), "LETTER_EXPLICIT", sentence_at(text, m.start), m.line)
    return None


def header_end(text: str) -> int:
    """Offset where the sender header ends (first addressee or salutation line)."""
    m = _HEADER_END_RE.search(text)
    return m.start() if m else min(len(text), 400)


# --------------------------------------------------------------------------- extraction result
@dataclass
class Extraction:
    """Deterministic facts extracted from one text (all values verbatim-quoted)."""

    language: str | None
    patient_refs: list[Evidence] = field(default_factory=list)
    patient_count: int | None = None
    same_lot_phrase: Evidence | None = None
    lots: list[Evidence] = field(default_factory=list)
    udi_dis: list[Evidence] = field(default_factory=list)
    models: list[Evidence] = field(default_factory=list)  # value = model code
    unknown_models: list[Evidence] = field(default_factory=list)  # brand model codes not in catalogue
    families: list[Evidence] = field(default_factory=list)  # value = family id
    sender_refs: list[Evidence] = field(default_factory=list)
    follow_up_marker: Evidence | None = None
    dates: list[Evidence] = field(default_factory=list)
    letter_date: Evidence | None = None
    implant_dates: list[Evidence] = field(default_factory=list)
    event_dates: list[Evidence] = field(default_factory=list)
    reference_dates: list[Evidence] = field(default_factory=list)
    implant_sites: list[Evidence] = field(default_factory=list)  # value = state named in an implant sentence
    jurisdictions: list[JurisdictionMention] = field(default_factory=list)
    incident_location: LocationFinding | None = None
    sender_country: JurisdictionMention | None = None

    # convenient value views -------------------------------------------------
    @property
    def patient_values(self) -> list[str]:
        return [e.value for e in self.patient_refs]

    @property
    def lot_values(self) -> list[str]:
        return [e.value for e in self.lots]

    @property
    def udi_values(self) -> list[str]:
        return [e.value for e in self.udi_dis]

    @property
    def model_values(self) -> list[str]:
        return [e.value for e in self.models]

    @property
    def family_values(self) -> list[str]:
        return [e.value for e in self.families]

    @property
    def sender_ref_values(self) -> list[str]:
        return [e.value for e in self.sender_refs]

    @property
    def has_device_identifiers(self) -> bool:
        """Model, lot or UDI-DI present (used for UNKNOWN vs NONE_DETECTED)."""
        return bool(self.models or self.unknown_models or self.lots or self.udi_dis)

    @property
    def has_registry_identifiers(self) -> bool:
        return bool(self.patient_refs or self.lots or self.udi_dis)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract(text: str, catalogue: ProductCatalogue | None = None) -> Extraction:
    """Extract identifiers, dates, places and markers from ``text``.

    ``text`` should be the injection-masked text (see INJ-1.0) so that injected
    instructions cannot steer identifiers or places; the offsets equal the raw text.
    """
    ex = Extraction(language=detect_language(text))
    ex.patient_refs = _dedupe(evidence(text, m.group(0), m.start(), m.end()) for m in PSEUDONYM_RE.finditer(text))
    ex.udi_dis = _dedupe(
        evidence(text, m.group(1) or m.group(2), m.start(), m.end()) for m in UDI_RE.finditer(text)
    )
    ex.lots = _dedupe(evidence(text, m.group(1), m.start(1), m.end(1)) for m in LOT_RE.finditer(text))
    ex.sender_refs = _dedupe(
        evidence(text, m.group(1).rstrip(".,;:)"), m.start(1), m.end(1)) for m in SENDER_REF_RE.finditer(text)
    )

    models: list[Evidence] = []
    families: list[Evidence] = []
    if catalogue is not None:
        for code, model in catalogue.models.items():
            pat = re.compile(r"(?<![A-Za-z0-9-])" + re.escape(code) + r"(?![A-Za-z0-9])")
            for m in pat.finditer(text):
                models.append(evidence(text, code, m.start(), m.end()))
                families.append(evidence(text, model.family, m.start(), m.end()))
        for fam in catalogue.families.values():
            for needle in (fam.brand_prefix, fam.family_id):
                for m in re.finditer(r"(?<![\w-])" + re.escape(needle) + r"(?![\w])", text):
                    families.append(evidence(text, fam.family_id, m.start(), m.end()))
    models.sort(key=lambda e: e.start)
    families.sort(key=lambda e: e.start)
    ex.models = _dedupe(models)
    ex.families = _dedupe(families)
    known = set(ex.model_values)
    ex.unknown_models = _dedupe(
        evidence(text, m.group(1), m.start(1), m.end(1))
        for m in BRAND_MODEL_RE.finditer(text)
        if m.group(1) not in known and (catalogue is None or m.group(1) not in catalogue.models)
    )

    counts = [len(ex.patient_refs)]
    for m in _PATIENT_COUNT_RE.finditer(text):
        token = m.group(1).lower()
        counts.append(int(token) if token.isdigit() else _NUMBER_WORDS.get(token, 0))
    ex.patient_count = max(counts) if max(counts) > 0 else None
    same_lot = _SAME_LOT_RE.search(text)
    ex.same_lot_phrase = evidence(text, same_lot.group(0), same_lot.start(), same_lot.end()) if same_lot else None

    marker = FOLLOW_UP_RE.search(text)
    ex.follow_up_marker = evidence(text, marker.group(0).strip(), marker.start(), marker.end()) if marker else None

    ex.dates = find_dates(text)
    head_end = header_end(text)
    header_dates = [d for d in ex.dates if d.start < head_end]
    ex.letter_date = header_dates[0] if header_dates else None
    ex.implant_dates, ex.event_dates, ex.reference_dates = classify_dates(text, ex.dates, ex.letter_date)

    ex.jurisdictions = find_jurisdictions(text)
    implant_spans = [sentence_span(text, d.start) for d in ex.implant_dates]
    ex.implant_sites = _dedupe(
        evidence(text, j.code, j.start, j.end) for j in ex.jurisdictions
        if any(s <= j.start < e for s, e in implant_spans)
    )
    ex.incident_location = incident_location(text, ex.jurisdictions)
    header_mentions = [j for j in ex.jurisdictions if j.start < head_end]
    ex.sender_country = header_mentions[0] if header_mentions else None
    return ex


# --------------------------------------------------------------------------- completeness
#: Canonical field order of the completeness check (seed labels in expected.json,
#: concept appendix B; SCN-NULL: null means unknown, not checked negative).
COMPLETENESS_FIELDS: tuple[str, ...] = (
    "product_family",
    "market",
    "device_model_or_version",
    "lot_or_udi",
    "patient_reference",
    "patient_exposure",
    "event_date",
    "event_description",
    "outcome",
    "cause",
)

_EXPOSURE_UNKNOWN_RE = re.compile(
    r"(?i)patientenexposition[^.\n]{0,40}\b(?:nicht\s+berichtet|nicht\s+bekannt|unbekannt|unklar)"
    r"|patient\s+exposure[^.\n]{0,40}\b(?:not\s+reported|unknown|unclear|not\s+known)"
    r"|exposition\s+(?:du|d'un|des)\s+patients?[^.\n]{0,40}\b(?:non\s+signalée|non\s+rapportée|inconnue)"
    r"|esposizione\s+(?:del|di|dei)\s+pazient[ei][^.\n]{0,40}\b(?:non\s+riportata|non\s+segnalata|sconosciuta)"
)
_CAUSE_UNKNOWN_RE = re.compile(
    r"(?i)\bursache\b[^.\n]{0,60}\b(?:ungeklärt|unklar|unbekannt|offen|nicht\s+bekannt|fehlt|fehlen)\b"
    r"|\b(?:ungeklärte|unklare|unbekannte)\s+ursache\b"
    r"|\b(?:root\s+)?cause\b[^.\n]{0,60}\b(?:unknown|unclear|undetermined|not\s+known|remains?\s+open)\b"
    r"|\bunknown\s+cause\b"
    r"|\bcause\b[^.\n]{0,60}\b(?:inconnue|indéterminée|non\s+élucidée|pas\s+claire)"
    r"|\bcausa\b[^.\n]{0,60}\b(?:sconosciuta|ignota|non\s+nota|non\s+chiara|da\s+chiarire)"
)
_OUTCOME_UNKNOWN_RE = re.compile(
    r"(?i)\bausgang\b[^.\n]{0,60}\b(?:ungeklärt|unklar|unbekannt|offen|fehlt|fehlen|nicht\s+bekannt)"
    r"|\boutcome\b[^.\n]{0,60}\b(?:unknown|unclear|not\s+known|pending|missing)"
    r"|\bévolution\b[^.\n]{0,60}\b(?:inconnue|incertaine)|\besito\b[^.\n]{0,60}\b(?:sconosciuto|non\s+noto|incerto)"
)
_OUTCOME_STATED_RE = re.compile(
    r"(?i)\b(?:recover(?:ed|ing)|stable|discharged|resolved|died|deceased"
    r"|erholt|stabil|beschwerdefrei|entlassen|entlassung|verstorben|exitus"
    r"|évolution\s+est\s+favorable|sortie|guéri|décédé"
    r"|guarit[oa]|stabile|dimess[oa]|decedut[oa])\b"
)


def completeness(text: str, extraction: Extraction, metadata: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """Present and missing case-data fields of one message (seed completeness labels).

    Structured seed metadata (``product_family``, ``market``, ``event_date``) count as
    present when not null. Fields are never inferred: no value means missing. "cause"
    is assessed only when the text addresses it; "patient_exposure" replaces
    "patient_reference" when the text says exposure was not reported.
    """
    meta = metadata or {}
    present: list[str] = []
    missing: list[str] = []

    def mark(name: str, ok: bool) -> None:
        (present if ok else missing).append(name)

    mark("product_family", bool(meta.get("product_family")) or bool(extraction.families))
    mark("market", bool(meta.get("market")) or bool(extraction.jurisdictions))
    mark("device_model_or_version", bool(meta.get("device_model")) or bool(extraction.models or extraction.unknown_models))
    mark("lot_or_udi", bool(meta.get("lot") or meta.get("udi_di")) or bool(extraction.lots or extraction.udi_dis))
    if _EXPOSURE_UNKNOWN_RE.search(text):
        mark("patient_exposure", False)
    else:
        mark("patient_reference", bool(meta.get("patient_reference")) or bool(extraction.patient_refs))
    if "event_date" in meta:
        mark("event_date", meta.get("event_date") is not None)
    else:
        mark("event_date", bool(extraction.event_dates))
    mark("event_description", bool(text.strip()))
    mark("outcome", bool(_OUTCOME_STATED_RE.search(text)) and not _OUTCOME_UNKNOWN_RE.search(text))
    if _CAUSE_UNKNOWN_RE.search(text):
        mark("cause", False)
    order = {name: i for i, name in enumerate(COMPLETENESS_FIELDS)}
    return {"present": sorted(present, key=order.__getitem__), "missing": sorted(missing, key=order.__getitem__)}
