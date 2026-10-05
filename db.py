"""db.py — MongoDB (pymongo) with in-memory fallback.

Used by every other module. Collections:
  - messages      group chat history (text only, bot & cmd skipped)
  - channel_posts channel posts (text/caption + media)
  - qa            user Q + admin/owner A pairs (for future learning)
  - escalations   pending admin->owner escalations
  - state         tiny k/v store (model, group_id, channel_id, etc.)
  - links         group<->channel links the owner set up
"""
from __future__ import annotations
import re
import sys
import time
import uuid
from typing import Any, Optional

import config

_DB: Any = None


# ---------- in-memory fallback ----------

def _match_value(value: Any, condition: Any) -> bool:
    if isinstance(condition, dict):
        if "$regex" in condition:
            pat = condition["$regex"]
            opts = condition.get("$options", "")
            flags = re.IGNORECASE if "i" in opts else 0
            return isinstance(value, str) and re.search(pat, value, flags) is not None
        for op, fn in (
            ("$gte", lambda v, t: v >= t),
            ("$lte", lambda v, t: v <= t),
            ("$gt", lambda v, t: v > t),
            ("$lt", lambda v, t: v < t),
        ):
            if op in condition and not (isinstance(value, (int, float)) and fn(value, condition[op])):
                return False
        return True
    return value == condition


def _match_doc(doc: dict, flt: Optional[dict]) -> bool:
    if not flt:
        return True
    for k, c in flt.items():
        if k == "$or":
            if not any(_match_doc(doc, sub) for sub in (c or [])):
                return False
            continue
        if isinstance(c, dict):
            if "$exists" in c and (k in doc) != bool(c["$exists"]):
                return False
            if "$ne" in c and doc.get(k) == c["$ne"]:
                return False
            if "$in" in c and doc.get(k) not in c["$in"]:
                return False
        if not _match_value(doc.get(k), c):
            return False
    return True


class _R:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class IMCollection:
    def __init__(self, name: str):
        self.name = name
        self._docs: list[dict] = []

    def insert_one(self, doc: dict):
        d = dict(doc)
        d.setdefault("_id", uuid.uuid4().hex)
        self._docs.append(d)
        return _R(inserted_id=d["_id"])

    def insert_many(self, docs: list):
        ids = []
        for doc in docs:
            r = self.insert_one(doc)
            ids.append(r.inserted_id)
        return _R(inserted_ids=ids)

    def find_one(self, flt=None):
        for d in self._docs:
            if _match_doc(d, flt):
                return d
        return None

    def find(self, flt=None):
        return [d for d in self._docs if _match_doc(d, flt)]

    def update_one(self, flt, update, upsert=False):
        for d in self._docs:
            if _match_doc(d, flt):
                if "$set" in update:
                    d.update(update["$set"])
                if "$inc" in update:
                    for k, v in update["$inc"].items():
                        d[k] = d.get(k, 0) + v
                if "$push" in update:
                    for k, v in update["$push"].items():
                        d.setdefault(k, []).append(v)
                return _R(matched_count=1, modified_count=1)
        if upsert:
            nd = dict(flt or {})
            if "$set" in update:
                nd.update(update["$set"])
            nd.setdefault("_id", uuid.uuid4().hex)
            self._docs.append(nd)
            return _R(matched_count=0, modified_count=0, upserted_id=nd["_id"])
        return _R(matched_count=0, modified_count=0)

    def delete_many(self, flt=None):
        before = len(self._docs)
        self._docs = [d for d in self._docs if not _match_doc(d, flt)]
        return _R(deleted_count=before - len(self._docs))

    def count_documents(self, flt=None):
        return sum(1 for d in self._docs if _match_doc(d, flt))

    def drop(self):
        self._docs.clear()


class IMDB:
    def __init__(self):
        self._cols: dict[str, IMCollection] = {}

    def __getitem__(self, name):
        if name not in self._cols:
            self._cols[name] = IMCollection(name)
        return self._cols[name]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]


# ---------- real connection ----------

def get_db() -> Any:
    global _DB
    if _DB is not None:
        return _DB
    uri = config.MONGODB_URI
    if uri:
        try:
            import pymongo  # type: ignore
            client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=8000)
            client.server_info()
            _DB = client[config.DB_NAME]
            print(f"[db] connected to MongoDB '{config.DB_NAME}'", file=sys.stderr)
            _ensure_indexes(_DB)
            return _DB
        except Exception as exc:
            print(f"[db] MongoDB connect failed ({exc!r}); using in-memory", file=sys.stderr)
            _DB = IMDB()
            return _DB
    print("[db] no MONGODB_URI; using in-memory (data lost on restart!)", file=sys.stderr)
    _DB = IMDB()
    return _DB


def is_persistent() -> bool:
    """Whether the initialized store is MongoDB rather than volatile RAM."""
    return not isinstance(get_db(), IMDB)


def _ensure_indexes(db) -> None:
    try:
        db["messages"].create_index("chat_id")
        db["messages"].create_index("user_id")
        db["messages"].create_index("date")
        db["messages"].create_index("importance")
        db["channel_posts"].create_index("channel_id")
        db["channel_posts"].create_index("date")
        db["channel_posts"].create_index("importance")
        db["channel_posts"].create_index([("channel_id", 1), ("message_id", 1)], unique=False)
        db["escalations"].create_index("status")
        db["escalations"].create_index("created_ts")
        db["links"].create_index("group_id")
        db["links"].create_index("channel_id")
    except Exception as exc:  # noqa: BLE001
        print(f"[db] index create failed: {exc!r}", file=sys.stderr)


# ---------- state (k/v) ----------

# SPEED: state values are tiny k/v docs (filters list, terms, rules, ids)
# that are read on EVERY message but change rarely. Atlas M0 free tier
# charges ~100-150ms per find_one — 6 reads/message = ~0.8s of pure
# roundtrips. This in-process write-through cache removes them from the
# hot path. Entries are keyed to the underlying db OBJECT, so swapping
# the store (tests patch db._DB with a fresh IMDB) invalidates instantly.
_STATE_CACHE: dict[str, tuple[Any, float, Any]] = {}
_STATE_TTL = 20.0  # seconds; write-through invalidation makes it exact
_MISS = object()  # sentinel: distinguishes "cached None" from "no entry"


def _cache_get(k: str):
    """Cached value, or _MISS. Drops entries whose db object was swapped."""
    hit = _STATE_CACHE.get(k)
    if hit is None:
        return _MISS
    v, ts, dbobj = hit
    if dbobj is not _DB or _DB is None:
        _STATE_CACHE.pop(k, None)
        return _MISS
    if time.time() - ts >= _STATE_TTL:
        return _MISS
    return v


def save_state(k: str, v: Any) -> None:
    cur = get_db()
    cur["state"].update_one({"k": k}, {"$set": {"v": v}}, upsert=True)
    _STATE_CACHE[k] = (v, time.time(), cur)


def get_state(k: str, default: Any = None) -> Any:
    v = _cache_get(k)
    if v is not _MISS:
        return v if v is not None else default
    cur = get_db()
    doc = cur["state"].find_one({"k": k})
    v = doc["v"] if doc is not None else None
    _STATE_CACHE[k] = (v, time.time(), cur)
    return v if v is not None else default


def invalidate_state_cache() -> None:
    """Drop the in-memory state cache (tests / manual refresh)."""
    _STATE_CACHE.clear()


# ---------- chat-type resolution ----------

def is_persistent_id(v: Any) -> bool:
    return isinstance(v, (str, int)) and not str(v).endswith("...") and v != 0