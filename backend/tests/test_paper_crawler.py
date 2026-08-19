"""Pure-logic tests for paper crawler dedup / never-repeat filtering."""

from __future__ import annotations

import unittest

from app.services import paper_crawler


class PaperKeyTest(unittest.TestCase):
    def test_arxiv_id_wins(self):
        self.assertEqual(
            paper_crawler.paper_key({"arxiv_id": "2608.12345", "title": "Some Paper"}),
            "2608.12345",
        )

    def test_title_fallback_lowercased_stripped(self):
        self.assertEqual(
            paper_crawler.paper_key({"arxiv_id": None, "title": "  A Survey of LLMs  "}),
            "a survey of llms",
        )


class FilterSeenTest(unittest.TestCase):
    def test_drops_seen_and_keeps_fresh(self):
        pool = [
            {"arxiv_id": "2608.1", "title": "A"},
            {"arxiv_id": None, "title": "B Paper"},
            {"arxiv_id": "2608.3", "title": "C"},
        ]
        seen = {"2608.1", "b paper"}
        out = paper_crawler.filter_seen(pool, seen)
        self.assertEqual([p["title"] for p in out], ["C"])

    def test_empty_seen_returns_pool_unchanged(self):
        pool = [{"arxiv_id": "2608.1", "title": "A"}]
        self.assertIs(paper_crawler.filter_seen(pool, set()), pool)

    def test_no_duplicates_after_two_passes(self):
        pool = [{"arxiv_id": f"2608.{i}", "title": f"P{i}"} for i in range(10)]
        first = paper_crawler.filter_seen(pool, set())
        seen = {paper_crawler.paper_key(p) for p in first}
        second = paper_crawler.filter_seen(pool, seen)
        self.assertEqual(second, [])


if __name__ == "__main__":
    unittest.main()
