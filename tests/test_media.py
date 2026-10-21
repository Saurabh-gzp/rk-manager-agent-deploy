import base64
import unittest
from unittest.mock import patch, MagicMock

import agent
import ai_engine
import bot
import config
import tg_api


OWNER = 8967004608
CHAT = -1001430303536097


def group_msg(text=None, user_id=123, **kw):
    m = {
        "message_id": 50, "date": 1000,
        "from": {"id": user_id, "first_name": "Sam", "username": "sam"},
        "chat": {"id": CHAT, "type": "supergroup", "title": "Test"},
        "entities": [],
    }
    if text is not None:
        m["text"] = text
    m.update(kw)
    return m


class MediaKindsTests(unittest.TestCase):
    def test_detects(self):
        self.assertEqual(bot._media_kinds({"photo": [{"file_id": "a"}]}), ["photo"])
        self.assertEqual(bot._media_kinds({"voice": {"file_id": "v"}}), ["voice"])
        self.assertEqual(bot._media_kinds({"video": {"file_id": "x"}}), ["video"])
        self.assertEqual(bot._media_kinds({"video_note": {"file_id": "x"}}), ["video"])
        self.assertEqual(bot._media_kinds({"text": "hi"}), [])


class DownloadMediaTests(unittest.TestCase):
    def test_photo_downloaded(self):
        msg = {"photo": [{"file_id": "small", "file_size": 100},
                         {"file_id": "big", "file_size": 9999}]}
        with patch.object(bot.tg_api, "download_file",
                          return_value=b"imgbytes") as dl:
            media, err = bot._download_media(msg)
        dl.assert_called_once_with("big", max_bytes=bot._MAX_MEDIA_BYTES)
        self.assertIsNone(err)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["mime_type"], "image/jpeg")
        self.assertEqual(base64.b64decode(media[0]["data"]), b"imgbytes")

    def test_voice_too_long(self):
        msg = {"voice": {"file_id": "v", "duration": 601}}
        with patch.object(bot.tg_api, "download_file") as dl:
            media, err = bot._download_media(msg)
        dl.assert_not_called()
        self.assertEqual(media, [])
        self.assertIn("10 minute", err)

    def test_video_too_long(self):
        msg = {"video": {"file_id": "x", "duration": 9999}}
        media, err = bot._download_media(msg)
        self.assertEqual(media, [])
        self.assertIn("lambi", err)

    def test_download_failure(self):
        msg = {"photo": [{"file_id": "a", "file_size": 10}]}
        with patch.object(bot.tg_api, "download_file", return_value=None):
            media, err = bot._download_media(msg)
        self.assertEqual(media, [])
        self.assertIsNotNone(err)


class GenerateMmTests(unittest.TestCase):
    def _router_with(self, gemini_keys):
        r = ai_engine.AIRouter.__new__(ai_engine.AIRouter)
        r.providers = []
        if gemini_keys:
            r.providers.append(ai_engine._Provider("gemini", "gemini-x",
                                                  gemini_keys, "vision"))
        return r

    def test_mm_uses_gemini_only(self):
        router = self._router_with(["k1"])
        captured = {}

        def fake_mm(key, model, prompt, system, max_tokens, timeout, media):
            captured["media"] = media
            captured["prompt"] = prompt
            return "dekha!", ""

        with patch.object(ai_engine, "_gemini_call_mm", side_effect=fake_mm):
            out = router.generate_mm("sawal", [{"mime_type": "image/jpeg",
                                                "data": "AAA", "kind": "photo"}])
        self.assertEqual(out, "dekha!")
        self.assertIn("AAA", str(captured["media"]))
        self.assertIn("sawal", captured["prompt"])

    def test_mm_no_gemini_raises(self):
        router = self._router_with([])
        with self.assertRaises(ai_engine.NoProvidersError):
            router.generate_mm("sawal", [])

    def test_mm_wire_format_is_camelcase(self):
        bodies = []

        def fake_post(key, model, body, timeout):
            bodies.append(body)
            return "ok!", ""

        router = self._router_with(["k1"])
        with patch.object(ai_engine, "_gemini_post", side_effect=fake_post):
            router.generate_mm("sawal", [{"mime_type": "image/jpeg",
                                          "data": "AAA", "kind": "photo"}])
        raw = str(bodies[0])
        self.assertNotIn("inline_data", raw)
        self.assertNotIn("mime_type", raw)
        parts = bodies[0]["contents"][0]["parts"]
        inline = [p for p in parts if "inlineData" in p]
        self.assertEqual(len(inline), 1)
        self.assertEqual(inline[0]["inlineData"]["mimeType"], "image/jpeg")
        self.assertEqual(inline[0]["inlineData"]["data"], "AAA")


class GenerateAnswerTests(unittest.TestCase):
    def test_with_media_calls_mm(self):
        router = MagicMock()
        media = [{"mime_type": "image/jpeg", "data": "AAA", "kind": "photo"}]
        with patch.object(agent.ai_engine, "get_router", return_value=router):
            agent._generate_answer("prompt", agent.SYSTEM_PROMPT,
                                   {"media": media})
        router.generate_mm.assert_called_once()
        sent_prompt = router.generate_mm.call_args.args[0]
        self.assertIn("photo", sent_prompt)
        router.generate.assert_not_called()

    def test_without_media_calls_generate(self):
        router = MagicMock()
        with patch.object(agent.ai_engine, "get_router", return_value=router):
            agent._generate_answer("prompt", agent.SYSTEM_PROMPT, {})
        router.generate.assert_called_once()
        router.generate_mm.assert_not_called()


class GroupMediaFlowTests(unittest.TestCase):
    def _patches(self):
        return (
            patch.object(bot.links, "is_managed_group", return_value=True),
            patch.object(bot.moderation, "handle_group_message", return_value=False),
            patch.object(bot.moderation, "handle_mod_command", return_value=False),
            patch.object(bot.knowledge, "ingest_message"),
            patch.object(bot, "_bot_username", return_value="rk_test_bot"),
            patch.object(bot.tg_api, "send_chat_action"),
            patch.object(bot.tg_api, "send_message"),
        )

    def test_directed_photo_reaches_agent_with_media(self):
        seen = {}

        def fake_answer(question, ctx):
            seen.update(ctx)
            return {"text": "ok"}

        msg = group_msg("@rk_test_bot ye kya hai?",
                        photo=[{"file_id": "p1", "file_size": 50}])
        p = self._patches()
        with p[0], p[1], p[2], p[3], p[4], p[5], p[6], \
                patch.object(bot, "_download_media",
                             return_value=([{"mime_type": "image/jpeg",
                                             "data": "AAA",
                                             "kind": "photo"}], None)), \
                patch.object(bot.agent, "answer", side_effect=fake_answer):
            bot.handle_message(msg)
        self.assertEqual(len(seen.get("media", [])), 1)
        self.assertEqual(seen["media"][0]["kind"], "photo")

    def test_media_download_failure_sends_error(self):
        msg = group_msg("@rk_test_bot dekho",
                        photo=[{"file_id": "p1", "file_size": 50}])
        p = self._patches()
        with p[0], p[1], p[2], p[3], p[4], p[5], p[6] as send, \
                patch.object(bot, "_download_media",
                             return_value=([], "Photo download nahi ho paya")), \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(msg)
        answer.assert_not_called()
        self.assertIn("download nahi", send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
