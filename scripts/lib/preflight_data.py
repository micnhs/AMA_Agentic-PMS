"""Print infrastructure data for scripts/preflight.sh and scripts/preflight.ps1 (stdlib only).

Usage:
    python scripts/lib/preflight_data.py models       -> role, model, version, deployment (tab separated)
    python scripts/lib/preflight_data.py definitions  -> kind, id, expected name (tab separated)

Sources: infra/models.json and infra/roles.json, the same files the Bicep templates load.
"""

import json
import pathlib
import sys

INFRA = pathlib.Path(__file__).resolve().parents[2] / "infra"


def _load(name: str) -> dict:
    return json.loads((INFRA / name).read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("models", "definitions"):
        print(__doc__, file=sys.stderr)
        return 2
    if argv[1] == "models":
        data = _load("models.json")
        for role in ("analysis", "general"):
            model = data[role]
            print("\t".join((role, model["modelName"], model["modelVersion"], model["deploymentName"])))
        return 0
    data = _load("roles.json")
    for item in data["azureRbac"].values():
        print("\t".join(("role", item["id"], item["name"])))
    for item in data["policyDefinitions"].values():
        print("\t".join(("policy", item["id"], item["name"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
