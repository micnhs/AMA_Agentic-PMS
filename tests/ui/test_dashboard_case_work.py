"""Goal Card check 8 (dashboard and case work) and the UI part of check 4 - DOM-level tests against a real
server on a free localhost port (http.client + html.parser, standard library only).

* current cases with status and the progress of each agent (A1 A2 A6 A3 A4, CORR-1.0; A5 trend view);
* the critical queue on top with a live countdown to the earliest deadline, overdue marked in text;
* the rejected view with reason codes;
* the case view with the original letter and all signals (duplicates, follow-ups), facts with source quotes,
  registry data, the draft recommendation, IMDRF candidates with attribution, deadlines per rule set, audit;
* adding a signal; a metadata edit needs a reason and logs user, old and new value;
* check 4: a critical upload is first in the queue with triage task and outbox alert before the analysis,
  and only a person moves it on (an AI 'not serious' never lowers urgency).
"""
from __future__ import annotations

import re
import unittest

from pms.agents.specs import IMDRF_ATTRIBUTION
from pms.agents.trend import statistical_terms_in
from pms.reports import DISCLAIMER_DE

from tests.unit._pipeline_support import cleanup_runs, letter_file, new_pipeline, seeded
from tests.unit._support import RECEIVED_AT, letter_text

from ._support import LiveServer, document_index, parse_html

AGENT_STEPS = ("A1", "A2", "CORR", "A6", "A3", "A4")
PAGES = ("/", "/cases/CASE-L02", "/rejected", "/unreadable", "/trend", "/costs", "/governance", "/audit")


def tearDownModule() -> None:
    cleanup_runs()


def normalise(text: str) -> str:
    return " ".join(text.split())


class SeededDashboard(unittest.TestCase):
    """All 12 letters and 6 seed messages, analysed; one shared server (each test uses its own case)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pipeline, cls.clock = seeded("ui-seeded")
        cls.server = LiveServer(cls.pipeline).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.pipeline.close()

    # ------------------------------------------------------------------ dashboard
    def test_dashboard_lists_current_cases_with_status_and_agent_progress(self) -> None:
        dom = self.server.page("/")
        data = self.pipeline.dashboard()
        rows = {r.get("data-case-id"): r for r in dom.find_all("tr", class_="case-row")}
        expected = data["critical_queue"] + data["cases"]
        self.assertEqual(set(rows), {r["case_id"] for r in expected})
        self.assertEqual(len(rows), 14)
        for case in expected:
            row = rows[case["case_id"]]
            self.assertEqual(row.get("data-status"), case["status"])
            status = row.find("td", class_="status")
            self.assertIn(case["status_label"], status.text())
            self.assertIn(case["status"], status.text())
            job = case["progress"]["latest_job"] or {}
            for step in AGENT_STEPS:
                cell = row.find("td", data_step=step)
                self.assertIsNotNone(cell, f"{case['case_id']} misses the progress of {step}")
                wanted = case["progress"]["steps"][step].get("status") or (
                    "QUEUED" if job.get("status") in ("QUEUED", "RUNNING") else "NOT_QUEUED")
                self.assertEqual(cell.get("data-status"), wanted)
                self.assertTrue(cell.text().strip())
        a5 = dom.find("section", id="a5-trend")
        self.assertIsNotNone(a5, "A5 light trend view panel")
        self.assertIn("no rates", a5.text())
        self.assertIsNotNone(a5.find("a", href="/trend"))
        headers = [th.text() for th in dom.find("table", id="critical-table").find_all("th")]
        for step in AGENT_STEPS:
            self.assertIn(step, headers)

    def test_critical_queue_is_on_top_with_a_live_countdown(self) -> None:
        dom = self.server.page("/")
        critical = dom.find("section", id="critical-queue")
        others = dom.find("section", id="other-cases")
        self.assertLess(document_index(dom, critical), document_index(dom, others))
        first_table = dom.find("table", class_="cases")
        self.assertEqual(first_table.get("id"), "critical-table", "the critical queue is the first case table")
        data = self.pipeline.dashboard()
        shown = [r.get("data-case-id") for r in critical.find_all("tr", class_="case-row")]
        self.assertEqual(shown, [r["case_id"] for r in data["critical_queue"]])
        self.assertTrue(all(r.get("data-critical") == "true" or r.get("data-status") == "TRIAGE_ERFORDERLICH"
                            for r in critical.find_all("tr", class_="case-row")))
        earliest = dom.find("span", id="earliest-countdown")
        self.assertIsNotNone(earliest)
        self.assertIn("data-countdown", earliest.attrs)
        self.assertEqual(int(earliest.get("data-seconds")), data["critical_queue"][0]["countdown_seconds"])
        for row, case in zip(critical.find_all("tr", class_="case-row"), data["critical_queue"]):
            countdown = row.find("span", class_="countdown")
            self.assertEqual(int(countdown.get("data-seconds")), case["countdown_seconds"])
        seconds = [c["countdown_seconds"] for c in data["critical_queue"] if c["countdown_seconds"] is not None]
        self.assertEqual(seconds, sorted(seconds), "earliest deadline first")
        script = self.server.get("/static/app.js", user=None)
        self.assertEqual(script.status, 200)
        self.assertIn("data-countdown", script.text)
        self.assertIn("setInterval", script.text)
        self.assertIn('src="/static/app.js', self.server.get("/").text)

    def test_overdue_cases_are_marked_in_text_not_only_by_colour(self) -> None:
        dom = self.server.page("/")
        overdue = [r for r in dom.find_all("tr", class_="case-row") if r.get("data-overdue") == "true"]
        self.assertEqual(len(overdue), self.pipeline.dashboard()["counts"]["overdue"])
        self.assertTrue(overdue, "the seed cases are overdue (received in September 2026)")
        for row in overdue:
            self.assertIn("overdue", row.classes)
            self.assertIn("OVERDUE", row.find("span", class_="countdown").text())
        waiting = [r for r in dom.find_all("tr", class_="case-row") if r.get("data-status") == "TRIAGE_ERFORDERLICH"]
        for row in waiting:
            self.assertIn("waiting for a person to take over the triage", row.text())

    # ------------------------------------------------------------------ rejected view
    def test_rejected_view_shows_reason_codes(self) -> None:
        empty = self.server.page("/rejected").find("section", id="rejected")
        self.assertIsNotNone(empty)
        legend = self.server.page("/rejected").find("ul", id="reason-codes")
        for code in ("R1", "R2", "R3", "R4", "R5", "R6"):
            self.assertIn(code, legend.text())
        result = self.server.request("POST", "/api/cases/CASE-L06/decision", user="reviewer",
                                     form={"decision": "reject", "reason_code": "R2",
                                           "reason": "The event date was extracted from the wrong line."})
        self.assertEqual(result.status, 303, result.text[:300])
        self.assertTrue(result.headers["location"].startswith("/cases/CASE-L06?done=decision"))
        dom = self.server.page("/rejected")
        rows = dom.find("table", id="rejected-table").find_all("tr", class_="rejected-row")
        row = next(r for r in rows if r.get("data-case-id") == "CASE-L06")
        self.assertEqual(row.get("data-reason-code"), "R2")
        self.assertIn("R2", row.find("td", class_="reason-code").text())
        self.assertIn("facts extracted incorrectly", row.text())
        self.assertIn("The event date was extracted from the wrong line.", row.text())
        self.assertIn("reviewer", row.text())

    # ------------------------------------------------------------------ case view
    def test_case_view_shows_the_original_letter_and_all_signals(self) -> None:
        dom = self.server.page("/cases/CASE-L02")
        articles = dom.find("section", id="signals").find_all("article", class_="signal")
        self.assertEqual([a.get("data-ref") for a in articles], ["L02", "L12"])
        self.assertEqual(articles[1].get("data-relation"), "duplicate_of:L02")
        for article, letter in zip(articles, ("L02", "L12")):
            pre = article.find("pre", class_="letter-text")
            self.assertIsNotNone(pre, f"original letter text of {letter}")
            self.assertEqual(normalise(pre.text()), normalise(letter_text(letter)))
        self.assertEqual(articles[1].get("data-injection"), "true")
        withheld = articles[1].find("mark", class_="withheld")
        self.assertIsNotNone(withheld, "the injected paragraph of L12 is marked as withheld from the agents")
        self.assertIn("FLAGGED", articles[1].text())
        follow = self.server.page("/cases/CASE-L09").find("section", id="signals")
        refs = [(a.get("data-ref"), a.get("data-relation")) for a in follow.find_all("article", class_="signal")]
        self.assertEqual(refs, [("L09", "new_case"), ("L10", "follow_up_of:L09")])

    def test_case_view_shows_facts_registry_draft_imdrf_deadlines_and_audit(self) -> None:
        dom = self.server.page("/cases/CASE-L02")
        facts = dom.find("table", id="rule-facts")
        self.assertIn("P-GB-2208", facts.text())
        self.assertIsNotNone(facts.find("q"), "facts carry their source quote")
        self.assertIn("Re: Femoral stem fracture followed by death", dom.find("section", id="facts").text())
        registry = dom.find("section", id="registry")
        self.assertIn("MATCH:REG-0002", registry.text())
        self.assertIn("OA-S5", registry.text())
        recommendation = dom.find("section", id="recommendation")
        self.assertEqual(recommendation.get("data-draft"), "true")
        self.assertIn("Draft (Entwurf)", recommendation.find("span", class_="draft-label").text())
        self.assertIn("probable", recommendation.text())
        imdrf = dom.find("section", id="imdrf")
        self.assertIn("A0401", imdrf.text())
        self.assertIn("pending human review", imdrf.text())
        self.assertIn(normalise(IMDRF_ATTRIBUTION), normalise(imdrf.find("p", class_="attribution").text()))
        deadlines = dom.find("table", id="deadlines-table").find_all("tr", class_="deadline-row")
        self.assertTrue(deadlines)
        driving = [r for r in deadlines if r.get("data-driving") == "true"]
        self.assertEqual(len(driving), 1)
        self.assertIn("earliest - drives priority", driving[0].text())
        self.assertEqual(driving[0].get("data-rule-set"), "UK-MDR-PART4A:GB")
        audit = dom.find("table", id="audit-table").find_all("tr", class_="audit-row")
        self.assertEqual(len(audit), len(self.pipeline.audit_records(case_id="CASE-L02")))
        self.assertIn("CASE_CREATED", {r.get("data-event") for r in audit})
        self.assertIsNotNone(dom.find("section", id="progress"))
        self.assertIsNotNone(dom.find("a", href="/api/cases/CASE-L02/report?format=md"))

    def test_french_case_shows_route_unverified_and_every_rule_set(self) -> None:
        dom = self.server.page("/cases/CASE-L05")
        self.assertIsNotNone(dom.find("p", id="route-unverified"))
        rows = dom.find("table", id="deadlines-table").find_all("tr", class_="deadline-row")
        self.assertEqual({r.get("data-rule-set") for r in rows}, {"EU-MDR:FR"})
        both = self.server.page("/cases/CASE-L08").find("table", id="deadlines-table").find_all("tr",
                                                                                                class_="deadline-row")
        self.assertGreaterEqual(len({r.get("data-rule-set") for r in both}), 2, "GB or NI unclear: both rule sets")
        self.assertEqual(sum(1 for r in both if r.get("data-driving") == "true"), 1, "the earliest drives priority")

    def test_ai_not_serious_never_lowers_urgency_and_is_shown(self) -> None:
        dom = self.server.page("/")
        row = dom.find("tr", class_="case-row", data_case_id="CASE-L07")
        self.assertIsNotNone(dom.find("section", id="critical-queue").find("tr", data_case_id="CASE-L07"))
        self.assertEqual(row.get("data-critical"), "true")
        progress = self.server.page("/cases/CASE-L07").find("table", id="progress-table")
        a3 = progress.find("tr", data_step="A3")
        self.assertIn("NOT_SERIOUS", a3.text())
        self.assertIn("ignored_lower", a3.text())

    # ------------------------------------------------------------------ adding a signal
    def test_adding_a_signal_works(self) -> None:
        before = len(self.pipeline.store.list_signals("CASE-L11"))
        text = ("SYNTHETIC DEMO DOCUMENT - FICTIONAL DATA\n\nFollow-up from the clinic (fictional): the patient is "
                "doing well after the revision; no further event.\n")
        result = self.server.request("POST", "/api/cases/CASE-L11/signals", user="analyst",
                                     multipart={"text": text, "filename": "followup-ui-test.txt",
                                                "received_at": "2026-11-23T08:30:00Z"})
        self.assertEqual(result.status, 303, result.text[:400])
        location = result.headers["location"]
        self.assertTrue(location.startswith("/cases/CASE-L11?done=signal"), location)
        dom = self.server.page(location.split("#")[0])
        self.assertIn("Signal added", dom.find("p", id="flash").text())
        articles = dom.find("section", id="signals").find_all("article", class_="signal")
        self.assertEqual(len(articles), before + 1)
        self.assertEqual(normalise(articles[-1].find("pre", class_="letter-text").text()), normalise(text))
        self.assertIn("human_assignment", articles[-1].text())
        added = self.pipeline.audit_records(case_id="CASE-L11", event_type="SIGNAL_ADDED")
        self.assertEqual(added[-1]["actor_id"], "analyst")

    def test_adding_a_signal_as_file_upload(self) -> None:
        letter = letter_file("L04")
        result = self.server.request("POST", "/api/cases/CASE-L04/signals", user="reviewer",
                                     multipart={"file": (letter.name, letter.read_bytes(), "text/plain")})
        self.assertEqual(result.status, 303, result.text[:400])
        articles = self.server.page("/cases/CASE-L04").find("section", id="signals").find_all("article",
                                                                                              class_="signal")
        self.assertEqual(normalise(articles[-1].find("pre", class_="letter-text").text()),
                         normalise(letter_text("L04")))
        self.assertEqual(articles[-1].get("data-relation"), "duplicate_of:L04")

    # ------------------------------------------------------------------ metadata edit
    def test_metadata_edit_without_reason_is_refused(self) -> None:
        records = len(self.pipeline.audit_records(case_id="CASE-L08", event_type="METADATA_EDITED"))
        result = self.server.request("POST", "/api/cases/CASE-L08/metadata", user="analyst",
                                     form={"field": "reporter_reference", "new_value": "REF-WITHOUT-REASON",
                                           "reason": "  "})
        self.assertEqual(result.status, 400)
        dom = result.dom()
        self.assertIn("reason is mandatory", dom.find("p", id="error-reason").text())
        self.assertIn(DISCLAIMER_DE, dom.find("div", id="disclaimer").text())
        self.assertEqual(len(self.pipeline.audit_records(case_id="CASE-L08", event_type="METADATA_EDITED")), records)
        self.assertNotIn("reporter_reference", self.pipeline.store.get_case("CASE-L08").metadata)
        api = self.server.request("POST", "/api/cases/CASE-L08/metadata", user="analyst",
                                  json_body={"field": "reporter_reference", "new_value": "X", "reason": ""})
        self.assertEqual((api.status, api.json()["error"]), (400, "reason_required"))

    def test_metadata_edit_with_reason_logs_user_old_and_new_value(self) -> None:
        result = self.server.request("POST", "/api/cases/CASE-L03/metadata", user="regulatory",
                                     form={"field": "implant_site", "new_value": "Westshire Clinic (fictional)",
                                           "reason": "Implant site confirmed by the sender on the phone."})
        self.assertEqual(result.status, 303, result.text[:300])
        self.assertIn("#metadata-edits", result.headers["location"])
        dom = self.server.page("/cases/CASE-L03")
        row = dom.find("table", id="metadata-table").find("tr", class_="metadata-edit", data_field="implant_site")
        self.assertEqual(row.find("td", class_="user").text(), "regulatory")
        self.assertEqual(row.find("td", class_="old-value").text(), "unknown")
        self.assertEqual(row.find("td", class_="new-value").text(), "Westshire Clinic (fictional)")
        self.assertIn("Implant site confirmed by the sender on the phone.", row.text())
        record = self.pipeline.audit_records(case_id="CASE-L03", event_type="METADATA_EDITED")[-1]
        self.assertEqual((record["actor_type"], record["actor_id"], record["source"]), ("human", "regulatory", "ui"))
        self.assertEqual(record["before"], {"implant_site": None})
        self.assertEqual(record["after"], {"implant_site": "Westshire Clinic (fictional)"})
        self.assertEqual(record["reason"], "Implant site confirmed by the sender on the phone.")
        audit_row = [r for r in dom.find("table", id="audit-table").find_all("tr", class_="audit-row")
                     if r.get("data-event") == "METADATA_EDITED"][-1]
        self.assertIn("regulatory", audit_row.find("td", class_="actor").text())
        self.assertIn("Westshire Clinic (fictional)", audit_row.find("td", class_="after").text())
        second = self.server.request("POST", "/api/cases/CASE-L03/metadata", user="analyst",
                                     form={"field": "implant_site", "new_value": "",
                                           "reason": "The site is not confirmed after all."})
        self.assertEqual(second.status, 303)
        rows = self.server.page("/cases/CASE-L03").find("table", id="metadata-table").find_all(
            "tr", class_="metadata-edit", data_field="implant_site")
        self.assertEqual(rows[-1].find("td", class_="old-value").text(), "Westshire Clinic (fictional)")
        self.assertEqual(rows[-1].find("td", class_="new-value").text(), "unknown", "empty means unknown")

    # ------------------------------------------------------------------ every page
    def test_disclaimer_banner_on_every_page(self) -> None:
        for path in PAGES + ("/login",):
            with self.subTest(path):
                dom = self.server.page(path)
                banner = dom.find("div", id="disclaimer")
                self.assertIsNotNone(banner)
                text = normalise(banner.text())
                for phrase in ("Synthetic data only", "illustrates", "no compliance claim",
                               "Nothing is ever sent to an authority", normalise(DISCLAIMER_DE)):
                    self.assertIn(phrase, text)

    def test_other_views(self) -> None:
        trend = self.server.page("/trend").find("section", id="trend")
        self.assertEqual(len(trend.find("table", id="trend-table").find_all("tr")) - 1, 6, "six monthly rows")
        self.assertIn("Nenner unbekannt, keine Raten", trend.text())
        self.assertEqual(statistical_terms_in(trend.text()), [])
        inputs = self.server.page("/unreadable").find("table", id="inputs-table").find_all("tr", class_="input-row")
        self.assertEqual(len(inputs), len(self.pipeline.unreadable_unmatched_view()))
        costs = self.server.page("/costs")
        self.assertIn("simulated", costs.find("section", id="platform-costs").text())
        self.assertIsNotNone(costs.find("table", id="per-agent-table"))
        governance = self.server.page("/governance")
        self.assertEqual(governance.find("strong", id="audit-ok").text(), "OK")
        audit = self.server.page("/audit")
        self.assertEqual(audit.find("strong", id="chain-status").text(), "Chain OK")

    def test_every_case_page_renders_for_every_role(self) -> None:
        for case in self.pipeline.store.list_cases():
            with self.subTest(case=case.case_id):
                dom = self.server.page(f"/cases/{case.case_id}", user="analyst")
                self.assertIsNotNone(dom.find("section", id="signals"))
                self.assertEqual(len(dom.find("section", id="signals").find_all("article", class_="signal")),
                                 len(self.pipeline.store.list_signals(case.case_id)))
        for user in ("viewer", "analyst", "regulatory", "reviewer", "operator"):
            with self.subTest(user=user):
                dom = self.server.page("/cases/CASE-L02", user=user)
                blocks = {d.get("id"): d.get("data-permitted") for d in dom.find_all("details", class_="action")}
                permissions = self.server.get("/api/me", user=user).json()["permissions"]
                self.assertEqual(blocks["action-decision"], "true" if permissions["approve"] else "false")
                self.assertEqual(blocks["action-submission"],
                                 "true" if permissions["record_submission"] else "false")
        self.assertEqual(self.server.get("/cases/NO-SUCH-CASE").status, 404)

    def test_pages_need_a_signed_in_user_and_the_picker_signs_in(self) -> None:
        anonymous = self.server.get("/", user=None)
        self.assertEqual(anonymous.status, 401)
        picker = anonymous.dom()
        self.assertIsNotNone(picker.find("p", id="sign-in-problem"))
        names = [b.get("value") for b in picker.find_all("button", class_="user-choice")]
        self.assertEqual(names, ["viewer", "analyst", "regulatory", "reviewer", "operator"])
        self.assertEqual(self.server.get("/api/cases", user=None).status, 401)
        self.assertEqual(self.server.get("/api/cases", user="nobody").status, 401)
        login = self.server.request("POST", "/login", form={"user": "reviewer", "next": "/cases/CASE-L02"})
        self.assertEqual(login.status, 303)
        self.assertEqual(login.headers["location"], "/cases/CASE-L02")
        cookie = login.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        page = self.server.request("GET", "/", cookie=cookie.split(";")[0])
        self.assertEqual(page.status, 200)
        self.assertEqual(page.dom().find("div", class_="user").get("data-user"), "reviewer")
        evil = self.server.request("POST", "/login", form={"user": "reviewer", "next": "//evil.example/"})
        self.assertEqual(evil.headers["location"], "/")
        logout = self.server.request("POST", "/logout", form={})
        self.assertEqual((logout.status, logout.headers["location"]), (303, "/login"))
        self.assertIn("Max-Age=0", logout.headers["set-cookie"])

    def test_security_headers_and_escaping(self) -> None:
        result = self.server.get("/cases/CASE-L02")
        csp = result.headers["content-security-policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(result.headers["x-content-type-options"], "nosniff")
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertIsNone(re.search(r"<script>(?!</script>)", result.text), "no inline script")
        marker = "<script>alert('x')</script>"
        self.server.request("POST", "/api/cases/CASE-L01/metadata", user="analyst",
                            json_body={"field": "note", "new_value": marker, "reason": "escaping test"})
        page = self.server.get("/cases/CASE-L01").text
        self.assertNotIn(marker, page)
        self.assertIn("&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;", page)
        self.assertEqual(self.server.get("/healthz", user=None).json()["status"], "ok")


class CriticalCaseStopsTheFlow(unittest.TestCase):
    """Check 4, UI part: critical first with countdown, triage task and alert before the analysis; only a
    person moves the case on. The analysis worker is not started: the test runs it explicitly."""

    def setUp(self) -> None:
        self.pipeline, self.clock = new_pipeline("ui-critical", at="2026-11-17T08:30:00Z")
        self.server = LiveServer(self.pipeline).start()

    def tearDown(self) -> None:
        self.server.stop()
        self.pipeline.close()

    def upload(self, letter: str, user: str = "analyst") -> dict:
        self.clock.set(RECEIVED_AT[letter])
        result = self.server.request("POST", "/api/letters", user=user,
                                     json_body={"filename": letter_file(letter).name, "text": letter_text(letter),
                                                "received_at": RECEIVED_AT[letter]})
        self.assertEqual(result.status, 201, result.text[:400])
        return result.json()

    def test_critical_upload_is_first_and_only_a_person_moves_it_on(self) -> None:
        minor = self.upload("L06")
        self.assertFalse(minor["critical"])
        critical = self.upload("L07")
        self.assertTrue(critical["critical"])
        self.assertEqual(critical["status"], "TRIAGE_ERFORDERLICH")
        triage = self.pipeline.store.list_tasks(case_id=critical["case_id"], status="OPEN", kind="TRIAGE")
        self.assertEqual(len(triage), 1)
        self.assertIn(triage[0].task_id, critical["tasks"])
        self.assertEqual([a["status"] for a in critical["alerts"]], ["SENT"])
        self.assertTrue(critical["alerts"][0]["within_target"])
        self.assertEqual(self.pipeline.jobs.pending_count(), 2, "the analysis has not run yet")
        outbox = self.server.get("/api/outbox").json()["entries"]
        self.assertEqual([e["case_id"] for e in outbox], [critical["case_id"]])

        dom = self.server.page("/")
        queue = dom.find("section", id="critical-queue").find_all("tr", class_="case-row")
        self.assertEqual([r.get("data-case-id") for r in queue], [critical["case_id"]])
        self.assertIsNotNone(queue[0].find("span", class_="countdown").get("data-seconds"))
        self.assertIsNotNone(dom.find("span", id="earliest-countdown"))
        others = dom.find("section", id="other-cases").find_all("tr", class_="case-row")
        self.assertEqual([r.get("data-case-id") for r in others], [minor["case_id"]])
        self.assertEqual(queue[0].find("td", data_step="A1").get("data-status"), "QUEUED")

        self.assertEqual(self.pipeline.drain(), 2)
        case_id = critical["case_id"]
        case = self.pipeline.store.get_case(case_id)
        self.assertEqual((case.status, case.critical, case.analysis_state), ("TRIAGE_ERFORDERLICH", True, "DONE"))
        page = self.server.page(f"/cases/{case_id}")
        self.assertIsNotNone(page.find("p", id="triage-callout"))
        self.assertEqual(page.find("tr", data_step="A4").find("td", class_="step").get("data-status"), "DONE")

        for user in ("viewer", "regulatory", "operator"):
            refused = self.server.request("POST", f"/api/cases/{case_id}/triage", user=user, form={})
            self.assertEqual(refused.status, 403, user)
            self.assertIn("may not take_triage", refused.dom().find("p", id="error-reason").text())
        self.assertEqual(self.pipeline.store.get_case(case_id).status, "TRIAGE_ERFORDERLICH")

        taken = self.server.request("POST", f"/api/cases/{case_id}/triage", user="analyst",
                                    form={"reason": "Triage: appendicitis, implant unremarkable - review."})
        self.assertEqual(taken.status, 303)
        self.assertEqual(self.pipeline.store.get_case(case_id).status, "ENTWURF_BEREIT")
        record = [r for r in self.pipeline.audit_records(case_id=case_id, event_type="STATUS_CHANGED")
                  if (r["before"] or {}).get("status") == "TRIAGE_ERFORDERLICH"][-1]
        self.assertEqual((record["actor_type"], record["actor_id"]), ("human", "analyst"))
        row = self.server.page("/").find("tr", class_="case-row", data_case_id=case_id)
        self.assertEqual(row.get("data-status"), "ENTWURF_BEREIT")
        self.assertEqual(row.get("data-critical"), "true", "urgency is only lowered by the medical reviewer")

    def test_upload_form_and_unreadable_input_stay_visible(self) -> None:
        result = self.server.request("POST", "/api/letters", user="analyst",
                                     multipart={"file": ("scan.tiff", b"II*\x00 binary scan", "image/tiff")})
        self.assertEqual(result.status, 303, result.text[:300])
        inputs = self.server.page("/unreadable").find_all("tr", class_="input-row")
        self.assertEqual([r.get("data-kind") for r in inputs], ["UNREADABLE"])
        case_id = result.headers["location"].split("/cases/")[1].split("?")[0]
        signal = self.server.page(f"/cases/{case_id}").find("article", class_="signal")
        self.assertIn("Unreadable", signal.text())
        refused = self.server.request("POST", "/api/letters", user="viewer", multipart={"text": "x"})
        self.assertEqual(refused.status, 403)
        self.assertEqual(len(self.pipeline.store.list_signals()), 1)

    def test_progress_endpoint_feeds_the_live_view(self) -> None:
        uploaded = self.upload("L01")
        progress = self.server.get(f"/api/cases/{uploaded['case_id']}/progress").json()
        self.assertEqual(progress["status"], "TRIAGE_ERFORDERLICH")
        self.assertEqual(progress["progress"]["latest_job"]["status"], "QUEUED")
        page = parse_html(self.server.get(f"/cases/{uploaded['case_id']}").text)
        self.assertEqual({c.get("data-step"): c.get("data-status") for c in page.find_all("td", class_="step")},
                         dict.fromkeys(AGENT_STEPS, "QUEUED"))
        self.pipeline.drain()
        progress = self.server.get(f"/api/cases/{uploaded['case_id']}/progress").json()
        self.assertEqual(progress["analysis_state"], "DONE")
        self.assertTrue(all(v["status"] == "DONE" for v in progress["progress"]["steps"].values()))
        html = parse_html(self.server.get(f"/cases/{uploaded['case_id']}").text)
        self.assertEqual(html.find("section", id="progress").get("data-analysis-state"), "DONE")


if __name__ == "__main__":
    unittest.main()
