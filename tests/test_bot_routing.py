import time
import unittest
from unittest.mock import patch

import bot


def message(text, user_id=123):
    return {
        "message_id": 99,
        "date": 1000,
        "text": text,
        "from": {"id": user_id, "first_name": "Sam", "username": "sam"},
        "chat": {"id": -100123, "type": "supergroup", "title": "Test"},
        "entities": [],
    }


def private_message(text, user_id=123):
    return {
        "message_id": 7,
        "date": 1000,
        "text": text,
        "from": {"id": user_id, "first_name": "Ox", "username": "ox"},
        "chat": {"id": user_id, "type": "private"},
        "entities": [],
    }


class BotRoutingTests(unittest.TestCase):
    def setUp(self):
        # v4.0: budget/min-gap state across tests isolate karo (undirected
        # replies ab timestamps track karte hain — stale entries bina clear
        # ke agle test ka reply min-gap se block kar dete hain)
        bot._REPLY_TIMES.clear()
        bot._GROUP_CONTEXT.clear()
        self.addCleanup(bot._REPLY_TIMES.clear)
        self.addCleanup(bot._GROUP_CONTEXT.clear)

    def _base_patches(self):
        return (
            patch.object(bot.links, "is_managed_group", return_value=True),
            patch.object(bot.moderation, "handle_group_message", return_value=False),
            patch.object(bot.knowledge, "ingest_message"),
            patch.object(bot, "_bot_username", return_value="rk_test_bot"),
            patch.object(bot.tg_api, "send_chat_action"),
            patch.object(bot.tg_api, "send_message"),
        )

    def test_plain_greeting_is_ingested_but_does_not_get_reply(self):
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(message("hyy"))
        answer.assert_not_called()

    def test_natural_question_reaches_agent_and_answerer(self):
        # v3.4: undirected '?'-questions no longer bypass the reply gate
        # (that was the '2 users ki baat-cheet me bahot message' root cause).
        # They reach the agent only when the LLM gate approves them.
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patch.object(bot, "_should_reply_llm", return_value=True) as gate, \
                patch.object(bot.agent, "answer", return_value={
                    "text": "It starts at 6.", "grounded": True,
                }) as answer:
            bot.handle_message(message("What time does it start?"))
        gate.assert_called_once()
        answer.assert_called_once()
        self.assertEqual(answer.call_args.args[0], "What time does it start?")
        self.assertEqual(answer.call_args.args[1]["user_id"], 123)

    def test_no_auto_escalation_bot_replies_itself(self):
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot, "_should_reply_llm", return_value=True), \
                patch.object(bot.agent, "answer", return_value={
                    "text": "Mera jawab ye hai.", "grounded": False,
                }), patch.object(bot.escalation, "open_escalation") as esc:
            bot.handle_message(message("When is the next meeting?"))
        esc.assert_not_called()
        send.assert_called()
        self.assertEqual(send.call_args.args[1], "Mera jawab ye hai.")

    def test_admin_command_opens_manual_escalation(self):
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot.escalation, "open_escalation",
                             return_value="abc123") as esc, \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(message("/admin mujhe help chahiye"))
        esc.assert_called_once()
        self.assertEqual(esc.call_args.kwargs["user_id"], 123)
        self.assertEqual(esc.call_args.kwargs["question"], "mujhe help chahiye")
        answer.assert_not_called()
        self.assertIn("Admin ko bhej diya", send.call_args.args[1])

    def test_llm_gate_answers_when_member_talks_to_bot(self):
        # "managar mujhse baat kro" — no /ask, no @mention; the LLM gate
        # decides it is addressed to the bot.
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot, "_should_reply_llm",
                             return_value=True) as gate, \
                patch.object(bot.agent, "answer", return_value={
                    "text": "Haan bolo, kya help chahiye?", "grounded": True,
                }) as answer:
            bot.handle_message(message("managar mujhse baat kro"))
        gate.assert_called_once()
        answer.assert_called_once()
        self.assertEqual(send.call_args.args[1], "Haan bolo, kya help chahiye?")

    def test_llm_gate_stays_silent_on_member_chatter(self):
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot, "_should_reply_llm",
                             return_value=False) as gate, \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(message("are ye abhi chota bachha hai"))
        gate.assert_called_once()
        answer.assert_not_called()
        send.assert_not_called()

    def test_gate_prompt_includes_recent_conversation(self):        # The reported case: bot asked "batao kya help chahiye", owner
        # replied "me owner hu tum kya mujhe doge" — the gate must see the
        # follow-up in context.
        bot._GROUP_CONTEXT.clear()
        bot._remember(-100123, "RK Manager", "batao kya help chahiye",
                      is_bot=True)
        bot._remember(-100123, "Oxsidian X", "ha ab krega baat hogya fix",
                      is_owner=True)
        bot._remember(-100123, "Oxsidian X", "me owner hu tum kya mujhe doge",
                      is_owner=True)
        with patch.object(bot.ai_engine, "get_router") as router:
            router.return_value.generate.return_value = "YES"
            result = bot._should_reply_llm("me owner hu tum kya mujhe doge",
                                           "Oxsidian X", -100123,
                                           is_owner=True)
        self.assertTrue(result)
        prompt = router.return_value.generate.call_args.args[0]
        self.assertIn("batao kya help chahiye", prompt)
        self.assertIn("me owner hu tum kya mujhe doge", prompt)
        self.assertIn("RK Manager (you)", prompt)
        self.assertIn("[OWNER]", prompt)
        bot._GROUP_CONTEXT.clear()

    def test_channel_post_ingestion_never_sends_a_message(self):
        with patch.object(bot.links, "is_managed_channel", return_value=True), \
                patch.object(bot.knowledge, "ingest_channel_post") as ingest, \
                patch.object(bot.tg_api, "send_message") as send:
            bot.handle_channel_post({
                "chat": {"id": -100777, "type": "channel"},
                "message_id": 4,
                "text": "Read-only post",
            })
        ingest.assert_called_once()
        send.assert_not_called()

    def test_cancel_clears_stuck_forward_mode(self):
        store = {"123:group_fwd_mode": time.time()}
        get = lambda k, d=None: store.get(k, d)  # noqa: E731
        save = lambda k, v: store.__setitem__(k, v)  # noqa: E731
        with patch.object(bot.config, "OWNER_CHAT_ID", 123), \
                patch.object(bot.db, "get_state", side_effect=get), \
                patch.object(bot.db, "save_state", side_effect=save), \
                patch.object(bot.tg_api, "send_message") as send, \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(private_message("/cancel"))
        self.assertFalse(store["123:group_fwd_mode"])
        self.assertIn("Cancelled", send.call_args.args[1])
        answer.assert_not_called()

    def test_expired_forward_mode_falls_through_to_chat(self):
        store = {"123:group_fwd_mode": time.time() - 700}  # 11 min ago
        get = lambda k, d=None: store.get(k, d)  # noqa: E731
        save = lambda k, v: store.__setitem__(k, v)  # noqa: E731
        with patch.object(bot.config, "OWNER_CHAT_ID", 123), \
                patch.object(bot.db, "get_state", side_effect=get), \
                patch.object(bot.db, "save_state", side_effect=save), \
                patch.object(bot.tg_api, "send_message") as send, \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.agent, "answer",
                             return_value={"text": "Haan bolo!",
                                            "grounded": True}) as answer, \
                patch.object(bot.escalation, "handle_admin_message",
                             return_value=False):
            bot.handle_message(private_message("hy"))
        self.assertFalse(store["123:group_fwd_mode"])  # stale mode cleared
        answer.assert_called_once()
        self.assertEqual(send.call_args.args[1], "Haan bolo!")


if __name__ == "__main__":
    unittest.main()