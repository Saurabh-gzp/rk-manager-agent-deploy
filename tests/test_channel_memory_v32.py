"""v3.2 CHANNEL MEMORY + link-preview tests.

Locks in:
  1. Owner rule: link preview OFF in EVERY bot text (sendMessage /
     editMessageText / copyMessage carry link_preview_options disabled).
  2. Channel history backfill makes OLD posts deliverable:
     'crown loader de do' -> REAL forward of the BEASTCROWN post,
     'zyrox photo chahiye' -> fullwidth ＺＹＲＯＸ post matches (NFKC),
     'latest post dikhao' -> highest message_id,
     '/get crown' -> name search.
  3. Anti-mis-fire guards: 'key dedo' (generic only) and casual talk
     ('mujhe crown pasand nahi') never auto-forward.
  4. Album posts forward the WHOLE album, first one replies to the asker.
  5. Live channel ingestion stores file_name / links / media_group_id
     (parity with the backfill schema).
"""
import unittest
from unittest.mock import patch

import bot
import channel_forward
import config
import db
import knowledge
import tg_api

OWNER = 8967004608
BOT_ID = 8781539682
CHAT = -100123
CHAN = -100777


def group_message(text, user_id=123, message_id=99):
    return {
        "message_id": message_id,
        "date": 1000,
        "text": text,
        "from": {"id": user_id, "first_name": "Sam", "username": "sam"},
        "chat": {"id": CHAT, "type": "supergroup", "title": "Test"},
        "entities": [],
    }


class IMDBTestCase(unittest.TestCase):
    """Fresh in-memory store + owner id for every test."""

    def setUp(self):
        self._db = db.IMDB()
        self.p_db = patch.object(db, "_DB", self._db)
        self.p_db.start()
        db.invalidate_state_cache()
        self.p_owner = patch.object(config, "OWNER_CHAT_ID", OWNER)
        self.p_owner.start()
        # module-level state must not leak between tests
        channel_forward._LAST_LINK.clear()
        bot._GROUP_CONTEXT.clear()
        self.addCleanup(self.p_db.stop)
        self.addCleanup(self.p_owner.stop)
        self.addCleanup(db.invalidate_state_cache)
        self.addCleanup(channel_forward._LAST_LINK.clear)
        self.addCleanup(bot._GROUP_CONTEXT.clear)

    def _link_channel(self, username="testchan"):
        db.save_state("channel_id", CHAN)
        db.save_state("managed_channel_username", username)

    def _seed_post(self, message_id, text="", caption="", file_name=None,
                   file_type=None, file_id="f1", media_group_id=None,
                   links=None, date=1000):
        doc = {
            "channel_id": CHAN,
            "message_id": message_id,
            "text": text or caption,
            "caption": caption,
            "file_id": file_id,
            "file_type": file_type,
            "file_name": file_name,
            "links": links or [],
            "media_group_id": media_group_id,
            "date": date,
        }
        self._db["channel_posts"].insert_one(doc)
        return doc


# ---------------- link preview OFF everywhere ----------------

class NoLinkPreviewTests(unittest.TestCase):

    def test_send_message_sends_disabled_preview(self):
        with patch.object(tg_api, "api", return_value={"ok": True}) as m:
            tg_api.send_message(CHAT, "dekho https://spoo.me/slIpCW")
        p = m.call_args[0][1]
        self.assertEqual(p["link_preview_options"], {"is_disabled": True})

    def test_send_message_custom_preview_still_possible(self):
        with patch.object(tg_api, "api", return_value={"ok": True}) as m:
            tg_api.send_message(
                CHAT, "x", link_preview_options={"is_disabled": False})
        p = m.call_args[0][1]
        self.assertEqual(p["link_preview_options"], {"is_disabled": False})

    def test_edit_message_text_sends_disabled_preview(self):
        with patch.object(tg_api, "api", return_value={"ok": True}) as m:
            tg_api.edit_message_text(CHAT, 5, "edit https://spoo.me/x")
        p = m.call_args[0][1]
        self.assertEqual(p["link_preview_options"], {"is_disabled": True})

    def test_copy_message_sends_disabled_preview(self):
        with patch.object(tg_api, "api", return_value={"ok": True}) as m:
            tg_api.copy_message(CHAT, CHAN, 245)
        p = m.call_args[0][1]
        self.assertEqual(p["link_preview_options"], {"is_disabled": True})

    def test_forward_message_accepts_reply_linkage(self):
        with patch.object(tg_api, "api", return_value={"ok": True}) as m:
            tg_api.forward_message(CHAT, CHAN, 245,
                                   reply_to_message_id=99)
        p = m.call_args[0][1]
        self.assertEqual(p["reply_to_message_id"], 99)


# ---------------- name-based delivery from channel history ----------------

class NameDeliveryTests(IMDBTestCase):

    def _run_group(self, text, user_id=123):
        fwd = []
        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message",
                             return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot, "_bot_username", return_value="rk_test_bot"), \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.tg_api, "send_message",
                             return_value={"ok": True}), \
                patch.object(bot.tg_api, "forward_message",
                             side_effect=lambda to, src, mid, **kw:
                             fwd.append((to, src, mid, kw)) or {"ok": True}), \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(group_message(text, user_id=user_id))
        return fwd, answer

    def test_crown_loader_request_forwards_beastcrown_post(self):
        """THE owner scenario: member bole 'crown loader de do' -> the
        BEASTCROWN APK post (backfilled) is delivered, LLM never asked."""
        self._link_channel()
        self._seed_post(245, caption="supports login via facebook",
                        file_name="BEASTCROWN~JAVA.apk", file_type="document")
        self._seed_post(100, caption="old text post", file_name=None,
                        file_id=None)
        fwd, answer = self._run_group("bhai crown loader de do")
        self.assertEqual(len(fwd), 1)
        self.assertEqual(fwd[0][1], CHAN)
        self.assertEqual(fwd[0][2], 245)
        self.assertEqual(fwd[0][3].get("reply_to_message_id"), 99)
        answer.assert_not_called()

    def test_crown_loader_key_request_also_finds_post(self):
        self._link_channel()
        self._seed_post(245, caption="TRYL KEY click me for key",
                        file_name="BEASTCROWN~JAVA.apk", file_type="document")
        fwd, answer = self._run_group("crown loader ka key dedo")
        self.assertEqual(len(fwd), 1)
        self.assertEqual(fwd[0][2], 245)
        answer.assert_not_called()

    def test_newest_duplicate_wins(self):
        """Multiple versions of the same loader: newest message_id wins."""
        self._link_channel()
        self._seed_post(200, caption="BEASTCROWN v1", file_name="BEASTCROWN~JAVA.apk")
        self._seed_post(240, caption="BEASTCROWN v2", file_name="BEASTCROWN~JAVA.apk")
        fwd, _ = self._run_group("beastcrown bhejo")
        self.assertEqual(fwd[0][2], 240)

    def test_fullwidth_zyrox_matches_via_nfkc(self):
        """Channel stylized names (ＺＹＲＯＸ) match plain queries (zyrox)."""
        self._link_channel()
        self._seed_post(80, text="ＺＹＲＯＸ ＳＴＯＲＡＧＥ: 📄 Zyrox_Photo_1.jpg 🔗 https://spoo.me/slIpCW")
        fwd, answer = self._run_group("zyrox photo de do")
        self.assertEqual(len(fwd), 1)
        self.assertEqual(fwd[0][2], 80)
        answer.assert_not_called()

    def test_vague_key_request_no_auto_forward(self):
        """'key dedo' with no specific name = too vague -> no auto-forward."""
        self._link_channel()
        self._seed_post(245, caption="TRYL KEY", file_name="BEASTCROWN~JAVA.apk")
        fwd, answer = self._run_group("bhai key dedo")
        self.assertEqual(fwd, [])
        answer.assert_not_called()

    def test_casual_talk_never_auto_forwards(self):
        """No ask ('mujhe crown pasand nahi') -> no delivery, no LLM claim."""
        self._link_channel()
        self._seed_post(245, caption="crown", file_name="BEASTCROWN~JAVA.apk")
        fwd, answer = self._run_group("mujhe crown pasand nahi hai")
        self.assertEqual(fwd, [])
        answer.assert_not_called()

    def test_get_by_name_command(self):
        self._link_channel()
        self._seed_post(245, caption="crown", file_name="BEASTCROWN~JAVA.apk")
        fwd, _ = self._run_group("/get crown")
        self.assertEqual(len(fwd), 1)
        self.assertEqual(fwd[0][2], 245)

    def test_latest_post_uses_highest_backfilled_id(self):
        self._link_channel()
        self._seed_post(1, caption="first")
        self._seed_post(245, caption="newest crown", file_name="X.apk")
        fwd, _ = self._run_group("latest post dikhao")
        self.assertEqual(fwd[0][2], 245)

    def test_file_name_outranks_caption_mention(self):
        """'twitter apk' must find the Twitter APK (#197), not a loader
        whose caption merely lists 'login via twitter' (#245)."""
        self._link_channel()
        self._seed_post(197, caption="twitter client",
                        file_name="com_twitter_android.apk")
        self._seed_post(245, caption="supports login via facebook twitter",
                        file_name="BEASTCROWN~JAVA.apk")
        fwd, _ = self._run_group("twitter wala apk dedo")
        self.assertEqual(fwd[0][2], 197)

    def test_short_name_loader_match(self):
        """3-letter loader names ('bat') match too."""
        self._link_channel()
        self._seed_post(238, caption="BAT LOADER V3",
                        file_name="Bat Loader v3.apk")
        fwd, _ = self._run_group("manager bat loader bhejo")
        self.assertEqual(fwd[0][2], 238)


class SanitizeHoJayegaTests(unittest.TestCase):
    """v3.2: 'abhi system ke zariye forward ho jayega' class of lies."""

    def test_passive_future_promise_stripped(self):
        import agent as _agent
        lies = [
            "Bhai, maine link note kar liya hai, aapka ye message abhi "
            "system ke zariye forward ho jayega!",
            "aapka message forward hojayega turant",
            "wait karo, ho jaayega thodi der me",
        ]
        for t in lies:
            self.assertIsNone(_agent.sanitize_claims(t), msg=t)

    def test_legit_sentences_survive(self):
        import agent as _agent
        keep = [
            "Ye loader safe hai, aap use kar sakte ho.",
            "Avron loader ka setup video channel pe hai.",
        ]
        for t in keep:
            self.assertEqual(_agent.sanitize_claims(t), t)


class AlbumForwardTests(IMDBTestCase):

    def test_album_forwards_whole_group_first_replies(self):
        """An album (media_group_id) delivers ALL items; first one replies."""
        self._link_channel()
        self._seed_post(210, media_group_id="m1", file_type="photo",
                        caption="album 1")
        self._seed_post(211, media_group_id="m1", file_type="photo")
        self._seed_post(212, media_group_id="m1", file_type="photo")
        fwd = []
        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message",
                             return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot, "_bot_username", return_value="rk_test_bot"), \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.tg_api, "send_message",
                             return_value={"ok": True}), \
                patch.object(bot.tg_api, "forward_message",
                             side_effect=lambda to, src, mid, **kw:
                             fwd.append((mid, kw)) or {"ok": True}), \
                patch.object(bot.agent, "answer"):
            bot.handle_message(group_message("210 wala post bhejo"))
        ids = [f[0] for f in fwd]
        self.assertEqual(ids, [210, 211, 212])
        # only the first item replies to the asker's message
        self.assertEqual(fwd[0][1].get("reply_to_message_id"), 99)
        self.assertIsNone(fwd[1][1].get("reply_to_message_id"))
        self.assertIsNone(fwd[2][1].get("reply_to_message_id"))


# ---------------- live ingestion parity with backfill schema ----------------

class IngestParityTests(IMDBTestCase):

    def test_ingest_channel_post_stores_rich_fields(self):
        post = {
            "message_id": 300,
            "date": 2000,
            "caption": "TRYL KEY https://spoo.me/Y5p4T1",
            "chat": {"id": CHAN},
            "document": {"file_id": "doc1", "file_name": "BEASTCROWN~JAVA.apk"},
            "media_group_id": "mg9",
        }
        knowledge.ingest_channel_post(post)
        doc = self._db["channel_posts"].find_one(
            {"channel_id": CHAN, "message_id": 300})
        self.assertEqual(doc["file_name"], "BEASTCROWN~JAVA.apk")
        self.assertEqual(doc["media_group_id"], "mg9")
        self.assertIn("https://spoo.me/Y5p4T1", doc["links"])

    def test_search_finds_posts_by_file_name(self):
        knowledge.ingest_channel_post({
            "message_id": 300, "date": 2000,
            "caption": "loader post",
            "chat": {"id": CHAN},
            "document": {"file_id": "doc1", "file_name": "BEASTCROWN~JAVA.apk"},
        })
        hits = knowledge.search("beastcrown", user_id=123)
        self.assertTrue(any(h["kind"] == "post" for h in hits))
        hit = [h for h in hits if h["kind"] == "post"][0]
        self.assertEqual(hit["file_name"], "BEASTCROWN~JAVA.apk")


if __name__ == "__main__":
    unittest.main(verbosity=2)
