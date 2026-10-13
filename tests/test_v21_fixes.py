"""Tests for v2.1: respect tone, /users, prune notification, fast paths."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent
import commands
import knowledge
import moderation


class RespectToneTests(unittest.TestCase):
    def test_respect_rule_in_system_prompt(self):
        self.assertIn("NEVER 'tu', 'tujhe', 'tum', 'tera'", agent.SYSTEM_PROMPT)

    def test_respect_rule_in_general_prompt(self):
        self.assertIn("never 'tu/tujhe/tum'", agent.GENERAL_PROMPT)

    def test_negative_instructions_are_rules(self):
        self.assertIn("negative/prohibition orders", agent.SYSTEM_PROMPT)


class UsersListTests(unittest.TestCase):
    def _fake_db(self, docs):
        class C:
            def find(self, flt=None):
                return [d for d in docs if all(d.get(k) == v
                                                for k, v in (flt or {}).items())]
        class DB:
            def __getitem__(self, name):
                return C()
        return DB()

    def test_list_users_counts_and_names(self):
        docs = [
            {"user_id": 1, "first_name": "Alpha", "username": "alpha",
             "date": 100},
            {"user_id": 1, "first_name": "Alpha", "username": "alpha",
             "date": 200},
            {"user_id": 2, "first_name": "Beta", "username": None,
             "date": 50},
        ]
        with patch.object(commands, "get_db", lambda: self._fake_db(docs)):
            lines = commands.list_users()
        joined = "\n".join(lines)
        self.assertIn("2 log yaad hain", joined)
        self.assertIn("Alpha (@alpha) — 2 msgs", joined)
        self.assertIn("Beta — 1 msgs", joined)

    def test_list_users_empty(self):
        with patch.object(commands, "get_db", lambda: self._fake_db([])):
            lines = commands.list_users()
        self.assertIn("koi user memory me nahi", "\n".join(lines))


class PruneNotifyTests(unittest.TestCase):
    def test_prune_notifies_owner(self):
        sent = []

        class C:
            def __init__(self, docs):
                self.docs = docs

            def find(self, flt=None):
                return [d for d in self.docs
                        if all(d.get(k) == v for k, v in (flt or {}).items())]

            def find_one(self, flt=None):
                hits = self.find(flt)
                return hits[0] if hits else {}

            def count_documents(self, flt):
                return len(self.find(flt))

            def delete_many(self, flt):
                n = len(self.find(flt))
                self.docs = [d for d in self.docs
                             if not all(d.get(k) == v
                                        for k, v in (flt or {}).items())]
                return MagicMock(deleted_count=n)

        db = {"messages": C([{"user_id": 7, "first_name": "Ghost",
                               "username": "ghost", "date": 1,
                               "importance": 0, "ingested_at": 1}]),
              "qa": C([]),
              "channel_posts": C([]),
              "escalations": C([])}

        def fake_get_db():
            return db

        class FakeState:
            def __init__(self):
                self.store = {}

            def get_state(self, k, default=None):
                return self.store.get(k, default)

            def save_state(self, k, v):
                self.store[k] = v

        st = FakeState()
        with patch.object(knowledge, "get_db", fake_get_db), \
                patch.object(knowledge, "get_state", st.get_state), \
                patch.object(knowledge, "save_state", st.save_state), \
                patch.object(knowledge.config, "USER_MEMORY_IDLE_DAYS", 7), \
                patch.object(knowledge.tg_api, "send_message",
                             side_effect=lambda cid, text, **kw:
                             sent.append(text)):
            counts = knowledge.prune_once()
        self.assertEqual(counts["inactive_users"], 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("Ghost", sent[0])
        self.assertIn("memory cleanup", sent[0].lower() + " Memory cleanup")


class FastPathTests(unittest.TestCase):
    def test_moderation_no_api_call_on_clean_message(self):
        """Clean message → zero Telegram API calls (regex-first fast path)."""
        msg = {"chat": {"id": -100}, "from": {"id": 5, "first_name": "A"},
               "message_id": 1, "text": "bhai kaise ho sab"}
        with patch.object(moderation.tg_api, "get_chat_member") as gcm, \
                patch.object(moderation, "matches_term", return_value=None), \
                patch.object(moderation, "find_external_promo",
                             return_value=None), \
                patch.object(moderation, "matches_filter", return_value=None), \
                patch.object(moderation, "get_state", lambda k, d=None: d), \
                patch.object(moderation, "get_db", MagicMock()):
            handled = moderation.handle_group_message(msg)
        self.assertFalse(handled)
        gcm.assert_not_called()

    def test_mentions_bot_name_words(self):
        import bot as rk_bot
        with patch.object(rk_bot, "_bot_username", return_value="rkbot"):
            self.assertTrue(rk_bot._mentions_bot("manager kaha gya", []))
            self.assertTrue(rk_bot._mentions_bot("is bot se pucho", []))
            self.assertFalse(rk_bot._mentions_bot("ajkal kamzor din hai", []))


if __name__ == "__main__":
    unittest.main()
