"""Tests for the v2 power upgrade.

Covers:
  - fuzzy abuse matching (evasive spellings caught, safe words spared)
  - name-based target resolution ('unmute rich ko')
  - owner teach system (/sikhao parsing + firing)
  - owner spoken-command synthesis with names
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import moderation
import teach
import bot as rk_bot


def _db_state():
    store = {}

    class FakeColl:
        def __init__(self, name):
            self.name = name
            self.docs = []

        def find(self, flt=None):
            return [d for d in self.docs if _match(d, flt or {})]

        def find_one(self, flt=None):
            hits = self.find(flt)
            return hits[0] if hits else None

        def insert_one(self, doc):
            self.docs.append(doc)
            return MagicMock(inserted_id="x")

        def update_many(self, flt, upd):
            return MagicMock(modified_count=0)

    def _match(doc, flt):
        for k, v in flt.items():
            if k not in doc:
                return False
            if isinstance(v, dict) and "$ne" in v:
                if doc[k] == v["$ne"]:
                    return False
            elif doc[k] != v:
                return False
        return True

    class FakeDB:
        def __init__(self):
            self.cols = {}

        def __getitem__(self, name):
            if name not in self.cols:
                self.cols[name] = FakeColl(name)
            return self.cols[name]

    fake_db = FakeDB()

    def get_state(k, default=None):
        return store.get(k, default)

    def save_state(k, v):
        store[k] = v

    return fake_db, get_state, save_state


class FuzzyModerationTests(unittest.TestCase):
    def test_evasive_spellings_caught(self):
        cases = [
            "madharchood",       # double-o spelling (live incident)
            "teri maa ka bhosda",  # live incident
            "m@darch0d",         # leet
            "b.h.o.s.d.a",       # separator spam
            "maderchod",         # direct
            "MADHARCHOD",        # caps
        ]
        for text in cases:
            self.assertIsNotNone(
                moderation.matches_term(text, moderation.DEFAULT_TERMS),
                f"should catch: {text}")

    def test_safe_words_not_caught(self):
        cases = [
            "gandhi", "wholesale", "assess", "class",
            "chhutti ho gayi", "kutton ki film", "bhai kaise ho",
        ]
        for text in cases:
            self.assertIsNone(
                moderation.matches_term(text, moderation.DEFAULT_TERMS),
                f"should NOT catch: {text}")


class NameResolveTests(unittest.TestCase):
    def setUp(self):
        self.db, gs, ss = _db_state()
        self.p_get = patch.object(moderation, "get_state", gs)
        self.p_save = patch.object(moderation, "save_state", ss)
        self.p_db = patch.object(moderation, "get_db", lambda: self.db)
        # Name hits are VERIFIED live via getChatMember (safety: a stale
        # memory match must never receive the action). Simulate Telegram
        # confirming membership for the stored user.
        self.p_member = patch.object(
            moderation, "_member_in_group",
            lambda chat_id, uid: uid == 6857910750)
        for p in (self.p_get, self.p_save, self.p_db, self.p_member):
            p.start()
        self.addCleanup(
            lambda: [p.stop() for p in (self.p_get, self.p_save, self.p_db,
                                        self.p_member)])
        # stored messages: 'the rich' @richislive0
        self.db["messages"].docs = [
            {"chat_id": -100, "user_id": 6857910750, "username": "richislive0",
             "first_name": "the rich", "text": "hii", "date": 100},
        ]

    def test_bare_name_resolves(self):
        msg = {"reply_to_message": {}}
        uid, name = moderation._resolve_target(msg, "rich", -100)
        self.assertEqual(uid, 6857910750)
        self.assertEqual(name, "the rich")

    def test_exact_username_resolves(self):
        msg = {"reply_to_message": {}}
        uid, _ = moderation._resolve_target(msg, "@richislive0", -100)
        self.assertEqual(uid, 6857910750)

    def test_unknown_name_returns_none(self):
        msg = {"reply_to_message": {}}
        uid, _ = moderation._resolve_target(msg, "nobodysname", -100)
        self.assertIsNone(uid)

    def test_left_member_not_resolved(self):
        """A stale memory hit whose live membership check FAILS is dropped —
        this is the wrong-user-action safety net."""
        with patch.object(moderation, "_member_in_group", return_value=False):
            msg = {"reply_to_message": {}}
            uid, _ = moderation._resolve_target(msg, "rich", -100)
            self.assertIsNone(uid)


class TeachTests(unittest.TestCase):
    def test_parse_reply_form(self):
        trigger, reply, file_q = teach.parse_teach(
            "mod config :: ye lo mod config file")
        self.assertEqual(trigger, "mod config")
        self.assertEqual(reply, "ye lo mod config file")
        self.assertIsNone(file_q)

    def test_parse_file_form(self):
        trigger, reply, file_q = teach.parse_teach(
            "loader :: file BGMI loader")
        self.assertEqual(trigger, "loader")
        self.assertIsNone(reply)
        self.assertEqual(file_q, "BGMI loader")

    def test_parse_forward_form(self):
        trigger, reply, file_q = teach.parse_teach("contact :: forward")
        self.assertEqual(trigger, "contact")
        self.assertEqual(file_q, "contact")

    def test_parse_invalid(self):
        self.assertEqual(teach.parse_teach("no separator"),
                         (None, None, None))

    def test_add_and_remove(self):
        db, gs, ss = _db_state()
        with patch.object(teach, "get_state", gs), \
                patch.object(teach, "save_state", ss):
            teach.add_teaching("hi bot", "Namaste!", None, 1)
            self.assertEqual(len(teach.get_teachings()), 1)
            removed = teach.remove_teaching("hi bot")
            self.assertEqual(removed, "hi bot")
            self.assertEqual(teach.get_teachings(), [])


class SynthesizeNameTests(unittest.TestCase):
    """'bro unmute kr do rich ko' must become /unmute rich (the live bug)."""

    def _synth(self, text):
        msg = {"message_id": 55, "from": {"id": 1}, "chat": {"id": -100},
               "reply_to_message": {}}
        with patch.object(rk_bot, "_bot_username", return_value="rk_test_bot"):
            return rk_bot._synthesize_owner_command(text, msg, -100)

    def test_name_with_ko(self):
        synth = self._synth("bro unmute kr do rich ko")
        self.assertIsNotNone(synth)
        self.assertEqual(synth["text"], "/unmute rich")

    def test_name_without_ko(self):
        synth = self._synth("bhai rich ko 10 min ke liye mute karo")
        self.assertIsNotNone(synth)
        self.assertEqual(synth["text"], "/tmute 10m rich")

    def test_action_word_not_a_target(self):
        synth = self._synth("usko ban kar do")
        self.assertIsNone(synth)

    def test_kick_spoken(self):
        synth = self._synth("rich ko group se nikal do")
        self.assertIsNotNone(synth)
        self.assertEqual(synth["text"], "/kick rich")


if __name__ == "__main__":
    unittest.main()
