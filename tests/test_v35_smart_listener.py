"""v3.5 SMART LISTENER + MEMORY MANAGEMENT tests.

Locks in the owner's live-reported issues:
  1. 'user kisi se baat nahi kar raha, usko key chahiye to agent ko help
     karna chahiye' — 'key kha milega' (2 baar pucha, screenshot proof)
     ab GUARANTEED answer hota hai: help-seeking question tier direct
     route hota hai (gate failure kabhi use swallow nahi kar sakta).
  2. Agent messages DEKHNA kabhi nahi band karta — ingest + context
     remember har message par chalta hai (monitoring intact).
  3. 2-member exchange ki hifazat: exchange PARTICIPANT ka sawal
     user-to-user maana jata hai (silence), par TEESRE member ka
     help-sawal ka jawab milta hai.
  4. 'channel me jo data hai hmesha uske hi base pe dena chahiye' —
     GROUP questions group-scoped search se answer hote hain (dusre
     members ki shared group messages bhi grounding me).
  5. 'chats ka data bahot bada ho jayega, agent slow/stuck ho sakta hai'
     — hard caps per chat, LLM digests (short summaries), Q&A caps,
     digest search me milta hai.
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
        knowledge._CHAT_MSG_COUNT.clear()
        self.addCleanup(self.p_db.stop)
        self.addCleanup(self.p_owner.stop)
        self.addCleanup(db.invalidate_state_cache)
        self.addCleanup(channel_forward._LAST_LINK.clear)
        self.addCleanup(bot._GROUP_CONTEXT.clear)
        self.addCleanup(bot._REPLY_TIMES.clear)
        self.addCleanup(tg_api.OWN_MSG_IDS.clear)
        self.addCleanup(knowledge._CHAT_MSG_COUNT.clear)

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


# ---------------------------------------------------------------------------
# 1. THE SCREENSHOT SCENARIO: 'key kha milega' gets ANSWERED
# ---------------------------------------------------------------------------
class HelpSeekingTriggerTests(unittest.TestCase):
    def test_real_help_questions_detected(self):
        for text in ("key kha milega", "hello koi btayega key kha milega",
                     "key kaha milegi", "kaha se milega",
                     "crown loader kaise milega", "key kaha hai",
                     "app kaha se lu", "where can I get the key",
                     "koi help karo", "help chahiye", "key milega kya",
                     "madad chahiye bhai", "how to get crown key"):
            with self.subTest(text=text):
                self.assertTrue(triggers.is_help_seeking_question(text))

    def test_member_to_member_chatter_not_help_questions(self):
        for text in ("kal aayega?", "kya baat kar raha hai?",
                     "twitter pe ye video dekho", "aaj match jeet gaya",
                     "mila tha bhai", "koi bata diya tha mujhe",
                     "haan mila bhai", "ok", "hmm", "hello",
                     "game khel raha hu", "mil gaya tha kal",
                     "crown ka key mil gaya"):
            with self.subTest(text=text):
                self.assertFalse(triggers.is_help_seeking_question(text))


class ScreenshotScenarioTests(IMDBTestCase):
    """Exact replay of the owner's screenshot: new member PA, after the
    bot's own welcome and his own 'Thanks for joining', asks
    'key kha milega' — then AGAIN 'hello koi btayega key kha milega'.
    v3.4 me dono par silence tha. v3.5 me answer GUARANTEED hai."""

    def _seed_screenshot_context(self):
        # Rose ka welcome + bot ka reply + PA ka thanks (screenshot order)
        bot._remember(CHAT, "Rose", "Hey there Papa bol papa, welcome!",
                      uid=555)
        bot._remember(CHAT, "RK Manager", "Welcome bhai.", is_bot=True)
        bot._remember(CHAT, "PA", "Thanks for joining", uid=444)

    def test_key_question_answered_without_gate(self):
        self._seed_screenshot_context()
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot, "_should_reply_llm") as gate, \
                patch.object(bot.agent, "answer",
                             return_value={"text": "Key ka process..."}) as answer:
            bot.handle_message(group_message("key kha milega", user_id=444))
        answer.assert_called_once()
        # help-question tier: NO gate call at all — gate failure can
        # never swallow a genuine help question again
        gate.assert_not_called()
        send.assert_called()

    def test_repeat_key_question_also_answered(self):
        self._seed_screenshot_context()
        bot._remember(CHAT, "PA", "key kha milega", uid=444)
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], \
                patch.object(bot.agent, "answer",
                             return_value={"text": "Key ka process..."}) as answer:
            bot.handle_message(
                group_message("hello koi btayega key kha milega",
                              user_id=444))
        answer.assert_called_once()

    def test_gate_error_never_eats_help_question(self):
        """gate None (LLM error/timeout) + member + help question ->
        answer (fail-open). v3.4 me yahan silence tha — wahi live bug."""
        self._seed_screenshot_context()
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], \
                patch.object(bot, "_should_reply_llm", return_value=None), \
                patch.object(bot.agent, "answer",
                             return_value={"text": "haan bhai"}) as answer:
            bot.handle_message(group_message("kaha se milega ye key",
                                             user_id=444))
        # help-question tier gate ko call hi nahi karta — direct answer
        answer.assert_called_once()

    def test_gate_error_normal_chatter_still_silent(self):
        """Members ki normal baat par gate error = silence (v3.4 intact).
        Sirf GENUINE help-questions fail-open hote hain."""
        self._seed_screenshot_context()
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot, "_should_reply_llm", return_value=None), \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(group_message("aaj match jeet gaya",
                                             user_id=444))
        answer.assert_not_called()
        send.assert_not_called()


class AgentStillSeesEverythingTests(IMDBTestCase):
    """Owner ka sawal: 'aisa to nhi h na ki kisi ka msg hi nahi dekh rha'.
    Agent ko har message INGEST + CONTEXT-REMEMBER hona chahiye — chahe
    wo jawab de ya na de. Monitoring kabhi band nahi hoti."""

    def _run_silenced(self, text, user_id=444):
        patches = self._base_patches()
        # ingest_message patch KE andar real call bhi chalna chahiye —
        # isliye side_effect se wrapped real fn use karo
        with patches[0], patches[1], \
                patch.object(bot.knowledge, "ingest_message",
                             side_effect=bot.knowledge.ingest_message), \
                patches[3], patches[4], patches[5], \
                patch.object(bot, "_should_reply_llm", return_value=False), \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(group_message(text, user_id=user_id))
        return answer

    def test_even_silenced_message_is_remembered(self):
        """2-member exchange ka message (bot chup rehta hai) — phir bhi
        _GROUP_CONTEXT me yaad rehta hai (gate context ke liye)."""
        bot._remember(CHAT, "A", "crown loader ka key mila?", uid=111)
        bot._remember(CHAT, "B", "haan mila bhai", uid=222)
        self._run_silenced("acha mast hai", user_id=111)
        # current message context me remembered hai
        ctx = list(bot._GROUP_CONTEXT[CHAT])
        self.assertEqual(ctx[-1]["text"], "acha mast hai")
        self.assertEqual(ctx[-1]["uid"], 111)


# ---------------------------------------------------------------------------
# 2. EXCHANGE GUARD: participants silent, third-party helped
# ---------------------------------------------------------------------------
class ExchangeGuardV35Tests(IMDBTestCase):
    def _seed_exchange(self):
        bot._remember(CHAT, "A", "crown loader ka key mila?", uid=111)
        bot._remember(CHAT, "B", "haan mila bhai", uid=222)

    def test_exchange_participant_help_question_silent(self):
        """A<->B baat kar rahe hain, A B se 'maal kaha milega?' puch raha
        hai — user-to-user, bot chup rahega (anti-spam intact)."""
        self._seed_exchange()
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot, "_should_reply_llm") as gate, \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(group_message("bhai maal kaha milega?",
                                             user_id=111))
        gate.assert_not_called()
        answer.assert_not_called()
        send.assert_not_called()

    def test_third_party_help_question_answered_mid_exchange(self):
        """A<->B baat kar rahe hain, TEESRA member PA 'key kha milega'
        puchta hai — uska sawal group ko tha. v3.5: bot help karega."""
        self._seed_exchange()
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], \
                patch.object(bot, "_should_reply_llm") as gate, \
                patch.object(bot.agent, "answer",
                             return_value={"text": "key ka process hai..."}) as answer:
            bot.handle_message(group_message("key kha milega", user_id=444))
        answer.assert_called_once()
        gate.assert_not_called()  # help tier — no gate needed

    def test_third_party_plain_chatter_still_silent_mid_exchange(self):
        """Teesre member ki CASUAL baat par bhi interrupt nahi (sirf
        help/explicit hi pass hote hain)."""
        self._seed_exchange()
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(group_message("wow nice game", user_id=444))
        answer.assert_not_called()
        send.assert_not_called()

    def test_help_question_budget_capped(self):
        """Anti-spam backstop: help-question answers bhi reply-budget
        (4/300s) me hain — ye tier spam wapas nahi layega."""
        import time as _time
        with patch.object(bot, "_REPLY_BUDGET", 4), \
                patch.object(bot, "_REPLY_BUDGET_SECONDS", 300):
            bot._REPLY_TIMES.clear()
            for i in range(4):
                patches = self._base_patches()
                with patches[0], patches[1], patches[2], patches[3], \
                        patches[4], patches[5], \
                        patch.object(bot.agent, "answer",
                                     return_value={"text": "j"}) as answer:
                    bot.handle_message(group_message(
                        f"key kha milega {i}", user_id=444,
                        message_id=500 + i))
                answer.assert_called_once()
            # 5th — budget over, silence
            patches = self._base_patches()
            with patches[0], patches[1], patches[2], patches[3], \
                    patches[4], patches[5], \
                    patch.object(bot.agent, "answer") as answer:
                bot.handle_message(group_message("aur key kaha milega",
                                                 user_id=444,
                                                 message_id=999))
            answer.assert_not_called()


# ---------------------------------------------------------------------------
# 3. GROUNDING: group-scoped search (channel ke hisab se)
# ---------------------------------------------------------------------------
class GroupScopedSearchTests(IMDBTestCase):
    def _seed_group_messages(self):
        # RK GAMER (dusra member) ne key ka process samjhaya tha —
        # ye GROUP ki shared info hai (sab padh sakte hain)
        db.get_db()["messages"].insert_one({
            "chat_id": CHAT, "user_id": 777, "first_name": "RK GAMER",
            "text": "yaad rakhna night me key chahiye to game khel ke "
                    "screenshot bhejna sb group me ya dm me",
            "date": 900, "importance": 0.2,
        })
        # PA ki apni purani message
        db.get_db()["messages"].insert_one({
            "chat_id": CHAT, "user_id": 444, "first_name": "PA",
            "text": "aaj match khela", "date": 950, "importance": 0.0,
        })

    def test_group_scope_finds_other_members_message(self):
        self._seed_group_messages()
        hits = knowledge.search("key screenshot", limit=10, chat_id=CHAT)
        texts = " ".join(h.get("text", "") for h in hits)
        self.assertIn("screenshot", texts)

    def test_user_scope_still_private_for_dm(self):
        self._seed_group_messages()
        hits = knowledge.search("key screenshot", limit=10, user_id=444)
        self.assertEqual(hits, [])  # PA ki DM me RK GAMER ka msg nahi

    def test_agent_search_scope_group(self):
        self._seed_group_messages()
        ctx = {"group": True, "group_id": CHAT, "chat_id": CHAT,
               "user_id": 444}
        self.assertEqual(agent._search_scope(ctx), {"chat_id": CHAT})

    def test_agent_search_scope_private(self):
        ctx = {"user_id": 444, "chat_id": 444}
        self.assertEqual(agent._search_scope(ctx), {"user_id": 444})


# ---------------------------------------------------------------------------
# 4. MEMORY MANAGEMENT: caps + digests + bounded growth
# ---------------------------------------------------------------------------
class MemoryCapTests(IMDBTestCase):
    def setUp(self):
        super().setUp()
        self.p_digest = patch.object(config, "DIGEST_ENABLED", 1)
        self.p_digest.start()
        self.addCleanup(self.p_digest.stop)

    def _seed_chat(self, n, importance=0.0):
        import time as _time
        base = int(_time.time()) - 60  # RECENT dates — idle cleanup fires nahi
        for i in range(n):
            db.get_db()["messages"].insert_one({
                "chat_id": CHAT, "user_id": 100 + i % 5,
                "text": f"message number {i} about game",
                "date": base + i, "importance": importance,
            })

    def test_cap_enforced_and_digest_created(self):
        self._seed_chat(30)
        with patch.object(knowledge, "summarize_batch",
                          return_value="- keys screenshot bhejne par milti hai"
                                       "\n- crown loader popular hai") as sb:
            res = knowledge.compact_chat(CHAT, cap=10)
        self.assertEqual(res["removed"], 20)
        self.assertTrue(res["digest"])
        sb.assert_called_once()
        # raw messages capped at 10
        self.assertEqual(
            db.get_db()["messages"].count_documents({"chat_id": CHAT}), 10)
        # digest stored — community memory zinda hai
        self.assertEqual(db.get_db()["digests"].count_documents({}), 1)
        dg = db.get_db()["digests"].find_one({})
        self.assertIn("keys", dg["summary"])

    def test_high_importance_never_deleted(self):
        # 5 high-importance (owner rules) + 30 low
        self._seed_chat(30)
        for i in range(5):
            db.get_db()["messages"].insert_one({
                "chat_id": CHAT, "user_id": OWNER,
                "text": f"RULE: key sirf screenshot ke baad — note {i}",
                "date": 2000 + i, "importance": 0.9,
            })
        with patch.object(knowledge, "summarize_batch", return_value="- d"):
            knowledge.compact_chat(CHAT, cap=10)
        # high-importance messages sab zinda hain
        high = db.get_db()["messages"].find({
            "chat_id": CHAT, "importance": {"$gte": 0.5}})
        self.assertEqual(len(list(high)), 5)

    def test_no_digest_llm_no_silent_loss(self):
        """LLM digest fail ho to raw delete SKIP — data kabhi silently
        loss nahi hota (owner trust)."""
        self._seed_chat(30)
        with patch.object(knowledge, "summarize_batch", return_value=None):
            res = knowledge.compact_chat(CHAT, cap=10)
        self.assertEqual(res["removed"], 0)
        self.assertFalse(res["digest"])
        self.assertEqual(
            db.get_db()["messages"].count_documents({"chat_id": CHAT}), 30)

    def test_under_cap_noop(self):
        self._seed_chat(5)
        res = knowledge.compact_chat(CHAT, cap=10)
        self.assertEqual(res["removed"], 0)

    def test_digest_found_in_search(self):
        """Compaction ke baad bhi purani community info search me milti
        hai — 'short summary' grounding me zinda rehta hai."""
        self._seed_chat(30)
        db.get_db()["digests"].insert_one({
            "chat_id": CHAT,
            "summary": "- keys screenshot bhejne par milti hai\n"
                       "- crown loader v2 popular hai",
            "msg_count": 20, "ts_from": 1000, "ts_to": 1020,
            "created_at": 2000.0,
        })
        hits = knowledge.search("crown loader", limit=10, chat_id=CHAT)
        digest_hits = [h for h in hits if h.get("kind") == "digest"]
        self.assertTrue(digest_hits)
        self.assertIn("crown", digest_hits[0]["text"])

    def test_prune_once_enforces_caps(self):
        """12h prune cycle bhi caps enforce karta hai — restart ke baad
        bhi growth bounded guarantee."""
        self._seed_chat(30)
        # qa bhi jyada
        for i in range(50):
            db.get_db()["qa"].insert_one({
                "user_id": 444, "chat_id": CHAT,
                "question": f"q{i}", "answer": f"a{i}", "date": 100 + i,
            })
        with patch.object(knowledge, "summarize_batch", return_value="- d"), \
                patch.object(knowledge.tg_api, "send_message"), \
                patch.object(config, "MESSAGE_TTL_DAYS", 60), \
                patch.object(config, "USER_MEMORY_IDLE_DAYS", 7), \
                patch.object(config, "POST_TTL_DAYS", 120), \
                patch.object(config, "FILE_POST_TTL_DAYS", 240), \
                patch.object(config, "MEMORY_MAX_MESSAGES_PER_CHAT", 10), \
                patch.object(config, "MEMORY_MAX_QA_PER_USER", 20):
            counts = knowledge.prune_once()
        self.assertEqual(counts["compacted"], 20)
        self.assertGreater(counts["qa_capped"], 0)
        self.assertEqual(
            db.get_db()["messages"].count_documents({"chat_id": CHAT}), 10)
        self.assertEqual(
            db.get_db()["qa"].count_documents({"user_id": 444}), 20)

    def test_stats_includes_digests(self):
        st = knowledge.stats()
        self.assertIn("digests", st)


class PrefetchScopeTests(IMDBTestCase):
    """bot._start_answer_prefetch group messages par chat_id scope use
    karta hai (grounding ke liye)."""

    def test_prefetch_uses_chat_scope_for_group(self):
        captured = {}

        def fake_search(query, limit=10, user_id=None, chat_id=None):
            captured.update({"user_id": user_id, "chat_id": chat_id})
            return []

        with patch.object(bot.knowledge, "search", side_effect=fake_search):
            box = bot._start_answer_prefetch("key kha milega", 444,
                                             chat_id=CHAT)
            # daemon thread — wait for it
            import time as _t
            for _ in range(50):
                if "hits" in box:
                    break
                _t.sleep(0.02)
        self.assertEqual(captured["chat_id"], CHAT)
        self.assertEqual(captured["user_id"], 444)


if __name__ == "__main__":
    unittest.main()
