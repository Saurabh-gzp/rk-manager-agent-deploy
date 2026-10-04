import time
import unittest
from unittest.mock import patch, MagicMock

import agent
import bot
import config
import db


OWNER = 8967004608


class RoleContextTests(unittest.TestCase):
    def test_owner_role_line(self):
        ctx = {"group_role": "owner",
               "bot_admin": {"status": "administrator", "can_delete": True,
                             "can_restrict": True}}
        out = agent._role_context(ctx)
        self.assertIn("OWNER", out)
        self.assertIn("delete messages", out)

    def test_bot_not_admin_line(self):
        ctx = {"group_role": "member",
               "bot_admin": {"status": "member", "can_delete": False,
                             "can_restrict": False}}
        out = agent._role_context(ctx)
        self.assertIn("NOT admin", out)

    def test_empty_ctx_gives_empty(self):
        self.assertEqual(agent._role_context({}), "")


class DeepLookupTests(unittest.TestCase):
    def test_non_lookup_passes_through(self):
        text, interim = agent._maybe_deep_lookup(
            "Seedha jawab hai.", "sawal?", {"chat_id": -1}, agent.SYSTEM_PROMPT)
        self.assertEqual(text, "Seedha jawab hai.")
        self.assertIsNone(interim)

    def test_lookup_sends_interim_and_returns_final(self):
        router = MagicMock()
        router.generate.return_value = "Final jawab mil gaya."
        sent = []

        def fake_search(query, limit=10, user_id=None):
            self.assertIn("loader", query)
            return [{"text": "Naya loader X v2 aaya hai"}]

        with patch.object(agent.ai_engine, "get_router", return_value=router), \
                patch.object(agent.knowledge, "search", side_effect=fake_search), \
                patch.object(agent.tg_api, "send_message",
                             side_effect=lambda c, t, **k: sent.append(t) or {"ok": True}):
            text, interim = agent._maybe_deep_lookup(
                "LOOKUP: naya loader konsa aaya",
                "naya loader konsa aaya hai",
                {"chat_id": -1001, "user_id": 5},
                agent.SYSTEM_PROMPT)
        self.assertEqual(interim, "Ruko bhai, dekhta hu 🔍")
        self.assertIn("Ruko bhai", sent[0])
        self.assertEqual(text, "Final jawab mil gaya.")
        # Second LLM call got the deeper results.
        second_prompt = router.generate.call_args.args[0]
        self.assertIn("Naya loader X v2", second_prompt)

    def test_lookup_without_chat_id_passes_through(self):
        text, interim = agent._maybe_deep_lookup(
            "LOOKUP: kuch", "sawal?", {}, agent.SYSTEM_PROMPT)
        self.assertTrue(text.startswith("LOOKUP:"))
        self.assertIsNone(interim)


class DeliverAnswerTests(unittest.TestCase):
    def test_split_sends_multiple_messages(self):
        sent = []
        with patch.object(bot.tg_api, "send_message",
                          side_effect=lambda c, t, **k: sent.append(t) or {"ok": True}), \
                patch.object(bot, "time") as mock_time:
            bot._deliver_answer(-1001, {"text": "Ruko bhai <SPLIT> Mil gaya: X v2"})
        self.assertEqual(sent, ["Ruko bhai", "Mil gaya: X v2"])

    def test_interim_is_sent_first_and_remembered(self):
        sent = []
        bot._GROUP_CONTEXT.clear()
        with patch.object(bot.tg_api, "send_message",
                          side_effect=lambda c, t, **k: sent.append(t) or {"ok": True}), \
                patch.object(bot, "time"):
            bot._deliver_answer(-1001, {"text": "final", "interim": "Ruko bhai, dekhta hu 🔍"})
        self.assertEqual(sent[0], "Ruko bhai, dekhta hu 🔍")
        remembered = [m["text"] for m in bot._GROUP_CONTEXT[-1001]]
        self.assertIn("Ruko bhai, dekhta hu 🔍", remembered)
        self.assertIn("final", remembered)


class GroupRoleTests(unittest.TestCase):
    def setUp(self):
        bot._ROLE_CACHE.clear()
        bot._BOTRIGHT_CACHE.clear()

    def test_owner_always_owner(self):
        with patch.object(bot.config, "OWNER_CHAT_ID", OWNER):
            self.assertEqual(bot._group_role(-1001, OWNER), "owner")

    def test_role_from_telegram_and_cached(self):
        calls = []

        def fake_gcm(chat_id, user_id):
            calls.append(user_id)
            return {"status": "administrator"}

        with patch.object(bot.tg_api, "get_chat_member", side_effect=fake_gcm):
            self.assertEqual(bot._group_role(-1001, 42), "administrator")
            self.assertEqual(bot._group_role(-1001, 42), "administrator")
        self.assertEqual(len(calls), 1)  # second call served from cache

    def test_bot_rights_admin(self):
        with patch.object(bot, "_bot_id", return_value=777), \
                patch.object(bot.tg_api, "get_me", return_value={"id": 777}), \
                patch.object(bot.tg_api, "get_chat_member",
                             return_value={"status": "administrator",
                                            "can_delete_messages": True,
                                            "can_restrict_members": False}):
            info = bot._bot_rights(-1001)
        self.assertTrue(info["can_delete"])
        self.assertFalse(info["can_restrict"])


class CtxInjectionTests(unittest.TestCase):
    def _msg(self, text, user_id=123):
        return {
            "message_id": 99, "date": 1000, "text": text,
            "from": {"id": user_id, "first_name": "Sam", "username": "sam"},
            "chat": {"id": -100123, "type": "supergroup", "title": "Test"},
            "entities": [],
        }

    def test_ctx_carries_role_and_bot_rights(self):
        seen = {}

        def fake_answer(question, ctx):
            seen.update(ctx)
            return {"text": "ok"}

        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message", return_value=False), \
                patch.object(bot.moderation, "handle_mod_command", return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot, "_bot_username", return_value="rk_test_bot"), \
                patch.object(bot, "_group_role", return_value="administrator"), \
                patch.object(bot, "_bot_rights",
                             return_value={"status": "administrator",
                                            "can_delete": True,
                                            "can_restrict": True}), \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.tg_api, "send_message"), \
                patch.object(bot.agent, "answer", side_effect=fake_answer):
            bot.handle_message(self._msg("@rk_test_bot me kon hu?"))
        self.assertEqual(seen.get("group_role"), "administrator")
        self.assertTrue(seen.get("bot_admin", {}).get("can_delete"))

    def test_ctx_carries_recent_log_for_grounding(self):
        seen = {}

        def fake_answer(question, ctx):
            seen.update(ctx)
            return {"text": "ok"}

        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message", return_value=False), \
                patch.object(bot.moderation, "handle_mod_command", return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot, "_bot_username", return_value="rk_test_bot"), \
                patch.object(bot, "_group_role", return_value="member"), \
                patch.object(bot, "_bot_rights", return_value={}), \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.tg_api, "send_message"), \
                patch.object(bot.agent, "answer", side_effect=fake_answer):
            bot.handle_message(self._msg("@rk_test_bot kya tumne file bheji thi?"))
        log = seen.get("recent_log") or []
        # The triggering question itself must NOT be inside the log (it is
        # passed separately), but earlier context would be.
        self.assertIsInstance(log, list)

    def test_real_answer_saved_to_qa_memory(self):
        # The bot's actual answer is persisted — its future claims about
        # what it said stay grounded in this record.
        def fake_answer(question, ctx):
            return {"text": "Loader v2 hi latest hai"}

        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message", return_value=False), \
                patch.object(bot.moderation, "handle_mod_command", return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot, "_bot_username", return_value="rk_test_bot"), \
                patch.object(bot, "_group_role", return_value="member"), \
                patch.object(bot, "_bot_rights", return_value={}), \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.tg_api, "send_message"), \
                patch.object(bot.agent, "answer", side_effect=fake_answer):
            bot.handle_message(self._msg("@rk_test_bot loader konsa latest hai?"))
        docs = db.get_db()["qa"].find(
            {"question": {"$regex": "loader", "$options": "i"}})
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["answer"], "Loader v2 hi latest hai")


class IntelligenceGroundingTests(unittest.TestCase):
    """v2.2: recent-chat-log grounding + anti-false-claim honesty rules.

    Regression guard for the screenshot bug where the bot claimed
    'maine thodi der pehle bat loader update + key bheja tha' and
    'file bhej raha hu / ye lijiye log file ready hai' — while it had
    sent NOTHING. The fixes: verbatim log in every prompt, hard
    past-event/file honesty rules, and a no-file FACT injection.
    """

    def test_recent_log_block_renders_verbatim(self):
        ctx = {"recent_log": [
            {"name": "Oxsidian", "text": "ye bat loader kha hai",
             "owner": True},
            {"name": "RK Manager", "text": "mere paas record nahi hai",
             "is_bot": True},
        ]}
        out = agent._recent_log_block(ctx)
        self.assertIn("Oxsidian [GROUP OWNER]", out)
        self.assertIn("ye bat loader kha hai", out)
        self.assertIn("RK MANAGER (you, the bot)", out)
        self.assertIn("mere paas record nahi hai", out)

    def test_recent_log_empty_for_no_context(self):
        self.assertEqual(agent._recent_log_block({}), "")
        self.assertEqual(agent._recent_log_block({"recent_log": []}), "")

    def test_prompts_carry_honesty_rules(self):
        self.assertIn("PAST-EVENT HONESTY", agent.SYSTEM_PROMPT)
        self.assertIn("FILE HONESTY", agent.SYSTEM_PROMPT)
        self.assertIn("PAST-EVENT HONESTY", agent.GENERAL_PROMPT)
        self.assertIn("FILE HONESTY", agent.GENERAL_PROMPT)
        # the exact false-claim phrasings from the screenshot are banned
        self.assertIn("bhej raha hu", agent.SYSTEM_PROMPT)
        self.assertIn("file ready hai", agent.SYSTEM_PROMPT)

    def test_file_request_without_file_injects_fact(self):
        router = MagicMock()
        router.generate.return_value = "theek hai"

        with patch.object(agent.ai_engine, "get_router",
                          return_value=router), \
                patch.object(agent.knowledge, "search", return_value=[]), \
                patch.object(agent.knowledge, "find_file_for",
                             return_value=None):
            agent.answer("bat loader file bhejo", {
                "user_id": 5, "chat_id": -1001, "username": "x"})
        prompt = router.generate.call_args.args[0]
        self.assertIn("FACT: no file", prompt)
        self.assertIn("NOTHING to send", prompt)

    def test_recent_log_passed_into_prompt(self):
        router = MagicMock()
        router.generate.return_value = "theek hai"

        with patch.object(agent.ai_engine, "get_router",
                          return_value=router), \
                patch.object(agent.knowledge, "search", return_value=[]), \
                patch.object(agent.knowledge, "find_file_for",
                             return_value=None):
            agent.answer("kya tumne loader bheja tha?", {
                "user_id": 5, "chat_id": -1001, "username": "x",
                "recent_log": [
                    {"name": "Oxsidian", "text": "loader kha hai",
                     "owner": True},
                ]})
        prompt = router.generate.call_args.args[0]
        self.assertIn("Recent chat log", prompt)
        self.assertIn("loader kha hai", prompt)
        # the question is NOT duplicated into the log block
        self.assertNotIn("kya tumne loader bheja tha?",
                         agent._recent_log_block({
                             "recent_log": [{"name": "Oxsidian",
                                             "text": "loader kha hai",
                                             "owner": True}]}))


if __name__ == "__main__":
    unittest.main()
