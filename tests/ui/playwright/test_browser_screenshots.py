"""Browser tests with screenshots (CI job "UI tests in a browser"; Goal Card check 8 evidence).

Skipped when Playwright is not installed (the local standard-library run). ``PMS_BASE_URL`` points to a running
offline demo (CI starts ``python -m pms serve --seed``); without it the test starts its own seeded offline server
on a free port. Screenshots go to ``SCREENSHOT_DIR`` (default ``artifacts/screenshots``):

01-dashboard.png, 02-critical-case.png, 03-rejected-view.png, 04-case-after-adding-a-signal.png,
05-metadata-edit.png - synthetic data only.
"""
from __future__ import annotations

import json
import os
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # the standard-library test run has no browser
    sync_playwright = None

ANALYSIS_TIMEOUT_S = 180


@unittest.skipIf(sync_playwright is None, "playwright is not installed (browser tests run in CI)")
class BrowserScreenshots(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.local_server = None
        cls.base = os.environ.get("PMS_BASE_URL", "").strip().rstrip("/")
        if not cls.base:
            from tests.ui._support import LiveServer
            from tests.unit._pipeline_support import seeded

            pipeline, _ = seeded("playwright")
            cls.local_server = LiveServer(pipeline).start()
            cls.base = cls.local_server.origin
        cls.shots = Path(os.environ.get("SCREENSHOT_DIR", "artifacts/screenshots"))
        cls.shots.mkdir(parents=True, exist_ok=True)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        if cls.local_server is not None:
            cls.local_server.stop()
            cls.local_server.pipeline.close()

    # ------------------------------------------------------------------ helpers
    def api(self, path: str, user: str = "viewer") -> Any:
        request = urllib.request.Request(self.base + path, headers={"X-Demo-User": user,
                                                                    "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - local test server
            return json.loads(response.read().decode("utf-8"))

    def wait_for_analysis(self, case_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + ANALYSIS_TIMEOUT_S
        while True:
            progress = self.api(f"/api/cases/{case_id}/progress")
            if progress["analysis_state"] in ("DONE", "FAILED") or time.monotonic() > deadline:
                return progress
            time.sleep(1)

    def signed_in(self, user: str) -> Any:
        context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.base}/login")
        page.click(f"#pick-{user}")
        page.wait_for_selector("#critical-queue")
        return page

    @staticmethod
    def open_action(page: Any, key: str) -> None:
        """Action forms sit in <details>; open the block if it is collapsed."""
        if not page.locator(f"#action-{key}[open]").count():
            page.click(f"#action-{key} > summary")

    def shot(self, page: Any, name: str, *, selector: str | None = None) -> Path:
        path = self.shots / name
        if selector:
            page.locator(selector).screenshot(path=str(path))
        else:
            page.screenshot(path=str(path), full_page=True)
        self.assertTrue(path.exists() and path.stat().st_size > 0)
        return path

    # ------------------------------------------------------------------ tests (numbered = screenshot order)
    def test_1_dashboard_with_the_critical_queue_on_top(self) -> None:
        page = self.signed_in("analyst")
        self.assertIn("Synthetic data only", page.inner_text("#disclaimer"))
        sections = page.eval_on_selector_all("main section[id]", "nodes => nodes.map(n => n.id)")
        self.assertLess(sections.index("critical-queue"), sections.index("other-cases"))
        first = page.inner_text("#earliest-countdown")
        page.wait_for_timeout(1500)
        self.assertNotEqual(page.inner_text("#earliest-countdown"), first, "the countdown runs in the browser")
        self.assertGreater(page.locator("tr.case-row td[data-step='A3']").count(), 0)
        self.shot(page, "01-dashboard.png")

    def test_2_critical_case(self) -> None:
        page = self.signed_in("reviewer")
        page.click("#critical-queue tr.case-row a.case-link >> nth=0")
        page.wait_for_selector("#signals pre.letter-text")
        self.assertIn("Draft", page.inner_text("#recommendation"))
        self.shot(page, "02-critical-case.png")

    def test_3_rejected_view(self) -> None:
        progress = self.wait_for_analysis("CASE-L06")
        self.assertEqual(progress["analysis_state"], "DONE")
        page = self.signed_in("reviewer")
        if progress["status"] == "TRIAGE_ERFORDERLICH":  # live clock: the 48-hour deadline sweep may raise urgency
            page.goto(f"{self.base}/cases/CASE-L06#actions")
            self.open_action(page, "triage")
            page.fill("#form-triage textarea[name=reason]", "Triage before the review (browser test).")
            page.click("#form-triage button[type=submit]")
            page.wait_for_selector("#flash")
        if progress["status"] != "ABGELEHNT":
            page.goto(f"{self.base}/cases/CASE-L06#actions")
            self.open_action(page, "decision")
            page.select_option("#form-decision select[name=decision]", "reject")
            page.select_option("#form-decision select[name=reason_code]", "R2")
            page.fill("#form-decision textarea[name=reason]", "The event date was extracted from the wrong line.")
            page.click("#form-decision button[type=submit]")
            page.wait_for_selector("#flash")
        page.goto(f"{self.base}/rejected")
        row = page.locator("tr.rejected-row[data-case-id='CASE-L06']")
        self.assertEqual(row.get_attribute("data-reason-code"), "R2")
        self.shot(page, "03-rejected-view.png")

    def test_4_case_after_adding_a_signal(self) -> None:
        page = self.signed_in("analyst")
        page.goto(f"{self.base}/cases/CASE-L11")
        before = page.locator("#signals article.signal").count()
        self.open_action(page, "add-signal")
        page.fill("#form-add-signal textarea[name=text]",
                  "SYNTHETIC DEMO DOCUMENT - FICTIONAL DATA\n\nFollow-up (fictional): the patient is doing well; "
                  "no further event.\n")
        page.fill("#form-add-signal input[name=filename]", "followup-browser-test.txt")
        page.click("#form-add-signal button[type=submit]")
        page.wait_for_selector("#flash")
        self.assertEqual(page.locator("#signals article.signal").count(), before + 1)
        page.locator("#signals article.signal").last.scroll_into_view_if_needed()
        self.shot(page, "04-case-after-adding-a-signal.png")

    def test_5_metadata_edit_with_reason(self) -> None:
        page = self.signed_in("analyst")
        page.goto(f"{self.base}/cases/CASE-L08")
        self.open_action(page, "metadata")
        page.select_option("#form-metadata select[name=field]", "reporter_reference")
        page.fill("#form-metadata input[name=new_value]", "DEMO-REF-BROWSER")
        page.fill("#form-metadata textarea[name=reason]", "Reference corrected from the letterhead.")
        page.click("#form-metadata button[type=submit]")
        page.wait_for_selector("#flash")
        row = page.locator("#metadata-table tr.metadata-edit[data-field='reporter_reference']").last
        self.assertEqual(row.locator("td.user").inner_text(), "analyst")
        self.assertEqual(row.locator("td.new-value").inner_text(), "DEMO-REF-BROWSER")
        self.shot(page, "05-metadata-edit.png", selector="#metadata-edits")


if __name__ == "__main__":
    unittest.main()
