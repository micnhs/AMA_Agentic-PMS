"""Build the Microsoft Graph PATCH body that pre-authorises Azure CLI for the demo API (stdlib only).

Reads the application's 'api' object as JSON on stdin (output of
'az ad app show --id <client-id> --query api -o json') and prints {"api": {...}} with Azure CLI
added to preAuthorizedApplications for every enabled delegated scope. Prints nothing when Azure CLI
is already pre-authorised. Exit code 3 when the application exposes no delegated scope.

Pre-authorisation lets 'az account get-access-token --scope <api>/user_impersonation' return a
token for scripts/seed.* and scripts/smoke-test.* without a consent prompt. It grants no role:
the user still needs a PMS app role assignment.
"""

import json
import sys

AZURE_CLI_APP_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"


def build_patch(api: dict) -> dict | None:
    scopes = api.get("oauth2PermissionScopes") or []
    scope_ids = sorted(s["id"] for s in scopes if s.get("id") and s.get("isEnabled", True))
    if not scope_ids:
        raise ValueError("the application exposes no enabled delegated scope")
    pre_authorized = api.get("preAuthorizedApplications") or []
    for entry in pre_authorized:
        if entry.get("appId") == AZURE_CLI_APP_ID and set(scope_ids) <= set(entry.get("delegatedPermissionIds") or []):
            return None
    others = [entry for entry in pre_authorized if entry.get("appId") != AZURE_CLI_APP_ID]
    others.append({"appId": AZURE_CLI_APP_ID, "delegatedPermissionIds": scope_ids})
    patched = dict(api)
    patched["preAuthorizedApplications"] = others
    return {"api": patched}


def main() -> int:
    raw = sys.stdin.read().strip()
    api = json.loads(raw) if raw else {}
    try:
        body = build_patch(api or {})
    except ValueError as exc:
        print(f"entra_api_patch: {exc}", file=sys.stderr)
        return 3
    if body is not None:
        print(json.dumps(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
