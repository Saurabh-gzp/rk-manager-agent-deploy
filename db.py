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


def _ensure_indexes(db) -> None:
    try:
        db["messages"].create_index("chat_id")
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

def save_state(k: str, v: Any) -> None:
    get_db()["state"].update_one({"k": k}, {"$set": {"v": v}}, upsert=True)


def get_state(k: str, default: Any = None) -> Any:
    doc = get_db()["state"].find_one({"k": k})
    return doc["v"] if doc is not None else default


# ---------- chat-type resolution ----------

def is_persistent_id(v: Any) -> bool:
    return isinstance(v, (str, int)) and not str(v).endswith("...") and v != 0