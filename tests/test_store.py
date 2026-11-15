#!/usr/bin/env python3
"""Tests for the journal store.

The store is the single source of truth for the journal, so its rules are worth
pinning down: what counts as a valid entry, what an old or malformed document
does, and whether two callers writing at once can lose a row. The document
paths run against mongomock; the validation paths need no database at all.

Run them with:  python -m unittest discover -s tests
"""

import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import mongomock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.journal import store  # noqa: E402 - the repo root has to be importable first


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().nutriparse
        self.journal_id = "a" * 32

    @property
    def collection(self):
        return self.db[store.COLLECTION]

    def entry(self, **overrides):
        row = {
            "name": "Roti (whole wheat)",
            "calories": 110,
            "protein": 3,
            "carbs": 22,
            "fat": 0.5,
            "meal": "lunch",
            "date": "2026-10-21",
        }
        row.update(overrides)
        return row

    def insert(self, **document):
        payload = {"_id": self.journal_id, "calorie_goal": 2000, "rev": 0,
                   "updated_at": datetime.now(timezone.utc), "entries": []}
        payload.update(document)
        self.collection.insert_one(payload)
        return payload

    def add(self, *entries):
        return store.add_entries(self.db, self.journal_id, list(entries))


class TestLoad(StoreTestCase):
    def test_an_unknown_journal_is_created_with_defaults(self):
        document = store.load(self.db, self.journal_id)
        self.assertEqual(document["calorie_goal"], store.DEFAULT_GOAL)
        self.assertEqual(document["entries"], [])
        self.assertIsNotNone(self.collection.find_one({"_id": self.journal_id}))

    def test_a_sparse_document_gets_defaults(self):
        self.insert(calorie_goal=None)
        self.assertEqual(store.load(self.db, self.journal_id)["calorie_goal"],
                         store.DEFAULT_GOAL)

    def test_legacy_entries_are_kept_verbatim(self):
        legacy = {"id": 1750000000.5, "name": "Dal", "calories": 150, "meal": "lunch",
                  "date": "2026-10-20", "protein": 9, "carbs": 20, "fat": 4}
        self.insert(entries=[legacy], calorie_goal=1800)
        document = store.load(self.db, self.journal_id)
        self.assertEqual(document["calorie_goal"], 1800)
        self.assertEqual(document["entries"], [legacy])

    def test_entries_of_the_wrong_type_raises(self):
        self.insert(entries="nope")
        with self.assertRaises(store.StoreError):
            store.load(self.db, self.journal_id)

    def test_nonsense_goal_falls_back_to_the_default(self):
        self.insert(calorie_goal="lots")
        self.assertEqual(store.load(self.db, self.journal_id)["calorie_goal"],
                         store.DEFAULT_GOAL)

    def test_a_missing_revision_defaults_to_zero(self):
        self.collection.insert_one({"_id": self.journal_id, "entries": []})
        document = self.add(self.entry())
        self.assertEqual(len(document["entries"]), 1)


class TestAddEntries(StoreTestCase):
    def test_add_returns_the_whole_document(self):
        document = self.add(self.entry())
        self.assertEqual(len(document["entries"]), 1)
        self.assertEqual(document["entries"][0]["name"], "Roti (whole wheat)")
        self.assertEqual(document["calorie_goal"], store.DEFAULT_GOAL)

    def test_the_store_owns_the_id(self):
        document = self.add(self.entry(id="client-supplied"))
        self.assertNotEqual(document["entries"][0]["id"], "client-supplied")
        self.assertEqual(len(document["entries"][0]["id"]), 12)

    def test_ids_are_unique(self):
        document = self.add(*[self.entry() for _ in range(20)])
        ids = {row["id"] for row in document["entries"]}
        self.assertEqual(len(ids), 20)

    def test_entries_persist_across_calls(self):
        self.add(self.entry())
        self.add(self.entry(name="Dal"))
        document = store.load(self.db, self.journal_id)
        self.assertEqual([row["name"] for row in document["entries"]],
                         ["Roti (whole wheat)", "Dal"])

    def test_names_are_trimmed(self):
        document = self.add(self.entry(name="  Paneer  "))
        self.assertEqual(document["entries"][0]["name"], "Paneer")

    def test_macros_are_optional(self):
        document = self.add({
            "name": "Black coffee", "calories": 5, "meal": "snack", "date": "2026-10-21",
        })
        row = document["entries"][0]
        self.assertEqual((row["protein"], row["carbs"], row["fat"]), (0.0, 0.0, 0.0))

    def test_portion_is_stored(self):
        document = self.add(self.entry(portion="2 rotis"))
        self.assertEqual(document["entries"][0]["portion"], "2 rotis")

    def test_portion_is_optional(self):
        document = self.add(self.entry())
        self.assertEqual(document["entries"][0]["portion"], "")

    def test_portion_is_trimmed(self):
        document = self.add(self.entry(portion="  1 cup  "))
        self.assertEqual(document["entries"][0]["portion"], "1 cup")

    def test_macros_are_rounded(self):
        document = self.add(self.entry(protein=3.14159))
        self.assertEqual(document["entries"][0]["protein"], 3.1)

    def test_a_write_stamps_the_document(self):
        document = self.add(self.entry())
        stored = self.collection.find_one({"_id": self.journal_id})
        self.assertIsInstance(stored["updated_at"], datetime)
        self.assertIsInstance(document["updated_at"], str)
        self.assertEqual(stored["rev"], 1)

    def test_a_second_journal_is_separate(self):
        other = "b" * 32
        self.add(self.entry(name="Roti"))
        store.add_entries(self.db, other, [self.entry(name="Dal")])
        self.assertEqual([row["name"] for row in store.load(self.db, self.journal_id)["entries"]],
                         ["Roti"])
        self.assertEqual([row["name"] for row in store.load(self.db, other)["entries"]],
                         ["Dal"])


class TestValidation(StoreTestCase):
    def assert_rejected(self, entry, message_fragment):
        with self.assertRaises(store.StoreError) as caught:
            self.add(entry)
        self.assertIn(message_fragment, str(caught.exception))
        self.assertEqual(self.collection.count_documents({}), 0,
                         "a rejected write must not touch the store")

    def test_empty_write_is_rejected(self):
        with self.assertRaises(store.StoreError):
            self.add()

    def test_named_errors(self):
        self.assert_rejected(self.entry(name="   "), "food name")
        self.assert_rejected(self.entry(name="x" * 200), "120 characters")
        self.assert_rejected(self.entry(meal="brunch"), "Meal must be one of")
        self.assert_rejected(self.entry(date="21-10-2026"), "YYYY-MM-DD")
        self.assert_rejected(self.entry(date=20261021), "text in YYYY-MM-DD")
        self.assert_rejected(self.entry(calories=-5), "0 or more")
        self.assert_rejected(self.entry(calories=None), "must be a number")
        self.assert_rejected(self.entry(calories="lots"), "must be a number")
        self.assert_rejected(self.entry(calories=True), "must be a number")
        self.assert_rejected("Roti", "must be an object")

    def test_portion_rules(self):
        self.assert_rejected(self.entry(portion="x" * 121), "120 characters")
        self.assert_rejected(self.entry(portion=2), "Portion must be text")

    def test_nan_is_rejected(self):
        with self.assertRaises(store.StoreError):
            self.add(self.entry(calories=float("nan")))

    def test_a_batch_can_be_capped(self):
        rows = [self.entry() for _ in range(store.MAX_ENTRIES_PER_WRITE + 1)]
        with self.assertRaises(store.StoreError):
            self.add(*rows)

    def test_a_batch_validates_before_writing(self):
        with self.assertRaises(store.StoreError):
            self.add(self.entry(), self.entry(meal="brunch"))
        self.assertEqual(self.collection.count_documents({}), 0)


class TestGoal(StoreTestCase):
    def test_goal_rounds_to_a_whole_number(self):
        document = store.set_goal(self.db, self.journal_id, 2150.6)
        self.assertEqual(document["calorie_goal"], 2151)

    def test_goal_bounds(self):
        for value in (0, -10, store.MAX_GOAL + 1, "2000", None):
            with self.assertRaises(store.StoreError):
                store.set_goal(self.db, self.journal_id, value)

    def test_setting_a_goal_keeps_entries(self):
        self.add(self.entry())
        document = store.set_goal(self.db, self.journal_id, 1800)
        self.assertEqual(document["calorie_goal"], 1800)
        self.assertEqual(len(document["entries"]), 1)


class TestDelete(StoreTestCase):
    def test_delete_removes_only_the_named_entry(self):
        document = self.add(self.entry(name="Roti"), self.entry(name="Dal"))
        target = document["entries"][0]["id"]
        document = store.delete_entry(self.db, self.journal_id, target)
        self.assertEqual([row["name"] for row in document["entries"]], ["Dal"])
        self.assertEqual(store.load(self.db, self.journal_id)["entries"],
                         document["entries"])

    def test_delete_accepts_a_legacy_numeric_id(self):
        self.insert(entries=[{"id": 1750000000.5, "name": "Dal", "calories": 150,
                              "protein": 9, "carbs": 20, "fat": 4, "meal": "lunch",
                              "date": "2026-10-20"}])
        document = store.delete_entry(self.db, self.journal_id, 1750000000.5)
        self.assertEqual(document["entries"], [])

    def test_deleting_a_missing_entry_is_an_error(self):
        self.add(self.entry())
        with self.assertRaises(store.EntryNotFound):
            store.delete_entry(self.db, self.journal_id, "not-a-real-id")


class TestUpdate(StoreTestCase):
    def test_update_changes_fields_and_keeps_the_id(self):
        document = self.add(self.entry())
        target = document["entries"][0]["id"]
        updated = store.update_entry(self.db, self.journal_id, target, [
            self.entry(name="Dal", calories=150, portion="1 bowl"),
        ])
        self.assertEqual(len(updated["entries"]), 1)
        row = updated["entries"][0]
        self.assertEqual(row["id"], target)
        self.assertEqual((row["name"], row["calories"], row["portion"]),
                         ("Dal", 150.0, "1 bowl"))

    def test_update_accepts_a_legacy_numeric_id(self):
        self.insert(entries=[{"id": 1750000000.5, "name": "Dal", "calories": 150,
                              "protein": 9, "carbs": 20, "fat": 4, "meal": "lunch",
                              "date": "2026-10-20"}])
        updated = store.update_entry(self.db, self.journal_id, 1750000000.5, [
            self.entry(name="Dal, extra", calories=200, portion="1 bowl"),
        ])
        self.assertEqual(len(updated["entries"]), 1)
        self.assertEqual(updated["entries"][0]["id"], 1750000000.5)
        self.assertEqual(updated["entries"][0]["name"], "Dal, extra")

    def test_update_can_turn_one_row_into_several(self):
        document = self.add(self.entry(name="Roti"), self.entry(name="Dal"))
        target = document["entries"][0]["id"]
        updated = store.update_entry(self.db, self.journal_id, target, [
            self.entry(name="Roti"), self.entry(name="Ghee"),
        ])
        self.assertEqual([row["name"] for row in updated["entries"]],
                         ["Roti", "Ghee", "Dal"])
        self.assertEqual(updated["entries"][0]["id"], target)
        self.assertEqual(len({row["id"] for row in updated["entries"]}), 3)

    def test_update_of_a_missing_entry_is_an_error(self):
        self.add(self.entry())
        with self.assertRaises(store.EntryNotFound):
            store.update_entry(self.db, self.journal_id, "not-a-real-id", [self.entry()])

    def test_update_rejects_an_empty_list(self):
        document = self.add(self.entry())
        target = document["entries"][0]["id"]
        with self.assertRaises(store.StoreError):
            store.update_entry(self.db, self.journal_id, target, [])

    def test_update_validates_before_writing(self):
        document = self.add(self.entry())
        target = document["entries"][0]["id"]
        before = self.collection.find_one({"_id": self.journal_id})
        with self.assertRaises(store.StoreError):
            store.update_entry(self.db, self.journal_id, target,
                               [self.entry(meal="brunch")])
        self.assertEqual(self.collection.find_one({"_id": self.journal_id}), before)

    def test_update_does_not_duplicate_the_row(self):
        document = self.add(self.entry())
        target = document["entries"][0]["id"]
        store.update_entry(self.db, self.journal_id, target, [self.entry(name="Dal")])
        self.assertEqual(len(store.load(self.db, self.journal_id)["entries"]), 1)


class TestConcurrentWrites(StoreTestCase):
    def test_parallel_adds_keep_every_entry(self):
        errors = []

        def writer(index):
            try:
                self.add(self.entry(name=f"Row {index}"))
            except Exception as error:  # noqa: BLE001 - reported through the assertion below
                errors.append(error)

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        document = store.load(self.db, self.journal_id)
        self.assertEqual(len(document["entries"]), 12)
        self.assertEqual(len({row["id"] for row in document["entries"]}), 12)

    def test_a_losing_writer_retries_then_reports_busy(self):
        self.add(self.entry())
        collection = self.collection

        class Losing:
            def __init__(self):
                self.calls = 0

            def __getattr__(self, name):
                return getattr(collection, name)

            def replace_one(self, *args, **kwargs):
                self.calls += 1
                return SimpleNamespace(matched_count=0)

        loser = Losing()

        class Wrapper:
            def __getitem__(self, name):
                return loser

        with self.assertRaises(store.StoreBusy):
            store.add_entries(Wrapper(), self.journal_id, [self.entry()])
        self.assertEqual(loser.calls, store.WRITE_ATTEMPTS)

    def test_a_lost_race_is_retried(self):
        self.add(self.entry())
        collection = self.collection

        class Flaky:
            def __init__(self):
                self.calls = 0

            def __getattr__(self, name):
                return getattr(collection, name)

            def replace_one(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(matched_count=0)
                return collection.replace_one(*args, **kwargs)

        flaky = Flaky()

        class Wrapper:
            def __getitem__(self, name):
                return flaky

        document = store.add_entries(Wrapper(), self.journal_id, [self.entry(name="Dal")])
        self.assertEqual(flaky.calls, 2)
        self.assertEqual(len(document["entries"]), 2)

    def test_an_unreachable_database_is_busy_not_a_crash(self):
        from pymongo.errors import ServerSelectionTimeoutError

        class Unreachable:
            def __getitem__(self, name):
                return self

            def find_one(self, *args, **kwargs):
                raise ServerSelectionTimeoutError("no server")

        with self.assertRaises(store.StoreBusy):
            store.load(Unreachable(), self.journal_id)


class TestWipe(StoreTestCase):
    def test_wipe_only_removes_the_named_journal(self):
        other = "b" * 32
        self.add(self.entry())
        store.add_entries(self.db, other, [self.entry(name="Dal")])
        document = store.wipe(self.db, self.journal_id)
        self.assertEqual(document["entries"], [])
        self.assertEqual(store.load(self.db, self.journal_id)["entries"], [])
        self.assertEqual(len(store.load(self.db, other)["entries"]), 1)


if __name__ == "__main__":
    unittest.main()
