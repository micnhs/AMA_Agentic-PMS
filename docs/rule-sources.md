# Rule sources

This page lists every constant of the fixed rule code (rule package 1.0) with its source: article or
section, file and page or line. The source IDs (for example `MDR-87-5`) are the keys of the
`source_catalogue` in [`data/synthetic/expected.json`](../data/synthetic/expected.json) (frozen golden
set v1.0); the full catalogue is repeated at the end of this page.

> **The demo illustrates; it makes no compliance claim.** The rules are a simplified demo rule set for
> fictional devices and synthetic letters. They are not regulatory or legal advice and say nothing about
> conformity with MDR, UK MDR, ISO 13485, GAMP 5 or any other requirement. No regulatory fact was
> invented: every value below comes from the sources named in the design concept (sections 4.2, 4.3,
> 6.3 and appendix C). The source documents themselves are not part of this repository.

## How the sources were used

Source hierarchy (design concept, section 4.2):

1. Official legal texts: Regulation (EU) 2017/745 (MDR; file `CELEX_32017R0745_DE_TXT.pdf`) and the UK
   Medical Devices Regulations 2002 including Part 4A (file `UK The Medical Devices Regulations.pdf`,
   as of 9 Apr 2026).
2. MDCG guidance as official European Commission downloads: MDCG 2023-3 Rev. 2 (January 2025) and
   MDCG 2020-10/1 Rev. 1 (October 2022).
3. Scenario and assumption files of the case study: `03-scenario.md`, `02-assumptions.md`,
   `01-vision.md`.
4. The regulatory analysis of 8 Oct 2026 (`regulatorische_analyse_pms_vigilanz_psur_eudamed_20261008_144024.pdf`)
   as a secondary source without a bibliography (OI-016).

**Where the provided analysis and the official text differ, the stricter rule is used**, and the
difference is recorded as a conflict (K-xx) or an open issue (OI-xxx); see
[Conflicts that changed a rule](#conflicts-that-changed-a-rule).

**Authorities are labels only.** The demo never submits anything and has no channel integration. IE
HPRA, DE BfArM and IT NSIS-Dispovigilance are published starting points; the French ANSM manufacturer
route is not evidenced and is shown as **"route unverified"** (OI-008). Any other EU state without a
sourced route is labelled "route unverified" as well (unknown stays unknown).

Every rule carries a version number (for example `CRIT-1.0`) that appears in every audit record and case
report (`src/pms/versions.py`). A rule change is a new version with a unit test and a change-log entry.

## Checked rules (design concept, section 4.3)

| Topic | Rule in the demo | Reference | Result |
|---|---|---|---|
| Deadlines EU | 2 days for a serious public health threat, 10 days for death or unanticipated serious deterioration, otherwise 15 days | MDR Art. 87(3)-(5), PDF p. 73; analysis p. 2 | confirmed |
| Deadlines GB | 2, 10 and 15 days after awareness | UK MDR reg. 44ZH(3)-(5), PDF p. 121; analysis p. 4 | confirmed |
| Uncertainty | If in doubt, report within the deadline | MDR Art. 87(7), PDF p. 73; MDCG 2023-3 question 1 | confirmed; the demo sets a provisional 15 days |
| Start of the deadline | Day after awareness, calendar days, end 23:59:59 | MDCG 2023-3 question 14, p. 17-19; analysis p. 8 | confirmed; without weekend extension (K-05) |
| Awareness | First receipt by an employee or representative of the manufacturer | MDCG 2023-3 question 15, p. 19-20; analysis p. 8 | confirmed |
| Expected side effects | Exception only with documentation, quantification and trend reporting; decision only by humans | MDR Art. 87(1)(a), PDF p. 73; MDCG 2023-3 question 10, p. 15-16 | confirmed; never automatic |
| GB: known side effects | Serious incidents remain individually reportable; reg. 44ZH has no exception like Art. 87(1)(a) | UK MDR reg. 44ZH(1), PDF p. 121; `03-scenario.md` line 192 | confirmed |
| Definitions | Death, serious deterioration, serious public health threat | MDR Art. 2(65)-(66), PDF p. 20; UK MDR reg. 44ZC, PDF p. 116-117; MDCG 2023-3 question 2 | confirmed |
| Competent authority EU | Member State in which the incident occurred | MDR Art. 92(5), PDF p. 77; analysis p. 6 and 9 | confirmed |
| Northern Ireland | EU MDR; MHRA competent; MORE with the EU form | analysis p. 13; `03-scenario.md` lines 200 and 205 | secondary sources only (OI-007) |
| Great Britain | UK MDR Part 4A since 16 June 2025; MORE with the GB form | UK MDR PDF p. 115 (F349); analysis p. 5 and 12; `03-scenario.md` line 578 | date disputed (K-08); stricter variant used |
| Causality scale | 4 levels: not related, possible, probable, causal; not assessable means possible | MDCG 2020-10/1 Rev. 1 section 9 (EC download, PDF p. 10-11); decision Q-2 | confirmed; guideline for clinical investigations, used by analogy in the demo (A-13) |
| Part 4A in the UK PDF | Included | UK MDR PDF p. 115 | confirmed (OI-026 closed) |

## Rule constants and their sources

"Goal Card" means the Goal Card of 10 Nov 2026 (QUALITY and DONE WHEN sections). "A-xx" are the design
assumptions (concept section 5), "K-xx" the conflicts (section 16), "OI-xxx" the open issues
(section 17), "Q-x" the decisions of the project owner (section 2).

### CRIT-1.0 – critical detection (`src/pms/rules/crit.py`)

| Constant or logic | Value in the demo | Source IDs | Source |
|---|---|---|---|
| Indicator group `DEATH`: raw-text term lists DE, EN, FR, IT (for example 'verstorben', 'Exitus letalis', 'died', 'décédé', 'deceduto') | critical; severity `DEATH` | `MDR-2-65`, `UK-44ZC`, `GC-CRIT` | MDR Art. 2(65)(a), `CELEX_32017R0745_DE_TXT.pdf`, PDF p. 20; UK MDR 2002 reg. 44ZC, `UK The Medical Devices Regulations.pdf`, PDF p. 116-117 |
| Indicator group `PUBLIC_HEALTH_THREAT`: at least two patients with the same lot, or an explicit cluster statement | critical; severity `PUBLIC_HEALTH_THREAT` | `MDR-2-65`, `MDCG-Q2`, `UK-44ZC` | MDR Art. 2(65)(c) and 2(66), PDF p. 20; MDCG 2023-3 Rev. 2 question 2, `mdcg_2023-3_en.pdf`, p. 9-11 |
| Indicator group `SERIOUS_DETERIORATION`: for example hospital admission, revision surgery, intensive care, life-threatening | critical; severity `SERIOUS_DETERIORATION` | `MDR-2-65`, `MDCG-Q2`, `UK-44ZC` | MDR Art. 2(65)(b), PDF p. 20; MDCG 2023-3 Rev. 2 question 2 ("serious deterioration incl. hospitalisation and surgical intervention"), p. 9-11 |
| `DEADLINE_WINDOW_HOURS = 48` | also critical when the earliest open deadline is due within at most 48 hours (pilot) | `ASM-SENT`, decision Q-3 | `02-assumptions.md`, section 'CHG-009 medical analysis', line 648; decision Q-3 (keep the window; observe the noise in Gate C, OI-014) |
| No negation logic; in doubt critical; the AI reading (A1, A3) is a second detector that may only raise | urgency is raised by rule or AI, lowered only by a medical reviewer with a reason | `GC-CRIT` | Goal Card, QUALITY |
| Severity `NONE_DETECTED` and `UNKNOWN` | `NONE_DETECTED` = no indicator in the raw text (not "non-serious"); `UNKNOWN` = essential data missing | `GC-UNK`, `SCN-NULL` | Goal Card, QUALITY; `03-scenario.md` line 1369 |

### DL-1.0 – reporting deadlines (`src/pms/rules/deadlines.py`)

| Constant or logic | Value in the demo | Source IDs | Source |
|---|---|---|---|
| Awareness anchor | earliest receipt of any signal of the case by the fictional manufacturer | `MDCG-Q15`, `SCN-AWARE`, A-02 | MDCG 2023-3 Rev. 2 question 15, p. 19-20; `03-scenario.md` line 1391 |
| `DEFAULT_TIMEZONE = "Europe/Dublin"` | day 0 is the calendar date of the anchor in Europe/Dublin (configurable) | A-01, OI-020 | design assumption: the fictional manufacturer is based in Ireland (not a legal rule) |
| Start and end of a deadline | day 1 is the calendar day after awareness from 00:00:01; day N ends at 23:59:59; calendar days; no weekend or holiday extension | `MDCG-Q14`, `RA-P8`, K-05, OI-004 | MDCG 2023-3 Rev. 2 question 14, p. 17-19; regulatory analysis p. 8 |
| `DAYS[PUBLIC_HEALTH_THREAT] = 2` | 2 days | `MDR-87-4`, `RA-P2`, `RA-P4` | MDR Art. 87(4), PDF p. 73; GB: UK MDR reg. 44ZH(3)-(5), PDF p. 121 (concept 4.3; the catalogue has no separate ID for paragraph (4)) |
| `DAYS[DEATH] = 10` | 10 days | `MDR-87-5`, `UK-44ZH-5`, `RA-P4` | MDR Art. 87(5), PDF p. 73; UK MDR reg. 44ZH(5), PDF p. 121 |
| `DAYS[SERIOUS_DETERIORATION] = 10` | 10 days until a human classifies the deterioration as anticipated | `MDR-87-5`, `MDCG-Q12`, `UK-44ZH-5`, A-03 | MDR Art. 87(5), PDF p. 73; MDCG 2023-3 Rev. 2 question 12 ("unanticipated = not considered in the risk analysis"), p. 16 |
| `DAYS[SERIOUS_DETERIORATION_ANTICIPATED] = 15` | 15 days, only after a human classified the deterioration as anticipated | `MDR-87-3`, `UK-44ZH-3` | MDR Art. 87(3), PDF p. 73; UK MDR reg. 44ZH(3), PDF p. 121 |
| `DAYS[PROVISIONAL_REPORTABILITY_UNCLEAR] = 15` | provisional 15 days while no serious basis is known and reportability is unclear | `MDR-87-3`, `MDR-87-7`, `MDCG-Q1`, `GC-EARLY`, A-04 | MDR Art. 87(3) and 87(7), PDF p. 73; MDCG 2023-3 Rev. 2 question 1, p. 5-8 |
| Several rule sets | every candidate of every rule set is shown; the earliest drives priority | `GC-GBNI`, `GC-EARLY` | Goal Card, QUALITY |
| Overdue and clock stop | overdue when the clock is not stopped; the clock stops only when a person with the regulatory role records a submission for the approved version | Goal Card | Goal Card, QUALITY ("deadline stops only when a person records the submission"); concept 6.3 and 7.2 |
| Closing as non-reportable | does not stop the clock; the deadline is shown as "not applicable according to a human decision" ("entfällt laut menschlicher Entscheidung") with user and reason | decision Q-4, K-13 | concept 7.2 |
| Follow-ups and duplicates | never reset the anchor; a deadline is never moved later automatically | `GC-DUP`, `SCN-AWARE`, K-06, OI-005 | Goal Card, QUALITY; `03-scenario.md` line 1391 |

### RS-1.0 – rule set and authority label (`src/pms/rules/rulesets.py`)

| Constant or logic | Value in the demo | Source IDs | Source |
|---|---|---|---|
| Incident state | place stated in the letter, otherwise the implant site in the registry, otherwise the sender country; a human confirms | A-07 | design assumption |
| EU Member State | rule set `EU-MDR:<state>`, authority of the incident state | `MDR-92-5`, `MDR-87-1`, `MDCG-Q1`, `RA-P6`, `RA-P9` | MDR Art. 92(5), PDF p. 77; MDR Art. 87(1), PDF p. 73; MDCG 2023-3 Rev. 2 question 1 (footnote 7), p. 5-8; regulatory analysis p. 6 and 9 |
| Northern Ireland | `EU-MDR:NI`; MHRA; MORE portal with the EU form | `RA-P13`, `RA-P5`, `RA-P6`, `SCN-GBNI` | regulatory analysis p. 13, 5 and 6; `03-scenario.md` lines 192, 200, 205 – secondary sources only (OI-007) |
| England, Scotland, Wales | `UK-MDR-PART4A:GB`; MHRA; MORE portal with the GB form | `UK-PART4A`, `UK-44ZD`, `UK-44ZH-1`, `RA-P5`, `RA-P12` | UK MDR 2002 Part 4A, annotation F349, PDF p. 115; reg. 44ZD, PDF p. 117-118; reg. 44ZH(1), PDF p. 121; regulatory analysis p. 5 and 12 |
| `GB_PART4A_FROM = 2025-06-16` | Part 4A for devices placed on the GB market from 16 June 2025; older units (or an unknown date) provisionally also Part 4A with a check note | `RA-P12`, `UK-PART4A`, `SCN-GBDATE`, A-06, K-08, OI-006 | the stricter of two dates: 16 June 2025 (primary text; analysis p. 12) instead of 17 June 2025 (annotation F349); `03-scenario.md` line 578 |
| GB or NI unclear | both rule sets with their own deadlines | `GC-GBNI` | Goal Card, QUALITY |
| GB: known side effects | serious incidents stay individually reportable; no exception like MDR Art. 87(1)(a) | `UK-44ZH-1`, `SCN-GBNI` | UK MDR reg. 44ZH(1), PDF p. 121; `03-scenario.md` line 192 |
| EU: expected side effects | exception only if documented, quantified and trend-reported; decided only by a human, never automatically | `MDR-87-1`, `MDCG-Q10` | MDR Art. 87(1)(a), PDF p. 73; MDCG 2023-3 Rev. 2 question 10, p. 15-16 |
| Authority label IE | "IE HPRA (published route)" | `SCN-IE`, `RA-P5`, `RA-P9`, `GC-AUTH` | `03-scenario.md` line 214 |
| Authority label DE | "DE BfArM (published starting point)" | `SCN-DE`, `RA-P6`, `GC-AUTH` | `03-scenario.md` line 215 |
| Authority label IT | "IT Ministero della Salute NSIS-Dispovigilance (published starting point)" | `SCN-IT`, `GC-AUTH` | `03-scenario.md` line 216 |
| Authority label FR | "FR ANSM (route unverified)" | `SCN-FR`, `GC-AUTH`, OI-008 | `03-scenario.md` line 217 (manufacturer route not evidenced) |
| Authority label GB | "GB MHRA, MORE portal, GB form" | `RA-P5`, `RA-P6`, `UK-44ZH-1` | regulatory analysis p. 5 and 6 |
| Authority label NI | "NI MHRA, MORE portal, EU form" | `RA-P5`, `RA-P13`, `SCN-GBNI` | regulatory analysis p. 5 and 13 |
| Other EU states, unresolved state | "competent authority of the incident state (route unverified)"; an unresolved state needs a human to confirm the authority | `GC-AUTH`, `GC-UNK` | Goal Card, QUALITY |
| `EU_MEMBER_STATES` | the 27 EU Member States | – | general reference list (no catalogue entry) |

### LINK-1.0 – duplicates and follow-ups (`src/pms/rules/linking.py`)

| Constant or logic | Value in the demo | Source IDs | Source |
|---|---|---|---|
| Duplicate | same sender reference, or the same patient pseudonym with the same product and event (an identical text is a duplicate as well) | `SCN-SEEDS`, `SCN-AWARE`, `GC-DUP` | `03-scenario.md` lines 1391, 1399-1413, 1435 |
| Follow-up | follow-up marker ('Nachtrag', 'follow-up', 'suite à', 'a seguito') **and** a resolvable reference to an existing case | `SCN-SEEDS`, `SCN-AWARE` | same lines; concept 6.3 |
| Follow-up without a resolvable reference | new case with the note "possible follow-up" | – | concept 6.3 (design rule) |
| Seed messages | the structured fields `previous_message_id` and `duplicate_of_message_id` apply | `SCN-SEEDS` | `03-scenario.md` lines 1395-1422 and 1430-1435 |
| Visibility and anchor | every signal stays visible; the awareness anchor of the case stays | `SCN-AWARE`, `GC-DUP` | `03-scenario.md` line 1391; Goal Card, QUALITY |

### REG-1.0 – registry check (`src/pms/rules/registry.py`)

| Constant or logic | Value in the demo | Source IDs | Source |
|---|---|---|---|
| Compared identifiers | patient pseudonym, lot and UDI-DI against the fictional registry (`data/synthetic/registry/registry.json`) | `GC-UNK`, `SCN-NULL` | Goal Card, QUALITY; `03-scenario.md` line 1369 |
| Results | `MATCH`, `MISMATCH` (every difference with field and value), `NO_IDENTIFIERS`, `NOT_FOUND`; nothing is dropped silently; the code never corrects data | `GC-UNK`, `SCN-NULL` | as above |
| Fictional UDI-DI | GS1 prefix 049 (numbers for internal use), so that no real GTIN is created | A-08, OI-022 | design assumption, to be verified |

### INJ-1.0 – prompt-injection detection (`src/pms/rules/injection.py`)

| Constant or logic | Value in the demo | Source IDs | Source |
|---|---|---|---|
| Pattern list DE, EN, FR, IT | instructions addressed to systems: "ignore all previous instructions", notes addressed to an AI system, role overrides, status commands, forwarding addresses, "do not mention" | `GC-INJ` | Goal Card, DONE WHEN check 11 |
| Effect of a hit | a visible flag and one audit record; no label changes; paragraphs with a hit are blanked for extraction and search queries; CRIT-1.0 still scans the full raw text (it may only raise) | `GC-INJ` | Goal Card, DONE WHEN check 11 ("same labels as its clean copy and raises a flag") |
| Letter text is data | the letter goes to a model only as a bounded data block; outputs are checked against a schema | – | concept 6.3 (INJ-1.0) and 13 |

Translation note: the concept text of INJ-1.0 still says "agents have no tools" (wording from v0.2).
Since v0.3 the agents A2, A3 and A6 use read-only function tools that the app validates and executes;
see [ADR-001](adr/ADR-001.md).

### CORR-1.0 – correlation index (`src/pms/rules/correlation.py`)

| Constant or logic | Value in the demo | Source IDs | Source |
|---|---|---|---|
| Event-term dictionary DE, EN, FR, IT | `event_terms` in `data/synthetic/knowledge/knowledge_index.json` (for example 'Segelriss', 'torn leaflet', 'déchirure', 'lacerazione') | `DEC-R15` | change request R-15 of 10 Nov 2026 |
| Match types | same lot; same model; same product family with the same event group; listed side effects of the product dossier for the event group; further registry patients with the same lot | `DEC-R15` | `expected.json` `conventions.correlation`; concept 6.3 and 6.5 |
| `DOSSIER_QUERIES` | maps each event group to listed side effects of the fictional dossiers; radiolucent line to loosening or migration and loosening to instability or dislocation follow the fictional corpus items LIT-008 and LIT-009 | – | `data/synthetic/products/demo-cardio-b_dossier.md` and `demo-ortho-a_dossier.md` (section on listed side effects); `data/synthetic/knowledge/literature/LIT-008.md`, `LIT-009.md` |
| Output | counts and named matches with source ID only; no rates, scores or significance; a match can only raise urgency; "no hits" never means all-clear | – | Goal Card, QUALITY (no statistical statements); `01-vision.md` VIS-003; K-15 |
| Queries | built only from allowed structured fields (lot, model, family, event group); letter text never becomes a query | – | test T21; concept 6.5 |

### Constants used by the agents (not rules, listed for completeness)

| Constant | Value in the demo | Source IDs | Source |
|---|---|---|---|
| Causality scale (A3) | not related, possible, probable, causal; "not assessable" means possible; ranges are "not clinically validated" | `MDCG-2020-10-1-S9`, `DEC-Q2` | MDCG 2020-10/1 Rev. 1 section 9, `md_mdcg_2020-10-1_guidance_safety_reporting_en.pdf`, PDF p. 10-11; written for clinical investigations, used by analogy (A-13); replaces the 5-level pilot scale of the Goal Card (`GC-CAUS`, K-07) |
| IMDRF code candidates (A3) | only A0401 (Annex A, device problem, "Break") and F1905 (Annex F, health impact, "Device Revision or Replacement"); always candidates pending human review; if no listed code fits, the list stays empty with the note "no candidate from the demo list" | OI-009 | `03-scenario.md` SRC-014: IMDRF Adverse Event Terminology, release 2026, annexes dated 2026-03-23 (https://www.imdrf.org/node/460). Attribution: International Medical Device Regulators Forum (IMDRF). The IMDRF terms of use restrict use to non-commercial research or teaching unless authorised. |
| Trend view (A5) | the six synthetic monthly rows 2026-04 to 2026-09 with unit and period; denominator unknown, no rates | `SCN-MONTHLY` | `03-scenario.md` lines 1494-1509; `data/synthetic/trend/monthly_counts.json` |
| Process findings | demo-finding-01 to -03, status Open; only a human closes them | `SCN-FINDINGS` | `03-scenario.md` lines 1511-1519 |
| Reject reason codes | R1 insufficient evidence, R2 facts extracted incorrectly, R3 wrong product assignment, R4 causality not traceable, R5 rule set or deadline wrong, R6 other (free text mandatory) | – | concept 7.2 (design, not a regulatory source) |

## Conflicts that changed a rule

From design concept section 16. Rule of precedence: the project owner's wishes win for functions; the
guardrails of the framing win for safety.

| No. | Conflict | Rule | Resolution in the demo |
|---|---|---|---|
| K-05 | MDCG 2023-3 question 14 extends deadlines that end on a weekend or public holiday; Goal Card: deadlines lean early | stricter rule | no extension; the calendar end applies (OI-004) |
| K-06 | MDCG 2023-3 question 14 special case: new information can set a new start; Goal Card: follow-ups never replace the first awareness | stricter rule | the anchor stays at the first receipt; no automatic shift (OI-005) |
| K-07 | Causality scale: Goal Card 5 levels (pilot) versus MDCG 2020-10/1 Rev. 1 with 4 levels without "unlikely" | decided by the project owner (Q-2) | 4 levels according to MDCG; L07 and L11 relabelled; ranges "not clinically validated" |
| K-08 | GB start date of Part 4A: 16 June 2025 (primary text, analysis p. 12) versus 17 June 2025 (annotation F349) | stricter rule | 16 June 2025 applies (OI-006) |
| K-09 | Analysis p. 12: GB vigilance "exclusively MORE"; `03-scenario.md` line 690: wrong as a general statement (special route reg. 44ZK) | official text and scenario win | MORE only as a label for incident reports; FSCA is not in scope |
| K-12 | Broad critical definition (also the unrelated letter L07) versus a quiet demo story | guardrail wins: only a human lowers; decided by the project owner (Q-3) | the 48-hour window stays; observe the noise in Gate C (OI-014) |
| K-13 | Goal Card: "deadline stops only when a person records the submission" versus closing as non-reportable | Goal Card verbatim; decided by the project owner (Q-4) | closing as non-reportable does not stop the clock; the deadline is marked "not applicable according to a human decision" |

## Related open issues

| No. | Topic |
|---|---|
| OI-004 | No weekend or holiday extension |
| OI-005 | MDCG special case for a new deadline start not applied |
| OI-006 | GB start date 16 or 17 June 2025 |
| OI-007 | Primary law for Northern Ireland not in the inputs |
| OI-008 | ANSM manufacturer route unverified |
| OI-009 | IMDRF codes and terms of use |
| OI-014 | Critical definition may be too noisy |
| OI-015 | Older standard editions: the ISO file is DIN EN ISO 13485:2007-10 (ISO 13485:2003), the GAMP file a 2013 overview |
| OI-016 | The regulatory analysis is a secondary source |
| OI-020 | Time zone for day 0 |
| OI-022 | GS1 prefix 049 for fictional UDI-DI |

## Source catalogue (from `expected.json`)

All 50 entries of `source_catalogue`, verbatim. Letter, dossier and registry references in the labels (for example `LTR:L01:14` or `REG:REG-0001`) point to files in `data/synthetic/`.

| Source ID | Article or section, file, page or line |
|---|---|
| `MDR-2-65` | MDR Art. 2(65) serious incident (a) death, (b) serious deterioration, (c) serious public health threat; Art. 2(66) – CELEX_32017R0745_DE_TXT.pdf, PDF p. 20 |
| `MDR-87-1` | MDR Art. 87(1)(a) reporting of serious incidents on the Union market – CELEX_32017R0745_DE_TXT.pdf, PDF p. 73 |
| `MDR-87-3` | MDR Art. 87(3) 15 days – CELEX_32017R0745_DE_TXT.pdf, PDF p. 73 |
| `MDR-87-4` | MDR Art. 87(4) 2 days, serious public health threat – CELEX_32017R0745_DE_TXT.pdf, PDF p. 73 |
| `MDR-87-5` | MDR Art. 87(5) 10 days, death or unanticipated serious deterioration – CELEX_32017R0745_DE_TXT.pdf, PDF p. 73 |
| `MDR-87-7` | MDR Art. 87(7) uncertainty about reportability: report within the deadline – CELEX_32017R0745_DE_TXT.pdf, PDF p. 73 |
| `MDR-92-5` | MDR Art. 92(5) authority of the Member State where the incident occurred – CELEX_32017R0745_DE_TXT.pdf, PDF p. 77 |
| `UK-44ZC` | UK MDR 2002 reg. 44ZC definitions (serious incident, serious deterioration, serious public health threat) – UK The Medical Devices Regulations.pdf, PDF p. 116-117 |
| `UK-44ZD` | UK MDR 2002 reg. 44ZD scope of Part 4A – UK The Medical Devices Regulations.pdf, PDF p. 117-118 |
| `UK-44ZH-1` | UK MDR 2002 reg. 44ZH(1) report to the Secretary of State – UK The Medical Devices Regulations.pdf, PDF p. 121 |
| `UK-44ZH-5` | UK MDR 2002 reg. 44ZH(5) 10 days, death or unanticipated serious deterioration – UK The Medical Devices Regulations.pdf, PDF p. 121 |
| `UK-44ZH-3` | UK MDR 2002 reg. 44ZH(3) 15 days – UK The Medical Devices Regulations.pdf, PDF p. 121 |
| `UK-PART4A` | UK MDR 2002 Part 4A, annotation F349 (inserted by S.I. 2024/1368) – UK The Medical Devices Regulations.pdf, PDF p. 115 |
| `MDCG-Q1` | MDCG 2023-3 Rev. 2 Q1 (if uncertain whether reportable, report within the timeframe; competent authority of the incident state, fn. 7) – mdcg_2023-3_en.pdf (EC download), p. 5-8 |
| `MDCG-Q2` | MDCG 2023-3 Rev. 2 Q2 (serious deterioration incl. hospitalisation and surgical intervention; serious public health threat) – mdcg_2023-3_en.pdf (EC download), p. 9-11 |
| `MDCG-Q10` | MDCG 2023-3 Rev. 2 Q10 (expected side-effect exception only if documented, quantified and trend-reported; otherwise MIR within the deadline) – mdcg_2023-3_en.pdf (EC download), p. 15-16 |
| `MDCG-Q12` | MDCG 2023-3 Rev. 2 Q12 (unanticipated = not considered in the risk analysis) – mdcg_2023-3_en.pdf (EC download), p. 16 |
| `MDCG-Q14` | MDCG 2023-3 Rev. 2 Q14 (calendar days; period starts the day after the awareness date at 00:00:01 and ends on day N at 23:59:59) – mdcg_2023-3_en.pdf (EC download), p. 17-19 |
| `MDCG-Q15` | MDCG 2023-3 Rev. 2 Q15 (awareness = first receipt by any employee or representative of the manufacturer) – mdcg_2023-3_en.pdf (EC download), p. 19-20 |
| `MDCG-2020-10-1-S9` | MDCG 2020-10/1 Rev. 1 section 9 (4-level causality definitions: not related, possible, probable, causal relationship; not assessable -> possible; written for clinical investigations, used here by analogy) – md_mdcg_2020-10-1_guidance_safety_reporting_en.pdf (EC download), PDF p. 10-11 |
| `RA-P2` | Regulatory analysis (secondary source), executive summary no. 2: EU 2/10/15 days – regulatorische_analyse_pms_vigilanz_psur_eudamed_20261008_144024.pdf, p. 2 |
| `RA-P4` | Regulatory analysis, comparison matrix 'Serious incident': EU, GB, NI 2/10/15 calendar days – regulatorische_analyse_pms_vigilanz_psur_eudamed_20261008_144024.pdf, p. 4 |
| `RA-P5` | Regulatory analysis, comparison matrix 'Vigilance channel Oct 2026': IE HPRA; GB MORE GB form; NI MORE EU form – regulatorische_analyse_pms_vigilanz_psur_eudamed_20261008_144024.pdf, p. 5 |
| `RA-P6` | Regulatory analysis, comparison matrix 'Competent authority': EU incident state; GB MHRA; NI MHRA – regulatorische_analyse_pms_vigilanz_psur_eudamed_20261008_144024.pdf, p. 6 |
| `RA-P8` | Regulatory analysis, section 2 EU vigilance: deadline starts on the day after the awareness date; first receipt by employee or representative – regulatorische_analyse_pms_vigilanz_psur_eudamed_20261008_144024.pdf, p. 8 |
| `RA-P9` | Regulatory analysis, section 2: incidents in Ireland to HPRA, other Member States to the authority of the incident state – regulatorische_analyse_pms_vigilanz_psur_eudamed_20261008_144024.pdf, p. 9 |
| `RA-P12` | Regulatory analysis, section 5 Great Britain: Part 4A since 16 June 2025 – regulatorische_analyse_pms_vigilanz_psur_eudamed_20261008_144024.pdf, p. 12 |
| `RA-P13` | Regulatory analysis, section 6 Northern Ireland: EU MDR applies, MHRA competent, MORE with EU form – regulatorische_analyse_pms_vigilanz_psur_eudamed_20261008_144024.pdf, p. 13 |
| `SCN-GBNI` | 03-scenario.md, section 'GB and NI update', lines 192, 200, 205 (GB known serious side effects individually reportable; NI follows EU MDR; NI uses EU forms in MORE) |
| `SCN-IE` | 03-scenario.md, section 'SRC-055 national manufacturer procedures', line 214 (IE: HPRA published route) |
| `SCN-DE` | 03-scenario.md, section 'SRC-055', line 215 (DE: BfArM published starting point) |
| `SCN-IT` | 03-scenario.md, section 'SRC-055', line 216 (IT: NSIS-Dispovigilance published starting point) |
| `SCN-FR` | 03-scenario.md, section 'SRC-055', line 217 (FR: ANSM manufacturer route not evidenced) |
| `SCN-GBDATE` | 03-scenario.md, section 'SRC-041 GB commencement date', line 578 (primary text includes 16 June 2025; annotation says 17 June) |
| `SCN-SEEDS` | 03-scenario.md, sections 'Demo physician messages' lines 1395-1422 and 'Demo PMS cases' lines 1430-1435 |
| `SCN-AWARE` | 03-scenario.md, section 'Demo input and provenance dictionary', line 1391 (duplicates and follow-ups never replace the original awareness time) |
| `SCN-NULL` | 03-scenario.md, line 1369 (null means unknown, not checked negative) |
| `ASM-SENT` | 02-assumptions.md, section 'CHG-009 medical analysis', line 648 (sentinel: possible acute danger or time-critical deadline loss -> immediate human interruption) |
| `DEC-Q2` | Decision of Michael Niehues, 10 Nov 2026 (cycle-1 review, question Q-2): use the 4-level causality scale of MDCG 2020-10/1 Rev. 1 – PMS_Demo_Cycle_Log.md, cycle 2 |
| `DEC-R15` | Change request of Michael Niehues, 10 Nov 2026 (cycle-1 review): agents correlate further sources (literature, past cases and findings, product data, technical documents) – PMS_Demo_Cycle_Log.md, cycle 2 |
| `SCN-MONTHLY` | 03-scenario.md, section 'Demo monthly counts', lines 1494-1509 (six synthetic monthly rows 2026-04 to 2026-09, denominator null) |
| `SCN-FINDINGS` | 03-scenario.md, section 'Demo quality findings', lines 1511-1519 (demo-finding-01 to -03, status Open) |
| `GC-CRIT` | Goal Card of 10 Nov 2026, QUALITY: fixed rule scans raw text DE/EN/FR/IT; AI reading is a second detector; only a human lowers urgency |
| `GC-EARLY` | Goal Card of 10 Nov 2026, QUALITY: deadlines lean early; an unclear reporting obligation never postpones a deadline; earliest deadline drives priority |
| `GC-AUTH` | Goal Card of 10 Nov 2026, QUALITY: authorities are labels only; IE HPRA, DE BfArM, IT NSIS-Dispovigilance published starting points; FR ANSM route unverified |
| `GC-CAUS` | Goal Card of 10 Nov 2026, QUALITY: causality scale (Pilot) not related, unlikely, possible, probable, causal; ranges not clinically validated |
| `GC-DUP` | Goal Card of 10 Nov 2026, QUALITY: duplicates and follow-ups never replace the first awareness time |
| `GC-GBNI` | Goal Card of 10 Nov 2026, QUALITY: GB and NI handled separately; overlapping rule sets show each deadline, earliest drives priority |
| `GC-UNK` | Goal Card of 10 Nov 2026, QUALITY: unknown stays unknown; empty value means unknown; unreadable or unmatched input made visible |
| `GC-INJ` | Goal Card of 10 Nov 2026, DONE WHEN check 11: prompt-injection letter gets the same labels as its clean copy and raises a flag |
