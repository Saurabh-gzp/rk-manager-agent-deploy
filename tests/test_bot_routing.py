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


class BotRoutingTests(unittest.TestCase):
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
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patch.object(bot.agent, "answer", return_value={
                    "text": "It starts at 6.", "grounded": True,
                }) as answer:
            bot.handle_message(message("What time does it start?"))
        answer.assert_called_once()
        self.assertEqual(answer.call_args.args[0], "What time does it start?")
        self.assertEqual(answer.call_args.args[1]["user_id"], 123)

    def test_unknown_answer_escalates_original_question_and_user(self):
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patch.object(bot.agent, "answer", return_value={
                    "text": "I don't know.", "grounded": False,
                }), patch.object(bot.escalation, "open_escalation", return_value="abc123") as esc:
            bot.handle_message(message("When is the next meeting?"))
        esc.assert_called_once()
        self.assertEqual(esc.call_args.kwargs["user_id"], 123)
        self.assertEqual(esc.call_args.kwargs["question"], "When is the next meeting?")

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


if __name__ == "__main__":
    unittest.main()