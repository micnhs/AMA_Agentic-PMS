"""Audit trail: all fields, append-only triggers, hash chain verification and tampering."""
from __future__ import annotations

import sqlite3
import unittest

from pms import audit as A
from pms.models import FixedClock
from pms.store import Store

from ._support import new_engine, process


def fresh_store() -> Store:
    return Store(":memory:", clock=FixedClock("2026-11-16T08:00:00Z"))


def append_some(store: Store, n: int = 3) -> None:
    for i in range(n):
        store.audit.append(event_type=A.RULE_EVALUATED, source="rule", actor_type="rule", actor_id="CRIT-1.0",
                           rule_versions=["CRIT-1.0"], case_id="CASE-X", after={"i": i})


class AuditChain(unittest.TestCase):
    def test_every_record_has_all_fields_and_the_chain_verifies(self) -> None:
        store, engine, clock = new_engine()
        process(engine, clock, "L01")
        process(engine, clock, "L02")
        records = store.audit.records()
        self.assertGreater(len(records), 10)
        for rec in records:
            self.assertEqual(set(rec), set(A.RECORD_FIELDS))
            self.assertTrue(rec["ts_utc"].endswith("Z") and len(rec["ts_utc"]) == 24)  # ISO 8601 with ms
            if rec["actor_type"] == "rule":
                self.assertTrue(rec["rule_versions"])
        result = store.audit.verify()
        self.assertTrue(result.ok, result)
        self.assertEqual(result.checked, len(records))
        self.assertEqual(records[0]["prev_hash"], A.GENESIS_HASH)
        self.assertEqual(records[1]["prev_hash"], records[0]["hash"])

    def test_update_and_delete_are_blocked_by_triggers(self) -> None:
        store = fresh_store()
        append_some(store)
        with self.assertRaises(sqlite3.DatabaseError):
            store.conn.execute("UPDATE audit SET actor_id = 'someone else' WHERE seq = 2")
        with self.assertRaises(sqlite3.DatabaseError):
            store.conn.execute("DELETE FROM audit WHERE seq = 3")
        self.assertTrue(store.audit.verify().ok)
        self.assertEqual(store.audit.count(), 3)

    def tampered(self, statement: str) -> A.VerifyResult:
        store = fresh_store()
        append_some(store, 4)
        self.assertTrue(store.audit.verify().ok)
        # Simulate an attacker with direct database access who removes the protection.
        store.conn.execute("DROP TRIGGER audit_no_update")
        store.conn.execute("DROP TRIGGER audit_no_delete")
        store.conn.execute(statement)
        return store.audit.verify()

    def test_editing_one_record_breaks_the_chain(self) -> None:
        result = self.tampered("UPDATE audit SET record_json = replace(record_json, '\"i\":1', '\"i\":9') WHERE seq = 2")
        self.assertFalse(result.ok)
        self.assertEqual(result.first_break_seq, 2)

    def test_editing_an_indexed_column_breaks_the_chain(self) -> None:
        result = self.tampered("UPDATE audit SET case_id = 'CASE-Y' WHERE seq = 3")
        self.assertEqual((result.ok, result.first_break_seq), (False, 3))

    def test_rewriting_a_hash_breaks_the_next_link(self) -> None:
        result = self.tampered("UPDATE audit SET hash = 'f' || substr(hash, 2) WHERE seq = 1")
        self.assertFalse(result.ok)
        self.assertIn(result.first_break_seq, (1, 2))

    def test_deleting_a_record_is_detected(self) -> None:
        result = self.tampered("DELETE FROM audit WHERE seq = 2")
        self.assertEqual((result.ok, result.first_break_seq), (False, 3))


class RecordValidation(unittest.TestCase):
    def test_incomplete_records_are_refused(self) -> None:
        store = fresh_store()
        with self.assertRaises(A.AuditError):  # agent without model/prompt versions
            store.audit.append(event_type=A.AGENT_RUN, source="agent", actor_type="agent", actor_id="A3")
        with self.assertRaises(A.AuditError):  # rule without rule versions
            store.audit.append(event_type=A.RULE_EVALUATED, source="rule", actor_type="rule", actor_id="CRIT-1.0")
        with self.assertRaises(A.AuditError):  # human decision without reason
            store.audit.append(event_type=A.DECISION, source="ui", actor_type="human", actor_id="reviewer-1")
        with self.assertRaises(A.AuditError):  # unknown source
            store.audit.append(event_type=A.SIGNAL_ADDED, source="email", actor_type="system", actor_id="system")
        self.assertEqual(store.audit.count(), 0)


if __name__ == "__main__":
    unittest.main()
