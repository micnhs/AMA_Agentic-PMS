"""scripts/lib/entra_app.py: the app registration outside Bicep (Gate C finding, cycle 15).

The first live deployment stopped at the Entra app registration: Microsoft Graph answered
"Authorization_RequestDenied: Insufficient privileges" although the preflight had passed (it read only
the tenant setting). These tests pin the probe that now proves the right before provisioning, the
bring-your-own registration with the settings of infra/modules/entra-app.bicep, and the wiring in the
scripts. Microsoft Graph is replaced by a fake; nothing here calls Azure.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS / "lib"))

import entra_app  # noqa: E402
from entra_api_patch import AZURE_CLI_APP_ID  # noqa: E402

APP = {"id": "obj-app-1", "appId": "11111111-2222-3333-4444-555555555555"}
SP = {"id": "obj-sp-1", "appId": APP["appId"]}
ME = {"id": "user-1", "userPrincipalName": "deployer@contoso.example", "userType": "Member"}
DENIED = entra_app.GraphError(403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation.")


class FakeGraph:
    """Answers scripted (method, path prefix) pairs in order; records every call."""

    def __init__(self, script: list[tuple[str, str, object]]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path, body))
        for index, (want_method, prefix, answer) in enumerate(self.script):
            if want_method == method and path.startswith(prefix):
                del self.script[index]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected Graph call {method} {path}")

    def methods(self) -> list[str]:
        return [f"{method} {path}" for method, path, _ in self.calls]


def run(function, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = function(*args, **kwargs)
    return code, out.getvalue(), err.getvalue()


def created_app(environment: str = "pms-demo", redirect_uri: str = "") -> dict:
    """What Graph returns for POST /applications with the manifest."""
    app = json.loads(json.dumps(entra_app.manifest(environment, redirect_uri)))
    app.update(APP)
    app["identifierUris"] = []
    app["api"]["preAuthorizedApplications"] = []
    return app


class NoSleep(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeps: list[float] = []
        self._sleep = entra_app.SLEEP
        entra_app.SLEEP = self.sleeps.append

    def tearDown(self) -> None:
        entra_app.SLEEP = self._sleep


class ManifestTests(unittest.TestCase):
    def test_app_roles_match_the_bicep_module(self) -> None:
        bicep = (ROOT / "infra" / "modules" / "entra-app.bicep").read_text(encoding="utf-8")
        roles = re.findall(r"value: '([^']+)'\s+displayName: '([^']+)'\s+description: '([^']+)'", bicep)
        self.assertEqual(len(roles), 5)
        self.assertEqual(tuple(roles), entra_app.APP_ROLES)

    def test_manifest_has_the_settings_of_the_bicep_module(self) -> None:
        uri = "https://ca-web-x.example.azurecontainerapps.io/.auth/login/aad/callback"
        manifest = entra_app.manifest("pms-demo", uri)
        self.assertEqual(manifest["signInAudience"], "AzureADMyOrg")
        self.assertIn("pms-demo", manifest["displayName"])
        self.assertEqual(manifest["api"]["requestedAccessTokenVersion"], 2)
        scopes = manifest["api"]["oauth2PermissionScopes"]
        self.assertEqual([(s["value"], s["type"], s["isEnabled"]) for s in scopes], [("user_impersonation", "User", True)])
        self.assertEqual(manifest["web"]["redirectUris"], [uri])
        self.assertEqual(manifest["web"]["homePageUrl"], "https://ca-web-x.example.azurecontainerapps.io")
        self.assertEqual(manifest["web"]["implicitGrantSettings"],
                         {"enableIdTokenIssuance": True, "enableAccessTokenIssuance": False})
        self.assertEqual(manifest["requiredResourceAccess"][0]["resourceAccess"],
                         [{"id": "e1fe6dd8-ba31-4d61-89e7-88639da4683d", "type": "Scope"}])
        for role in manifest["appRoles"]:
            self.assertEqual(role["allowedMemberTypes"], ["User"])
            self.assertTrue(role["isEnabled"])
        self.assertNotIn("identifierUris", manifest)

    def test_ids_are_stable_and_distinct(self) -> None:
        ids = [entra_app.role_id(value) for value in entra_app.ROLE_VALUES]
        self.assertEqual(len(set(ids)), 5)
        self.assertEqual(ids, [entra_app.role_id(value) for value in entra_app.ROLE_VALUES])
        self.assertNotIn(entra_app.SCOPE_ID, ids)

    def test_no_redirect_uri_before_provisioning(self) -> None:
        manifest = entra_app.manifest("pms-demo")
        self.assertEqual(manifest["web"]["redirectUris"], [])
        self.assertNotIn("homePageUrl", manifest["web"])


class ProbeTests(NoSleep):
    def test_success_creates_and_deletes_both_objects(self) -> None:
        graph = FakeGraph([
            ("GET", "/me", ME),
            ("POST", "/applications", APP),
            ("POST", "/servicePrincipals", SP),
            ("DELETE", "/servicePrincipals/obj-sp-1", None),
            ("DELETE", "/applications/obj-app-1", None),
            ("DELETE", "/directory/deletedItems/obj-app-1", None),
            ("DELETE", "/directory/deletedItems/obj-sp-1", None),
        ])
        code, out, err = run(entra_app.probe, graph, tenant="tenant-1", shell="ps1", suffix="abcd1234")
        self.assertEqual(code, 0, err)
        self.assertEqual(graph.script, [])
        post_app = graph.calls[1]
        self.assertEqual(post_app[2], {"displayName": "pms-preflight-probe-abcd1234", "signInAudience": "AzureADMyOrg"})
        self.assertEqual(graph.calls[2][2], {"appId": APP["appId"]})
        self.assertIn("deployer@contoso.example (Member) in tenant tenant-1", out)
        self.assertIn("may create app registrations", out)

    def test_refused_app_registration_stops_with_the_bring_your_own_fix(self) -> None:
        graph = FakeGraph([("GET", "/me", ME), ("POST", "/applications", DENIED)])
        code, out, err = run(entra_app.probe, graph, tenant="tenant-1", shell="ps1")
        self.assertEqual(code, 2)
        self.assertNotIn("DELETE", " ".join(graph.methods()))
        line = err.strip().splitlines()[-1]
        self.assertTrue(line.startswith("[pms] FAILED: Microsoft Graph does not let deployer@contoso.example"), line)
        self.assertIn("Authorization_RequestDenied: Insufficient privileges to complete the operation.", line)
        self.assertIn("Fix: bring your own app registration (README)", line)
        self.assertIn("./scripts/create-app-registration.ps1 -AssignTo deployer@contoso.example "
                      "-Owner deployer@contoso.example", line)
        self.assertIn("azd env set ENTRA_EXISTING_APP_CLIENT_ID <client-id>", line)

    def test_refused_service_principal_still_deletes_the_test_registration(self) -> None:
        graph = FakeGraph([
            ("GET", "/me", ME),
            ("POST", "/applications", APP),
            ("POST", "/servicePrincipals", DENIED),
            ("DELETE", "/applications/obj-app-1", None),
            ("DELETE", "/directory/deletedItems/obj-app-1", None),
        ])
        code, _, err = run(entra_app.probe, graph, tenant="tenant-1", shell="sh")
        self.assertEqual(code, 2)
        self.assertEqual(graph.script, [])
        self.assertIn("enterprise application (service principal)", err)
        self.assertIn("bash scripts/create-app-registration.sh --assign-to deployer@contoso.example", err)

    def test_guest_gets_the_member_account_hint(self) -> None:
        guest = dict(ME, userType="Guest", userPrincipalName="deployer_contoso.example#EXT#@fabrikam.example")
        graph = FakeGraph([("GET", "/me", guest), ("POST", "/applications", DENIED)])
        code, _, err = run(entra_app.probe, graph, tenant="tenant-9", shell="ps1")
        self.assertEqual(code, 2)
        self.assertIn("you are a guest in tenant tenant-9", err)
        self.assertIn("az login --tenant tenant-9", err)
        self.assertIn("azd auth login --tenant-id tenant-9", err)

    def test_sign_in_problem_is_not_reported_as_missing_right(self) -> None:
        graph = FakeGraph([("GET", "/me", ME),
                           ("POST", "/applications", entra_app.GraphError(401, "InvalidAuthenticationToken", "expired"))])
        code, _, err = run(entra_app.probe, graph, shell="sh")
        self.assertEqual(code, 1)
        self.assertIn("Could not test your right", err)
        self.assertIn("export PREFLIGHT_SKIP=entra-probe", err)

    def test_failed_cleanup_names_the_leftover(self) -> None:
        busy = entra_app.GraphError(500, "ServiceUnavailable", "try later")
        graph = FakeGraph([
            ("GET", "/me", ME),
            ("POST", "/applications", APP),
            ("POST", "/servicePrincipals", SP),
            ("DELETE", "/servicePrincipals/obj-sp-1", None),
            ("DELETE", "/applications/obj-app-1", busy),
            ("DELETE", "/applications/obj-app-1", busy),
            ("DELETE", "/applications/obj-app-1", busy),
        ])
        code, _, err = run(entra_app.probe, graph, suffix="feed0001")
        self.assertEqual(code, 0)
        self.assertEqual(graph.script, [])
        self.assertIn("Could not delete the test app registration pms-preflight-probe-feed0001", err)

    def test_replication_delay_is_retried_not_reported_as_refusal(self) -> None:
        not_yet = entra_app.GraphError(400, "Request_BadRequest", "The appId '1111' of the service principal does not "
                                                                  "reference a valid application object.")
        graph = FakeGraph([
            ("GET", "/me", ME),
            ("POST", "/applications", APP),
            ("POST", "/servicePrincipals", not_yet),
            ("POST", "/servicePrincipals", not_yet),
            ("POST", "/servicePrincipals", SP),
            ("DELETE", "/servicePrincipals/obj-sp-1", entra_app.GraphError(404, "Request_ResourceNotFound", "x")),
            ("DELETE", "/servicePrincipals/obj-sp-1", None),
            ("DELETE", "/applications/obj-app-1", None),
            ("DELETE", "/directory/deletedItems/obj-app-1", None),
            ("DELETE", "/directory/deletedItems/obj-sp-1", None),
        ])
        code, _, err = run(entra_app.probe, graph)
        self.assertEqual(code, 0, err)
        self.assertEqual(graph.script, [])
        self.assertEqual(self.sleeps, [2.0, 4.0, 2.0])

    def test_enterprise_application_removed_with_the_registration_is_no_leftover(self) -> None:
        graph = FakeGraph([
            ("GET", "/me", ME),
            ("POST", "/applications", APP),
            ("POST", "/servicePrincipals", SP),
            ("DELETE", "/servicePrincipals/obj-sp-1", DENIED),
            ("DELETE", "/applications/obj-app-1", None),
            ("GET", "/servicePrincipals(appId=", entra_app.GraphError(404, "Request_ResourceNotFound", "gone")),
            ("DELETE", "/directory/deletedItems/obj-app-1", None),
            ("DELETE", "/directory/deletedItems/obj-sp-1", None),
        ])
        code, _, err = run(entra_app.probe, graph)
        self.assertEqual(code, 0)
        self.assertEqual(graph.script, [])
        self.assertNotIn("Could not delete", err)


class CreateTests(NoSleep):
    def test_new_registration_with_roles_owner_and_result_file(self) -> None:
        app = created_app()
        owner = {"id": "user-2", "userPrincipalName": "michael@contoso.example"}
        graph = FakeGraph([
            ("GET", "/me", ME),
            ("POST", "/applications", app),
            ("PATCH", "/applications/obj-app-1", None),
            ("GET", "/servicePrincipals(appId=", entra_app.GraphError(404, "Request_ResourceNotFound", "not found")),
            ("POST", "/servicePrincipals", SP),
            ("GET", "/users/michael%40contoso.example", owner),
            ("POST", "/applications/obj-app-1/owners/$ref", None),
            ("POST", "/servicePrincipals/obj-sp-1/owners/$ref", None),
            ("GET", "/users/michael%40contoso.example", owner),
            ("GET", "/servicePrincipals/obj-sp-1/appRoleAssignedTo", {"value": []}),
            ("POST", "/servicePrincipals/obj-sp-1/appRoleAssignedTo", {}),
            ("POST", "/servicePrincipals/obj-sp-1/appRoleAssignedTo", {}),
        ])
        with tempfile.TemporaryDirectory() as folder:
            result_path = str(Path(folder) / "result.json")
            code, out, err = run(entra_app.create, graph, environment="pms-demo",
                                 roles=["PMS.Operator", "PMS.MedicalReviewer"], assign_to="michael@contoso.example",
                                 owners=["michael@contoso.example"], tenant="tenant-1", shell="ps1",
                                 result_path=result_path)
            result = json.loads(Path(result_path).read_text(encoding="utf-8"))
        self.assertEqual(code, 0, err)
        self.assertEqual(graph.script, [])
        post = graph.calls[1][2]
        self.assertEqual(post, entra_app.manifest("pms-demo"))
        patch = graph.calls[2][2]
        self.assertEqual(patch["identifierUris"], [f"api://{APP['appId']}"])
        self.assertEqual(patch["api"]["preAuthorizedApplications"],
                         [{"appId": AZURE_CLI_APP_ID, "delegatedPermissionIds": [entra_app.SCOPE_ID]}])
        self.assertEqual(patch["api"]["requestedAccessTokenVersion"], 2)
        self.assertNotIn("web", patch)
        self.assertEqual(graph.calls[4][2], {"appId": APP["appId"], "appRoleAssignmentRequired": True})
        self.assertEqual(graph.calls[6][2], {"@odata.id": "https://graph.microsoft.com/v1.0/directoryObjects/user-2"})
        assigned = [call[2] for call in graph.calls if call[1].endswith("/appRoleAssignedTo") and call[0] == "POST"]
        self.assertEqual(assigned, [
            {"principalId": "user-2", "resourceId": "obj-sp-1", "appRoleId": entra_app.role_id("PMS.Operator")},
            {"principalId": "user-2", "resourceId": "obj-sp-1", "appRoleId": entra_app.role_id("PMS.MedicalReviewer")},
        ])
        self.assertEqual(result, {"clientId": APP["appId"], "objectId": "obj-app-1", "servicePrincipalId": "obj-sp-1",
                                  "identifierUri": f"api://{APP['appId']}", "created": True, "tenantId": "tenant-1"})
        self.assertIn(f"Client ID: {APP['appId']}", out)

    def test_second_run_completes_the_same_registration(self) -> None:
        app = created_app(redirect_uri="https://old.example/.auth/login/aad/callback")
        app["identifierUris"] = [f"api://{APP['appId']}"]
        app["api"]["preAuthorizedApplications"] = [
            {"appId": AZURE_CLI_APP_ID, "delegatedPermissionIds": [entra_app.SCOPE_ID]}]
        new_uri = "https://ca-web-y.example.azurecontainerapps.io/.auth/login/aad/callback"
        graph = FakeGraph([
            ("GET", "/me", ME),
            ("GET", f"/applications(appId='{APP['appId']}')", app),
            ("PATCH", "/applications/obj-app-1", None),
            ("GET", "/servicePrincipals(appId=", dict(SP, appRoleAssignmentRequired=True)),
            ("GET", "/servicePrincipals/obj-sp-1/appRoleAssignedTo",
             {"value": [{"principalId": "user-1", "appRoleId": entra_app.role_id("PMS.Operator")}]}),
        ])
        code, out, err = run(entra_app.create, graph, environment="pms-demo", client_id=APP["appId"],
                             redirect_uri=new_uri, roles=["PMS.Operator"])
        self.assertEqual(code, 0, err)
        self.assertEqual(graph.script, [])
        self.assertNotIn("POST /applications", graph.methods())
        patch = graph.calls[2][2]
        self.assertEqual(set(patch), {"web"})
        self.assertEqual(patch["web"]["redirectUris"], ["https://old.example/.auth/login/aad/callback", new_uri])
        self.assertTrue(patch["web"]["implicitGrantSettings"]["enableIdTokenIssuance"])
        self.assertIn("already has the app role PMS.Operator", out)

    def test_refused_creation_exits_2_with_the_fix(self) -> None:
        graph = FakeGraph([("GET", "/me", ME), ("POST", "/applications", DENIED)])
        code, _, err = run(entra_app.create, graph, environment="pms-demo", roles=["PMS.Operator"], tenant="t-1")
        self.assertEqual(code, 2)
        self.assertIn("[pms] FAILED: Microsoft Graph does not let deployer@contoso.example (Member) create the app "
                      "registration in tenant t-1", err)
        self.assertIn("bash scripts/create-app-registration.sh", err)


class CompleteTests(NoSleep):
    URI = "https://ca-web-z.example.azurecontainerapps.io/.auth/login/aad/callback"

    def test_adds_redirect_uri_and_pre_authorisation(self) -> None:
        app = created_app()
        graph = FakeGraph([("GET", "/applications(appId=", app), ("PATCH", "/applications/obj-app-1", None)])
        code, out, _ = run(entra_app.complete, graph, client_id=APP["appId"], redirect_uri=self.URI)
        self.assertEqual(code, 0)
        patch = graph.calls[1][2]
        self.assertEqual(set(patch), {"api", "web"})
        self.assertEqual(patch["web"]["redirectUris"], [self.URI])
        self.assertEqual(patch["web"]["homePageUrl"], "https://ca-web-z.example.azurecontainerapps.io")
        self.assertIn(f"set Web redirect URI {self.URI}", out)
        self.assertIn("set Azure CLI pre-authorised for user_impersonation", out)

    def test_nothing_to_do(self) -> None:
        app = created_app(redirect_uri=self.URI)
        app["api"]["preAuthorizedApplications"] = [
            {"appId": AZURE_CLI_APP_ID, "delegatedPermissionIds": [entra_app.SCOPE_ID]}]
        graph = FakeGraph([("GET", "/applications(appId=", app)])
        code, out, _ = run(entra_app.complete, graph, client_id=APP["appId"], redirect_uri=self.URI)
        self.assertEqual(code, 0)
        self.assertEqual(graph.methods(), [f"GET /applications(appId='{APP['appId']}')"])
        self.assertIn("are in place", out)

    def test_not_allowed_to_change_lists_what_the_owner_must_set(self) -> None:
        app = created_app()
        app["appRoles"] = [role for role in app["appRoles"] if role["value"] != "PMS.Regulatory"]
        graph = FakeGraph([("GET", "/applications(appId=", app), ("PATCH", "/applications/obj-app-1", DENIED)])
        code, out, _ = run(entra_app.complete, graph, client_id=APP["appId"], redirect_uri=self.URI)
        self.assertEqual(code, 0)
        self.assertIn("you may not change it", out)
        self.assertIn(f"  - Web redirect URI {self.URI}", out)
        self.assertIn("app roles PMS.Regulatory", out)

    def test_unreadable_registration_prints_the_checklist(self) -> None:
        graph = FakeGraph([("GET", "/applications(appId=", DENIED)])
        code, out, _ = run(entra_app.complete, graph, client_id=APP["appId"], redirect_uri=self.URI)
        self.assertEqual(code, 0)
        self.assertIn(f"Web redirect URI: {self.URI}", out)
        self.assertIn("requestedAccessTokenVersion = 2", out)


class GraphClientTests(unittest.TestCase):
    def test_http_error_becomes_graph_error_with_code_and_message(self) -> None:
        body = json.dumps({"error": {"code": "Authorization_RequestDenied",
                                     "message": "Insufficient privileges to complete the operation."}}).encode()

        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(body))

        graph = entra_app.Graph("token", opener=opener)
        with self.assertRaises(entra_app.GraphError) as caught:
            graph.request("POST", "/applications", {"displayName": "x"})
        self.assertTrue(caught.exception.refused)
        self.assertEqual(caught.exception.describe(),
                         "HTTP 403 Authorization_RequestDenied: Insufficient privileges to complete the operation.")

    def test_request_sends_bearer_token_and_json(self) -> None:
        seen = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout):
            seen["auth"] = request.get_header("Authorization")
            seen["type"] = request.get_header("Content-type")
            seen["body"] = json.loads(request.data)
            return Response(b'{"id": "1"}')

        result = entra_app.Graph("secret-token", opener=opener).request("POST", "/applications", {"a": 1})
        self.assertEqual(result, {"id": "1"})
        self.assertEqual(seen, {"auth": "Bearer secret-token", "type": "application/json", "body": {"a": 1}})

    def test_tenant_from_token(self) -> None:
        payload = base64.urlsafe_b64encode(json.dumps({"tid": "tenant-42"}).encode()).decode().rstrip("=")
        self.assertEqual(entra_app.tenant_of(f"x.{payload}.y"), "tenant-42")
        self.assertEqual(entra_app.tenant_of("not-a-token"), "")

    def test_without_token(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"PMS_GRAPH_TOKEN": ""}), mock.patch.object(entra_app.shutil, "which",
                                                                                     return_value=None):
            code, out, _ = run(entra_app.main, ["complete", "--client-id", "abc", "--redirect-uri", "https://x/cb"])
            self.assertEqual(code, 0)
            self.assertIn("Web redirect URI: https://x/cb", out)
            code, _, err = run(entra_app.main, ["probe"])
            self.assertEqual(code, 1)
            self.assertIn("Could not get a Microsoft Graph token", err)


class ScriptWiringTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (SCRIPTS / name).read_text(encoding="utf-8")

    def test_wrappers_exist_for_both_shells(self) -> None:
        ps1, sh = self.read("create-app-registration.ps1"), self.read("create-app-registration.sh")
        for text in (ps1, sh):
            self.assertIn("entra_app.py", text)
            self.assertIn("ENTRA_EXISTING_APP_CLIENT_ID", text)
            self.assertIn("get-access-token --resource-type ms-graph", text)
        self.assertIn("-AssignTo", ps1)
        self.assertIn("--assign-to", sh)

    def test_preflight_probes_unless_skipped(self) -> None:
        self.assertIn("entra_app.py') probe --shell ps1", self.read("preflight.ps1"))
        self.assertIn("entra_app.py\" probe --shell sh", self.read("preflight.sh"))
        for name in ("preflight.ps1", "preflight.sh"):
            self.assertIn("entra-probe", self.read(name))

    def test_deploy_names_the_entra_step_and_the_way_out(self) -> None:
        for name in ("deploy.ps1", "deploy.sh"):
            text = self.read(name)
            self.assertIn("--name pms-entra-app", text)
            self.assertIn("create-app-registration", text)
        bicep = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        self.assertIn("name: 'pms-entra-app'", bicep)

    def test_postprovision_completes_a_bring_your_own_registration(self) -> None:
        for name in ("postprovision.ps1", "postprovision.sh"):
            self.assertIn("complete", self.read(name))
            self.assertIn("entra_app.py", self.read(name))

    def test_rerun_after_a_stopped_provisioning(self) -> None:
        # No azd outputs exist after a stopped provisioning: the preflight finds the resource group and the
        # Foundry account itself (own model quota), and the teardown checks rg-<env> and finds the custom role.
        self.assertIn('"rg-$envName"', self.read("preflight.ps1"))
        self.assertIn('resource_group="rg-$env_name"', self.read("preflight.sh"))
        for name in ("preflight.ps1", "preflight.sh"):
            self.assertIn("cognitiveservices account list --resource-group", self.read(name))
        for name in ("teardown.ps1", "teardown.sh"):
            text = self.read(name)
            self.assertIn("PMS demo ACS e-mail sender", text)
            self.assertIn("az group delete --name", text)
            self.assertIn("az ad app delete --id", text)

    def test_stop_pms_line_reaches_tee_object(self) -> None:
        common = (SCRIPTS / "lib" / "Common.ps1").read_text(encoding="utf-8")
        body = common[common.index("function Stop-Pms"):common.index("function Test-PmsCommand")]
        self.assertIn('Write-Host "[pms] FAILED: $Problem Fix: $Fix"', body)
        self.assertNotIn("[Console]::Error", body)

    def test_readme_documents_the_way_out(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        byo = readme[readme.index("### Bring your own app registration"):readme.index("### Model quota and fallback")]
        for needle in ("create-app-registration.sh", "create-app-registration.ps1", "-AssignTo", "--assign-to",
                       "ENTRA_EXISTING_APP_CLIENT_ID", "az ad app delete"):
            self.assertIn(needle, byo)
        troubleshooting = readme[readme.index("## Troubleshooting"):readme.index("## Sign-in with Microsoft Entra ID")]
        self.assertIn("Authorization_RequestDenied", troubleshooting)
        self.assertIn("entra-probe", readme)


if __name__ == "__main__":
    unittest.main()
