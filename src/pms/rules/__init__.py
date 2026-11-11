"""Fixed rule code ("Regelpaket 1.0", concept section 6.3) - deterministic, no AI.

* :mod:`.extract` - text extraction helpers (identifiers, dates, places, markers)
* :mod:`.injection` - INJ-1.0 prompt-injection detection
* :mod:`.crit` - CRIT-1.0 critical detection and the raise-only urgency state
* :mod:`.linking` - LINK-1.0 duplicates and follow-ups
* :mod:`.registry` - REG-1.0 registry check
* :mod:`.rulesets` - RS-1.0 rule set and authority label
* :mod:`.deadlines` - DL-1.0 reporting deadlines (Europe/Dublin calendar)
* :mod:`.correlation` - CORR-1.0 deterministic correlation
* :mod:`.engine` - runs the rules for one signal in the order of concept 6.4

Rules never read ``expected.json`` and contain no special case for a letter id or file name.
"""
