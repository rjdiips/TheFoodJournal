#!/usr/bin/env python3
"""The demo seed fills the demo database, not the real journal."""

import sys
import unittest
from pathlib import Path

import mongomock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.journal import store  # noqa: E402
from scripts import seed_demo  # noqa: E402


class TestSeedDemo(unittest.TestCase):
    def test_seed_fills_the_demo_journal(self):
        db = mongomock.MongoClient()[seed_demo.DEMO_DB]
        document = seed_demo.seed(db=db)
        stored = store.load(db, store.DEFAULT_JOURNAL_ID)
        self.assertGreater(len(document["entries"]), 30)
        self.assertEqual(document["calorie_goal"], seed_demo.DEMO_GOAL)
        self.assertEqual(len(stored["entries"]), len(document["entries"]))

    def test_seeding_twice_does_not_duplicate_rows(self):
        db = mongomock.MongoClient()[seed_demo.DEMO_DB]
        first = seed_demo.seed(db=db)
        second = seed_demo.seed(db=db)
        self.assertEqual(len(first["entries"]), len(second["entries"]))


if __name__ == "__main__":
    unittest.main()
