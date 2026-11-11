"""Cosmos DB store, audit chain and job queue against FAKE Cosmos DB clients (no network, no real package).

Contract: the Cosmos DB store behaves like the SQLite store for every operation the pipeline uses; the audit
container is written with create_item only and the chain survives concurrent writers (409 -> next seq); web and
worker processes share counters, jobs and alert claims safely; the full golden flow on the Azure adapters gives
the same results as offline.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
import time
import unittest
from collections import Counter

from pms import audit as A
from pms import runtime as rt
from pms.audit import AuditError
from pms.config import local_test_settings
from pms.golden import (
    GROUPING_FIELDS, LABEL_FIELDS, RULE_FIELDS, SEED_FIELDS, _compare, _letter_actual, _letter_expected,
)
from pms.golden_e2e import _scripted_review
from pms.governance import audit_completeness, audit_quality
from pms.models import Actor, FixedClock
from pms.pipeline import Pipeline
from pms.reports import build_case_report
from pms.store import NotFound, Store
from pms.telemetry import Telemetry

from ._pipeline_support import cleanup_runs, work_dir
from ._support import DATA
from .test_azure_fakes import (
    FakeAzureTestCase, FakeCosmosAccount, cosmos_account_for, patched_modules, reset_fakes,
)

AT = "2026-11-16T08:00:00Z"


def tearDownModule() -> None:
    cleanup_runs()


def cosmos_store(account: FakeCosmosAccount | None = None, *, clock=None, **kwargs):
    from pms.azure.cosmos_store import CosmosStore

    return CosmosStore(cosmos_account_for(account or FakeCosmosAccount()), clock=clock or FixedClock(AT), **kwargs)


def exercise(store) -> dict:
    """Every repository operation of the pipeline, workflow and views; returns a comparable snapshot."""
    out: dict = {}
    clock = store.clock
    c1 = store.insert_case(case_id="CASE-A", case_key="CASE-A", status="EINGEGANGEN", severity="UNKNOWN")
    store.insert_case(case_id="CASE-B", case_key="CASE-B", status="TRIAGE_ERFORDERLICH", critical=True,
                      severity="DEATH", urgency={"level": "critical"}, rule_sets=["EU-MDR"],
                      metadata={"note": "x", "n": 1.5}, deadline_waiver=None, content_version=3)
    out["defaults"] = dataclasses.asdict(c1)
    clock.set("2026-11-16T09:00:00Z")
    out["update_case"] = dataclasses.asdict(store.update_case("CASE-A", critical=1, metadata={"k": [1, 2]},
                                                              analysis_state="DONE"))
    out["get_case"] = dataclasses.asdict(store.get_case("CASE-B"))
    out["find"] = (store.find_case_by_key("CASE-B").case_id, store.find_case_by_key("nope"),
                   store.case_key_available("CASE-A"), store.case_key_available("CASE-C"))
    out["list_cases"] = [c.case_id for c in store.list_cases()]
    out["list_critical"] = [c.case_id for c in store.list_cases(critical=True)]
    out["list_status"] = [c.case_id for c in store.list_cases(status="TRIAGE_ERFORDERLICH")]
    s1 = store.insert_signal(case_id="CASE-A", ref="L01", source="upload", filename=None,
                             received_at="2026-11-16T08:15:00Z", text="t1", text_sha256="h1", readable=True,
                             crit={"severity": "DEATH"}, extraction={"models": [{"value": "OA-S5"}]})
    store.insert_signal(case_id="CASE-B", ref="L02", source="seed", filename="b.txt",
                        received_at="2026-11-16T07:00:00Z", text="t2", text_sha256="h2", readable=False)
    store.insert_signal(case_id="CASE-A", ref="L03", source="upload", filename="c.txt",
                        received_at="2026-11-16T06:00:00Z", text="t3", text_sha256="h3", readable=True)
    out["signal"] = dataclasses.asdict(store.update_signal(s1.signal_id, deadline_not_reset=True))
    out["signals_all"] = [s.signal_id for s in store.list_signals()]
    out["signals_a"] = [dataclasses.asdict(s) for s in store.list_signals("CASE-A")]
    out["get_signal"] = dataclasses.asdict(store.get_signal("SIG-00002"))
    values = {"rule_set": "EU-MDR", "basis": "DEATH", "days": 10, "anchor_utc": "2026-11-16T08:15:00Z",
              "awareness_local_date": "2026-11-16", "start_date": "2026-11-17", "due_date": "2026-11-26",
              "due_end_local": "2026-11-26T23:59:59", "due_end_utc": "2026-11-26T22:59:59Z",
              "origin_signal_id": s1.signal_id, "sources": ["DL-1.0"]}
    d1 = store.upsert_deadline("CASE-A", 0, dict(values))
    store.upsert_deadline("CASE-A", 1, {**values, "basis": "SERIOUS_DETERIORATION", "days": 15})
    store.upsert_deadline("CASE-A", 0, {**values, "due_date": "2026-11-25", "origin_signal_id": "SIG-x"})
    store.update_deadline(d1.deadline_id, superseded={"by": "reviewer"})
    out["deadlines"] = [dataclasses.asdict(d) for d in store.list_deadlines("CASE-A")]
    t1 = store.insert_task(case_id="CASE-B", kind="TRIAGE", title="Triage", created_by="rule")
    store.insert_task(case_id=None, kind="UNREADABLE", title="global", created_by="rule", detail={"a": 1})
    store.update_task(t1.task_id, status="DONE", closed_by="analyst", closed_at="2026-11-16T09:30:00Z")
    out["tasks"] = [dataclasses.asdict(t) for t in store.list_tasks()]
    out["tasks_filtered"] = ([t.task_id for t in store.list_tasks(case_id="CASE-B", status="DONE", kind="TRIAGE")],
                             [t.task_id for t in store.list_tasks(status="OPEN")], dataclasses.asdict(
                                 store.get_task(t1.task_id)))
    a1 = store.insert_alert(case_id="CASE-B", reason="CRIT-1.0")
    store.insert_alert(case_id="CASE-A", reason="window")
    store.update_alert(a1.alert_id, status="SENT", channel="outbox", sent_at="2026-11-16T09:01:00Z",
                       detail={"latency_s": 0.0, "attempts": 1})
    out["alerts"] = [dataclasses.asdict(a) for a in store.list_alerts()]
    out["alerts_pending"] = [a.alert_id for a in store.list_alerts(status="PENDING")]
    store.insert_metadata_edit(case_id="CASE-A", field="lot", old_value=None, new_value={"v": 1}, reason="r",
                               user_id="u", ts="2026-11-16T09:02:00Z")
    out["edits"] = [dataclasses.asdict(e) for e in store.list_metadata_edits()]
    ap = store.insert_approval(case_id="CASE-A", content_hash="c", content_version=1, reviewer_id="r", reason="ok",
                               ts="2026-11-16T09:03:00Z", valid=True)
    store.insert_approval(case_id="CASE-A", content_hash="d", content_version=2, reviewer_id="r", reason="ok",
                          ts="2026-11-16T09:04:00Z", valid=True)
    store.update_approval(ap.approval_id, valid=False, voided_reason="changed")
    out["approvals"] = [dataclasses.asdict(a) for a in store.list_approvals("CASE-A")]
    out["valid_approval"] = dataclasses.asdict(store.valid_approval("CASE-A"))
    out["no_approval"] = store.valid_approval("CASE-B")
    store.insert_submission(case_id="CASE-A", approval_id="APR-00002", content_hash="d", channel_label="x",
                            reference="REF", submitted_on="2026-11-16", receipt_status="unconfirmed",
                            recorded_by="reg", ts="2026-11-16T09:05:00Z")
    out["submissions"] = [dataclasses.asdict(s) for s in store.list_submissions()]
    for kind in ("approve", "reject", "approve"):
        store.insert_decision(case_id="CASE-A", kind=kind, reason="why", reason_code=None, actor_id="r",
                              status_before="A", status_after="B", ts="2026-11-16T09:06:00Z")
    out["decisions"] = [dataclasses.asdict(d) for d in store.list_decisions(kind="approve")]
    f1 = store.insert_finding(kind="process_finding", ref="PF-1", case_id=None, status="Open", summary="s",
                              content_hash=None, reviewer_id=None, reason=None)
    store.insert_finding(kind="approved_assessment", ref=None, case_id="CASE-A", status="approved", summary="s2",
                         content_hash="c", reviewer_id="r", reason="ok", data={"rec": "REC-1"})
    store.update_finding(f1.finding_id, status="Closed", reason="done")
    out["findings"] = [dataclasses.asdict(f) for f in store.list_findings()]
    out["findings_filtered"] = ([f.finding_id for f in store.list_findings(kind="process_finding", status="Closed")],
                                [f.finding_id for f in store.list_findings(case_id="CASE-A")],
                                dataclasses.asdict(store.get_finding(f1.finding_id)))
    store.insert_cost(case_id="CASE-A", run_id="RUN-00001", agent_id="A1", model="m", input_tokens=10,
                      cached_tokens=0, output_tokens=5, cost_usd=0, price_table_version="p", simulated=True)
    store.insert_cost(case_id="CASE-A", run_id=None, agent_id="A3", model=None, input_tokens=1, cached_tokens=1,
                      output_tokens=1, cost_usd=0.000123, price_table_version=None, simulated=False)
    out["costs"] = [dataclasses.asdict(c) for c in store.list_costs("CASE-A")]
    run = store.insert_agent_run(case_id="CASE-A", signal_id=s1.signal_id, agent_id="A1", status="RUNNING",
                                 model="m", model_version="v", prompt_version="p", agent_version="a",
                                 started_at="2026-11-16T09:07:00Z", finished_at=None, output=None, error=None,
                                 usage=None)
    store.update_agent_run(run.run_id, status="DONE", output={"facts": [{"name": "lot", "value": None}]},
                           usage={"input": 1})
    out["runs"] = [dataclasses.asdict(r) for r in store.list_agent_runs("CASE-A")]
    out["get_run"] = dataclasses.asdict(store.get_agent_run(run.run_id))
    for version in (1, 2):
        store.insert_recommendation(case_id="CASE-A", version=version, content={"causality": "possible", "v": version},
                                    content_hash=f"h{version}", run_id=run.run_id)
    out["recs"] = [dataclasses.asdict(r) for r in store.list_recommendations("CASE-A")]
    out["latest_rec"] = dataclasses.asdict(store.latest_recommendation("CASE-A"))
    out["no_rec"] = store.latest_recommendation("CASE-B")
    store.insert_fact(case_id="CASE-A", signal_id=s1.signal_id, name="lot", value=["A", "B"], quote="q", line=3,
                      verified=True, origin="rule")
    store.insert_fact(case_id="CASE-A", signal_id=None, name="event", value=None, quote=None, line=None,
                      verified=False, origin="agent")
    out["facts"] = [dataclasses.asdict(f) for f in store.list_facts("CASE-A")]
    store.insert_correlation(case_id="CASE-A", signal_id=s1.signal_id, result={"hits": [{"ref": "LIT-001"}]},
                             corpus_version="1.0")
    out["correlations"] = [dataclasses.asdict(c) for c in store.list_correlations(case_id="CASE-A",
                                                                                   signal_id=s1.signal_id)]
    out["meta"] = (store.get_meta("missing", "dflt"), store.set_meta("k", "v1"), store.set_meta("k", "v2"),
                   store.get_meta("k"), store.get_meta("schema_version"))
    out["ids"] = [store.next_id("XYZ") for _ in range(3)]
    out["now"] = store.now()
    errors = {}
    for name, call in {
        "get_unknown": lambda: store.get_case("nope"),
        "update_unknown": lambda: store.update_task("TASK-99999", status="DONE"),
        "get_signal_unknown": lambda: store.get_signal("SIG-99999"),
        "insert_unknown_column": lambda: store.insert_task(case_id="CASE-A", kind="X", title="t", created_by="x",
                                                           bogus=1),
        "update_pk": lambda: store._update("cases", "CASE-A", {"case_id": "other"}),
        "list_unknown_filter": lambda: store._list("cases", {"bogus": 1}),
        "missing_not_null": lambda: store.insert_decision(case_id="CASE-A", kind="x", actor_id="r", status_before="a",
                                                          status_after="b", ts="t"),
        "duplicate_case": lambda: store.insert_case(case_id="CASE-A", case_key="CASE-A2", status="x", severity="y"),
    }.items():
        try:
            call()
            errors[name] = None
        except NotFound:
            errors[name] = "NotFound"
        except (ValueError, sqlite3.IntegrityError):
            errors[name] = "rejected"
    out["errors"] = errors
    with store.transaction():
        with store.transaction():
            store.set_meta("nested", "ok")
    out["nested"] = store.get_meta("nested")
    return out


class StoreContract(FakeAzureTestCase):
    """The same operations give the same results on SQLite and on Cosmos DB."""

    def test_every_operation_matches_the_sqlite_store(self) -> None:
        sqlite_result = exercise(Store(":memory:", clock=FixedClock(AT)))
        cosmos_result = exercise(cosmos_store())
        self.assertEqual(set(sqlite_result), set(cosmos_result))
        for key in sqlite_result:
            with self.subTest(key):
                self.assertEqual(cosmos_result[key], sqlite_result[key])
        self.assertEqual(sqlite_result["errors"], {
            "get_unknown": "NotFound", "update_unknown": "NotFound", "get_signal_unknown": "NotFound",
            "insert_unknown_column": "rejected", "update_pk": "rejected", "list_unknown_filter": "rejected",
            "missing_not_null": "rejected", "duplicate_case": "rejected"})

    def test_document_layout_follows_the_contract(self) -> None:
        account = FakeCosmosAccount()
        store = cosmos_store(account)
        store.insert_case(case_id="CASE-A", case_key="CASE-A", status="EINGEGANGEN", severity="UNKNOWN")
        store.insert_signal(case_id="CASE-A", ref="L01", source="upload", received_at=AT, text="t",
                            text_sha256="h", readable=True)
        store.insert_task(case_id="CASE-A", kind="TRIAGE", title="t", created_by="rule")
        store.insert_finding(kind="process_finding", ref="PF", case_id=None, status="Open", summary="s",
                             content_hash=None, reviewer_id=None, reason=None)
        case = account.containers["cases"].raw_doc("CASE-A", "CASE-A")
        signal = account.containers["signals"].raw_doc("SIG-00001", "CASE-A")
        task = account.containers["records"].raw_doc("tasks:TASK-00001", "CASE-A")
        finding = account.containers["records"].raw_doc("findings:FND-00001", "global")
        self.assertEqual((case["pk"], case["type"]), ("CASE-A", "cases"))
        self.assertEqual((signal["pk"], signal["case_id"]), ("CASE-A", "CASE-A"))
        self.assertEqual((task["type"], task["task_id"]), ("tasks", "TASK-00001"))
        self.assertEqual(finding["pk"], "global")
        counter = account.containers["records"].raw_doc("counter:TASK", "counters")
        self.assertEqual(counter["value"], 1)

    def test_ids_with_reserved_characters_are_encoded(self) -> None:
        from pms.azure.cosmos_store import safe_id

        self.assertEqual(safe_id("a/b?c#d\\e%"), "a%2Fb%3Fc%23d%5Ce%25")
        store = cosmos_store()
        store.insert_case(case_id="CASE-x/y#1", case_key="CASE-x/y#1", status="EINGEGANGEN", severity="UNKNOWN")
        self.assertEqual(store.get_case("CASE-x/y#1").case_key, "CASE-x/y#1")

    def test_missing_container_or_permission_fails_with_a_plain_message(self) -> None:
        from pms.azure.cosmos_store import CosmosStoreError

        account = FakeCosmosAccount()
        account.containers["records"].exists = False
        with self.assertRaisesRegex(CosmosStoreError, "container 'records' does not exist.*cannot create"):
            cosmos_store(account)
        account = FakeCosmosAccount()
        account.containers["audit"].forbidden = True
        with self.assertRaisesRegex(CosmosStoreError, "no data-plane access.*'audit'"):
            cosmos_store(account)

    def test_sql_is_refused_but_the_transaction_flag_works(self) -> None:
        from pms.azure.cosmos_store import CosmosStoreError

        store = cosmos_store()
        self.assertFalse(store.conn.in_transaction)
        with store.transaction():
            self.assertTrue(store.conn.in_transaction)
        with self.assertRaises(CosmosStoreError):
            store.conn.execute("SELECT 1")
        with self.assertRaises(NotImplementedError):
            rt.snapshot_store(store)


class AuditChain(FakeAzureTestCase):
    """Hash-chained, append-only audit with create_item only (pk 'audit-chain', id = seq)."""

    RECORDS = [
        dict(event_type=A.CASE_CREATED, source="rule", actor_type="rule", actor_id="LINK-1.0", case_id="C1",
             rule_versions=["LINK-1.0"], after={"status": "EINGEGANGEN"}),
        dict(event_type=A.DECISION, source="ui", actor_type="human", actor_id="reviewer", case_id="C1",
             reason="  approved  ", before={"status": "A"}, after={"decision": "approve", "x": 1.0}),
        dict(event_type=A.AGENT_RUN, source="agent", actor_type="agent", actor_id="A1", case_id="C2",
             agent_version="a", model="m", model_version="v", prompt_version="p", usage={"input": 3},
             cost_estimate={"usd": 0.5}),
        dict(event_type=A.STATUS_CHANGED, source="system", actor_type="system", actor_id="system", case_id="C1",
             before={"status": "A"}, after={"status": "B"}),
        dict(event_type=A.CORRELATION_RUN, source="rule", actor_type="rule", actor_id="CORR-1.0",
             rule_versions=["CORR-1.0"], corpus_version="1.0", source_ids=["LIT-001"], case_id="C2",
             signal_id="SIG-1"),
    ]

    def fill(self, store) -> list[dict]:
        out = []
        for i, rec in enumerate(self.RECORDS):
            out.append(store.audit.append(**rec, ts=f"2026-11-16T08:0{i}:00Z"))
        return out

    def test_same_records_reads_and_chain_check_as_sqlite(self) -> None:
        sqlite_store = Store(":memory:", clock=FixedClock(AT))
        account = FakeCosmosAccount()
        store = cosmos_store(account)
        mask = lambda r: {k: v for k, v in r.items() if k not in ("record_id", "hash", "prev_hash")}  # noqa: E731
        appended = self.fill(store)
        self.assertEqual([mask(r) for r in appended], [mask(r) for r in self.fill(sqlite_store)])
        self.assertEqual([r["seq"] for r in appended], [1, 2, 3, 4, 5])
        for kwargs in ({}, {"case_id": "C1"}, {"event_type": A.DECISION}, {"after_seq": 2}, {"limit": 2},
                       {"case_id": "C1", "after_seq": 1, "limit": 1}):
            with self.subTest(kwargs):
                self.assertEqual([mask(r) for r in store.audit.records(**kwargs)],
                                 [mask(r) for r in sqlite_store.audit.records(**kwargs)])
        self.assertEqual((store.audit.count(), store.audit.count(case_id="C1"), store.audit.count(event_type="X")),
                         (5, 3, 0))
        self.assertEqual(store.audit.head(), appended[-1]["hash"])
        result = store.audit.verify()
        self.assertEqual((result.ok, result.checked, result.head_hash), (True, 5, appended[-1]["hash"]))
        doc = account.containers["audit"].raw_doc("2", "audit-chain")
        self.assertEqual((doc["id"], doc["pk"], doc["seq"]), ("2", "audit-chain", 2))
        self.assertEqual(json.loads(doc["record_json"])["reason"], "approved")
        with self.assertRaises(AuditError):
            store.audit.append(event_type=A.DECISION, source="ui", actor_type="human", actor_id="r")

    def test_tampering_is_detected_like_in_sqlite(self) -> None:
        cases = {
            "content": (lambda doc: doc.update(record_json=json.dumps({**json.loads(doc["record_json"]),
                                                                       "reason": "edited"}, sort_keys=True)),
                        "UPDATE audit SET record_json = json_set(record_json, '$.reason', 'edited') WHERE seq = 3",
                        "hash does not match the record content"),
            "column": (lambda doc: doc.update(actor_id="someone else"),
                       "UPDATE audit SET actor_id = 'someone else' WHERE seq = 3",
                       "column actor_id differs from the hashed record"),
        }
        for name, (edit_doc, sql, reason) in cases.items():
            with self.subTest(name):
                sqlite_store = Store(":memory:", clock=FixedClock(AT))
                self.fill(sqlite_store)
                sqlite_store.conn.execute("DROP TRIGGER audit_no_update")
                sqlite_store.conn.execute(sql)
                expected = sqlite_store.audit.verify()
                account = FakeCosmosAccount()
                store = cosmos_store(account)
                self.fill(store)
                account.containers["audit"].tamper("3", "audit-chain", edit_doc)
                result = store.audit.verify()
                self.assertEqual((result.ok, result.first_break_seq, result.reason),
                                 (expected.ok, expected.first_break_seq, expected.reason))
                self.assertEqual((result.ok, result.first_break_seq, result.reason), (False, 3, reason))
        account = FakeCosmosAccount()
        store = cosmos_store(account)
        self.fill(store)
        del account.containers["audit"].partitions["audit-chain"]["2"]
        self.assertEqual(store.audit.verify().reason, "sequence gap: expected 2, found 3")

    def test_the_app_only_creates_audit_documents_and_the_role_refuses_updates(self) -> None:
        account = FakeCosmosAccount()
        store = cosmos_store(account)
        self.fill(store)
        store.audit.records()
        store.audit.verify()
        self.assertEqual(set(account.ops_on("audit")), {"read_container", "create", "query"})
        tamper = rt.audit_tamper_test(store)
        self.assertTrue(tamper["detected"], tamper)
        self.assertEqual((tamper["edited_seq"], tamper["first_break_seq"]), (2, 2))
        self.assertTrue(tamper["update_blocked_by_rbac"]["blocked"])
        self.assertIn("403", tamper["update_blocked_by_rbac"]["replace"])
        self.assertTrue(store.audit.verify().ok)  # the real chain is unchanged
        self.assertEqual(account.ops_on("audit")["replace"], 1)  # the one refused probe, nothing else

    def test_concurrent_writers_retry_with_the_next_sequence_number(self) -> None:
        account = FakeCosmosAccount()
        web, worker = cosmos_store(account), cosmos_store(account)
        web.audit.append(**self.RECORDS[0])
        worker.audit.append(**self.RECORDS[3])
        worker.audit.append(**self.RECORDS[3])
        third = web.audit.append(**self.RECORDS[1])  # web's cached head is stale: 409 on seq 2, then seq 4
        self.assertEqual(third["seq"], 4)
        self.assertGreaterEqual(web.audit.conflicts, 1)
        threads = [threading.Thread(target=lambda s=s: [s.audit.append(**self.RECORDS[3]) for _ in range(15)])
                   for s in (web, worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        result = web.audit.verify()
        self.assertEqual((result.ok, result.checked), (True, 34))
        self.assertEqual(sorted(d["seq"] for d in account.containers["audit"].all_docs()), list(range(1, 35)))

    def test_a_retried_create_that_had_succeeded_is_not_written_twice(self) -> None:
        account = FakeCosmosAccount()
        store = cosmos_store(account)
        store.audit.append(**self.RECORDS[0])
        state = {"done": False}

        def lost_response(container, info):  # the service stored the record, the answer got lost, the SDK retried
            if not state["done"]:
                state["done"] = True
                container.create_item(body=info["body"])

        account.hooks[("audit", "create")] = lost_response
        rec = store.audit.append(**self.RECORDS[3])
        account.hooks.clear()
        self.assertEqual(rec["seq"], 2)
        self.assertEqual(store.audit.count(), 2)
        self.assertTrue(store.audit.verify().ok)


class SharedStoreConcurrency(FakeAzureTestCase):
    """Web and worker container: two store objects on one account."""

    def setUp(self) -> None:
        super().setUp()
        self.account = FakeCosmosAccount()
        self.web, self.worker = cosmos_store(self.account), cosmos_store(self.account)

    def test_transactions_are_serialised_across_processes(self) -> None:
        inside, release, order = threading.Event(), threading.Event(), []

        def web_tx() -> None:
            with self.web.transaction():
                order.append("web-in")
                inside.set()
                release.wait(5)
                order.append("web-out")

        def worker_tx() -> None:
            inside.wait(5)
            with self.worker.transaction():
                order.append("worker-in")

        threads = [threading.Thread(target=web_tx), threading.Thread(target=worker_tx)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        self.assertEqual(order, ["web-in"])  # the worker waits for the lease
        release.set()
        for t in threads:
            t.join(5)
        self.assertEqual(order, ["web-in", "web-out", "worker-in"])
        self.assertGreater(self.worker.lease.waits, 0)
        self.assertEqual(self.account.containers["records"].partitions.get("leases"), {})

    def test_expired_lease_of_a_crashed_process_is_taken_over_and_a_live_one_is_not(self) -> None:
        from pms.azure.cosmos_store import StoreBusy

        records = self.account.containers["records"]
        records.create_item(body={"id": "lease:store", "pk": "leases", "owner": "crashed", "expires": time.time() - 1})
        with self.worker.transaction():
            pass
        self.assertEqual(self.worker.lease.takeovers, 1)
        records.create_item(body={"id": "lease:store", "pk": "leases", "owner": "busy", "expires": time.time() + 60})
        impatient = cosmos_store(self.account, lease_timeout_s=0.2)
        with self.assertRaises(StoreBusy):
            with impatient.transaction():
                pass

    def test_optimistic_concurrency_keeps_both_changes(self) -> None:
        self.web.insert_case(case_id="CASE-A", case_key="CASE-A", status="EINGEGANGEN", severity="UNKNOWN")
        state = {"done": False}

        def concurrent_writer(container, info):
            if not state["done"] and info.get("id") == "CASE-A":
                state["done"] = True
                self.worker.update_case("CASE-A", analysis_state="RUNNING", updated_at="w")

        self.account.hooks[("cases", "replace")] = concurrent_writer
        case = self.web.update_case("CASE-A", status="IN_ANALYSE", updated_at="x")
        self.account.hooks.clear()
        self.assertEqual((case.status, case.analysis_state), ("IN_ANALYSE", "RUNNING"))
        self.assertGreaterEqual(self.web.conflicts, 1)

    def test_counters_stay_unique(self) -> None:
        ids = []
        for i in range(10):
            ids.append((self.web if i % 3 else self.worker).next_id("SIG"))
        self.assertEqual(ids, [f"SIG-{n:05d}" for n in range(1, 11)])
        threads = [threading.Thread(target=lambda s=s: [ids.append(s.next_id("SIG")) for _ in range(20)])
                   for s in (self.web, self.worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(ids)), 50)

    def test_jobs_are_claimed_once_and_recovered(self) -> None:
        from pms.azure.cosmos_store import CosmosJobQueue
        import datetime as dt

        web_q, worker_q = CosmosJobQueue(self.web), CosmosJobQueue(self.worker)
        first = web_q.enqueue("CASE-A", "SIG-1", reason="new")
        self.assertEqual(web_q.enqueue("CASE-A", "SIG-1", reason="again").job_id, first.job_id)
        web_q.enqueue("CASE-B", "SIG-2", reason="new")
        self.web.clock.set("2026-11-16T08:00:01Z")
        web_q.enqueue("CASE-A", "SIG-3", reason="new")
        self.assertEqual(worker_q.pending_count(), 3)
        state = {"done": False}

        def other_worker_claims_first(container, info):
            if not state["done"] and str(info.get("id", "")).startswith("analysis_jobs:"):
                state["done"] = True
                web_q.claim_next("web-thread")

        self.account.hooks[("records", "replace")] = other_worker_claims_first
        claimed = worker_q.claim_next("worker-1")
        self.account.hooks.clear()
        third = worker_q.claim_next("worker-1")
        self.assertIsNone(worker_q.claim_next("worker-1"))
        jobs = {j.job_id: j for j in worker_q.list()}
        self.assertEqual(Counter(j.status for j in jobs.values()), Counter({"RUNNING": 3}))
        self.assertEqual(Counter(j.worker for j in jobs.values()), Counter({"worker-1": 2, "web-thread": 1}))
        self.assertNotEqual(claimed.job_id, third.job_id)
        worker_q.update_progress(claimed.job_id, {"A1": {"status": "DONE", "ms": 1.0}}, rounds=2)
        done = worker_q.finish(claimed.job_id, "DONE")
        self.assertEqual((done.status, done.rounds, done.progress["A1"]["status"]), ("DONE", 2, "DONE"))
        worker_q.update_progress("JOB-99999", {})  # unknown job: no change, no error (like SQLite)
        with self.assertRaises(KeyError):
            worker_q.get("JOB-99999")
        with self.assertRaises(ValueError):
            worker_q.finish(third.job_id, "WHATEVER")
        self.worker.clock.set("2026-11-16T09:00:00Z")
        self.assertEqual(worker_q.recover_stale(dt.timedelta(minutes=15)), 2)
        self.assertEqual(sorted(j.status for j in worker_q.list()), ["DONE", "QUEUED", "QUEUED"])
        self.assertEqual(worker_q.get(claimed.job_id).status, "DONE")
        self.assertIn("re-queued", worker_q.list(status="QUEUED")[0].error)

    def test_only_one_process_claims_an_alert_delivery(self) -> None:
        alert = self.web.insert_alert(case_id="CASE-A", reason="CRIT")
        self.assertTrue(self.web.claim_alert(alert.alert_id, "web", 45))
        self.assertTrue(self.web.claim_alert(alert.alert_id, "web", 45))  # the owner may renew
        self.assertFalse(self.worker.claim_alert(alert.alert_id, "worker", 45))
        self.web.update_alert(alert.alert_id, status="FAILED", sent_at=AT, detail={"attempts": 1})
        self.assertFalse(self.worker.claim_alert(alert.alert_id, "worker", 45))  # retry pause 5 s
        self.worker.clock.set("2026-11-16T08:00:06Z")
        self.assertTrue(self.worker.claim_alert(alert.alert_id, "worker", 45))
        self.web.update_alert(alert.alert_id, status="SENT")
        self.assertFalse(self.web.claim_alert(alert.alert_id, "web", 45))
        self.assertFalse(self.web.claim_alert("ALR-99999", "web", 45))
        crashed = self.web.insert_alert(case_id="CASE-A", reason="CRIT")
        self.assertTrue(self.web.claim_alert(crashed.alert_id, "web", 0.05))  # the web container dies meanwhile
        self.assertFalse(self.worker.claim_alert(crashed.alert_id, "worker", 30))
        time.sleep(0.1)
        self.assertTrue(self.worker.claim_alert(crashed.alert_id, "worker", 30))  # expired claim: worker takes over


def run_golden_flow(store, search, jobs=None):
    """The golden e2e flow (letters and seeds in received_at order, analysis, scripted human steps)."""
    settings = local_test_settings(work_dir("p3-golden"), synthetic_data_dir=DATA)
    clock = store.clock
    pipeline = Pipeline(settings, store=store, search=search, jobs=jobs, clock=clock, files=rt.MemoryFileStore(),
                        telemetry=Telemetry(trace_path=None))
    items = pipeline.demo_items()
    clock.set(items[0].received_at)
    pipeline.workflow.import_process_findings(pipeline.ref.process_findings(), Actor.system(), source="seed")
    labels, results = {}, {}
    for item in items:
        clock.set(item.received_at)
        result = pipeline.ingest_item(item)
        pipeline.drain()
        labels[item.ref] = pipeline.engine.case_labels(result.case_id, result.signal_id)
        results[item.ref] = result
    steps = _scripted_review(pipeline, items[-1].received_at)
    return pipeline, items, labels, results, steps


def table_snapshot(store) -> dict:
    def rows(items, drop=()):
        return [{k: v for k, v in dataclasses.asdict(x).items() if k not in drop} for x in items]

    cases = store.list_cases()
    return {
        "cases": rows(cases),
        "signals": rows(store.list_signals()),
        "deadlines": [rows(store.list_deadlines(c.case_id)) for c in cases],
        "tasks": rows(store.list_tasks()),
        "alerts": rows(store.list_alerts(), drop=("detail",)),
        "edits": rows(store.list_metadata_edits()),
        "approvals": rows(store.list_approvals(), drop=("content_hash",)),
        "submissions": rows(store.list_submissions(), drop=("content_hash",)),
        "decisions": rows(store.list_decisions()),
        "findings": rows(store.list_findings(), drop=("content_hash",)),
        "facts": [rows(store.list_facts(c.case_id)) for c in cases],
        "correlations": rows(store.list_correlations()),
        "runs": [[(r.run_id, r.case_id, r.signal_id, r.agent_id, r.status, r.model, r.prompt_version)
                  for r in store.list_agent_runs(c.case_id)] for c in cases],
        "costs": [rows(store.list_costs(c.case_id)) for c in cases],
        "recommendations": [[(r.rec_id, r.version, r.draft, r.run_id) for r in store.list_recommendations(c.case_id)]
                            for c in cases],
    }


class GoldenFlowOnTheAzureAdapters(unittest.TestCase):
    """Cosmos DB store + job queue + Azure AI Search adapter (fakes) give exactly the offline golden results."""

    @classmethod
    def setUpClass(cls) -> None:
        reset_fakes()
        from pms.azure.cosmos_store import CosmosJobQueue
        from pms.azure.search import AzureKnowledgeSearch

        offline_store = Store(":memory:", clock=FixedClock("2026-01-01T00:00:00Z"))
        cls.offline = run_golden_flow(offline_store, None)
        cls.account = FakeCosmosAccount()
        with patched_modules():
            store = cosmos_store(cls.account, clock=FixedClock("2026-01-01T00:00:00Z"))
            search = AzureKnowledgeSearch(cls.offline[0].ref, endpoint="https://fake-search.search.windows.net",
                                          index_name="pms-knowledge", credential_factory=lambda: None)
            cls.azure = run_golden_flow(store, search, CosmosJobQueue(store))
            cls.reports = {c.case_id: build_case_report(cls.azure[0], c.case_id) for c in store.list_cases()}
            cls.tamper = rt.audit_tamper_test(store)

    def test_every_rule_based_field_matches_expected_json(self) -> None:
        expected = json.loads((DATA / "expected.json").read_text(encoding="utf-8"))
        letters = {x["id"]: x for x in expected["letters"]}
        seeds = {x["message_id"]: x for x in expected["seeds"]}
        _, items, labels, _, _ = self.azure
        compared = matched = 0
        mismatches = []
        for item in items:
            snapshot = labels[item.ref]
            if item.kind == "letter":
                exp, act = _letter_expected(letters[item.ref]), _letter_actual(snapshot)
                fields = [(f, exp[f], act[f]) for f in LABEL_FIELDS + RULE_FIELDS + GROUPING_FIELDS]
            else:
                act = {"linking": {"case_id": snapshot["case_key"], "relation": snapshot["linking"],
                                   "awareness_anchor": snapshot["awareness_anchor"]},
                       "completeness": snapshot["completeness"]}
                fields = [(f, seeds[item.ref]["labels"][f]["value"], act[f]) for f in SEED_FIELDS]
            for name, exp_value, act_value in fields:
                compared += 1
                ok, _detail = _compare(name, exp_value, act_value)
                matched += ok
                if not ok:
                    mismatches.append((item.ref, name))
        self.assertEqual((compared, matched, mismatches), (228, 228, []))

    def test_same_labels_tables_and_audit_steps_as_offline(self) -> None:
        self.assertEqual(self.azure[2], self.offline[2])
        self.assertEqual(self.azure[4], self.offline[4])
        self.assertEqual(table_snapshot(self.azure[0].store), table_snapshot(self.offline[0].store))
        azure_counts = Counter(r["event_type"] for r in self.azure[0].store.audit.records())
        offline_counts = Counter(r["event_type"] for r in self.offline[0].store.audit.records())
        self.assertEqual(azure_counts, offline_counts)
        self.assertEqual({j.status for j in self.azure[0].jobs.list()}, {"DONE"})

    def test_audit_is_complete_verifiable_and_tamper_evident(self) -> None:
        store = self.azure[0].store
        self.assertTrue(store.audit.verify().ok)
        self.assertEqual(audit_quality(store)["missing_fields"], 0)
        completeness = audit_completeness(store)
        self.assertTrue(completeness["ok"], completeness)
        self.assertTrue(self.tamper["detected"])
        self.assertTrue(self.tamper["update_blocked_by_rbac"]["blocked"])
        self.assertEqual(set(self.account.ops_on("audit")) - {"replace"}, {"read_container", "create", "query"})

    def test_every_case_report_is_complete(self) -> None:
        incomplete = {k: r["completeness"]["missing"] for k, r in self.reports.items()
                      if not r["completeness"]["complete"]}
        self.assertEqual(incomplete, {})
        self.assertEqual(len(self.reports), 14)


if __name__ == "__main__":
    unittest.main()
