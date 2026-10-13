"""v3.4 POWER AGENT — anti-spam + honest-delivery tests.

Locks in the owner's live-reported issues:
  1. '2 users ke baat-cheet me bahot message krta hai' — bot interrupts
     member-to-member exchanges: exchange guard + conservative gate +
     '?'-questions routed through the gate (no cheap-path bypass) +
     undirected-reply budget.
  2. 'bina samjhe kuch bhi bhej deta hai' — undirected chatter with
     intent words no longer auto-forwards; file requests need real ask
     verbs; file_name matches need a file-topic message.
  3. 'jo channel se delete ho chuka hai vo file bhi bhej deta hai' —
     deleted posts are NEVER resurrected via file_id; live not-found
     purges the post from records; honest message sent.
  4. 'hmesha channel ke data ke base pe dena chahiye' — SYSTEM_PROMPT
     forbids general-knowledge substitution for community content.
"""
import unittest
from unittest.mock import patch, MagicMock

import agent
import bot
import channel_forward
import config
import db
import knowledge
import tg_api
import triggers

OWNER = 8967004608
BOT_ID = 8781539682
CHAT = -100123
CHAN = -100777


def group_message(text, user_id=123, message_id=99, reply_to_message=None):
    msg = {
        "message_id": message_id,
        "date": 1000,
        "text": text,
        "from": {"id": user_id, "first_name": f"U{user_id}",
                 "username": f"u{user_id}"},
        "chat": {"id": CHAT, "type": "supergroup", "title": "Test"},
        "entities": [],
    }
    if reply_to_message:
        msg["reply_to_message"] = reply_to_message
    return msg


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
        bot._REPLY_TIMES.clear()
        tg_api.OWN_MSG_IDS.clear()
        self.addCleanup(self.p_db.stop)
        self.addCleanup(self.p_owner.stop)
        self.addCleanup(db.invalidate_state_cache)
        self.addCleanup(channel_forward._LAST_LINK.clear)
        self.addCleanup(bot._GROUP_CONTEXT.clear)
        self.addCleanup(bot._REPLY_TIMES.clear)
        self.addCleanup(tg_api.OWN_MSG_IDS.clear)

    def _link_channel(self, username="testchan"):
        db.save_state("channel_id", CHAN)
        db.save_state("managed_channel_username", username)

    def _seed_post(self, message_id, caption="", file_name=None,
                   file_type="document", file_id="f1"):
        doc = {
            "channel_id": CHAN,
            "message_id": message_id,
            "text": caption,
            "caption": caption,
            "file_id": file_id,
            "file_type": file_type,
            "file_name": file_name,
            "links": [],
            "entities": [],
            "date": 1000,
            "contact": None,
        }
        db.get_db()["channel_posts"].insert_one(doc)
        return doc


# ---------------------------------------------------------------------------
# 1. REPLY-NOISE: member-to-member exchanges are never interrupted
# ---------------------------------------------------------------------------
class MemberExchangeGuardTests(IMDBTestCase):
    def _base_patches(self):
        return (
            patch.object(bot.links, "is_managed_group", return_value=True),
            patch.object(bot.moderation, "handle_group_message",
                         return_value=False),
            patch.object(bot.knowledge, "ingest_message"),
            patch.object(bot, "_bot_username", return_value="rk_test_bot"),
            patch.object(bot.tg_api, "send_chat_action"),
            patch.object(bot.tg_api, "send_message", return_value={"ok": True}),
        )

    def _seed_exchange(self):
        """A and B are actively talking (no bot involved)."""
        bot._remember(CHAT, "A", "crown loader ka key mila?", uid=111)
        bot._remember(CHAT, "B", "haan mila bhai", uid=222)
        # B's next message (current) is already remembered by handle_message

    def test_question_between_two_members_gets_silence(self):
        """A<->B exchange + A's '?' question — bot stays SILENT (no gate
        call, no answer, no forward)."""
        self._seed_exchange()
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot, "_should_reply_llm") as gate, \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(group_message("kya baat kar raha hai?", user_id=111))
        gate.assert_not_called()
        answer.assert_not_called()
        send.assert_not_called()

    def test_request_between_two_members_gets_silence(self):
        """Even an explicit request ('app do') inside an A<->B exchange
        stays silent — the bot must not interject with files mid-chat."""
        self._seed_exchange()
        self._link_channel()
        self._seed_post(197, caption="twitter app",
                        file_name="com_twitter_android.apkm")
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot.tg_api, "forward_message") as fw:
            bot.handle_message(group_message("twitter wala app do", user_id=111))
        fw.assert_not_called()
        send.assert_not_called()

    def test_mention_breaks_silence_even_mid_exchange(self):
        """@mention always works — guard is only for undirected messages."""
        self._seed_exchange()
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot.agent, "answer",
                             return_value={"text": "haan bhai"}) as answer:
            bot.handle_message(
                group_message("@rk_test_bot twitter wala app do", user_id=111))
        answer.assert_called_once()
        send.assert_called()

    def test_bot_in_recent_exchange_keeps_gate_path(self):
        """Bot asked something 2 messages back -> follow-up still reaches
        the gate (guard only fires when the bot is fully uninvolved)."""
        bot._remember(CHAT, "A", "kya help chahiye?", uid=111)
        bot._remember(CHAT, "RK Manager", "batao kya file chahiye?", is_bot=True)
        bot._remember(CHAT, "B", "wo crown wala", uid=222)
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], \
                patch.object(bot, "_should_reply_llm", return_value=True) as gate, \
                patch.object(bot.agent, "answer",
                             return_value={"text": "crown mil jayega"}) as answer:
            bot.handle_message(group_message("haan wahi wala", user_id=111))
        gate.assert_called_once()
        answer.assert_called_once()

    def test_same_user_monologue_not_guarded(self):
        """A talking alone (no B) -> gate decides, guard stays off."""
        bot._remember(CHAT, "A", "game khel raha hu", uid=111)
        bot._remember(CHAT, "A", "match acha tha", uid=111)
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], \
                patch.object(bot, "_should_reply_llm", return_value=False) as gate, \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(group_message("aaj match jeet gaya", user_id=111))
        gate.assert_called_once()
        answer.assert_not_called()


class QuestionGateRoutingTests(IMDBTestCase):
    """'?' questions no longer bypass the gate (the live spam root cause)."""

    def _run(self, text, user_id=123, gate_val=False):
        sent = []
        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message",
                             return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot, "_bot_username", return_value="rk_bot"), \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.tg_api, "send_message",
                             side_effect=lambda c, t, **k:
                             sent.append(t) or {"ok": True}), \
                patch.object(bot, "_should_reply_llm",
                             return_value=gate_val) as gate, \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(group_message(text, user_id=user_id))
        return gate, answer, sent

    def test_question_mark_goes_to_gate(self):
        gate, answer, _ = self._run("kal aayega?", user_id=123)
        gate.assert_called_once()
        answer.assert_not_called()  # gate said NO

    def test_question_mark_answered_when_gate_yes(self):
        gate, answer, _ = self._run("kal aayega?", user_id=123, gate_val=True)
        gate.assert_called_once()
        answer.assert_called_once()

    def test_explicit_request_skips_gate(self):
        """'app do' is a real request — direct route, no gate call."""
        gate, answer, _ = self._run("bhai key dedo")
        gate.assert_not_called()
        answer.assert_called_once()


class ReplyBudgetTests(IMDBTestCase):
    def _run(self, user_id=123):
        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message",
                             return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot, "_bot_username", return_value="rk_bot"), \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.tg_api, "send_message", return_value={"ok": True}), \
                patch.object(bot, "_should_reply_llm", return_value=True), \
                patch.object(bot.agent, "answer",
                             return_value={"text": "jawab"}) as answer:
            bot.handle_message(
                group_message("sawal hai bhai?", user_id=user_id,
                              message_id=int(bot.time.time() * 1000) % 100000))
        return answer

    def test_budget_exhaustion_silences_undirected(self):
        for _ in range(4):
            self._run().assert_called_once()
        # 5th undirected reply: budget over (4/300s already spent)
        self._run().assert_not_called()

    def test_owner_never_budget_limited(self):
        for _ in range(6):
            self._run(user_id=OWNER).assert_called_once()


# ---------------------------------------------------------------------------
# 2. NO MORE RANDOM FILE DELIVERY
# ---------------------------------------------------------------------------
class TightenedFileRequestTests(unittest.TestCase):
    def test_chatter_nouns_not_file_requests(self):
        self.assertFalse(agent._looks_like_file_request("twitter pe ye video dekho"))
        self.assertFalse(agent._looks_like_file_request("photo khinchi mene"))
        self.assertFalse(agent._looks_like_file_request("send nahi hua uska msg"))

    def test_real_asks_still_file_requests(self):
        self.assertTrue(agent._looks_like_file_request("twitter wala app do"))
        self.assertTrue(agent._looks_like_file_request("crown loader bhejo"))
        self.assertTrue(agent._looks_like_file_request("arvon loader ka key kya hai"))
        self.assertTrue(agent._looks_like_file_request("photo dedo"))

    def test_okay_not_key(self):
        self.assertFalse(agent._looks_like_file_request("okay ho gaya"))


class FileOfferTopicGateTests(IMDBTestCase):
    def _hit(self, file_name, caption=""):
        return {"kind": "post", "text": caption, "caption": caption,
                "file_name": file_name, "file_id": "f", "file_type": "document",
                "channel_id": CHAN, "message_id": 197, "match_score": 3,
                "links": [], "entities": [], "contact": None}

    def test_casual_brand_mention_no_offer(self):
        """'twitter pe video dekho' — token matches file name, but the
        message is chatter, not a request/problem -> NO file offer."""
        hits = [self._hit("com_twitter_android.apkm")]
        self.assertIsNone(agent._best_file_offer(
            hits, "twitter pe ye video dekho"))

    def test_problem_report_gives_app(self):
        """Owner rule: 'twitter login problem -> app bhi do'."""
        hits = [self._hit("com_twitter_android.apkm")]
        fo = agent._best_file_offer(
            hits, "twitter se login nhi ho rha kya karu")
        self.assertIsInstance(fo, dict)

    def test_explicit_request_gives_app(self):
        hits = [self._hit("com_twitter_android.apkm")]
        fo = agent._best_file_offer(hits, "twitter wala app do")
        self.assertIsInstance(fo, dict)


class NoAutoForwardOnChatterTests(IMDBTestCase):
    """Undirected chatter + link no longer auto-forwards (v3.4: the
    forward handler moved behind the directedness check)."""

    def _run(self, text, user_id=123):
        fwd = []
        with patch.object(bot.links, "is_managed_group", return_value=True), \
                patch.object(bot.moderation, "handle_group_message",
                             return_value=False), \
                patch.object(bot.knowledge, "ingest_message"), \
                patch.object(bot, "_bot_username", return_value="rk_bot"), \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.tg_api, "send_message", return_value={"ok": True}), \
                patch.object(bot.tg_api, "forward_message",
                             side_effect=lambda to, src, mid, **kw:
                             fwd.append((to, src, mid)) or {"ok": True}), \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(group_message(text, user_id=user_id))
        return fwd, answer

    def test_mujhe_link_pasand_no_forward(self):
        """'mujhe' is no longer an intent word — link + mujhe = no bot."""
        self._link_channel()
        self._seed_post(245, caption="crown")
        fwd, answer = self._run(
            "mujhe ye link pasand aaya https://t.me/testchan/245")
        self.assertEqual(fwd, [])
        # undirected chatter: no gate mock -> NoProviders -> member silent
        answer.assert_not_called()

    def test_request_with_link_forwards(self):
        self._link_channel()
        self._seed_post(245, caption="crown")
        fwd, _ = self._run("ye post bhejo https://t.me/testchan/245")
        self.assertEqual(fwd, [(CHAT, CHAN, 245)])


# ---------------------------------------------------------------------------
# 3. DELETED POSTS ARE NEVER SENT
# ---------------------------------------------------------------------------
class DeletedPostPurgeTests(IMDBTestCase):
    def test_forward_not_found_purges_and_reports(self):
        """channel_forward: not-found -> post purged + honest message."""
        self._link_channel()
        self._seed_post(245, caption="crown", file_name="BEASTCROWN.apk")
        sent = []
        with patch.object(tg_api, "forward_message",
                          return_value={"ok": False,
                                        "description": "Bad Request: message to forward not found"}), \
                patch.object(tg_api, "copy_message",
                             return_value={"ok": False,
                                           "description": "Bad Request: message to copy not found"}), \
                patch.object(tg_api, "send_message",
                             side_effect=lambda c, t, **k:
                             sent.append(t) or {"ok": True}):
            handled = channel_forward.handle_forward_request(
                group_message("ye post bhejo https://t.me/testchan/245"),
                CHAT, 123)
        self.assertTrue(handled)
        self.assertTrue(any("delete ho chuka" in t for t in sent))
        self.assertIsNone(db.get_db()["channel_posts"].find_one(
            {"channel_id": CHAN, "message_id": 245}))

    def test_purged_post_not_found_by_search(self):
        self._link_channel()
        self._seed_post(300, caption="old crown thing")
        self.assertTrue(knowledge.purge_post(CHAN, 300))
        self.assertEqual(knowledge.search("old crown thing"), [])

    def test_agent_direct_delivery_deleted_honest(self):
        """agent.answer step-1: deleted post -> honest reply, no LLM call."""
        self._link_channel()
        self._seed_post(197, caption="twitter app",
                        file_name="com_twitter_android.apkm")
        with patch.object(knowledge, "search") as search, \
                patch.object(agent.tg_api, "forward_message",
                             return_value={"ok": False,
                                           "description": "message to forward not found"}), \
                patch.object(agent.tg_api, "copy_message",
                             return_value={"ok": False,
                                           "description": "message to copy not found"}), \
                patch.object(agent.ai_engine, "get_router") as router:
            search.return_value = [{
                "kind": "post", "text": "twitter app", "caption": "twitter app",
                "file_name": "com_twitter_android.apkm", "file_id": "f1",
                "file_type": "document", "channel_id": CHAN,
                "message_id": 197, "match_score": 3, "links": [],
                "entities": [], "contact": None,
            }]
            res = agent.answer("twitter wala app do",
                               {"user_id": 123, "chat_id": CHAT, "group": True})
        self.assertIn("delete ho chuka", res.get("text") or "")
        self.assertTrue(res.get("file_deleted"))
        router.generate.assert_not_called()  # honest path, no LLM roundtrip
        self.assertIsNone(db.get_db()["channel_posts"].find_one(
            {"channel_id": CHAN, "message_id": 197}))


# ---------------------------------------------------------------------------
# 4. GROUNDING: channel data is the single source of truth
# ---------------------------------------------------------------------------
class GroundingPromptTests(unittest.TestCase):
    def test_system_prompt_forbids_general_knowledge_substitution(self):
        self.assertIn("SINGLE SOURCE OF TRUTH", agent.SYSTEM_PROMPT)
        self.assertIn("NEVER substitute your own general knowledge",
                      agent.SYSTEM_PROMPT)

    def test_general_prompt_grounded(self):
        self.assertIn("single source of truth", agent.GENERAL_PROMPT)

    def test_gate_prompt_is_conservative(self):
        router = MagicMock()
        router.generate.return_value = "NO"
        bot._GROUP_CONTEXT.clear()
        with patch.object(bot.ai_engine, "get_router", return_value=router):
            bot._should_reply_llm("hello", "x", CHAT)
        prompt = router.generate.call_args.args[0]
        self.assertIn("staying silent is always safe", prompt)
        self.assertIn("interrupting their", prompt)
        self.assertIn("Answer YES ONLY", prompt)


class ExplicitRequestTests(unittest.TestCase):
    def test_requests(self):
        for text in ("bhai key dedo", "crown loader bhejo", "app do",
                     "help me", "group scan karo", "status batao",
                     "photo chahiye"):
            with self.subTest(text=text):
                self.assertTrue(triggers.is_explicit_request(text))

    def test_questions_and_chatter_not_requests(self):
        for text in ("kal aayega?", "kya hua bhai", "aaj mausam acha hai",
                     "hyy", "ok", "khel raha hu abhi"):
            with self.subTest(text=text):
                self.assertFalse(triggers.is_explicit_request(text))


if __name__ == "__main__":
    unittest.main()
