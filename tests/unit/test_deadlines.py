"""DL-1.0: day 1 after awareness, Europe/Dublin calendar, earliest wins, overdue."""
from __future__ import annotations

import datetime as dt
import unittest

from pms.models import UTC, parse_utc
from pms.rules import deadlines as dl

CAL = dl.Calendar()


class DeadlineStart(unittest.TestCase):
    def test_deadline_starts_the_day_after_awareness(self) -> None:
        cand = dl.make_candidate("EU-MDR:DE", dl.DEATH, parse_utc("2026-11-16T08:15:00Z"), CAL)
        self.assertEqual(cand.awareness_local_date, dt.date(2026, 11, 16))
        self.assertEqual(cand.start_date, dt.date(2026, 11, 17))
        self.assertEqual(cand.due_date, dt.date(2026, 11, 26))
        self.assertEqual(cand.due_end_local, "2026-11-26T23:59:59 Europe/Dublin")
        self.assertEqual(cand.due_end_utc, dt.datetime(2026, 11, 26, 23, 59, 59, tzinfo=UTC))
        self.assertEqual(CAL.start_of_day1_utc(cand.start_date), dt.datetime(2026, 11, 17, 0, 0, 1, tzinfo=UTC))

    def test_late_evening_receipt_in_summer_counts_on_the_dublin_date(self) -> None:
        # 23:30 UTC on 1 July is 00:30 Irish Standard Time on 2 July: day 0 = 2 July.
        cand = dl.make_candidate("EU-MDR:IE", dl.PUBLIC_HEALTH_THREAT, parse_utc("2026-07-01T23:30:00Z"), CAL)
        self.assertEqual(cand.awareness_local_date, dt.date(2026, 7, 2))
        self.assertEqual(cand.start_date, dt.date(2026, 7, 3))
        self.assertEqual(cand.due_date, dt.date(2026, 7, 4))
        self.assertEqual(cand.due_end_utc, dt.datetime(2026, 7, 4, 22, 59, 59, tzinfo=UTC))

    def test_no_weekend_extension(self) -> None:
        # Due date falls on Saturday 2026-11-28: it stays there (stricter rule, K-05).
        cand = dl.make_candidate("EU-MDR:DE", dl.PROVISIONAL_REPORTABILITY_UNCLEAR, parse_utc("2026-11-13T10:00:00Z"), CAL)
        self.assertEqual(cand.due_date, dt.date(2026, 11, 28))
        self.assertEqual(cand.due_date.weekday(), 5)

    def test_stdlib_fallback_matches_zoneinfo(self) -> None:
        fallback = dl.Calendar(use_zoneinfo=False)
        self.assertTrue(fallback.uses_fallback)
        moments = ["2026-03-29T00:30:00Z", "2026-03-29T01:30:00Z", "2026-10-25T00:30:00Z", "2026-10-25T01:30:00Z",
                   "2026-07-01T23:30:00Z", "2026-12-31T23:59:59Z", "2027-03-28T00:59:59Z"]
        for m in moments:
            with self.subTest(m):
                self.assertEqual(fallback.local_date(parse_utc(m)), CAL.local_date(parse_utc(m)))
        for day in (dt.date(2026, 3, 28), dt.date(2026, 3, 29), dt.date(2026, 6, 15), dt.date(2026, 10, 24),
                    dt.date(2026, 10, 25), dt.date(2026, 11, 26)):
            with self.subTest(day.isoformat()):
                self.assertEqual(fallback.end_of_day_utc(day), CAL.end_of_day_utc(day))


class Candidates(unittest.TestCase):
    ANCHOR = parse_utc("2026-11-16T10:05:00Z")

    def test_bases_per_indicator_group(self) -> None:
        self.assertEqual(dl.bases_for({dl.DEATH, dl.SERIOUS_DETERIORATION}), [dl.DEATH])
        self.assertEqual(dl.bases_for({dl.PUBLIC_HEALTH_THREAT, dl.SERIOUS_DETERIORATION}),
                         [dl.PUBLIC_HEALTH_THREAT, dl.SERIOUS_DETERIORATION])
        self.assertEqual(dl.bases_for(set()), [dl.PROVISIONAL_REPORTABILITY_UNCLEAR])
        self.assertEqual(dl.DAYS[dl.PUBLIC_HEALTH_THREAT], 2)
        self.assertEqual(dl.DAYS[dl.DEATH], 10)
        self.assertEqual(dl.DAYS[dl.SERIOUS_DETERIORATION], 10)
        self.assertEqual(dl.DAYS[dl.PROVISIONAL_REPORTABILITY_UNCLEAR], 15)

    def test_earliest_deadline_wins(self) -> None:
        cands = dl.candidates_for(["EU-MDR:IE"], {dl.PUBLIC_HEALTH_THREAT, dl.SERIOUS_DETERIORATION}, self.ANCHOR, CAL)
        self.assertEqual([c.days for c in cands], [2, 10])
        drive = dl.driving(cands)
        self.assertEqual((drive.basis, drive.due_date), (dl.PUBLIC_HEALTH_THREAT, dt.date(2026, 11, 18)))

    def test_earliest_wins_across_rule_sets_and_every_deadline_is_shown(self) -> None:
        provisional = dl.candidates_for(["EU-MDR:DE"], set(), self.ANCHOR, CAL)
        serious = dl.candidates_for(["UK-MDR-PART4A:GB", "EU-MDR:NI"], {dl.SERIOUS_DETERIORATION}, self.ANCHOR, CAL)
        merged = dl.merge(provisional, serious)
        self.assertEqual(len(merged), 3)
        self.assertEqual(dl.driving(merged).days, 10)
        self.assertEqual(dl.driving(merged, exclude_keys=[c.key for c in serious]).days, 15)

    def test_overdue_status_and_clock_stop(self) -> None:
        cand = dl.make_candidate("EU-MDR:DE", dl.DEATH, parse_utc("2026-11-16T08:15:00Z"), CAL)
        on_time = dt.datetime(2026, 11, 26, 23, 59, 59, tzinfo=UTC)
        late = on_time + dt.timedelta(seconds=1)
        self.assertEqual(dl.status(cand, on_time), dl.OPEN)
        self.assertEqual(dl.status(cand, late), dl.OVERDUE)
        self.assertEqual(dl.status(cand, late, stopped=True), dl.STOPPED_BY_SUBMISSION)
        self.assertEqual(dl.status(cand, late, waived=True), dl.WAIVED_BY_HUMAN)
        self.assertEqual(dl.seconds_remaining(cand, late), -1)

    def test_merge_never_drops_and_anchor_never_moves_later(self) -> None:
        first = dl.candidates_for(["EU-MDR:DE"], set(), self.ANCHOR, CAL)
        merged = dl.merge(first, dl.candidates_for(["EU-MDR:DE"], {dl.SERIOUS_DETERIORATION}, self.ANCHOR, CAL))
        self.assertEqual([c.basis for c in merged], [dl.PROVISIONAL_REPORTABILITY_UNCLEAR, dl.SERIOUS_DETERIORATION])
        with self.assertRaises(ValueError):
            dl.rebase(merged, self.ANCHOR + dt.timedelta(days=6), CAL)
        earlier = dl.rebase(merged, self.ANCHOR - dt.timedelta(days=1), CAL)
        self.assertEqual(earlier[0].due_date, merged[0].due_date - dt.timedelta(days=1))
        self.assertEqual(dl.earliest_anchor(["2026-11-23T09:00:00Z", "2026-11-17T11:00:00Z"]),
                         parse_utc("2026-11-17T11:00:00Z"))


if __name__ == "__main__":
    unittest.main()
