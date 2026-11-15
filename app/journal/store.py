#!/usr/bin/env python3
"""The journal store: one MongoDB document and one set of rules.

The web app's HTTP API (``app.web``) reads and writes the journal through this
module, so the Compose view and the Food Journal share one store instead of
keeping separate ones. Each journal is one document in the ``journals``
collection; the document is also the API response.

Writes are read-modify-write, guarded by the document's ``rev`` counter: read
the document, validate, then ``replaceOne`` only when ``rev`` still matches.
The losing writer retries a few times and then reports ``StoreBusy``, so two
writers can never drop each other's rows.

``store`` is the only module that knows a database is involved, so swapping it
back out stays a change in one file.
"""

import math
import uuid
from datetime import date, datetime, timezone

from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, ServerSelectionTimeoutError

DEFAULT_GOAL = 2000
MAX_GOAL = 20000
MAX_ENTRIES_PER_WRITE = 50
MAX_NAME_LENGTH = 120
MAX_PORTION_LENGTH = 120
MEALS = ("breakfast", "lunch", "dinner", "snack")

# 04a keeps one shared journal. 04b replaces this with a per-browser id.
DEFAULT_JOURNAL_ID = "0" * 32
COLLECTION = "journals"
WRITE_ATTEMPTS = 3
CLIENT_OPTIONS = {
    "serverSelectionTimeoutMS": 3000,
    "connectTimeoutMS": 3000,
    "socketTimeoutMS": 5000,
    "maxPoolSize": 10,
    "retryWrites": True,
}


class StoreError(Exception):
    """The store is unreadable, or a write to it would be invalid."""


class EntryNotFound(StoreError):
    """The entry a caller asked to delete is not in the store."""


class StoreBusy(StoreError):
    """Another writer kept winning the revision race."""


def create_client(uri):
    """Return the one pooled client the process shares."""
    return MongoClient(uri, **CLIENT_OPTIONS)


def number(value, default=0.0):
    """Best-effort numeric read of a value that came from a file we did not write.

    Shared with ``evals/run_parse_eval.py``, which grades whatever a model
    returned before any of it reaches the store.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = float(value)
    return value if math.isfinite(value) else default


def _clean_number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoreError(f"{field} must be a number.")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise StoreError(f"{field} must be a number of 0 or more.")
    return round(value, 1)


def _clean_goal(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoreError("The calorie goal must be a number.")
    value = float(value)
    if not math.isfinite(value) or not 1 <= value <= MAX_GOAL:
        raise StoreError(f"The calorie goal must be between 1 and {MAX_GOAL}.")
    return int(round(value))


def normalize_entry(raw):
    """Validate one entry from a client and give it an id owned by the store."""
    if not isinstance(raw, dict):
        raise StoreError("Each entry must be an object.")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise StoreError("Each entry needs a food name.")
    name = name.strip()
    if len(name) > MAX_NAME_LENGTH:
        raise StoreError(f"Food names must be {MAX_NAME_LENGTH} characters or fewer.")

    portion = raw.get("portion", "")
    if not isinstance(portion, str):
        raise StoreError("Portion must be text.")
    portion = portion.strip()
    if len(portion) > MAX_PORTION_LENGTH:
        raise StoreError(f"Portions must be {MAX_PORTION_LENGTH} characters or fewer.")

    meal = raw.get("meal", "snack")
    if meal not in MEALS:
        raise StoreError(f"Meal must be one of: {', '.join(MEALS)}.")

    day = raw.get("date") or date.today().isoformat()
    if not isinstance(day, str):
        raise StoreError("Date must be text in YYYY-MM-DD form.")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise StoreError("Date must be in YYYY-MM-DD form.") from None

    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "portion": portion,
        "calories": _clean_number(raw.get("calories"), "Calories"),
        "protein": _clean_number(raw.get("protein", 0), "Protein"),
        "carbs": _clean_number(raw.get("carbs", 0), "Carbs"),
        "fat": _clean_number(raw.get("fat", 0), "Fat"),
        "meal": meal,
        "date": day,
    }


def _collection(db):
    return db[COLLECTION]


def _empty(journal_id):
    return {
        "_id": journal_id,
        "calorie_goal": DEFAULT_GOAL,
        "rev": 0,
        "updated_at": datetime.now(timezone.utc),
        "entries": [],
    }


def _stored(raw):
    """Normalise a document read from the database, filling old defaults in."""
    entries = raw.get("entries")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise StoreError("The journal does not contain a list of entries.")
    goal = raw.get("calorie_goal")
    if (
        isinstance(goal, bool)
        or not isinstance(goal, (int, float))
        or not math.isfinite(float(goal))
        or not 1 <= goal <= MAX_GOAL
    ):
        goal = DEFAULT_GOAL
    return {
        "_id": raw["_id"],
        "calorie_goal": int(round(goal)),
        "rev": raw.get("rev") or 0,
        "updated_at": raw.get("updated_at"),
        "entries": entries,
    }


def _public(document):
    updated = document.get("updated_at")
    if isinstance(updated, datetime):
        updated = updated.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    return {
        "calorie_goal": document["calorie_goal"],
        "entries": document["entries"],
        "updated_at": updated if isinstance(updated, str) else None,
    }


def _find(db, journal_id):
    try:
        return _collection(db).find_one({"_id": journal_id})
    except ServerSelectionTimeoutError as error:
        raise StoreBusy("The journal is busy; try again.") from error


def load(db, journal_id=DEFAULT_JOURNAL_ID):
    """Return the whole journal document, creating it with defaults if new.

    Lazy creation keeps a first visit from 404ing. An unreadable document
    raises instead of quietly starting over, so a corrupt store never gets
    overwritten by the next write.
    """
    raw = _find(db, journal_id)
    if raw is None:
        try:
            _collection(db).insert_one(_empty(journal_id))
        except DuplicateKeyError:
            pass  # Another request created it first; read theirs.
        except ServerSelectionTimeoutError as error:
            raise StoreBusy("The journal is busy; try again.") from error
        raw = _find(db, journal_id)
    return _public(_stored(raw))


def _mutate(db, journal_id, change):
    """Apply one change under the document's revision counter."""
    collection = _collection(db)
    for _ in range(WRITE_ATTEMPTS):
        raw = _find(db, journal_id)
        if raw is None:
            document = _empty(journal_id)
            change(document)
            document["rev"] = 1
            document["updated_at"] = datetime.now(timezone.utc)
            try:
                collection.insert_one(document)
                return _public(document)
            except DuplicateKeyError:
                continue  # Another writer got there first; try again.
            except ServerSelectionTimeoutError as error:
                raise StoreBusy("The journal is busy; try again.") from error

        document = _stored(raw)
        change(document)
        document["rev"] = raw.get("rev") or 0
        document["rev"] += 1
        document["updated_at"] = datetime.now(timezone.utc)
        try:
            result = collection.replace_one(
                {"_id": journal_id, "rev": raw.get("rev")}, document
            )
        except ServerSelectionTimeoutError as error:
            raise StoreBusy("The journal is busy; try again.") from error
        if result.matched_count:
            return _public(document)
    raise StoreBusy("The journal is busy; try again.")


def _validated_batch(entries):
    """Validate a caller's rows before anything touches the database."""
    if not isinstance(entries, list) or not entries:
        raise StoreError("Send at least one entry.")
    if len(entries) > MAX_ENTRIES_PER_WRITE:
        raise StoreError(f"At most {MAX_ENTRIES_PER_WRITE} entries can be written at once.")
    return [normalize_entry(entry) for entry in entries]


def add_entries(db, journal_id, entries):
    """Append validated entries and return the whole updated document."""
    validated = _validated_batch(entries)

    def change(document):
        document["entries"] = document["entries"] + validated

    return _mutate(db, journal_id, change)


def update_entry(db, journal_id, entry_id, entries):
    """Replace one entry with the given rows and return the whole document.

    The first new row keeps the original id, so a caller can treat this as an
    edit even when the user added or merged rows while editing.
    """
    validated = _validated_batch(entries)
    wanted = str(entry_id)

    def change(document):
        index = next(
            (i for i, entry in enumerate(document["entries"]) if str(entry.get("id")) == wanted),
            None,
        )
        if index is None:
            raise EntryNotFound("That entry is no longer in the journal.")
        existing_id = document["entries"][index].get("id")
        if existing_id is not None:
            validated[0]["id"] = existing_id
        document["entries"][index:index + 1] = validated

    return _mutate(db, journal_id, change)


def delete_entry(db, journal_id, entry_id):
    """Remove one entry by id and return the whole updated document."""
    wanted = str(entry_id)

    def change(document):
        remaining = [
            entry for entry in document["entries"] if str(entry.get("id")) != wanted
        ]
        if len(remaining) == len(document["entries"]):
            raise EntryNotFound("That entry is no longer in the journal.")
        document["entries"] = remaining

    return _mutate(db, journal_id, change)


def set_goal(db, journal_id, goal):
    """Set the daily calorie goal and return the whole updated document."""
    value = _clean_goal(goal)

    def change(document):
        document["calorie_goal"] = value

    return _mutate(db, journal_id, change)


def wipe(db, journal_id=DEFAULT_JOURNAL_ID):
    """Delete one journal and return an empty document for the next read."""
    try:
        _collection(db).delete_one({"_id": journal_id})
    except ServerSelectionTimeoutError as error:
        raise StoreBusy("The journal is busy; try again.") from error
    return {"calorie_goal": DEFAULT_GOAL, "entries": [], "updated_at": None}
