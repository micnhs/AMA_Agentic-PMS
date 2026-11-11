"""Goal Card check 5 - no decision without a human - through the web app (BUILD_SPEC 4.3; concept 7.2, 7.3).

* approve / reject / close / non-reportable are refused for unauthenticated callers, viewer, analyst,
  regulatory, operator and for agent, rule and system actors; accepted only for the medical reviewer with a
  reason (reject also with a reason code R1-R6); every refusal is audited (ACCESS_DENIED);
* lowering urgency: medical reviewer with a reason only; raising: analyst and above;
* any change after approval voids the approval;
* recording a submission needs the regulatory role and an approved version, and never sends anything;
* Azure mode: the X-MS-CLIENT-PRINCIPAL is parsed, app roles PMS.* are mapped, test users never exist;
* the app refuses to start with APP_ENV=azure and LOCAL_TEST_USERS=true (also as a WSGI import);
* cross-site requests are refused, and the web layer has no role logic of its own.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

from pms import auth
from pms.config import ConfigError, local_test_settings
from pms.models import Actor, ActorType, Role
from pms.versions import REPO_ROOT
from pms.web.app import WebApp, create_app, serve
from pms.workflow import PERMISSIONS, Action, PermissionDenied

from tests.unit._pipeline_support import cleanup_runs, ingest, work_dir
from tests.unit._support import letter_text

from ._support import WsgiClient, azure_pipeline, local_app

DECISIONS = ("approve", "reject", "close", "non_reportable")
NON_REVIEWERS = ((None, 401), ("viewer", 403), ("analyst", 403), ("regulatory", 403), ("operator", 403))
OID_REVIEWER = "6f1d2c1e-0000-4000-8000-00000000a001"  # fictional Entra object ids
OID_VIEWER = "6f1d2c1e-0000-4000-8000-00000000a002"
ROLE_URI = "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"
OID_URI = "http://schemas.microsoft.com/identity/claims/objectidentifier"


def tearDownModule() -> None:
    cleanup_runs()


def decision_body(decision: str) -> dict[str, str]:
    return {"decision": decision, "reason": "I would like to decide this case.", "reason_code": "R1"}


class DecisionsNeedTheMedicalReviewer(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline, self.clock, self.client = local_app("authz-decisions", ("L06", "L11"))
        self.addCleanup(self.pipeline.close)

    def denied(self) -> list[dict]:
        return self.pipeline.audit_records(event_type="ACCESS_DENIED")

    def test_refused_for_unauthenticated_viewer_analyst_regulatory_and_operator(self) -> None:
        for decision in DECISIONS:
            for user, status in NON_REVIEWERS:
                with self.subTest(decision=decision, user=user):
                    before = len(self.denied())
                    result = self.client.post("/api/cases/CASE-L06/decision", decision_body(decision), user=user)
                    self.assertEqual(result.status, status, result.text)
                    body = result.json()
                    self.assertEqual(body["error"], "unauthenticated" if user is None else "permission_denied")
                    self.assertIn("refused", body["reason"].lower())
                    denied = self.denied()
                    self.assertEqual(len(denied), before + 1, "exactly one ACCESS_DENIED record per attempt")
                    self.assertEqual(denied[-1]["actor_id"], user or "anonymous")
                    self.assertEqual(denied[-1]["case_id"], "CASE-L06")
        case = self.pipeline.store.get_case("CASE-L06")
        self.assertEqual(case.status, "ENTWURF_BEREIT")
        self.assertEqual(self.pipeline.store.list_decisions(case_id="CASE-L06"), [])
        self.assertEqual(self.pipeline.audit_records(event_type="DECISION"), [])
        self.assertEqual(self.pipeline.store.list_approvals("CASE-L06"), [])

    def test_refused_through_the_ui_form_as_well(self) -> None:
        result = self.client.request("POST", "/api/cases/CASE-L06/decision", user="analyst",
                                     form={"decision": "approve", "reason": "looks fine"})
        self.assertEqual(result.status, 403)
        self.assertIn("text/html", result.headers["content-type"])
        self.assertIn("analyst may not approve", result.text)
        self.assertEqual(self.denied()[-1]["source"], "ui")

    def test_agents_and_the_system_have_no_decision_rights(self) -> None:
        actors = (Actor.agent("A3-medical-analysis"), Actor.agent("A1-signal-ingestion"), Actor.system())
        for actor in actors:
            for decision in DECISIONS:
                with self.subTest(actor=actor.actor_id, decision=decision):
                    with self.assertRaises(PermissionDenied):
                        self.pipeline.decide("CASE-L06", actor, decision, "agent decides", reason_code="R1")
                    record = self.denied()[-1]
                    self.assertEqual((record["actor_id"], record["actor_type"]),
                                     (actor.actor_id, actor.actor_type.value))
            with self.assertRaises(PermissionDenied):
                self.pipeline.lower_urgency("CASE-L06", actor, "not serious according to the model")
            with self.assertRaises(PermissionDenied):
                self.pipeline.record_submission("CASE-L06", actor, channel_label="portal", reference="X",
                                                submitted_on="2026-11-20")
        self.assertEqual(self.pipeline.store.get_case("CASE-L06").status, "ENTWURF_BEREIT")
        self.assertEqual(self.pipeline.audit_records(event_type="DECISION"), [])
        for name in ("A3-medical-analysis", "system", "CRIT-1.0", "medical_reviewer"):
            with self.subTest(header=name):
                result = self.client.post("/api/cases/CASE-L06/decision", decision_body("approve"), user=name)
                self.assertEqual(result.status, 401, "requests cannot act as an agent, rule or the system")

    def test_the_reviewer_decides_only_with_a_reason(self) -> None:
        for decision in DECISIONS:
            for reason in ("", "   "):
                with self.subTest(decision=decision, reason=reason):
                    result = self.client.post("/api/cases/CASE-L06/decision",
                                              {"decision": decision, "reason": reason, "reason_code": "R1"},
                                              user="reviewer")
                    self.assertEqual((result.status, result.json()["error"]), (400, "reason_required"))
        for code in ("", "R9"):
            result = self.client.post("/api/cases/CASE-L06/decision",
                                      {"decision": "reject", "reason": "facts wrong", "reason_code": code},
                                      user="reviewer")
            self.assertEqual(result.status, 400, f"reject needs a reason code R1-R6, got {code!r}")
        for decision in ("", "approve_all", "submit"):
            result = self.client.post("/api/cases/CASE-L06/decision", {"decision": decision, "reason": "x"},
                                      user="reviewer")
            self.assertEqual((result.status, result.json()["error"]), (400, "invalid_value"))
        self.assertEqual(self.pipeline.store.get_case("CASE-L06").status, "ENTWURF_BEREIT")
        self.assertEqual(self.pipeline.audit_records(event_type="DECISION"), [])

    def test_accepted_for_the_reviewer_with_a_reason(self) -> None:
        approve = self.client.post("/api/cases/CASE-L06/decision",
                                   {"decision": "approve", "reason": "Evidence is consistent."}, user="reviewer")
        self.assertEqual(approve.status, 200, approve.text)
        self.assertEqual(approve.json()["case"]["status"], "FREIGEGEBEN")
        approval = self.pipeline.store.valid_approval("CASE-L06")
        self.assertEqual(approval.content_hash, self.pipeline.workflow.content_hash("CASE-L06"))
        self.assertEqual(approval.reviewer_id, "reviewer")
        reject = self.client.post("/api/cases/CASE-L06/decision",
                                  {"decision": "reject", "reason": "Causality not traceable after all.",
                                   "reason_code": "r4"}, user="reviewer")
        self.assertEqual(reject.status, 200, reject.text)
        self.assertIsNone(self.pipeline.store.valid_approval("CASE-L06"))
        close = self.client.post("/api/cases/CASE-L06/decision",
                                 {"decision": "close", "reason": "Handled outside the demo."}, user="reviewer")
        self.assertEqual(close.json()["case"]["status"], "ABGESCHLOSSEN")
        non_reportable = self.client.post("/api/cases/CASE-L11/decision",
                                          {"decision": "non_reportable", "reason": "No serious incident."},
                                          user="reviewer")
        self.assertEqual(non_reportable.json()["case"]["status"], "ABGESCHLOSSEN_NICHT_MELDEPFLICHTIG")
        overview = self.client.get("/api/cases/CASE-L11", user="viewer").json()["deadlines"]
        self.assertEqual(overview["clock"], "WAIVED")
        self.assertEqual(overview["waiver"]["label"], "entfällt laut menschlicher Entscheidung")
        records = self.pipeline.audit_records(event_type="DECISION")
        self.assertEqual([(r["after"]["decision"], r["actor_type"], r["actor_id"]) for r in records],
                         [("approve", "human", "reviewer"), ("reject", "human", "reviewer"),
                          ("close", "human", "reviewer"), ("non_reportable", "human", "reviewer")])
        self.assertEqual(records[1]["after"]["reason_code"], "R4")
        self.assertTrue(all(r["reason"] for r in records))


class UrgencyRules(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline, self.clock, self.client = local_app("authz-urgency", ("L07", "L06"))
        self.addCleanup(self.pipeline.close)

    def lower(self, user: str | None, reason: str = "Appendicitis, implant unremarkable.") -> int:
        return self.client.post("/api/cases/CASE-L07/urgency", {"direction": "lower", "reason": reason},
                                user=user).status

    def test_lowering_urgency_needs_the_reviewer_and_a_reason(self) -> None:
        for user, status in NON_REVIEWERS:
            with self.subTest(user=user):
                self.assertEqual(self.lower(user), status)
        self.assertEqual(self.lower("reviewer", reason=" "), 400)
        case = self.pipeline.store.get_case("CASE-L07")
        self.assertTrue(case.critical, "the offline AI answer 'not serious' did not lower it either")
        self.assertEqual(self.lower("reviewer"), 200)
        case = self.pipeline.store.get_case("CASE-L07")
        self.assertFalse(case.critical)
        self.assertEqual(case.status, "TRIAGE_ERFORDERLICH", "lowering urgency is no triage and no decision")
        record = self.pipeline.audit_records(event_type="URGENCY_LOWERED")[-1]
        self.assertEqual((record["actor_type"], record["actor_id"], record["reason"]),
                         ("human", "reviewer", "Appendicitis, implant unremarkable."))

    def test_raising_urgency_is_for_analyst_regulatory_and_reviewer(self) -> None:
        for user, status in ((None, 401), ("viewer", 403), ("operator", 403)):
            result = self.client.post("/api/cases/CASE-L06/urgency", {"direction": "raise"}, user=user)
            self.assertEqual(result.status, status)
        self.assertFalse(self.pipeline.store.get_case("CASE-L06").critical)
        result = self.client.post("/api/cases/CASE-L06/urgency",
                                  {"direction": "raise", "reason": "Second opinion: possible serious event."},
                                  user="analyst")
        self.assertEqual(result.status, 200, result.text)
        case = self.pipeline.store.get_case("CASE-L06")
        self.assertEqual((case.critical, case.status), (True, "TRIAGE_ERFORDERLICH"))
        outbox = self.client.get("/api/outbox", user="viewer").json()["entries"]
        self.assertIn("CASE-L06", [e["case_id"] for e in outbox])
        self.assertEqual(self.client.post("/api/cases/CASE-L06/urgency", {"direction": "sideways"},
                                          user="analyst").status, 400)


class OtherReviewerOnlySteps(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline, self.clock, self.client = local_app("authz-reviewer-steps", ("L07",))
        self.addCleanup(self.pipeline.close)

    def test_classifying_a_deterioration_as_anticipated(self) -> None:
        body = {"reason": "Listed in the dossier as an anticipated side-effect."}
        for user, status in NON_REVIEWERS:
            self.assertEqual(self.client.post("/api/cases/CASE-L07/anticipated", body, user=user).status, status)
        self.assertEqual(self.client.post("/api/cases/CASE-L07/anticipated", {"reason": ""}, user="reviewer").status,
                         400)
        result = self.client.post("/api/cases/CASE-L07/anticipated", body, user="reviewer")
        self.assertEqual(result.status, 200, result.text)
        candidates = self.client.get("/api/cases/CASE-L07", user="viewer").json()["deadlines"]["candidates"]
        self.assertEqual({c["basis"]: c["status"] for c in candidates},
                         {"SERIOUS_DETERIORATION": "SUPERSEDED_BY_HUMAN", "SERIOUS_DETERIORATION_ANTICIPATED": "OPEN"})
        page = self.client.get("/cases/CASE-L07", user="viewer")
        self.assertIn("superseded by a human classification (still shown)", page.text)

    def test_findings_are_managed_by_the_reviewer_with_a_reason(self) -> None:
        self.pipeline.seed_demo(Actor("operator", roles=frozenset({Role.OPERATOR})))
        finding = self.pipeline.store.list_findings(kind="process_finding")[0]
        path = f"/api/findings/{finding.finding_id}"
        for user, status in NON_REVIEWERS:
            self.assertEqual(self.client.post(path, {"status": "Closed", "reason": "done"}, user=user).status, status)
        self.assertEqual(self.client.post(path, {"status": "Closed", "reason": ""}, user="reviewer").status, 400)
        self.assertEqual(self.client.post(path, {"status": "withdrawn", "reason": "x"}, user="reviewer").status, 409)
        result = self.client.request("POST", path, user="reviewer", form={"status": "Closed", "reason": "Fixed."})
        self.assertEqual((result.status, result.headers["location"]), (303, "/governance?done=finding#findings"))
        self.assertEqual(self.pipeline.store.get_finding(finding.finding_id).status, "Closed")
        self.assertEqual(self.client.post("/api/findings/NO-SUCH", {"status": "Closed", "reason": "x"},
                                          user="reviewer").status, 404)


class AnyChangeAfterApprovalVoidsIt(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline, self.clock, self.client = local_app("authz-void", ("L06",))
        self.addCleanup(self.pipeline.close)
        result = self.client.post("/api/cases/CASE-L06/decision",
                                  {"decision": "approve", "reason": "Draft is consistent."}, user="reviewer")
        self.assertEqual(result.status, 200, result.text)
        self.assertIsNotNone(self.pipeline.store.valid_approval("CASE-L06"))

    def assert_voided(self, change: str) -> None:
        self.assertIsNone(self.pipeline.store.valid_approval("CASE-L06"), change)
        self.assertTrue(self.pipeline.audit_records(case_id="CASE-L06", event_type="APPROVAL_VOIDED"), change)
        submission = self.client.post("/api/cases/CASE-L06/submission",
                                      {"channel_label": "national portal (label)", "reference": "VOID-1",
                                       "submitted_on": "2026-11-20"}, user="regulatory")
        self.assertEqual(submission.status, 409, f"no submission without a valid approval after {change}")

    def test_metadata_edit_voids_the_approval(self) -> None:
        result = self.client.post("/api/cases/CASE-L06/metadata",
                                  {"field": "lot", "new_value": "OA-S5-2506-C", "reason": "Lot read from the label."},
                                  user="analyst")
        self.assertEqual(result.status, 200, result.text)
        self.assertEqual(result.json()["case"]["status"], "IN_MEDIZINISCHER_PRUEFUNG")
        self.assert_voided("metadata edit")

    def test_new_signal_voids_the_approval(self) -> None:
        result = self.client.post("/api/cases/CASE-L06/signals",
                                  {"filename": "followup.txt", "text": "Follow-up (fictional): no new event."},
                                  user="analyst")
        self.assertEqual(result.status, 201, result.text)
        self.assertEqual(self.pipeline.store.get_case("CASE-L06").status, "IN_MEDIZINISCHER_PRUEFUNG")
        self.assert_voided("new signal")

    def test_reanalysis_voids_the_approval(self) -> None:
        result = self.client.post("/api/cases/CASE-L06/reanalysis", {"reason": "new corpus version"}, user="analyst")
        self.assertEqual(result.status, 202, result.text)
        self.assert_voided("re-analysis")
        self.assertEqual(self.client.post("/api/cases/CASE-L06/reanalysis", {}, user="regulatory").status, 403)


class SubmissionNeedsRegulatoryAndAnApprovedVersion(unittest.TestCase):
    BODY = {"channel_label": "national authority portal (label only)", "reference": "DEMO-REF-0001",
            "submitted_on": "2026-11-20", "receipt_status": "confirmed"}

    def setUp(self) -> None:
        self.pipeline, self.clock, self.client = local_app("authz-submission", ("L06",))
        self.addCleanup(self.pipeline.close)

    def submit(self, user: str | None, **changes: str) -> int:
        return self.client.post("/api/cases/CASE-L06/submission", {**self.BODY, **changes}, user=user).status

    def test_submission_rules(self) -> None:
        self.assertEqual(self.submit("regulatory"), 409, "not approved yet")
        approve = self.client.post("/api/cases/CASE-L06/decision", {"decision": "approve", "reason": "consistent"},
                                   user="reviewer")
        self.assertEqual(approve.status, 200)
        for user, status in ((None, 401), ("viewer", 403), ("analyst", 403), ("reviewer", 403), ("operator", 403)):
            with self.subTest(user=user):
                self.assertEqual(self.submit(user), status)
        self.assertEqual(self.submit("regulatory", reference=""), 400)
        self.assertEqual(self.submit("regulatory", submitted_on="20.11.2026"), 400)
        self.assertEqual(self.submit("regulatory", receipt_status="sent"), 400)
        outbox_before = len(self.pipeline.outbox_entries())
        result = self.client.post("/api/cases/CASE-L06/submission", self.BODY, user="regulatory")
        self.assertEqual(result.status, 201, result.text)
        body = result.json()
        self.assertEqual(body["case"]["status"], "MELDUNG_ERFASST")
        self.assertIn("never sends", body["note"])
        self.assertEqual(body["submission"]["approval_id"], self.pipeline.store.list_approvals("CASE-L06")[0].approval_id)
        self.assertEqual(len(self.pipeline.outbox_entries()), outbox_before, "nothing is sent")
        overview = self.client.get("/api/cases/CASE-L06", user="viewer").json()["deadlines"]
        self.assertEqual(overview["clock"], "STOPPED_BY_SUBMISSION")
        record = self.pipeline.audit_records(event_type="SUBMISSION_RECORDED")[-1]
        self.assertEqual((record["actor_type"], record["actor_id"]), ("human", "regulatory"))
        self.assertEqual(self.submit("regulatory", reference="DEMO-REF-0002"), 409, "only once, from FREIGEGEBEN")


class AzureSignIn(unittest.TestCase):
    """APP_ENV=azure: X-MS-CLIENT-PRINCIPAL only; test users never exist."""

    def setUp(self) -> None:
        self.pipeline, self.clock = azure_pipeline("authz-azure")
        self.addCleanup(self.pipeline.close)
        ingest(self.pipeline, self.clock, "L06")
        self.pipeline.drain()
        self.client = WsgiClient(WebApp(self.pipeline))

    @staticmethod
    def principal(oid: str, *roles: str, role_type: str = "roles", name: str = "Demo person (fictional)") -> str:
        claims = [(OID_URI, oid), ("name", name), ("preferred_username", f"{oid[-4:]}@example.invalid")]
        return auth.encode_client_principal(claims + [(role_type, r) for r in roles])

    def test_no_principal_means_401_and_test_users_are_ignored(self) -> None:
        api = self.client.get("/api/cases")
        self.assertEqual((api.status, api.json()["error"]), (401, "unauthenticated"))
        self.assertIn("Bearer", api.headers["www-authenticate"])
        page = self.client.get("/")
        self.assertEqual(page.status, 401)
        self.assertIn("Sign in with Microsoft Entra ID", page.text)
        self.assertIn("/.auth/login/aad", page.text)
        self.assertNotIn("pick-reviewer", page.text, "no test-user picker in Azure")
        self.assertEqual(self.client.get("/api/cases", user="reviewer").status, 401)
        self.assertEqual(self.client.get("/api/cases", cookie=f"{auth.DEMO_USER_COOKIE}=reviewer").status, 401)
        refused = self.client.post("/api/cases/CASE-L06/decision", {"decision": "approve", "reason": "x"},
                                   user="reviewer")
        self.assertEqual(refused.status, 401)
        self.assertEqual(self.pipeline.store.get_case("CASE-L06").status, "ENTWURF_BEREIT")
        login = self.client.request("POST", "/login", form={"user": "reviewer"})
        self.assertEqual(login.status, 404)
        self.assertNotIn("set-cookie", login.headers)
        self.assertEqual(self.client.get("/healthz").status, 200)

    def test_app_roles_from_both_claim_types_are_mapped(self) -> None:
        for role_type in ("roles", ROLE_URI):
            with self.subTest(role_type=role_type):
                me = self.client.get("/api/me", principal=self.principal(
                    OID_VIEWER, "PMS.Viewer", "PMS.Analyst", "Other.App.Role", role_type=role_type)).json()
                self.assertEqual(me["actor_id"], OID_VIEWER)
                self.assertEqual(me["method"], "entra")
                self.assertEqual(me["roles"], ["analyst", "viewer"])
                self.assertEqual(me["unmapped_roles"], ["Other.App.Role"])
                self.assertTrue(me["permissions"]["upload"])
                self.assertFalse(me["permissions"]["approve"])

    def test_viewer_reads_but_cannot_decide(self) -> None:
        viewer = self.principal(OID_VIEWER, "PMS.Viewer")
        self.assertEqual(self.client.get("/", principal=viewer).status, 200)
        self.assertEqual(self.client.get("/api/cases/CASE-L06", principal=viewer).status, 200)
        result = self.client.post("/api/cases/CASE-L06/decision", {"decision": "approve", "reason": "x"},
                                  principal=viewer)
        self.assertEqual(result.status, 403)
        self.assertEqual(self.pipeline.audit_records(event_type="ACCESS_DENIED")[-1]["actor_id"], OID_VIEWER)

    def test_medical_reviewer_decides_and_is_audited_with_the_object_id(self) -> None:
        reviewer = self.principal(OID_REVIEWER, "PMS.MedicalReviewer", role_type=ROLE_URI)
        result = self.client.post("/api/cases/CASE-L06/decision",
                                  {"decision": "approve", "reason": "Consistent with the dossier."}, principal=reviewer)
        self.assertEqual(result.status, 200, result.text)
        record = self.pipeline.audit_records(event_type="DECISION")[-1]
        self.assertEqual((record["actor_type"], record["actor_id"]), ("human", OID_REVIEWER))

    def test_invalid_or_role_less_principals(self) -> None:
        for value in ("not base64 !!", auth.encode_client_principal([("name", "")])[:-8] + "@@@@",
                      "bnVsbA==", auth.encode_client_principal([("roles", "PMS.MedicalReviewer")])):
            with self.subTest(value=value[:20]):
                self.assertEqual(self.client.get("/api/cases", principal=value).status, 401)
        nobody = self.principal(OID_VIEWER, "Other.App.Role")
        result = self.client.get("/api/cases", principal=nobody)
        self.assertEqual(result.status, 403, "signed in, but without a PMS app role")
        self.assertEqual(self.pipeline.audit_records(event_type="ACCESS_DENIED")[-1]["actor_id"], OID_VIEWER)
        self.assertEqual(self.client.get("/api/outbox", principal=self.principal(OID_VIEWER, "PMS.Viewer")).status,
                         404, "the local outbox does not exist in Azure")

    def test_principal_parsing(self) -> None:
        value = auth.encode_client_principal(
            [("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name", "Demo reviewer (fictional)"),
             ("preferred_username", "demo.reviewer@example.invalid"),
             ("http://schemas.microsoft.com/ws/2008/06/identity/claims/role", "PMS.MedicalReviewer"),
             ("custom_roles", '["PMS.Operator", "PMS.Regulatory"]')], role_typ="custom_roles",
            name_typ="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")
        actor, unmapped = auth.actor_from_principal(auth.parse_client_principal(value))
        self.assertEqual(actor.actor_id, "demo.reviewer@example.invalid", "no oid: preferred_username")
        self.assertEqual(actor.display_name, "Demo reviewer (fictional)")
        self.assertEqual(actor.roles, {Role.MEDICAL_REVIEWER, Role.OPERATOR, Role.REGULATORY})
        self.assertEqual(unmapped, ())
        self.assertEqual(actor.actor_type, ActorType.HUMAN)
        only_name = auth.parse_client_principal(auth.encode_client_principal([("name", "Demo (fictional)")]))
        self.assertEqual(auth.actor_from_principal(only_name)[0].actor_id, "Demo (fictional)")
        with self.assertRaises(auth.InvalidPrincipal):
            auth.actor_from_principal(auth.parse_client_principal(auth.encode_client_principal([("roles", "x")])))
        fallback, _ = auth.actor_from_principal({"claims": [{"typ": "roles", "val": "PMS.Viewer"}]},
                                                fallback_id=OID_VIEWER)
        self.assertEqual(fallback.actor_id, OID_VIEWER)
        for broken in ("", "%%%", "e30", "WzFd"):
            with self.subTest(broken=broken), self.assertRaises(auth.InvalidPrincipal):
                auth.actor_from_principal(auth.parse_client_principal(broken))


class StartupRefusal(unittest.TestCase):
    ENV = {"APP_ENV": "azure", "LOCAL_TEST_USERS": "true"}

    def test_app_refuses_azure_with_local_test_users(self) -> None:
        unsafe = local_test_settings(work_dir("authz-refusal"), app_env="azure", local_test_users=True)
        with self.assertRaises(ConfigError) as ctx:
            create_app(unsafe)
        self.assertIn("Refusing to start", str(ctx.exception))
        with self.assertRaises(ConfigError):
            serve(port=0, settings=unsafe)
        pipeline, _ = azure_pipeline("authz-refusal-app", local_test_users=True)
        self.addCleanup(pipeline.close)
        with self.assertRaises(ConfigError):
            WebApp(pipeline)

    def run_python(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in ("APP_ENV", "LOCAL_TEST_USERS", "AGENT_BACKEND")}
        env.update(self.ENV, PYTHONPATH=str(REPO_ROOT / "src"), DATA_DIR=str(work_dir("authz-refusal-proc")))
        return subprocess.run([sys.executable, *args], cwd=REPO_ROOT, env=env, capture_output=True, text=True,
                              timeout=120, check=False)

    def test_wsgi_server_import_and_cli_refuse_to_start(self) -> None:
        imported = self.run_python("-c", "import pms.web.app")
        self.assertNotEqual(imported.returncode, 0, "gunicorn pms.web.app:application must not start")
        self.assertIn("Refusing to start", imported.stderr)
        served = self.run_python("-m", "pms", "serve", "--port", "8799")
        self.assertEqual(served.returncode, 2)
        self.assertIn("Refusing to start", served.stderr)


class CsrfAndDelegationToTheDomain(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline, self.clock, self.client = local_app("authz-csrf", ("L06",))
        self.addCleanup(self.pipeline.close)

    def test_cross_site_requests_are_refused(self) -> None:
        form = {"decision": "approve", "reason": "forged"}
        cases = (
            ({"Origin": "https://evil.example"}, "application/x-www-form-urlencoded", 403),
            ({"Origin": "null"}, "application/x-www-form-urlencoded", 403),
            ({"Origin": "", "Referer": "https://evil.example/page"}, "application/x-www-form-urlencoded", 403),
            ({"Origin": "", "Referer": ""}, "application/x-www-form-urlencoded", 403),
            ({"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "cross-site"}, "application/x-www-form-urlencoded",
             403),
            ({"Origin": "https://evil.example"}, "application/json", 403),
            ({}, "text/plain", 415),
        )
        for headers, ctype, status in cases:
            with self.subTest(headers=headers, ctype=ctype):
                body = (b'{"decision": "approve", "reason": "forged"}' if ctype != "application/x-www-form-urlencoded"
                        else b"decision=approve&reason=forged")
                result = self.client.request("POST", "/api/cases/CASE-L06/decision", user="reviewer", body=body,
                                             content_type=ctype, headers=headers)
                self.assertEqual(result.status, status, result.text[:200])
        self.assertEqual(self.pipeline.store.get_case("CASE-L06").status, "ENTWURF_BEREIT")
        same_site = self.client.request("POST", "/api/cases/CASE-L06/decision", user="reviewer", form=form)
        self.assertEqual(same_site.status, 303, "our own form (same origin) works")
        self.assertEqual(self.client.get("/api/cases/CASE-L06/decision", user="reviewer").status, 405)

    def test_the_web_layer_has_no_role_logic_of_its_own(self) -> None:
        with mock.patch.dict(PERMISSIONS, {Action.APPROVE: frozenset({Role.VIEWER})}):
            refused = self.client.post("/api/cases/CASE-L06/decision", {"decision": "approve", "reason": "x"},
                                       user="reviewer")
            self.assertEqual(refused.status, 403, "the domain matrix, not the web layer, decides")
            allowed = self.client.post("/api/cases/CASE-L06/decision", {"decision": "approve", "reason": "x"},
                                       user="viewer")
            self.assertEqual(allowed.status, 200)
        self.assertEqual(self.pipeline.audit_records(event_type="DECISION")[-1]["actor_id"], "viewer")

    def test_local_mode_ignores_an_entra_principal_header(self) -> None:
        forged = auth.encode_client_principal([(OID_URI, OID_REVIEWER), ("roles", "PMS.MedicalReviewer")])
        self.assertEqual(self.client.get("/api/cases", principal=forged).status, 401)
        result = self.client.post("/api/cases/CASE-L06/decision", {"decision": "approve", "reason": "forged"},
                                  principal=forged)
        self.assertEqual(result.status, 401)
        self.assertEqual(self.pipeline.store.get_case("CASE-L06").status, "ENTWURF_BEREIT")

    def test_seed_is_for_the_operator_only(self) -> None:
        for user in ("viewer", "analyst", "reviewer"):
            self.assertEqual(self.client.post("/api/seed", {}, user=user).status, 403)
        self.assertEqual(len(self.pipeline.store.list_signals()), 1)
        self.assertNotIn(letter_text("L01"), [s.text for s in self.pipeline.store.list_signals()])


if __name__ == "__main__":
    unittest.main()
