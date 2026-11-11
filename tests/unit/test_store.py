"""SQLite store: file persistence across a restart, readable IDs, JSON round trips."""
from __future__ import annotations

import unittest

from pms.models import FixedClock, Status
from pms.rules.engine import RuleEngine
from pms.store import NotFound, Store

from ._pipeline_support import remove_tree, work_dir
from ._support import REVIEWER, ANALYST, knowledge_index, letter_signal, ref_data


class FilePersistence(unittest.TestCase):
    def setUp(self) -> None:
        # A folder of this test only: parallel test runs never share or delete each other's database.
        self.work = work_dir("test-store", create=True)

    def tearDown(self) -> None:
        remove_tree(self.work)

    def test_cases_times_and_decisions_survive_a_restart(self) -> None:
        path = self.work / "pms.sqlite"
        clock = FixedClock("2026-11-16T08:15:00Z")
        store = Store(path, clock=clock)
        engine = RuleEngine(store, ref_data(), knowledge_index(), clock=clock)
        result = engine.process_signal(letter_signal("L01"), now=clock.now())
        engine.workflow.take_triage(result.case_id, ANALYST, now=clock.now())
        engine.workflow.lower_urgency(result.case_id, REVIEWER, "demo: reviewed", now=clock.now())
        head = store.audit.head()
        store.close()

        reopened = Store(path, clock=clock)
        case = reopened.get_case(result.case_id)
        self.assertEqual(case.awareness_anchor, "2026-11-16T08:15:00Z")
        self.assertEqual(case.status, Status.IN_ANALYSE)
        self.assertFalse(case.critical)
        self.assertEqual(case.urgency["lowered"][0]["by"], REVIEWER.actor_id)
        self.assertEqual([d.due_date for d in reopened.list_deadlines(case.case_id)], ["2026-11-26"])
        self.assertEqual(reopened.audit.head(), head)
        self.assertTrue(reopened.audit.verify().ok)
        # triggers are part of the stored schema
        with self.assertRaises(Exception):
            reopened.conn.execute("DELETE FROM audit")
        reopened.close()


class RepositoryApi(unittest.TestCase):
    def test_ids_json_columns_and_not_found(self) -> None:
        store = Store(":memory:", clock=FixedClock("2026-11-16T08:00:00Z"))
        task = store.insert_task(case_id=None, kind="UNMATCHED", title="t", created_by="system",
                                 detail={"nested": [1, 2]})
        self.assertEqual(task.task_id, "TASK-00001")
        self.assertEqual(store.insert_task(case_id=None, kind="X", title="u", created_by="system").task_id,
                         "TASK-00002")
        self.assertEqual(store.get_task(task.task_id).detail, {"nested": [1, 2]})
        with self.assertRaises(NotFound):
            store.get_case("CASE-NOPE")
        with self.assertRaises(ValueError):
            store.update_task(task.task_id, not_a_column=1)


if __name__ == "__main__":
    unittest.main()
