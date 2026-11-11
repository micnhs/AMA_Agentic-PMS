"""The demo's Microsoft Entra app registration outside Bicep (stdlib only).

Called by scripts/preflight.*, scripts/create-app-registration.* and scripts/postprovision.*:

  probe     Proves that the signed-in user may create what infra/modules/entra-app.bicep creates: a
            test app registration and its enterprise application (service principal). Both are
            deleted right away (also from the Entra recycle bin where allowed). The tenant setting
            "Users can register applications" alone does not prove it (guest accounts, tenant policies).
  create    Bring your own app registration (README): creates the demo's registration with the
            settings of entra-app.bicep - five app roles, the scope user_impersonation with Azure CLI
            pre-authorised, ID tokens, access tokens v2, enterprise application with "assignment
            required" - and assigns app roles to a user. With --client-id it completes an existing
            registration instead of creating a second one.
  complete  After provisioning (postprovision hook, best effort): adds the Web redirect URI, ID tokens
            and the Azure CLI pre-authorisation to a bring-your-own registration when the signed-in
            user may change it; otherwise prints what an admin has to set.

Token: PMS_GRAPH_TOKEN (set by the calling script; never printed), else 'az account get-access-token'.
Exit codes: 0 done, 1 error, 2 Microsoft Graph refused the operation for the signed-in account.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from entra_api_patch import AZURE_CLI_APP_ID, build_patch

GRAPH = "https://graph.microsoft.com/v1.0"
MICROSOFT_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
GRAPH_USER_READ_SCOPE_ID = "e1fe6dd8-ba31-4d61-89e7-88639da4683d"
SCOPE_VALUE = "user_impersonation"
PROBE_PREFIX = "pms-preflight-probe-"

# The same five app roles as infra/modules/entra-app.bicep (tests compare both).
APP_ROLES = (
    ("PMS.Viewer", "PMS Viewer", "View cases, signals, reports, cost and governance views (read only)."),
    ("PMS.Analyst", "PMS Analyst",
     "Upload letters, add signals, edit metadata with a reason, take triage, raise urgency."),
    ("PMS.Regulatory", "PMS Regulatory",
     "Record a submission made outside the system, for the approved version only."),
    ("PMS.MedicalReviewer", "PMS Medical Reviewer",
     "The only role that approves, rejects, requests information, closes, closes as non-reportable or lowers "
     "urgency, always with a reason."),
    ("PMS.Operator", "PMS Operator", "Load demo data, start a new demo run, read operational views and traces."),
)
ROLE_VALUES = tuple(value for value, _, _ in APP_ROLES)

# Stable IDs, so that running 'create' again keeps the same roles and scope.
_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:pms-agent-demo:entra-app")
SCOPE_ID = str(uuid.uuid5(_ID_NAMESPACE, "scope:" + SCOPE_VALUE))


def role_id(value: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, "app-role:" + value))


# ---------------------------------------------------------------------------------------------------
# Output in the format of the other scripts
# ---------------------------------------------------------------------------------------------------

def info(message: str) -> None:
    print(f"[pms] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[pms] WARNING: {message}", file=sys.stderr, flush=True)


def failed(problem: str, fix: str) -> None:
    print(f"[pms] FAILED: {problem} Fix: {fix}", file=sys.stderr, flush=True)


def script(shell: str, name: str, ps_args: str = "", sh_args: str = "") -> str:
    """The command line of a repository script in the caller's shell."""
    if shell == "ps1":
        return f"./scripts/{name}.ps1{' ' + ps_args if ps_args else ''}"
    return f"bash scripts/{name}.sh{' ' + sh_args if sh_args else ''}"


# ---------------------------------------------------------------------------------------------------
# Microsoft Graph
# ---------------------------------------------------------------------------------------------------

class GraphError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    def describe(self) -> str:
        text = " ".join(f"{self.code}: {self.message}".split())
        return f"HTTP {self.status} {text}" if self.status else text

    @property
    def refused(self) -> bool:
        """Graph understood the request and refused it for this account (as opposed to sign-in or network)."""
        return 400 <= self.status < 500 and self.status not in (401, 404, 408, 429)


class Graph:
    def __init__(self, token: str, base: str = GRAPH, opener=urllib.request.urlopen) -> None:
        self._token = token
        self._base = base
        self._opener = opener

    def request(self, method: str, path: str, body: dict | None = None) -> dict | None:
        url = path if path.startswith("https://") else self._base + path
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self._token)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener(req, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise _graph_error(exc.code, exc.read()) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GraphError(0, "NetworkError", str(getattr(exc, "reason", exc))) from None
        return json.loads(raw) if raw else None


def _graph_error(status: int, raw: bytes) -> GraphError:
    code, message = "Error", ""
    try:
        error = json.loads(raw or b"{}").get("error") or {}
        code = str(error.get("code") or code)
        message = str(error.get("message") or "")
    except ValueError:
        message = (raw or b"").decode("utf-8", "replace")[:300]
    return GraphError(status, code, message)


def graph_token() -> str:
    token = os.environ.get("PMS_GRAPH_TOKEN", "").strip()
    if token:
        return token
    az = shutil.which("az")
    if not az:
        return ""
    try:
        done = subprocess.run(
            [az, "account", "get-access-token", "--resource-type", "ms-graph", "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def tenant_of(token: str) -> str:
    """Tenant ID from the token's tid claim (read only to name the tenant in messages)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(claims.get("tid") or "")
    except (IndexError, ValueError):
        return ""


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


SLEEP = time.sleep  # replaced in tests


def _transient(exc: GraphError) -> bool:
    """Replication delay right after a create (404, 'does not reference a valid application object'),
    throttling, server errors and network errors: worth another try."""
    if exc.status in (0, 404, 408, 429) or exc.status >= 500:
        return True
    return exc.status == 400 and "valid application" in exc.message


def call(graph: "Graph", method: str, path: str, body: dict | None = None, attempts: int = 6,
         delay: float = 2.0) -> dict | None:
    """graph.request with retries for transient answers; the last error is raised."""
    for attempt in range(1, attempts + 1):
        try:
            return graph.request(method, path, body)
        except GraphError as exc:
            if attempt == attempts or not _transient(exc):
                raise
            SLEEP(delay * attempt)
    return None


# ---------------------------------------------------------------------------------------------------
# The registration
# ---------------------------------------------------------------------------------------------------

def display_name(environment: str) -> str:
    return f"PMS agent demo ({environment}, bring your own)"


def home_page(redirect_uri: str) -> str:
    suffix = "/.auth/login/aad/callback"
    return redirect_uri[: -len(suffix)] if redirect_uri.endswith(suffix) else ""


def manifest(environment: str, redirect_uri: str = "") -> dict:
    """The application object of entra-app.bicep, without the Application ID URI (set once the appId is known)."""
    web: dict = {
        "redirectUris": [redirect_uri] if redirect_uri else [],
        "implicitGrantSettings": {"enableIdTokenIssuance": True, "enableAccessTokenIssuance": False},
    }
    if home_page(redirect_uri):
        web["homePageUrl"] = home_page(redirect_uri)
    return {
        "displayName": display_name(environment),
        "signInAudience": "AzureADMyOrg",
        "notes": "PMS agent demo with synthetic data only (scripts/create-app-registration). "
                 "Delete it when the demo is removed.",
        "appRoles": [
            {
                "id": role_id(value),
                "allowedMemberTypes": ["User"],
                "displayName": name,
                "description": description,
                "value": value,
                "isEnabled": True,
            }
            for value, name, description in APP_ROLES
        ],
        "api": {
            "requestedAccessTokenVersion": 2,
            "oauth2PermissionScopes": [
                {
                    "id": SCOPE_ID,
                    "type": "User",
                    "value": SCOPE_VALUE,
                    "adminConsentDisplayName": "Access the PMS agent demo API",
                    "adminConsentDescription": "Allows the signed-in user to call the PMS agent demo API with their "
                                               "own app roles.",
                    "userConsentDisplayName": "Access the PMS agent demo API",
                    "userConsentDescription": "Allows the app to call the PMS agent demo API on your behalf with your "
                                              "app roles.",
                    "isEnabled": True,
                }
            ],
        },
        "web": web,
        "requiredResourceAccess": [
            {
                "resourceAppId": MICROSOFT_GRAPH_APP_ID,
                "resourceAccess": [{"id": GRAPH_USER_READ_SCOPE_ID, "type": "Scope"}],
            }
        ],
    }


def settings_patch(app: dict, redirect_uri: str, set_identifier_uri: bool) -> tuple[dict, list[str], list[str]]:
    """PATCH body that adds what the demo needs and keeps everything else; (body, changes, problems)."""
    body: dict = {}
    changes: list[str] = []
    problems: list[str] = []
    if set_identifier_uri and not app.get("identifierUris"):
        body["identifierUris"] = [f"api://{app['appId']}"]
        changes.append(f"Application ID URI api://{app['appId']}")
    try:
        api_patch = build_patch(dict(app.get("api") or {}))
    except ValueError:
        api_patch = None
        problems.append(f"Expose an API: add the scope {SCOPE_VALUE} and Azure CLI ({AZURE_CLI_APP_ID}) as "
                        "authorised client application")
    if api_patch:
        body["api"] = api_patch["api"]
        changes.append(f"Azure CLI pre-authorised for {SCOPE_VALUE}")
    web = app.get("web") or {}
    redirects = list(web.get("redirectUris") or [])
    implicit = dict(web.get("implicitGrantSettings") or {})
    web_changes = []
    if redirect_uri and redirect_uri not in redirects:
        redirects.append(redirect_uri)
        web_changes.append(f"Web redirect URI {redirect_uri}")
    if not implicit.get("enableIdTokenIssuance"):
        implicit["enableIdTokenIssuance"] = True
        implicit.setdefault("enableAccessTokenIssuance", False)
        web_changes.append("ID tokens enabled")
    if web_changes:
        new_web = {"redirectUris": redirects, "implicitGrantSettings": implicit}
        for key in ("homePageUrl", "logoutUrl"):
            if web.get(key):
                new_web[key] = web[key]
        if "homePageUrl" not in new_web and home_page(redirect_uri):
            new_web["homePageUrl"] = home_page(redirect_uri)
        body["web"] = new_web
        changes.extend(web_changes)
    return body, changes, problems


def manual_settings(redirect_uri: str) -> list[str]:
    return [
        f"Web redirect URI: {redirect_uri or '<WEB_REDIRECT_URI after provisioning>'}",
        "ID tokens enabled (Authentication > Implicit grant and hybrid flows), single tenant",
        "app roles " + ", ".join(ROLE_VALUES) + " (allowed member type User)",
        f"Expose an API: scope {SCOPE_VALUE}, with Azure CLI ({AZURE_CLI_APP_ID}) as authorised client",
        "access token version 2 (manifest: requestedAccessTokenVersion = 2)",
    ]


def _who(me: dict) -> str:
    user_type = me.get("userType") or "unknown user type"
    return f"{me.get('userPrincipalName') or me.get('id') or 'the signed-in account'} ({user_type})"


def _me(graph: Graph) -> dict:
    try:
        return graph.request("GET", "/me?$select=id,userPrincipalName,userType") or {}
    except GraphError:
        return {}


def _refusal_fix(shell: str, me: dict, tenant: str) -> str:
    you = me.get("userPrincipalName") or "<your account>"
    parts = []
    if (me.get("userType") or "").lower() == "guest":
        parts.append(f"you are a guest in tenant {tenant or 'of this subscription'} and guests may not register "
                     f"applications: sign in with a member account of that tenant (az login --tenant {tenant or '<id>'} "
                     f"and azd auth login --tenant-id {tenant or '<id>'}), or")
    parts.append("bring your own app registration (README): someone who may create app registrations in this tenant "
                 "runs " + script(shell, "create-app-registration", f"-AssignTo {you} -Owner {you}",
                                  f"--assign-to {you} --owner {you}")
                 + ", then azd env set ENTRA_EXISTING_APP_CLIENT_ID <client-id>; or deploy to a subscription in a "
                   "tenant where you may create app registrations.")
    return " ".join(parts)


# ---------------------------------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------------------------------

def probe(graph: Graph, tenant: str = "", shell: str = "sh", suffix: str = "") -> int:
    me = _me(graph)
    who = _who(me)
    name = PROBE_PREFIX + (suffix or secrets.token_hex(4))
    info(f"Entra: signed in as {who} in tenant {tenant or 'unknown'}; testing with a temporary app "
         f"registration {name}")
    app = service_principal = None
    step = "create an app registration"
    try:
        app = graph.request("POST", "/applications", {"displayName": name, "signInAudience": "AzureADMyOrg"})
        step = "create the enterprise application (service principal) of an app registration"
        service_principal = call(graph, "POST", "/servicePrincipals", {"appId": app["appId"]})
    except GraphError as exc:
        if exc.refused and not _transient(exc):
            failed(f"Microsoft Graph does not let {who} {step} in tenant {tenant or 'of this subscription'} "
                   f"({exc.describe()}); the deployment would stop at the Entra app registration.",
                   _refusal_fix(shell, me, tenant))
            return 2
        skip = "$env:PREFLIGHT_SKIP = 'entra-probe'" if shell == "ps1" else "export PREFLIGHT_SKIP=entra-probe"
        failed(f"Could not test your right to create Entra app registrations ({exc.describe()}).",
               f"Sign in again (az login) and retry, or skip the test and check only the tenant setting: {skip}")
        return 1
    finally:
        _delete_probe(graph, app, service_principal, name)
    info("Entra: you may create app registrations and their enterprise applications (test registration "
         "created and deleted)")
    return 0


def _delete_probe(graph: Graph, app: dict | None, service_principal: dict | None, name: str) -> None:
    """Deletes the test objects; a 404 right after creating them is replication delay, so it is retried."""
    sp_left = app_left = False
    if service_principal and service_principal.get("id"):
        try:
            call(graph, "DELETE", f"/servicePrincipals/{service_principal['id']}", attempts=3)
        except GraphError as exc:
            sp_left = exc.status != 404
    if app and app.get("id"):
        try:
            call(graph, "DELETE", f"/applications/{app['id']}", attempts=3)
        except GraphError as exc:
            app_left = exc.status != 404
        if not app_left:
            if sp_left:
                # Deleting the registration also removes its enterprise application in the home tenant.
                try:
                    sp_left = _service_principal(graph, app["appId"]) is not None
                except GraphError:
                    pass
            # Permanently, so that test registrations do not collect in the recycle bin (best effort).
            for object_id in (app["id"], (service_principal or {}).get("id")):
                if object_id:
                    try:
                        graph.request("DELETE", f"/directory/deletedItems/{object_id}")
                    except GraphError:
                        pass
    leftovers = [label for label, left in (("enterprise application", sp_left), ("app registration", app_left))
                 if left]
    if leftovers:
        client_id = app.get("appId") if app else "?"
        warn(f"Could not delete the test {' and '.join(leftovers)} {name} (client ID {client_id}); "
             "delete it in the Microsoft Entra admin center > App registrations.")


# ---------------------------------------------------------------------------------------------------
# create / complete
# ---------------------------------------------------------------------------------------------------

def _user(graph: Graph, who: str, me: dict) -> dict:
    if who in ("", "me"):
        return me
    return graph.request("GET", f"/users/{_quote(who)}?$select=id,userPrincipalName,userType") or {}


def _service_principal(graph: Graph, app_id: str) -> dict | None:
    try:
        return graph.request("GET", f"/servicePrincipals(appId='{app_id}')")
    except GraphError as exc:
        if exc.status == 404:
            return None
        raise


def _add_owner(graph: Graph, app: dict, service_principal: dict | None, owner: dict) -> None:
    """Adds an owner to the registration and its enterprise application (the deployer can then complete it)."""
    reference = {"@odata.id": f"{GRAPH}/directoryObjects/{owner['id']}"}
    targets = [("applications", app)] + ([("servicePrincipals", service_principal)] if service_principal else [])
    for kind, target in targets:
        try:
            graph.request("POST", f"/{kind}/{target['id']}/owners/$ref", reference)
        except GraphError as exc:
            if "already exist" not in exc.message:
                warn(f"Could not add {owner.get('userPrincipalName')} as owner ({kind}: {exc.describe()}). Fix: "
                     "Microsoft Entra admin center > App registrations / Enterprise applications > the app > Owners.")
                return
    info(f"Owner of the registration and its enterprise application: {owner.get('userPrincipalName')}")


def assign_roles(graph: Graph, app: dict, service_principal: dict, user: dict, roles: list[str]) -> list[str]:
    """Assigns the app roles to the user; returns the roles that could not be assigned."""
    ids = {role.get("value"): role.get("id") for role in app.get("appRoles") or [] if role.get("isEnabled", True)}
    try:
        current = graph.request("GET", f"/servicePrincipals/{service_principal['id']}/appRoleAssignedTo") or {}
    except GraphError:
        current = {}
    have = {(item.get("principalId"), item.get("appRoleId")) for item in current.get("value") or []}
    missing = []
    for value in roles:
        if value not in ids:
            warn(f"App role {value} does not exist on the registration.")
            missing.append(value)
            continue
        if (user["id"], ids[value]) in have:
            info(f"{user.get('userPrincipalName')} already has the app role {value}.")
            continue
        body = {"principalId": user["id"], "resourceId": service_principal["id"], "appRoleId": ids[value]}
        try:
            graph.request("POST", f"/servicePrincipals/{service_principal['id']}/appRoleAssignedTo", body)
            info(f"Assigned the app role {value} to {user.get('userPrincipalName')}.")
        except GraphError as exc:
            warn(f"Could not assign {value} to {user.get('userPrincipalName')} ({exc.describe()}). Fix: Microsoft "
                 f"Entra admin center > Enterprise applications > {app.get('displayName')} > Users and groups.")
            missing.append(value)
    return missing


def create(graph: Graph, *, environment: str, client_id: str = "", redirect_uri: str = "",
           roles: list[str] | None = None, assign_to: str = "me", owners: list[str] | None = None,
           tenant: str = "", shell: str = "sh", result_path: str = "") -> int:
    me = _me(graph)
    who = _who(me)
    created = False
    if client_id:
        try:
            app = graph.request("GET", f"/applications(appId='{_quote(client_id)}')") or {}
        except GraphError as exc:
            failed(f"Could not read the app registration {client_id} ({exc.describe()}).",
                   "Check ENTRA_EXISTING_APP_CLIENT_ID, or remove it (azd env set ENTRA_EXISTING_APP_CLIENT_ID \"\") "
                   "to create a new registration.")
            return 1
        info(f"Completing the app registration {app.get('displayName')} ({client_id}).")
    else:
        try:
            app = graph.request("POST", "/applications", manifest(environment, redirect_uri)) or {}
        except GraphError as exc:
            if exc.refused:
                failed(f"Microsoft Graph does not let {who} create the app registration in tenant "
                       f"{tenant or 'of this subscription'} ({exc.describe()}).", _refusal_fix(shell, me, tenant))
                return 2
            failed(f"Could not create the app registration ({exc.describe()}).", "Sign in again (az login) and retry.")
            return 1
        created = True
        info(f"Created the app registration {app.get('displayName')} (client ID {app.get('appId')}).")

    body, changes, problems = settings_patch(app, redirect_uri, set_identifier_uri=True)
    if body:
        try:
            call(graph, "PATCH", f"/applications/{app['id']}", body)
            for change in changes:
                info(f"Set: {change}")
            if "identifierUris" in body:
                app["identifierUris"] = body["identifierUris"]
        except GraphError as exc:
            problems.extend(changes)
            warn(f"Could not update the registration ({exc.describe()}).")
    try:
        service_principal = _service_principal(graph, app["appId"])
        if service_principal is None:
            try:
                service_principal = call(
                    graph, "POST", "/servicePrincipals", {"appId": app["appId"], "appRoleAssignmentRequired": True})
            except GraphError:
                # A retried create may have succeeded the first time; use it if it is there.
                service_principal = _service_principal(graph, app["appId"])
                if service_principal is None:
                    raise
            info("Created the enterprise application (assignment required: users without an app role cannot "
                 "sign in).")
        elif not service_principal.get("appRoleAssignmentRequired"):
            warn("The enterprise application does not require an assignment; recommended: Enterprise applications "
                 "> the app > Properties > Assignment required = Yes.")
    except GraphError as exc:
        refused = exc.refused and not _transient(exc)
        failed(f"Could not create the enterprise application of {app.get('appId')} ({exc.describe()}).",
               _refusal_fix(shell, me, tenant) if refused else
               "Run the script again; it completes the same registration (az login first if the sign-in expired).")
        return 2 if refused else 1

    for owner_name in owners or []:
        try:
            owner = _user(graph, owner_name, me)
        except GraphError as exc:
            warn(f"Owner {owner_name} not found ({exc.describe()}).")
            continue
        _add_owner(graph, app, service_principal, owner)

    if roles and assign_to != "none":
        try:
            user = _user(graph, assign_to, me)
        except GraphError as exc:
            user = {}
            warn(f"User {assign_to} not found ({exc.describe()}); no app roles assigned.")
        if user.get("id"):
            assign_roles(graph, app, service_principal, user, roles)

    for problem in problems:
        warn(f"Still to set on the registration (needs its owner or an Entra admin): {problem}")
    identifier_uri = (app.get("identifierUris") or [f"api://{app['appId']}"])[0]
    result = {
        "clientId": app["appId"],
        "objectId": app.get("id"),
        "servicePrincipalId": service_principal.get("id"),
        "identifierUri": identifier_uri,
        "created": created,
        "tenantId": tenant,
    }
    if result_path:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle)
    info(f"Client ID: {app['appId']} (Application ID URI {identifier_uri})")
    return 0


def complete(graph: Graph, *, client_id: str, redirect_uri: str = "") -> int:
    """Best effort after provisioning; always returns 0 (the hook must not fail the deployment)."""
    try:
        app = graph.request("GET", f"/applications(appId='{_quote(client_id)}')") or {}
    except GraphError as exc:
        info(f"Bring-your-own app registration {client_id} could not be read ({exc.describe()}); make sure it has:")
        for line in manual_settings(redirect_uri):
            info(f"  - {line}")
        return 0
    body, changes, problems = settings_patch(app, redirect_uri, set_identifier_uri=False)
    if body:
        try:
            graph.request("PATCH", f"/applications/{app['id']}", body)
            for change in changes:
                info(f"Bring-your-own app registration {client_id}: set {change}")
        except GraphError as exc:
            info(f"Bring-your-own app registration {client_id}: you may not change it ({exc.describe()}). "
                 "Ask its owner or an Entra admin to set:")
            for change in changes:
                info(f"  - {change}")
    elif not problems:
        info(f"Bring-your-own app registration {client_id}: redirect URI, ID tokens and Azure CLI "
             "pre-authorisation are in place.")
    present = {role.get("value") for role in app.get("appRoles") or []}
    absent = [value for value in ROLE_VALUES if value not in present]
    if absent:
        problems.append("app roles " + ", ".join(absent) + " (allowed member type User)")
    for problem in problems:
        info(f"  - still to set by its owner or an Entra admin: {problem}")
    return 0


# ---------------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("probe", "create", "complete"):
        command = sub.add_parser(name)
        command.add_argument("--shell", choices=("ps1", "sh"), default="sh",
                             help="wording of the fix commands in messages")
        if name in ("create", "complete"):
            command.add_argument("--client-id", default="")
            command.add_argument("--redirect-uri", default="")
        if name == "create":
            command.add_argument("--environment", required=True)
            command.add_argument("--roles", default="PMS.Operator", help="comma-separated app role values")
            command.add_argument("--assign-to", default="me", help="UPN or object ID; 'me' (default) or 'none'")
            command.add_argument("--owner", action="append", default=[], help="UPN or object ID of an extra owner")
            command.add_argument("--result", default="", help="write the client ID and more as JSON to this file")
    args = parser.parse_args(argv)

    token = graph_token()
    if not token:
        if args.command == "complete":
            info(f"Bring-your-own app registration {args.client_id}: no Microsoft Graph token; make sure it has:")
            for line in manual_settings(args.redirect_uri):
                info(f"  - {line}")
            return 0
        failed("Could not get a Microsoft Graph token from the Azure CLI.", "Run: az login")
        return 1
    graph = Graph(token)
    tenant = tenant_of(token)
    if args.command == "probe":
        return probe(graph, tenant=tenant, shell=args.shell)
    if args.command == "complete":
        return complete(graph, client_id=args.client_id, redirect_uri=args.redirect_uri)
    roles = [role for role in re.split(r"[,;\s]+", args.roles) if role]
    return create(graph, environment=args.environment, client_id=args.client_id, redirect_uri=args.redirect_uri,
                  roles=roles, assign_to=args.assign_to, owners=args.owner, tenant=tenant, shell=args.shell,
                  result_path=args.result)


if __name__ == "__main__":
    sys.exit(main())
