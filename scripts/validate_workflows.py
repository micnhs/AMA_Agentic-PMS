#!/usr/bin/env python3
"""Validate the GitHub Actions workflows and azure.yaml (standard library; PyYAML optional).

Checks .github/workflows/*.yml:
  - required keys (name, on, permissions, jobs; per job runs-on and steps; per step uses or run)
  - every action pinned to a full version tag (vX.Y.Z) or a commit SHA; no pull_request_target
  - no stored secrets: '${{ secrets.* }}' is refused (only GITHUB_TOKEN is tolerated)
  - azure/login only with OIDC ids from repository variables (vars.*), id-token: write, manual trigger
  - ci.yml runs lint, tests, Playwright, Bicep build, PSRule and the secret scan; every PSRule call with
    -Module also passes -Path ./.ps-rule/ (otherwise the suppression groups are ignored)
Checks azure.yaml: services web and worker on Container Apps from the repo Dockerfile, preflight as
preprovision hook (posix and windows), referenced scripts exist. Also parses the JSON files in infra/.

Exit code 0 = valid, 1 = errors found, 2 = usage error.
Usage: python scripts/validate_workflows.py [--root PATH] [--no-pyyaml]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PINNED_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@(?:v\d+\.\d+\.\d+|[0-9a-f]{40})$")
SECRET_REFERENCE = re.compile(r"\$\{\{[^}]*\bsecrets\.([A-Za-z_][A-Za-z0-9_]*)")
VARS_EXPRESSION = re.compile(r"^\$\{\{\s*vars\.[A-Z][A-Z0-9_]*\s*\}\}$")
PSRULE_COMMAND = re.compile(r"\b(?:Assert|Invoke)-PSRule\b")
PSRULE_PATH = re.compile(r"\s-Path\s+(?:\./)?\.ps-rule/?(?=\s|$)")
REQUIRED_CI_STEPS = {
    "ruff lint": re.compile(r"\bruff check\b"),
    "unit/golden/authz/safety/ui tests": re.compile(r"unittest discover"),
    "golden run": re.compile(r"-m pms golden\b"),
    "Playwright UI tests": re.compile(r"playwright"),
    "Bicep build": re.compile(r"az bicep build"),
    "PSRule for Azure": re.compile(r"Assert-PSRule"),
    "secret scan": re.compile(r"secret_scan\.py"),
    "workflow validation": re.compile(r"validate_workflows\.py"),
}


# --------------------------------------------------------------------------------------------
# Minimal YAML subset parser (used when PyYAML is not installed). Supports what the repository
# uses: block mappings and sequences, plain/quoted scalars, block scalars (| >), flow sequences and
# simple flow mappings, comments.
# --------------------------------------------------------------------------------------------

class YamlSubsetError(ValueError):
    pass


def _strip_comment(text: str) -> str:
    quote = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
    return text.rstrip()


def _split_key(content: str) -> tuple[str, str] | None:
    quote = None
    for index, char in enumerate(content):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"" and index == 0:
            quote = char
            continue
        if char == ":" and (index + 1 == len(content) or content[index + 1] in " \t"):
            key = content[:index].strip()
            if len(key) >= 2 and key[0] == key[-1] and key[0] in "'\"":
                key = key[1:-1]
            return key, content[index + 1:].strip()
    return None


def _split_flow(text: str) -> list[str]:
    items, depth, quote, current = [], 0, None, ""
    for char in text:
        if quote:
            current += char
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "," and depth == 0:
            items.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        items.append(current.strip())
    return items


def _scalar(text: str):
    text = text.strip()
    if text == "" or text in ("~", "null", "Null", "NULL"):
        return None
    if text[0] == '"' and text.endswith('"') and len(text) >= 2:
        return bytes(text[1:-1], "utf-8").decode("unicode_escape")
    if text[0] == "'" and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(item) for item in _split_flow(text[1:-1])]
    if text.startswith("{") and text.endswith("}"):
        result = {}
        for item in _split_flow(text[1:-1]):
            pair = _split_key(item)
            if pair is None:
                raise YamlSubsetError(f"unsupported flow mapping item: {item}")
            result[pair[0]] = _scalar(pair[1])
        return result
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    return text


class MiniYaml:
    def __init__(self, text: str):
        self.lines = text.replace("\t", "    ").splitlines()
        self.index = 0

    @staticmethod
    def _indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def _skip_blank(self) -> None:
        while self.index < len(self.lines):
            stripped = self.lines[self.index].strip()
            marker = stripped in ("---", "...") and self._indent(self.lines[self.index]) == 0
            if stripped == "" or stripped.startswith("#") or marker:
                self.index += 1
                continue
            break

    def parse(self):
        self._skip_blank()
        if self.index >= len(self.lines):
            return None
        return self._block(self._indent(self.lines[self.index]))

    def _block(self, indent: int):
        self._skip_blank()
        stripped = self.lines[self.index].strip()
        if stripped == "-" or stripped.startswith("- "):
            return self._sequence(indent)
        return self._mapping(indent)

    def _value(self, rest: str, indent: int):
        rest = _strip_comment(rest)
        if rest[:1] in ("|", ">") and re.fullmatch(r"[|>][+-]?", rest):
            return self._block_scalar(rest, indent)
        if rest:
            return _scalar(rest)
        self._skip_blank()
        if self.index >= len(self.lines):
            return None
        line = self.lines[self.index]
        child_indent = self._indent(line)
        if child_indent > indent:
            return self._block(child_indent)
        if child_indent == indent and (line.strip() == "-" or line.strip().startswith("- ")):
            return self._sequence(indent)
        return None

    def _mapping(self, indent: int) -> dict:
        result: dict = {}
        while True:
            self._skip_blank()
            if self.index >= len(self.lines):
                return result
            line = self.lines[self.index]
            current = self._indent(line)
            if current < indent:
                return result
            if current > indent:
                raise YamlSubsetError(f"line {self.index + 1}: unexpected indentation")
            content = line.strip()
            if content == "-" or content.startswith("- "):
                return result
            pair = _split_key(_strip_comment(content))
            if pair is None:
                raise YamlSubsetError(f"line {self.index + 1}: expected 'key: value'")
            self.index += 1
            if pair[0] in result:
                raise YamlSubsetError(f"line {self.index}: duplicate key '{pair[0]}'")
            result[pair[0]] = self._value(pair[1], indent)

    def _sequence(self, indent: int) -> list:
        items = []
        while True:
            self._skip_blank()
            if self.index >= len(self.lines):
                return items
            line = self.lines[self.index]
            content = line.strip()
            if self._indent(line) != indent or not (content == "-" or content.startswith("- ")):
                return items
            after = content[1:].lstrip()
            item_indent = indent + (len(content) - len(after))
            if after == "":
                self.index += 1
                items.append(self._value("", indent))
            elif _split_key(_strip_comment(after)) is not None and not after.startswith(("'", '"', "[", "{")):
                self.lines[self.index] = " " * item_indent + after
                items.append(self._mapping(item_indent))
            else:
                self.index += 1
                items.append(self._value(after, indent))

    def _block_scalar(self, indicator: str, indent: int) -> str:
        collected = []
        block_indent = None
        while self.index < len(self.lines):
            line = self.lines[self.index]
            if line.strip() == "":
                collected.append("")
                self.index += 1
                continue
            current = self._indent(line)
            if current <= indent:
                break
            if block_indent is None:
                block_indent = current
            collected.append(line[block_indent:])
            self.index += 1
        while collected and collected[-1] == "":
            collected.pop()
        text = "\n".join(collected) if indicator.startswith("|") else " ".join(part.strip() for part in collected)
        return text if indicator.endswith("-") else text + "\n"


def load_yaml(path: Path, use_pyyaml: bool):
    text = path.read_text(encoding="utf-8")
    if use_pyyaml:
        try:
            import yaml
        except ImportError:
            use_pyyaml = False
    if use_pyyaml:
        data = yaml.safe_load(text)
        if isinstance(data, dict) and True in data and "on" not in data:
            data["on"] = data.pop(True)  # YAML 1.1 reads the key 'on' as boolean true
        return data
    return MiniYaml(text).parse()


# --------------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------------

def _walk_strings(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_strings(item)
    elif isinstance(node, str):
        yield node


def check_workflow(path: Path, data, errors: list[str], warnings: list[str]) -> None:
    name = path.name
    if not isinstance(data, dict):
        errors.append(f"{name}: not a YAML mapping")
        return
    for key in ("name", "on", "permissions", "jobs"):
        if key not in data:
            errors.append(f"{name}: missing top-level key '{key}'")
    triggers = data.get("on")
    if isinstance(triggers, (dict, list)):
        trigger_names = set(triggers)
    else:
        trigger_names = {triggers}
    if "pull_request_target" in trigger_names:
        errors.append(f"{name}: pull_request_target is not allowed")
    for text in _walk_strings(data):
        for match in SECRET_REFERENCE.finditer(text):
            if match.group(1) != "GITHUB_TOKEN":
                errors.append(f"{name}: stored secret referenced (secrets.{match.group(1)}); "
                              "use OIDC and repository variables")
    jobs = data.get("jobs") or {}
    if not isinstance(jobs, dict) or not jobs:
        errors.append(f"{name}: no jobs defined")
        return
    top_permissions = data.get("permissions") or {}
    uses_azure_login = False
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            errors.append(f"{name}: job '{job_name}' is not a mapping")
            continue
        if job.get("secrets") == "inherit":
            errors.append(f"{name}: job '{job_name}' inherits secrets")
        if "uses" in job:
            continue
        if "runs-on" not in job:
            errors.append(f"{name}: job '{job_name}' has no runs-on")
        if "timeout-minutes" not in job:
            warnings.append(f"{name}: job '{job_name}' has no timeout-minutes")
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append(f"{name}: job '{job_name}' has no steps")
            continue
        job_permissions = job.get("permissions") or {}
        for number, step in enumerate(steps, start=1):
            label = f"{name}: job '{job_name}' step {number}"
            if not isinstance(step, dict):
                errors.append(f"{label}: not a mapping")
                continue
            if ("uses" in step) == ("run" in step):
                errors.append(f"{label}: needs exactly one of 'uses' or 'run'")
            action = step.get("uses")
            if isinstance(action, str):
                if action.startswith("./"):
                    pass
                elif action.startswith("docker://"):
                    image = action[len("docker://"):]
                    if "@sha256:" not in image and (":" not in image or image.endswith(":latest")):
                        errors.append(f"{label}: docker image '{action}' must be pinned to a tag or digest")
                elif not PINNED_ACTION.match(action):
                    errors.append(f"{label}: action '{action}' is not pinned to a full version tag "
                                  "(vX.Y.Z) or commit SHA")
                if action.lower().startswith("azure/login@"):
                    uses_azure_login = True
                    with_block = step.get("with") or {}
                    for key in ("client-id", "tenant-id", "subscription-id"):
                        value = str(with_block.get(key, ""))
                        if not VARS_EXPRESSION.match(value):
                            errors.append(f"{label}: azure/login '{key}' must come from a repository variable (vars.*)")
                    for forbidden in ("creds", "client-secret"):
                        if forbidden in with_block:
                            errors.append(f"{label}: azure/login must not use '{forbidden}' (OIDC only)")
                    permissions = {**(top_permissions if isinstance(top_permissions, dict) else {}),
                                   **(job_permissions if isinstance(job_permissions, dict) else {})}
                    if permissions.get("id-token") != "write":
                        errors.append(f"{label}: OIDC login needs 'permissions: id-token: write'")
    if uses_azure_login and trigger_names - {"workflow_dispatch"}:
        errors.append(f"{name}: workflows that sign in to Azure must run on workflow_dispatch only")
    if name == "ci.yml":
        if not {"push", "pull_request"} <= trigger_names:
            errors.append(f"{name}: must run on push and pull_request")
        run_text = "\n".join(str(step.get("run", "")) for job in jobs.values() if isinstance(job, dict)
                             for step in (job.get("steps") or []) if isinstance(step, dict))
        for label, pattern in REQUIRED_CI_STEPS.items():
            if not pattern.search(run_text):
                errors.append(f"{name}: no step runs the {label}")
        # With -Module, PSRule does not read the working directory: without -Path the suppression
        # groups in .ps-rule/ are silently ignored (CI run #12, 11 Nov 2026).
        for line in run_text.splitlines():
            command = PSRULE_COMMAND.search(line)
            if command and "-Module" in line and not PSRULE_PATH.search(line):
                errors.append(f"{name}: {command.group(0)} with -Module must also pass -Path ./.ps-rule/ "
                              "(otherwise the suppression groups are not loaded)")
    if name == "deploy.yml":
        if trigger_names != {"workflow_dispatch"}:
            errors.append(f"{name}: must be triggered by workflow_dispatch only")
        if not uses_azure_login:
            errors.append(f"{name}: must sign in with azure/login (OIDC)")


def check_azure_yaml(root: Path, data, errors: list[str]) -> None:
    name = "azure.yaml"
    if not isinstance(data, dict):
        errors.append(f"{name}: not a YAML mapping")
        return
    if not data.get("name"):
        errors.append(f"{name}: missing 'name'")
    infra = data.get("infra") or {}
    if infra.get("provider", "bicep") != "bicep" or infra.get("path", "infra") != "infra":
        errors.append(f"{name}: infra must be Bicep in ./infra")
    if not (root / "infra" / f"{infra.get('module', 'main')}.bicep").is_file():
        errors.append(f"{name}: infra module file not found")
    services = data.get("services") or {}
    for service in ("web", "worker"):
        definition = services.get(service)
        if not isinstance(definition, dict):
            errors.append(f"{name}: service '{service}' missing")
            continue
        if definition.get("host") != "containerapp":
            errors.append(f"{name}: service '{service}' must use host: containerapp")
        docker = definition.get("docker") or {}
        dockerfile = root / str(definition.get("project", ".")) / str(docker.get("path", "Dockerfile"))
        if not dockerfile.is_file():
            errors.append(f"{name}: service '{service}' Dockerfile not found ({dockerfile.relative_to(root)})")
    hooks = data.get("hooks") or {}
    preprovision = hooks.get("preprovision") or {}
    for platform in ("posix", "windows"):
        run = str((preprovision.get(platform) or {}).get("run", ""))
        if "preflight" not in run:
            errors.append(f"{name}: preprovision hook for {platform} must run the preflight script")
    for hook_name, hook in hooks.items():
        for platform, definition in (hook or {}).items():
            run = str((definition or {}).get("run", ""))
            for script in re.findall(r"scripts/[\w./-]+\.(?:sh|ps1)", run):
                if not (root / script).is_file():
                    errors.append(f"{name}: hook {hook_name}/{platform} references missing {script}")
    for text in _walk_strings(data):
        if SECRET_REFERENCE.search(text):
            errors.append(f"{name}: secret reference found")


def check_infra_json(root: Path, errors: list[str]) -> int:
    count = 0
    for path in sorted((root / "infra").rglob("*.json")):
        count += 1
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{path.relative_to(root)}: invalid JSON ({exc})")
    parameters = root / "infra" / "main.parameters.json"
    if parameters.is_file():
        values = json.loads(parameters.read_text(encoding="utf-8")).get("parameters", {})
        for key, entry in values.items():
            value = entry.get("value") if isinstance(entry, dict) else None
            if not (isinstance(value, str) and value.startswith("${") and value.endswith("}")):
                errors.append(f"infra/main.parameters.json: parameter '{key}' must map an azd environment value")
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate GitHub workflows and azure.yaml.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="repository root")
    parser.add_argument("--no-pyyaml", action="store_true", help="use the built-in YAML subset parser")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    workflows = sorted((root / ".github" / "workflows").glob("*.y*ml"))
    if not workflows:
        print("validate_workflows: no workflows found in .github/workflows", file=sys.stderr)
        return 2
    errors: list[str] = []
    warnings: list[str] = []
    for path in workflows:
        try:
            data = load_yaml(path, use_pyyaml=not args.no_pyyaml)
        except Exception as exc:  # report any parser error as a validation error
            errors.append(f"{path.name}: cannot parse YAML ({exc})")
            continue
        check_workflow(path, data, errors, warnings)
    azure_yaml = root / "azure.yaml"
    if azure_yaml.is_file():
        try:
            check_azure_yaml(root, load_yaml(azure_yaml, use_pyyaml=not args.no_pyyaml), errors)
        except Exception as exc:  # report any parser error as a validation error
            errors.append(f"azure.yaml: cannot parse YAML ({exc})")
    else:
        errors.append("azure.yaml: not found")
    json_files = check_infra_json(root, errors)
    for warning in warnings:
        print(f"WARNING {warning}")
    for error in errors:
        print(f"ERROR {error}")
    print(f"validate_workflows: {len(workflows)} workflow(s), azure.yaml, {json_files} infra JSON file(s): "
          f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
