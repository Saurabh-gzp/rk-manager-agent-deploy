import time
import unittest
from unittest.mock import patch

import db
import knowledge
import moderation


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.db_patch = patch.object(db, "_DB", db.IMDB())
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()

    def test_group_memory_search_is_scoped_to_user(self):
        store = db.get_db()
        store["messages"].insert_one({
            "user_id": 11, "text": "my blue rocket plan", "date": 100,
        })
        store["messages"].insert_one({
            "user_id": 22, "text": "my blue rocket plan", "date": 101,
        })
        store["channel_posts"].insert_one({
            "text": "shared blue rocket post", "date": 102,
        })

        results = knowledge.search("blue rocket", user_id=11)
        self.assertEqual(
            [item["text"] for item in results if item["kind"] == "message"],
            ["my blue rocket plan"],
        )
        self.assertTrue(any(item["kind"] == "post" for item in results))

    def test_inactive_user_history_and_qa_are_removed_after_idle_limit(self):
        now = 2_000_000_000
        idle_seconds = knowledge.config.USER_MEMORY_IDLE_DAYS * 86400
        store = db.get_db()
        store["messages"].insert_one({
            "user_id": 11, "text": "old history", "date": now - idle_seconds - 1,
        })
        store["messages"].insert_one({
            "user_id": 22, "text": "recent history", "date": now - 60,
        })
        store["qa"].insert_one({"user_id": 11, "question": "old", "answer": "old"})
        store["qa"].insert_one({"user_id": 22, "question": "recent", "answer": "recent"})

        with patch("knowledge.time.time", return_value=now):
            result = knowledge.prune_once()

        self.assertEqual(result["inactive_users"], 1)
        self.assertEqual(store["messages"].count_documents({"user_id": 11}), 0)
        self.assertEqual(store["qa"].count_documents({"user_id": 11}), 0)
        self.assertEqual(store["messages"].count_documents({"user_id": 22}), 1)
        self.assertEqual(store["qa"].count_documents({"user_id": 22}), 1)


class ModerationTests(unittest.TestCase):
    def setUp(self):
        self.db_patch = patch.object(db, "_DB", db.IMDB())
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()

    def test_terms_match_whole_words_only(self):
        self.assertEqual(moderation.matches_term("you said BC!", ["bc"]), "bc")
        self.assertIsNone(moderation.matches_term("abcthing", ["bc"]))

    def test_first_violation_warns_and_repeat_mutes_without_ban(self):
        message = {
            "chat": {"id": -100123},
            "from": {"id": 42, "first_name": "Sam"},
            "message_id": 7,
            "text": "fuck",
        }
        with (
            patch.object(moderation.tg_api, "get_chat_member", return_value={"status": "member"}),
            patch.object(moderation.tg_api, "delete_message", return_value={"ok": True}),
            patch.object(moderation.tg_api, "send_message") as send,
            patch.object(moderation.tg_api, "restrict_chat_member", return_value={"ok": True}) as restrict,
        ):
            self.assertTrue(moderation.handle_group_message(message))
            restrict.assert_not_called()
            self.assertIn("Repeat", send.call_args.args[1])

            message["message_id"] = 8
            self.assertTrue(moderation.handle_group_message(message))
            restrict.assert_called_once()
            self.assertEqual(restrict.call_args.args[2]["can_send_messages"], False)


if __name__ == "__main__":
    unittest.main()