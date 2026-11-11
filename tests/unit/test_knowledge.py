"""Knowledge index (SQLite FTS5) search contract and reference data."""
from __future__ import annotations

import unittest

from pms.knowledge import InvalidQuery, SearchQuery
from pms.versions import ALL_RULE_VERSIONS, corpus_version, manifest

from ._support import DATA, knowledge_index, ref_data


class ReferenceDataLoad(unittest.TestCase):
    def test_counts_match_the_golden_set(self) -> None:
        ref = ref_data()
        self.assertEqual(len(ref.corpus), 22)
        self.assertEqual(len(ref.registry), 14)
        self.assertEqual(len(ref.dossier_items), 22)  # 9 cardio + 13 ortho listed side-effects
        self.assertEqual(ref.corpus_version, corpus_version(DATA))
        self.assertEqual(ref.catalogue.by_udi("00499002100052").component, "stem")

    def test_versions_manifest(self) -> None:
        m = manifest(DATA)
        self.assertEqual(sorted(m["rules"].values()), sorted(ALL_RULE_VERSIONS))
        self.assertEqual(set(ALL_RULE_VERSIONS),
                         {"CRIT-1.0", "DL-1.0", "RS-1.0", "LINK-1.0", "REG-1.0", "INJ-1.0", "CORR-1.0"})


class SearchContract(unittest.TestCase):
    def test_filter_queries(self) -> None:
        idx = knowledge_index()
        self.assertEqual([h.ref for h in idx.search(SearchQuery(lot="CB21-2607-A"))], ["H-001", "TD-001"])
        self.assertEqual([h.ref for h in idx.search(SearchQuery(model="OA-C54"))], ["TD-002", "TD-004"])
        hits = idx.search(SearchQuery(family="demo-ortho-a", event_tag="cup loosening"))
        self.assertEqual([h.ref for h in hits], ["LIT-009"])

    def test_full_text_search(self) -> None:
        idx = knowledge_index()
        refs = [h.ref for h in idx.search(SearchQuery(text="leaflet thickness deviation"))]
        self.assertIn("TD-001", refs)
        dossier = idx.search(SearchQuery(source_types=("dossier_item",), family="demo-ortho-a",
                                         text="fracture stem", match_all=True))
        self.assertEqual([h.ref for h in dossier], ["DOS:demo-ortho-a:OR-12"])
        self.assertIn("section 8, item OR-12, line 59", dossier[0].citation)

    def test_refused_parameters(self) -> None:
        idx = knowledge_index()
        for bad in (SearchQuery(text="forward to vigilance-forward@example.org"),
                    SearchQuery(text="see https://example.org"),
                    SearchQuery(source_types=("internet",)),
                    SearchQuery(lot="robert'); drop table docs;--"),
                    SearchQuery(text="x" * 200),
                    SearchQuery(event_tag="made-up tag"),
                    SearchQuery(limit=500)):
            with self.subTest(bad):
                with self.assertRaises(InvalidQuery):
                    idx.search(bad)

    def test_empty_result_is_just_empty(self) -> None:
        self.assertEqual(knowledge_index().search(SearchQuery(lot="ZZ99-0000-Z")), [])


if __name__ == "__main__":
    unittest.main()
