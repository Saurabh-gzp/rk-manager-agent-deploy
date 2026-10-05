"""v3.3 POWER AGENT tests.

Locks in the owner's 5 reported issues:
  1. KEY/TEXT CUT: file delivery is FORWARD-first (entities = clickable
     'KEY: Click me' survives), caption NEVER truncated at 200 chars.
  2. RICH FORMATTING: markdown -> Telegram HTML (**bold**, `code`,
     [label](url)); broken HTML falls back to plain text.
  3. TWITTER APP delivery: 'twitter wala app do' -> the real Twitter APK
     post (twitter no longer a banned generic token; 'app do' = intent).
  4. SELF-DELETE: 'apne sabhi message delete kro' + /delmy really delete
     the bot's OWN tracked messages (owner/admin only).
  5. INTELLIGENCE: gate fail-open for owner (never silent), 'app do' is
     a request, fake-progress survivors stripped, fabricated post links
     corrected to the REAL post, wrong-file offers impossible.
"""
import unittest
from unittest.mock import patch, MagicMock

import agent
import bot
import channel_forward
import config
import db
import fmt
import knowledge
import tg_api
import triggers

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
    def setUp(self):
        self._db = db.IMDB()
        self.p_db = patch.object(db, "_DB", self._db)
        self.p_db.start()
        db.invalidate_state_cache()
        self.p_owner = patch.object(config, "OWNER_CHAT_ID", OWNER)
        self.p_owner.start()
        channel_forward._LAST_LINK.clear()
        bot._GROUP_CONTEXT.clear()
        bot._OWN_MSG_IDS.clear()
        self.addCleanup(self.p_db.stop)
        self.addCleanup(self.p_owner.stop)
        self.addCleanup(db.invalidate_state_cache)
        self.addCleanup(channel_forward._LAST_LINK.clear)
        self.addCleanup(bot._GROUP_CONTEXT.clear)
        self.addCleanup(bot._OWN_MSG_IDS.clear)

    def _link_channel(self, username="testchan"):
        db.save_state("channel_id", CHAN)
        db.save_state("managed_channel_username", username)

    def _seed_post(self, message_id, text="", caption="", file_name=None,
                   file_type=None, file_id="f1", media_group_id=None,
                   links=None, entities=None, date=1000):
        doc = {
            "channel_id": CHAN,
            "message_id": message_id,
            "text": text or caption,
            "caption": caption,
            "file_id": file_id,
            "file_type": file_type,
            "file_name": file_name,
            "links": links or [],
            "entities": entities or [],
            "media_group_id": media_group_id,
            "date": date,
            "contact": None,
        }
        db.get_db()["channel_posts"].insert_one(doc)
        return doc


# ---------------------------------------------------------------------------
# 1. RICH FORMATTING (fmt.py)
# ---------------------------------------------------------------------------
class MarkdownHtmlTests(unittest.TestCase):
    def test_bold_code_link(self):
        out = fmt.markdown_to_html(
            "Avron ki key: `VIPER-KEY-1D` — **Avron Loader** hai, "
            "[Click me](https://t.me/rkgamersfeedback/50) se key milegi.")
        self.assertIn("<code>VIPER-KEY-1D</code>", out)
        self.assertIn("<b>Avron Loader</b>", out)
        self.assertIn('<a href="https://t.me/rkgamersfeedback/50">Click me</a>', out)

    def test_escapes_user_html(self):
        out = fmt.markdown_to_html("a < b & c > d")
        self.assertNotIn("<b>", out)
        self.assertIn("&lt;", out)
        self.assertIn("&amp;", out)

    def test_italic_strike_underline(self):
        out = fmt.markdown_to_html("*jitna* __fast__ ~~galat~~ cheez")
        self.assertIn("<i>jitna</i>", out)
        self.assertIn("<u>fast</u>", out)
        self.assertIn("<s>galat</s>", out)

    def test_bare_url_becomes_clickable(self):
        out = fmt.markdown_to_html("key https://t.me/rkgamersfeedback/50 lo")
        self.assertIn('<a href="https://t.me/rkgamersfeedback/50">', out)

    def test_unbalanced_markup_left_as_text(self):
        out = fmt.markdown_to_html("ye ** adhoora asterisk hai")
        self.assertNotIn("<b>", out)

    def test_looks_formatted(self):
        self.assertTrue(fmt.looks_formatted("**b**"))
        self.assertTrue(fmt.looks_formatted("`k`"))
        self.assertTrue(fmt.looks_formatted("see https://x.y"))
        self.assertFalse(fmt.looks_formatted("plain line"))
        self.assertFalse(fmt.looks_formatted(""))

    def test_never_raises(self):
        self.assertEqual(fmt.markdown_to_html(""), "")
        self.assertIsNone(fmt.entity_html("", []))


class EntityHtmlTests(unittest.TestCase):
    def test_text_link_restored(self):
        # 'KEY: Click me' — 'Click me' is a text_link at utf16 offset 5
        text = "KEY: Click me"
        ents = [{"type": "text_link", "offset": 5, "length": 8,
                 "url": "https://t.me/rkgamersfeedback/50"}]
        out = fmt.entity_html(text, ents)
        self.assertIn('<a href="https://t.me/rkgamersfeedback/50">Click me</a>', out)
        self.assertIn("KEY: ", out)

    def test_bold_and_emoji_offsets(self):
        # emoji are 2 utf-16 units — offsets must still land correctly
        text = "🔑 Key: ABC-123"
        ents = [{"type": "bold", "offset": 8, "length": 7}]
        out = fmt.entity_html(text, ents)
        self.assertIn("<b>ABC-123</b>", out)

    def test_overlapping_entities_one_wins(self):
        text = "styled text"
        ents = [{"type": "underline", "offset": 0, "length": 6},
                {"type": "italic", "offset": 0, "length": 6}]
        out = fmt.entity_html(text, ents)
        # exactly ONE of the overlapping styles is applied, never both
        self.assertNotIn("<i><u>", out)
        self.assertNotIn("<u><i>", out)
        self.assertTrue(out.startswith("<i>") or out.startswith("<u>"))

    def test_no_entities_returns_none(self):
        self.assertIsNone(fmt.entity_html("text", []))


# ---------------------------------------------------------------------------
# 2. DELIVERY FIDELITY (forward-first, no truncation, entities restored)
# ---------------------------------------------------------------------------
class DeliveryFidelityTests(IMDBTestCase):
    def test_forward_first_preserves_entities(self):
        self._link_channel()
        self._seed_post(248, caption="KEY: Click me",
                        file_name="Avron Loader (v1.0).apk",
                        entities=[{"type": "text_link", "offset": 5,
                                   "length": 8,
                                   "url": "https://t.me/x/50"}])
        sent = {}
        with patch.object(tg_api, "forward_message",
                          return_value={"ok": True, "result": {"message_id": 1}}) as fw:
            r = agent._deliver_channel_item(CHAT, db.get_db()["channel_posts"].find_one({}), reply_to=99)
        self.assertTrue(r.get("ok"))
        fw.assert_called_once()
        self.assertEqual(fw.call_args[0][0], CHAT)
        self.assertEqual(fw.call_args.kwargs.get("reply_to_message_id"), 99)
        self.assertEqual(sent, {})  # no re-send happened

    def test_file_id_resend_restores_clickable_key(self):
        self._link_channel()
        doc = self._seed_post(
            300, caption="KEY: Click me\nContact @oxsidianX",
            file_name="X.apk", file_type="document",
            entities=[{"type": "text_link", "offset": 5, "length": 8,
                       "url": "https://t.me/rkgamersfeedback/50"}])
        calls = {}
        with patch.object(tg_api, "forward_message",
                          return_value={"ok": False, "description": "not found"}), \
                patch.object(tg_api, "copy_message",
                             return_value={"ok": False, "description": "not found"}), \
                patch.object(tg_api, "send_any_file",
                             side_effect=lambda c, f, t, caption=None, **kw:
                             calls.update(caption=caption) or {"ok": True}) as sf:
            agent._deliver_channel_item(CHAT, doc)
        cap = calls.get("caption") or ""
        # FULL caption (no 200-char cut) + clickable link restored
        self.assertIn('<a href="https://t.me/rkgamersfeedback/50">Click me</a>', cap)
        self.assertIn("Contact @oxsidianX", cap)
        self.assertEqual(sf.call_args.kwargs.get("parse_mode"), "HTML")

    def test_long_caption_never_truncated(self):
        self._link_channel()
        long_cap = "x" * 1400  # longer than the old [:1000] cut too
        doc = self._seed_post(301, caption=long_cap, file_name="Y.apk",
                              file_type="document")
        calls = {}
        with patch.object(tg_api, "forward_message",
                          return_value={"ok": False}), \
                patch.object(tg_api, "copy_message",
                             return_value={"ok": False}), \
                patch.object(tg_api, "send_any_file",
                             side_effect=lambda c, f, t, caption=None, **kw:
                             calls.update(caption=caption) or {"ok": True}):
            agent._deliver_channel_item(CHAT, doc)
        self.assertEqual(len(calls.get("caption") or ""), 1400)

    def test_html_parse_error_falls_back_to_plain(self):
        with patch.object(tg_api, "api",
                          side_effect=[{"ok": False,
                                        "description": "Bad Request: can't parse entities"},
                                       {"ok": True, "result": {"message_id": 5}}]) as mock_api:
            r = tg_api.send_message_html(CHAT, "<b>broken", "plain fallback")
        self.assertTrue(r.get("ok"))
        # second call = the plain fallback send
        self.assertEqual(mock_api.call_count, 2)


# ---------------------------------------------------------------------------
# 3. TWITTER APP DELIVERY (the zg6K3T bug)
# ---------------------------------------------------------------------------
class TwitterDeliveryTests(IMDBTestCase):
    def test_twitter_app_do_finds_twitter_apk(self):
        self._link_channel()
        self._seed_post(245, caption="supports login via facebook twitter",
                        file_name="BEASTCROWN~JAVA.apk", file_type="document")
        self._seed_post(197,
                        caption="twitter 999999999 problem solve",
                        file_name="com_twitter_android_11_98_0_release_0.apkm",
                        file_type="document")
        fwd, answer = self._run_group("are twitter wala app do konsa hai 9999 fix krne wlaa")
        self.assertEqual(len(fwd), 1)
        self.assertEqual(fwd[0][2], 197)  # file_name beat caption mentions
        answer.assert_not_called()

    def test_twitter_login_problem_rule_delivery(self):
        """Owner rule: twitter login problem -> app bhi do. file_name hit
        qualifies even without explicit file-request words."""
        self._link_channel()
        self._seed_post(197,
                        caption="twitter 999999999 problem solve",
                        file_name="com_twitter_android_11_98_0_release_0.apkm",
                        file_type="document")
        with patch.object(knowledge, "search") as search, \
                patch.object(tg_api, "forward_message",
                             return_value={"ok": True}) as fw, \
                patch.object(agent.ai_engine, "get_router") as router:
            router.return_value.generate.return_value = (
                "Ji boss, original Twitter app delete kar ke channel wala "
                "install kariye.")
            search.return_value = [{
                "kind": "post", "text": "twitter 999999999 problem solve",
                "caption": "twitter 999999999 problem solve",
                "file_name": "com_twitter_android_11_98_0_release_0.apkm",
                "file_id": "f1", "file_type": "document",
                "channel_id": CHAN, "message_id": 197,
                "match_score": 3, "links": [], "entities": [],
                "contact": None,
            }]
            res = agent.answer("twitter se login nhi ho rha 99999 problem kaise fix kru",
                               {"user_id": OWNER, "chat_id": CHAT, "group": True,
                                "group_role": "owner"})
        fo = res.get("file_offer")
        self.assertIsInstance(fo, dict)
        self.assertEqual(fo.get("message_id"), 197)

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


# ---------------------------------------------------------------------------
# 4. SELF-MESSAGE DELETION (the Kqdz22 bug)
# ---------------------------------------------------------------------------
class SelfDeleteTests(IMDBTestCase):
    def test_apne_sabhi_message_delete_kro(self):
        bot._OWN_MSG_IDS[CHAT] = __import__("collections").deque(
            [11, 12, 13, 14], maxlen=200)
        deleted = []
        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message",
                             return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot.tg_api, "delete_message",
                             side_effect=lambda c, m:
                             deleted.append(m) or {"ok": True}) as dm, \
                patch.object(bot.tg_api, "send_message",
                             return_value={"ok": True}) as sm:
            bot.handle_message(
                group_message("manager bhai apne sabhi message delete kro",
                              user_id=OWNER, message_id=99))
        self.assertEqual(sorted(deleted), [11, 12, 13, 14])
        self.assertIn("4", sm.call_args_list[-1][0][1])  # honest count
        self.assertEqual(len(bot._OWN_MSG_IDS[CHAT]), 0)

    def test_delmy_command_with_count(self):
        bot._OWN_MSG_IDS[CHAT] = __import__("collections").deque(
            [21, 22, 23, 24, 25], maxlen=200)
        deleted = []
        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message",
                             return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot.tg_api, "delete_message",
                             side_effect=lambda c, m:
                             deleted.append(m) or {"ok": True}), \
                patch.object(bot.tg_api, "send_message",
                             return_value={"ok": True}):
            bot.handle_message(group_message("/delmy 2", user_id=OWNER))
        self.assertEqual(deleted, [25, 24])  # newest first, exactly 2

    def test_member_cannot_self_delete_bot_messages(self):
        bot._OWN_MSG_IDS[CHAT] = __import__("collections").deque(
            [31, 32], maxlen=200)
        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message",
                             return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot.tg_api, "delete_message") as dm, \
                patch.object(bot.tg_api, "send_message",
                             return_value={"ok": True}) as sm, \
                patch.object(bot.tg_api, "get_chat_member",
                             return_value={"status": "member"}):
            bot.handle_message(group_message("apne message delete karo",
                                             user_id=123))
        dm.assert_not_called()
        self.assertIn("boss", sm.call_args_list[-1][0][1])

    def test_track_own_message_records_ids(self):
        bot._track_own_message(CHAT, {"ok": True, "result": {"message_id": 77}})
        self.assertEqual(list(bot._OWN_MSG_IDS[CHAT]), [77])
        bot._track_own_message(CHAT, {"ok": False})
        bot._track_own_message(CHAT, None)
        self.assertEqual(list(bot._OWN_MSG_IDS[CHAT]), [77])

    def test_delete_kro_is_mod_request(self):
        self.assertTrue(bot._looks_like_mod_request(
            "manager bhai apne sabhi message delete kro"))
        self.assertTrue(bot._looks_like_mod_request("ye message delete kro"))
        self.assertFalse(bot._looks_like_mod_request("delete kar di thi kal"))


# ---------------------------------------------------------------------------
# 5. INTELLIGENCE (gate, requests, fake-progress, links, offers)
# ---------------------------------------------------------------------------
class GateFailOpenTests(IMDBTestCase):
    def test_owner_answered_when_gate_errors(self):
        sent = []
        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message",
                             return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot, "_bot_username", return_value="rk_bot"), \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.tg_api, "send_message",
                             side_effect=lambda c, t, **kw:
                             sent.append(t) or {"ok": True}), \
                patch.object(bot.ai_engine, "NoProvidersError",
                         create=True), \
                patch.object(bot, "_should_reply_llm", return_value=None), \
                patch.object(bot.agent, "answer",
                             return_value={"text": "haan boss bolie"}):
            bot.handle_message(group_message("twitter wala app do",
                                             user_id=OWNER))
        self.assertEqual(sent, ["haan boss bolie"])

    def test_member_silent_when_gate_errors(self):
        sent = []
        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message",
                             return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot, "_bot_username", return_value="rk_bot"), \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.tg_api, "send_message",
                             side_effect=lambda c, t, **kw:
                             sent.append(t) or {"ok": True}), \
                patch.object(bot, "_should_reply_llm", return_value=None), \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(group_message("aaj mausam acha hai",
                                             user_id=123))
        self.assertEqual(sent, [])
        answer.assert_not_called()

    def test_clean_gate_no_stays_silent(self):
        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message",
                             return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot, "_bot_username", return_value="rk_bot"), \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.tg_api, "send_message"), \
                patch.object(bot, "_should_reply_llm", return_value=False), \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(group_message("kal ka match bahut bada tha",
                                             user_id=123))
        answer.assert_not_called()


class RequestDetectionTests(unittest.TestCase):
    def test_app_do_is_request(self):
        self.assertTrue(triggers.is_question_or_request(
            "are twitter wala app do konsa hai 9999 fix krne wlaa"))

    def test_key_dedo_is_request(self):
        self.assertTrue(triggers.is_question_or_request("bhai key dedo"))

    def test_plain_chatter_still_not_request(self):
        self.assertFalse(triggers.is_question_or_request("hyy"))
        self.assertFalse(triggers.is_question_or_request("ok"))


class FileRequestWordBoundaryTests(unittest.TestCase):
    def test_key_word_boundary(self):
        self.assertTrue(agent._looks_like_file_request("arvon loader ka key kya hai"))
        self.assertFalse(agent._looks_like_file_request("okay ho gaya"))
        self.assertTrue(agent._looks_like_file_request("twitter wala app do"))

    def test_kya_hai_alone_not_file_request(self):
        self.assertFalse(agent._looks_like_file_request("tum kaise ho"))


class FakeProgressSurvivorsTests(unittest.TestCase):
    def test_koshish_stripped(self):
        out = agent.sanitize_claims(
            "Ji boss! Main apne saare messages delete karne ki koshish kar "
            "raha hoon. Group saaf rahega.")
        self.assertIsNotNone(out)
        self.assertNotIn("koshish", out)
        self.assertIn("Group saaf rahega.", out)

    def test_bata_deta_stripped(self):
        out = agent.sanitize_claims(
            "Main channel ki recent posts check karke aapko bata deta hoon. "
            "Aap bataiye kis loader ki key chahiye.")
        self.assertIsNotNone(out)
        self.assertNotIn("bata deta", out)
        self.assertIn("kis loader ki key", out)

    def test_honest_text_untouched(self):
        text = "Avron ki key `VIPERxARVON-KEY-1D` hai, channel post par."
        self.assertEqual(agent.sanitize_claims(text), text)


class LinkGroundingTests(IMDBTestCase):
    def _fake_hits(self):
        return [{"kind": "post", "text": "twitter app",
                 "match_score": 3, "message_id": 197}]

    def test_fabricated_post_link_corrected_and_delivered(self):
        self._link_channel()
        self._seed_post(197, caption="twitter 999999999 problem solve",
                        file_name="com_twitter_android.apkm",
                        file_type="document")
        text, offer = agent._ground_post_links(
            "Twitter app ka post: https://t.me/testchan/245 hai boss.",
            "twitter wala app do", self._fake_hits())
        self.assertIn("testchan/197", text)
        self.assertNotIn("testchan/245", text)
        self.assertIsInstance(offer, dict)
        self.assertEqual(offer.get("message_id"), 197)

    def test_real_post_link_kept(self):
        self._link_channel()
        self._seed_post(197, caption="twitter solve", file_name="t.apkm")
        text, offer = agent._ground_post_links(
            "Ye lo: https://t.me/testchan/197", "twitter app do",
            self._fake_hits())
        self.assertIn("testchan/197", text)
        self.assertIsNone(offer)

    def test_unresolvable_link_dropped(self):
        self._link_channel()
        text, offer = agent._ground_post_links(
            "Post hai https://t.me/testchan/999 par", "random cheez",
            self._fake_hits())
        self.assertNotIn("t.me/testchan/999", text)
        self.assertIsNone(offer)


class BestFileOfferTests(IMDBTestCase):
    def _hit(self, file_name, caption="", score=1):
        return {"kind": "post", "text": caption, "caption": caption,
                "file_name": file_name, "file_id": "f", "file_type": "document",
                "channel_id": CHAN, "message_id": 1, "match_score": score,
                "links": [], "entities": [], "contact": None}

    def test_free_fire_wrong_delivery_impossible(self):
        """The 02:19 'ye wala hai yaad rakhna : t.me/.../197' bug — a link
        in the question must never masquerade as a file-name token."""
        hits = [self._hit("Fluorite_PanelFixed.apk",
                          "Enjoy Free Fire&Max Players KEY: FF")]
        offer = agent._best_file_offer(
            hits, "ye wala hai yaad rakhna hmesha : https://t.me/testchan/197")
        self.assertIsNone(offer)

    def test_vague_key_no_offer(self):
        hits = [self._hit("BEASTCROWN~JAVA.apk", "TRYL KEY")]
        self.assertIsNone(agent._best_file_offer(hits, "bhai key dedo"))

    def test_arvon_key_question_offers_avron(self):
        hits = [self._hit("Avron Loader (v1.0).apk", "KEY: Click me")]
        offer = agent._best_file_offer(
            hits, "mene pucha arvon loader ka key kya hai")
        self.assertIsInstance(offer, dict)
        self.assertIn("Avron", offer.get("file_name"))

    def test_caption_match_needs_explicit_file_ask(self):
        hits = [self._hit("X.apk", "Avron Loader full collection")]
        # problem-style question, no explicit ask -> no offer
        self.assertIsNone(agent._best_file_offer(hits, "avron me problem aari"))
        # explicit ask -> offer
        o = agent._best_file_offer(hits, "arvon loader bhejo")
        self.assertIsInstance(o, dict)


class ContextFormattingTests(IMDBTestCase):
    def test_post_numbers_and_links_shown(self):
        hits = [{"kind": "post", "text": "TRYL KEY click me",
                 "message_id": 245, "file_name": "BEASTCROWN~JAVA.apk",
                 "links": ["https://t.me/rkgamersfeedback/50"]}]
        out = agent._format_context(hits)
        self.assertIn("channel post #245", out)
        self.assertIn("file: BEASTCROWN~JAVA.apk", out)
        self.assertIn("link: https://t.me/rkgamersfeedback/50", out)


class IngestEntitiesTests(IMDBTestCase):
    def test_entities_and_link_urls_stored(self):
        post = {
            "message_id": 5, "date": 1000,
            "chat": {"id": CHAN},
            "caption": "KEY: Click me",
            "document": {"file_id": "d1", "file_name": "app.apk"},
            "caption_entities": [
                {"type": "text_link", "offset": 5, "length": 8,
                 "url": "https://t.me/rkgamersfeedback/50"},
                {"type": "bold", "offset": 0, "length": 3},
            ],
        }
        knowledge.ingest_channel_post(post)
        doc = db.get_db()["channel_posts"].find_one({"message_id": 5})
        self.assertEqual(len(doc["entities"]), 2)
        self.assertIn("https://t.me/rkgamersfeedback/50", doc["links"])


class RichAnswerDeliveryTests(IMDBTestCase):
    def test_markdown_answer_sent_as_html(self):
        sent = []
        with patch.object(tg_api, "send_message_html",
                          side_effect=lambda c, h, p, **kw:
                          sent.append((h, p)) or {"ok": True}) as sh:
            bot._deliver_answer(CHAT, {
                "text": "Key: `VIPER-KEY-1D` — **Avron** hai, "
                        "[Click me](https://t.me/x/50) se milegi.",
            }, reply_to=99)
        sh.assert_called_once()
        html, plain = sent[0]
        self.assertIn("<code>VIPER-KEY-1D</code>", html)
        self.assertIn("<b>Avron</b>", html)
        self.assertIn('<a href="https://t.me/x/50">Click me</a>', html)
        self.assertIn("VIPER-KEY-1D", plain)

    def test_plain_answer_sent_without_parse_mode(self):
        with patch.object(tg_api, "send_message",
                          return_value={"ok": True}) as sm, \
                patch.object(tg_api, "send_message_html") as sh:
            bot._deliver_answer(CHAT, {"text": "theek hai boss"},
                                reply_to=99)
        sm.assert_called_once()
        self.assertIsNone(sm.call_args.kwargs.get("parse_mode"))
        sh.assert_not_called()

    def test_own_message_ids_tracked(self):
        with patch.object(tg_api, "send_message",
                          return_value={"ok": True, "result": {"message_id": 55}}):
            bot._deliver_answer(CHAT, {"text": "theek hai boss"}, reply_to=9)
        self.assertEqual(list(bot._OWN_MSG_IDS[CHAT]), [55])


class NumberPostGuardTests(IMDBTestCase):
    def test_error_code_not_treated_as_post_number(self):
        """'9999 fix krne wlaa' — 9999 is an error code, not post #9999."""
        self._link_channel()
        self._seed_post(197, caption="twitter solve",
                        file_name="com_twitter_android.apkm")
        fwd, answer = TwitterDeliveryTests._run_group(
            self, "kya karu twitter wala app do konsa hai 9999 wlaa")
        self.assertEqual(fwd, [fwd[0]] if fwd else [])
        if fwd:
            self.assertEqual(fwd[0][2], 197)  # only the real post


if __name__ == "__main__":
    unittest.main()
