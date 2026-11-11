#!/usr/bin/env python3
"""Secret scanner for the repository (Python standard library only).

Finds keys, tokens, connection strings and private keys in text files. Exit code 1 when anything is
found, 0 when clean, 2 on usage errors. Matches are printed redacted, never in full.

Only obvious placeholders are allowed (for example '<your-key>', '${VAR}', 'example', 'changeme',
an all-zero GUID); there are no inline suppressions.

Usage:
    python scripts/secret_scan.py [PATH ...]      scan the repository (default) or the given paths
    python scripts/secret_scan.py --self-test     check the detectors with generated samples
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_DIRS = {
    ".git", ".azure", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", "var", "outbox", "artifacts", "playwright-report", "test-results", "dist", "build",
}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tgz", ".whl", ".pyc", ".woff", ".woff2",
    ".ttf", ".eot", ".mp4", ".docx", ".xlsx", ".pptx", ".sqlite", ".sqlite3", ".db",
}
MAX_BYTES = 5 * 1024 * 1024

# Obvious placeholders only (case-insensitive substrings of the matched value).
PLACEHOLDER_MARKERS = (
    "<", ">", "${", "$(", "{{", "example", "placeholder", "changeme", "change-me", "dummy", "fake", "redacted",
    "xxxxxxxx", "********", "your-", "your_", "not-a-real", "00000000-0000-0000-0000-000000000000",
)

_BEGIN = "-----BEGIN"
_END_DASHES = "-----"


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    group: int = 0
    entropy_min: float = 0.0
    needs_digit_and_letter: bool = False


RULES = (
    Rule("private-key", re.compile(_BEGIN + r"[ A-Z0-9]*PRIVATE KEY(?: BLOCK)?" + _END_DASHES)),
    Rule("azure-storage-connection-string",
         re.compile(r"DefaultEndpointsProtocol=https?;AccountName=[^;\s]+;AccountKey=([A-Za-z0-9+/]{20,}={0,2})"), 1),
    Rule("account-key", re.compile(r"\bAccountKey\s*=\s*([A-Za-z0-9+/]{40,}={0,2})"), 1),
    Rule("shared-access-key", re.compile(r"\bSharedAccessKey\s*=\s*([A-Za-z0-9+/]{20,}={0,2})"), 1),
    Rule("communication-services-connection-string",
         re.compile(r"endpoint=https://[^;\s]+;accesskey=([A-Za-z0-9+/]{20,}={0,2})", re.IGNORECASE), 1),
    Rule("sas-token", re.compile(r"\bsv=\d{4}-\d{2}-\d{2}&[^\s'\"]*?\bsig=([A-Za-z0-9%+/=]{20,})"), 1),
    Rule("instrumentation-key",
         re.compile(r"InstrumentationKey=([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})"), 1),
    Rule("github-token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b"), 1),
    Rule("aws-access-key-id", re.compile(r"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b"), 1),
    Rule("aws-secret-access-key",
         re.compile(r"aws_secret_access_key\s*[=:]\s*['\"]?([A-Za-z0-9/+]{40})", re.IGNORECASE), 1),
    Rule("entra-client-secret",
         re.compile(r"(?<![A-Za-z0-9_~.\-])([A-Za-z0-9_~.\-]{3}\dQ~[A-Za-z0-9_~.\-]{31,34})(?![A-Za-z0-9_~.\-])"), 1),
    Rule("slack-token", re.compile(r"\b(xox[abposr]-[A-Za-z0-9-]{10,})\b"), 1),
    Rule("google-api-key", re.compile(r"\b(AIza[0-9A-Za-z_\-]{35})\b"), 1),
    Rule("openai-key", re.compile(r"\b(sk-(?:proj-)?[A-Za-z0-9_\-]{20,}T3BlbkFJ[A-Za-z0-9_\-]{20,})\b"), 1),
    Rule("jwt", re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"), 1),
    Rule("password-in-url", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@'\"]+:([^/\s:@'\"]{3,})@[^\s/'\"]+"), 1),
    Rule("generic-secret-assignment",
         re.compile(r"(?i)\b(?:password|passwd|pwd|secret|client_secret|clientsecret|api_key|apikey|access_key|"
                    r"accesskey|account_key|auth_token|access_token|private_key)\b"
                    r"['\"]?\s*[:=]\s*['\"]([^'\"\s]{16,})['\"]"),
         1, entropy_min=3.5, needs_digit_and_letter=True),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    preview: str


def _entropy(value: str) -> float:
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    return -sum((n / len(value)) * math.log2(n / len(value)) for n in counts.values())


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _redact(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-2:]} ({len(value)} chars)"


def scan_text(text: str, path: str = "<text>") -> list[Finding]:
    findings: list[Finding] = []
    for rule in RULES:
        for match in rule.pattern.finditer(text):
            value = match.group(rule.group)
            if _is_placeholder(value) or _is_placeholder(match.group(0)):
                continue
            if rule.needs_digit_and_letter and not (re.search(r"\d", value) and re.search(r"[A-Za-z]", value)):
                continue
            if rule.entropy_min and _entropy(value) < rule.entropy_min:
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(Finding(path, line, rule.name, _redact(value)))
    return findings


def _iter_files(paths: list[Path]):
    for base in paths:
        if base.is_file():
            yield base
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS)
            for name in sorted(files):
                yield Path(root) / name


def scan_paths(paths: list[Path]) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    scanned = 0
    for file_path in _iter_files(paths):
        if file_path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            if file_path.stat().st_size > MAX_BYTES:
                continue
            raw = file_path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        scanned += 1
        text = raw.decode("utf-8", errors="replace")
        try:
            shown = str(file_path.relative_to(REPO_ROOT))
        except ValueError:
            shown = str(file_path)
        findings.extend(scan_text(text, shown))
    return findings, scanned


def self_test() -> int:
    """Generated samples (built at run time so this file holds no secret-like literal)."""
    b64 = "Qk3" + "x9" * 30 + "Zq8+"
    samples = {
        "private-key": _BEGIN + " RSA PRIVATE KEY" + _END_DASHES + "\nMIIB\n",
        "azure-storage-connection-string": "DefaultEndpointsProtocol=https;AccountName=demo;AccountKey=" + b64 + "==",
        "account-key": "AccountKey=" + b64 + "==",
        "communication-services-connection-string": "endpoint=https://acs.communication.azure.com/;accesskey=" + b64,
        "sas-token": "https://a.blob.core.windows.net/c?sv=2024-01-01&ss=b&srt=o&sp=r&sig=" + "Ab3dE" * 8,
        "instrumentation-key": "InstrumentationKey=" + "1b2c3d4e-5f60-4718-9a0b-1c2d3e4f5a6b",
        "github-token": "ghp" + "_" + "A1b2C3d4E5" * 4,
        "aws-access-key-id": "AKI" + "A" + "Q2W3E4R5T6Y7U8I9",
        "entra-client-secret": "aB3" + "8Q~" + "Zx9yW8vU7tS6rQ5pO4nM3lK2jI1hG0fE",
        "jwt": "eyJ" + "hbGciOiJIUzI1" + ".eyJ" + "zdWIiOiIxMjM0" + "." + "SflKxwRJSMeKKF2QT4",
        "password-in-url": "postgres://admin:" + "S3cr" + "etPw9@db.internal/x",
        "generic-secret-assignment": "client_secret = '" + "Q7v" + "Lr2x9Kp4Zt8Wm3Yd" + "'",
    }
    allowed = [
        "AccountKey=<your-storage-key>",
        "password = '${DB_PASSWORD_FROM_KEY_VAULT}'",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000",
        "client_secret = 'example-client-secret-123'",
        "token = 'X-MS-CLIENT-PRINCIPAL-HEADER-NAME'",
        "https://login.microsoftonline.com/tenant/v2.0",
    ]
    failures = []
    for rule_name, sample in samples.items():
        names = {finding.rule for finding in scan_text(sample)}
        if rule_name not in names:
            failures.append(f"not detected: {rule_name}")
    for sample in allowed:
        hits = scan_text(sample)
        if hits:
            failures.append(f"false positive ({hits[0].rule}) on an allowed placeholder sample")
    for failure in failures:
        print(f"secret_scan self-test: {failure}", file=sys.stderr)
    print(f"secret_scan self-test: {len(samples)} detectors, {len(allowed)} placeholder samples, "
          f"{len(failures)} failure(s)")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan the repository for secrets (stdlib only).")
    parser.add_argument("paths", nargs="*", help="files or folders to scan (default: repository root)")
    parser.add_argument("--self-test", action="store_true", help="check the detectors with generated samples")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    paths = [Path(p).resolve() for p in args.paths] or [REPO_ROOT]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print(f"secret_scan: path not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    findings, scanned = scan_paths(paths)
    for finding in findings:
        print(f"{finding.path}:{finding.line}: [{finding.rule}] {finding.preview}")
    print(f"secret_scan: {scanned} files scanned, {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
