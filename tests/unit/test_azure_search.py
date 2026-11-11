"""Azure AI Search adapter against a FAKE search service: same hits as the offline FTS5 index for exact filters,
CORR-1.0 parity incl. the golden must-include references, idempotent index sync (no network, no real package)."""
from __future__ import annotations

import json
import re
import unittest

from pms import runtime as rt
from pms.config import Settings
from pms.knowledge import ALLOWED_SOURCE_TYPES, CORPUS_SOURCE_TYPES, InvalidQuery, KnowledgeIndex, SearchQuery
from pms.rules import correlation as corr

from ._support import DATA, ref_data
from .test_azure_fakes import (
    CREDENTIALS, FakeAzureTestCase, FakeHttp, FakeSearchService, HttpResponseError, ManagedIdentityCredential,
    patched_modules,
)

ENDPOINT = "https://fake-search.search.windows.net"
INDEX = "pms-knowledge"


def azure_index(**kwargs):
    from pms.azure.search import AzureKnowledgeSearch

    return AzureKnowledgeSearch(ref_data(), endpoint=ENDPOINT, index_name=INDEX,
                                credential_factory=lambda: ManagedIdentityCredential(client_id="mi"),
                                settle_timeout_s=1.0, **kwargs)


class SearchParity(FakeAzureTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.offline = KnowledgeIndex(ref_data())
        self.azure = azure_index()

    def tearDown(self) -> None:
        self.offline.close()
        self.azure.close()

    def queries(self) -> list[SearchQuery]:
        ref = ref_data()
        models = sorted({m for i in ref.corpus for m in i.models})
        lots = sorted({lot for i in ref.corpus for lot in i.lots})
        tags = sorted(set(ref.event_terms) | {t for i in ref.corpus for t in i.tags})
        out = [SearchQuery(model=m, limit=50) for m in models]
        out += [SearchQuery(lot=lot, limit=50) for lot in lots]
        out += [SearchQuery(event_tag=t, limit=50) for t in tags]
        for family in (None, "demo-ortho-a", "demo-cardio-b"):
            for types in (CORPUS_SOURCE_TYPES, ("dossier_item",), ("literature",), ("past_case", "technical_document"),
                          ALLOWED_SOURCE_TYPES):
                out.append(SearchQuery(source_types=types, family=family, limit=50))
                out += [SearchQuery(source_types=types, family=family, event_tag=t, limit=50) for t in tags[:4]]
        out += [SearchQuery(source_types=ALLOWED_SOURCE_TYPES, date_from=a, date_to=b, limit=50)
                for a, b in (("2025-01-01", None), (None, "2024-12-31"), ("2024-06-03", "2026-02-09"),
                             ("2030-01-01", None))]
        out += [SearchQuery(source_types=ALLOWED_SOURCE_TYPES, limit=n) for n in (1, 3, 50)]
        out += [SearchQuery(family="demo-ortho-a", model="OA-C54", lot="OA-C54-2601-C", event_tag="lot history")]
        return out

    def test_exact_filters_return_the_same_hits_in_the_same_order(self) -> None:
        queries = self.queries()
        self.assertGreater(len(queries), 100)
        compared = 0
        for query in queries:
            with self.subTest(query.as_dict()):
                self.assertEqual(self.azure.search(query), self.offline.search(query))
                compared += 1
        self.assertGreater(sum(1 for q in queries if self.offline.search(q)), 60)

    def test_text_queries_match_the_same_documents(self) -> None:
        texts = [(t, True) for tag in corr.DOSSIER_QUERIES for t in corr.DOSSIER_QUERIES[tag]]
        texts += [("fracture stem", False), ("loosening migration", False), ("ceramic-on-ceramic", False),
                  ("leaflet tear thickness", True), ("Endokarditis", False), ("FRACTURE", False)]
        for text, match_all in texts:
            for family in (None, "demo-ortho-a", "demo-cardio-b"):
                query = SearchQuery(source_types=ALLOWED_SOURCE_TYPES, family=family, text=text, match_all=match_all,
                                    limit=50)
                with self.subTest(text=text, family=family):
                    self.assertEqual({h.ref for h in self.azure.search(query)},
                                     {h.ref for h in self.offline.search(query)})

    def test_correlation_1_0_gives_the_same_hits_and_the_golden_must_include(self) -> None:
        expected = json.loads((DATA / "expected.json").read_text(encoding="utf-8"))
        from pms.models import FixedClock
        from pms.rules.engine import RuleEngine
        from pms.store import Store

        from ._support import RECEIVED_AT, letter_signal

        clock = FixedClock("2026-11-16T08:00:00Z")
        store = Store(":memory:", clock=clock)
        engine = RuleEngine(store, ref_data(), self.offline, clock=clock)
        checked = 0
        for letter in sorted(expected["letters"], key=lambda x: x["received_at"]):
            clock.set(RECEIVED_AT[letter["id"]])
            result = engine.process_signal(letter_signal(letter["id"]), now=clock.now())
            offline = engine.run_correlation(result.case_id, result.signal_id)
            keys = offline.keys
            tags = list(offline.event_tags)
            azure = corr.correlate(event_tags=tags, family=keys["family"], models=keys["models"], lots=keys["lots"],
                                   index=self.azure, registry=ref_data().registry,
                                   own_registry_ids=store.get_signal(result.signal_id).registry.get("entry_ids", []),
                                   cases=engine.case_profiles(), own_case_id=result.case_id,
                                   dictionary=ref_data().event_terms)
            with self.subTest(letter["id"]):
                self.assertEqual([(h.ref, h.match_types) for h in azure.hits],
                                 [(h.ref, h.match_types) for h in offline.hits])
                must = [m["ref"] for m in letter["rule_expectations"]["correlation"]["must_include"]]
                self.assertTrue(set(must) <= set(azure.refs), (must, azure.refs))
                checked += len(must)
        self.assertGreater(checked, 20)

    def test_queries_are_validated_like_offline(self) -> None:
        for bad in (SearchQuery(text="mail x@example.org"), SearchQuery(model="bad model"), SearchQuery(limit=0),
                    SearchQuery(event_tag="not a known tag"), SearchQuery(text=" ".join(["w"] * 9)),
                    SearchQuery(source_types=("web",))):
            with self.subTest(bad.as_dict()), self.assertRaises(InvalidQuery):
                self.azure.search(bad)
        self.assertEqual(FakeSearchService.get(ENDPOINT).ops, [])  # refused before any service call


class IndexMaintenance(FakeAzureTestCase):
    def test_first_sync_creates_and_fills_the_index_then_nothing_changes(self) -> None:
        search = azure_index()
        service = FakeSearchService.get(ENDPOINT)
        self.assertEqual(search.status(), {"index": INDEX, "exists": False, "documents": 0, "current": False})
        first = search.sync()
        self.assertEqual((first["index_definition"], first["documents"], first["uploaded"], first["deleted"]),
                         ("created or updated", 44, 44, 0))
        definition = service.indexes[INDEX]["definition"]
        fields = {f.name: f for f in definition.fields}
        self.assertTrue(fields["key"].key)
        self.assertTrue(fields["models"].filterable and fields["models"].type == "Collection(Edm.String)")
        self.assertEqual(fields["body"].analyzer_name, "pms_folding")
        self.assertEqual(definition.analyzers[0].token_filters, ["lowercase", "asciifolding"])
        keys = list(service.indexes[INDEX]["docs"])
        self.assertTrue(all(re.match(r"^[A-Za-z0-9_\-=]+$", k) for k in keys))
        second = search.sync()
        self.assertEqual((second["uploaded"], second["deleted"], second["unchanged"]), (0, 0, 44))
        self.assertTrue(search.status()["current"])
        docs = service.indexes[INDEX]["docs"]
        next(iter(docs.values()))["content_sha"] = "outdated"
        docs["c3RhbGU="] = {"key": "c3RhbGU=", "ref": "stale", "content_sha": "x"}
        third = search.sync()
        self.assertEqual((third["uploaded"], third["deleted"]), (1, 1))
        self.assertEqual(search.sync(force=True)["uploaded"], 44)

    def test_the_first_query_of_a_process_synchronises_a_missing_index(self) -> None:
        search = azure_index()
        hits = search.search(SearchQuery(model="OA-S5", limit=50))
        self.assertEqual({h.ref for h in hits}, {"TD-003", "H-003"})
        ops = [op[0] for op in FakeSearchService.get(ENDPOINT).ops]
        self.assertIn("create_or_update_index", ops)
        search.search(SearchQuery(model="OA-S5", limit=50))
        self.assertEqual([op[0] for op in FakeSearchService.get(ENDPOINT).ops].count("create_or_update_index"), 1)

    def test_without_auto_sync_a_missing_index_is_a_plain_error(self) -> None:
        from pms.azure.search import SearchIndexError

        with self.assertRaisesRegex(SearchIndexError, "seed --index-only"):
            azure_index(auto_sync=False).search(SearchQuery(model="OA-S5"))

    def test_an_existing_index_is_used_when_the_update_is_not_allowed(self) -> None:
        azure_index().sync()
        service = FakeSearchService.get(ENDPOINT)
        next(iter(service.indexes[INDEX]["docs"].values()))["content_sha"] = "outdated"
        service.fail_index_create = HttpResponseError("Forbidden", status_code=403)
        with self.assertLogs("pms.azure.search", "WARNING"):
            hits = azure_index().search(SearchQuery(model="OA-S5", limit=50))
        self.assertEqual({h.ref for h in hits}, {"TD-003", "H-003"})
        service.indexes.clear()
        from pms.azure.search import SearchIndexError

        with self.assertRaisesRegex(SearchIndexError, "missing and could not be created"):
            azure_index().search(SearchQuery(model="OA-S5"))

    def test_incompatible_definition_is_recreated(self) -> None:
        search = azure_index()
        search.sync()
        service = FakeSearchService.get(ENDPOINT)
        service.fail_index_create = [HttpResponseError("cannot change field type", status_code=400)]
        self.assertEqual(search.ensure_index(), "recreated")
        self.assertIn(("delete_index", INDEX), service.ops)

    def test_rest_fallback_when_the_sdk_cannot_serialise_the_index(self) -> None:
        service = FakeSearchService.get(ENDPOINT)
        service.fail_index_create = [TypeError("unexpected keyword argument 'analyzer_name'")]
        self.assertEqual(azure_index().ensure_index(), "created or updated (REST)")
        self.assertIn(("rest_put_index", INDEX), service.ops)

    def test_rest_fallback_when_the_sdk_models_do_not_fit(self) -> None:
        with patched_modules(without_search_models=True):
            result = azure_index().sync()
        self.assertEqual(result["index_definition"], "created or updated (REST)")
        service = FakeSearchService.get(ENDPOINT)
        self.assertIn(("rest_put_index", INDEX), service.ops)
        self.assertTrue(service.indexes[INDEX]["definition"].rest)
        self.assertEqual(len(service.indexes[INDEX]["docs"]), 44)
        self.assertIn("https://search.azure.com/.default", CREDENTIALS.tokens)
        self.assertEqual(FakeHttp.log[-1][0], "PUT")

    def test_runtime_seed_index_only(self) -> None:
        self.assertTrue(rt.seed_search_index(Settings())["skipped"])
        azure = Settings(app_env="azure", local_test_users=False, worker_mode="inprocess",
                         azure_search_endpoint=ENDPOINT, azure_search_index=INDEX, azure_client_id="mi-client")
        result = rt.seed_search_index(azure)
        self.assertEqual((result["index"], result["uploaded"], result["corpus_version"]), (INDEX, 44, "1.0"))
        self.assertEqual(rt.seed_search_index(azure)["uploaded"], 0)


if __name__ == "__main__":
    unittest.main()
