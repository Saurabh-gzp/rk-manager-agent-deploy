import unittest
from unittest.mock import patch, MagicMock

import agent
import bot
import config


OWNER = 8967004608
CHAT = -1001430303536097


def group_msg(text, user_id=123, reply_to=None):
    m = {
        "message_id": 50, "date": 1000, "text": text,
        "from": {"id": user_id, "first_name": "Sam", "username": "sam"},
        "chat": {"id": CHAT, "type": "supergroup", "title": "Test"},
        "entities": [],
    }
    if reply_to:
        m["reply_to_message"] = reply_to
    return m


class ModRequestDetectionTests(unittest.TestCase):
    def test_detects(self):
        for t in ["isko mute kar do", "ye msg delete kr do", "ban kar do ise",
                  "ye wala msg uda de", "unmute kardo", "message hatao"]:
            self.assertTrue(bot._looks_like_mod_request(t), t)

    def test_rejects(self):
        for t in ["hello bhai", "ban gaya party me", "delete kaise karte hain",
                  "kal maine file delete kar di thi", ""]:
            self.assertFalse(bot._looks_like_mod_request(t), t)


class SpokenDurationTests(unittest.TestCase):
    def test_parses(self):
        self.assertEqual(bot._spoken_duration("10 min ke liye mute"), "10m")
        self.assertEqual(bot._spoken_duration("2 ghante"), "2h")
        self.assertEqual(bot._spoken_duration("1 din"), "1d")
        self.assertEqual(bot._spoken_duration("30s"), "30s")

    def test_none(self):
        self.assertIsNone(bot._spoken_duration("hamesha ke liye"))
        self.assertIsNone(bot._spoken_duration("hello"))


class SynthesizeTests(unittest.TestCase):
    def setUp(self):
        self._u = patch.object(bot, "_bot_username", return_value="rk_test_bot")
        self._u.start()

    def tearDown(self):
        self._u.stop()

    def test_delete_via_reply(self):
        reply = {"message_id": 377, "from": {"id": 42, "first_name": "X"}}
        s = bot._synthesize_owner_command("ye msg delete kr do",
                                         group_msg("ye msg delete kr do", OWNER, reply), CHAT)
        self.assertEqual(s["text"], "/delete")
        self.assertEqual(s["reply_to_message"]["message_id"], 377)

    def test_delete_via_link(self):
        s = bot._synthesize_owner_command(
            "apna ye wala msg delete kr : https://t.me/c/1430303536097/377",
            group_msg("x", OWNER), CHAT)
        self.assertEqual(s["text"], "/delete")
        self.assertEqual(s["reply_to_message"]["message_id"], 377)

    def test_link_wrong_chat_rejected(self):
        s = bot._synthesize_owner_command(
            "delete kr https://t.me/c/9999999999/5", group_msg("x", OWNER), CHAT)
        self.assertIsNone(s)

    def test_mute_with_duration(self):
        reply = {"message_id": 10, "from": {"id": 42, "first_name": "X"}}
        s = bot._synthesize_owner_command("isko 10 min ke liye mute kar do",
                                         group_msg("x", OWNER, reply), CHAT)
        self.assertEqual(s["text"], "/tmute 10m")

    def test_mute_no_duration(self):
        reply = {"message_id": 10, "from": {"id": 42, "first_name": "X"}}
        s = bot._synthesize_owner_command("isko mute kar do",
                                         group_msg("x", OWNER, reply), CHAT)
        self.assertEqual(s["text"], "/mute")

    def test_no_target_gives_none(self):
        self.assertIsNone(bot._synthesize_owner_command(
            "usko ban kar do", group_msg("usko ban kar do", OWNER), CHAT))

    def test_ban_via_reply(self):
        reply = {"message_id": 10, "from": {"id": 42, "first_name": "X"}}
        s = bot._synthesize_owner_command("isko ban kar do",
                                         group_msg("x", OWNER, reply), CHAT)
        self.assertEqual(s["text"], "/ban")

    def test_bot_mention_is_not_a_target(self):
        with patch.object(bot, "_bot_username", return_value="rk_test_bot"):
            s = bot._synthesize_owner_command(
                "@rk_test_bot usko ban kar do",
                group_msg("@rk_test_bot usko ban kar do", OWNER), CHAT)
        self.assertIsNone(s)


class ConversationalModFlowTests(unittest.TestCase):
    def _patches(self):
        return (
            patch.object(bot.links, "is_managed_group", return_value=True),
            patch.object(bot.moderation, "handle_group_message", return_value=False),
            patch.object(bot.knowledge, "ingest_message"),
            patch.object(bot, "_bot_username", return_value="rk_test_bot"),
            patch.object(bot.tg_api, "send_chat_action"),
            patch.object(bot.tg_api, "send_message"),
        )

    def _directed(self, text, user_id, reply_to=None):
        # mention the bot so the message counts as directed (skips the gate)
        return group_msg(f"@rk_test_bot {text}", user_id, reply_to)

    def test_member_request_refused_without_llm(self):
        p = self._patches()
        with p[0], p[1], p[2], p[3], p[4], p[5] as send, \
                patch.object(bot.agent, "answer") as answer, \
                patch.object(bot.config, "OWNER_CHAT_ID", OWNER):
            bot.handle_message(self._directed("isko mute kar do", 123,
                              {"message_id": 9, "from": {"id": 42}}))
        answer.assert_not_called()
        sent_text = send.call_args.args[1]
        self.assertIn("sirf owner", sent_text)

    def test_owner_delete_reply_executes_real_command(self):
        p = self._patches()
        reply = {"message_id": 377, "from": {"id": 42, "first_name": "X"}}

        def fake_hmc(msg, uid, cid):
            # real hook behavior: only /commands are handled
            return (msg.get("text") or "").startswith("/")

        with p[0], p[1], p[2], p[3], p[4], p[5], \
                patch.object(bot.agent, "answer") as answer, \
                patch.object(bot.moderation, "handle_mod_command",
                             side_effect=fake_hmc) as hmc, \
                patch.object(bot.config, "OWNER_CHAT_ID", OWNER):
            bot.handle_message(self._directed("ye msg delete kr do", OWNER, reply))
        answer.assert_not_called()
        synth_calls = [c for c in hmc.call_args_list
                       if c.args[0].get("text") == "/delete"]
        self.assertEqual(len(synth_calls), 1)
        synth = synth_calls[0].args[0]
        self.assertEqual(synth["reply_to_message"]["message_id"], 377)

    def test_owner_no_target_asks_for_target_not_llm(self):
        p = self._patches()

        def fake_hmc(msg, uid, cid):
            return (msg.get("text") or "").startswith("/")

        with p[0], p[1], p[2], p[3], p[4], p[5] as send, \
                patch.object(bot.agent, "answer",
                             return_value={"text": "ok"}) as answer, \
                patch.object(bot.moderation, "handle_mod_command",
                             side_effect=fake_hmc) as hmc, \
                patch.object(bot.config, "OWNER_CHAT_ID", OWNER):
            bot.handle_message(self._directed("usko ban kar do", OWNER))
        self.assertFalse(any(c.args[0].get("text", "").startswith("/")
                             for c in hmc.call_args_list))
        # No target -> the bot must ASK for the target, never let the
        # LLM claim the action ("karta hu" fake-claim bug fix).
        answer.assert_not_called()
        sent = " ".join(str(c.args[1]) for c in send.call_args_list)
        self.assertIn("kis par", sent)


class PromptGuardrailTests(unittest.TestCase):
    def test_action_honesty_in_system_prompt(self):
        self.assertIn("NEVER write 'delete kar diya'", agent.SYSTEM_PROMPT)

    def test_lookup_honesty_in_followup(self):
        router = MagicMock()
        router.generate.side_effect = lambda prompt, system=None: f"CAPTURED::{prompt[:50]}"
        with patch.object(agent.ai_engine, "get_router", return_value=router), \
                patch.object(agent.knowledge, "search", return_value=[]), \
                patch.object(agent.tg_api, "send_message", return_value={"ok": True}):
            agent._maybe_deep_lookup("LOOKUP: deepseek bot hai kya?", "q?",
                                     {"chat_id": -1}, agent.SYSTEM_PROMPT)
        second_prompt = router.generate.call_args.args[0]
        self.assertIn("NOT live Telegram state", second_prompt)


if __name__ == "__main__":
    unittest.main()
