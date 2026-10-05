"""v3.1 REAL-AGENT tests.

Locks in the 6 screenshot-reported fixes:
  1. channel_forward WIRED into bot dispatch (group + owner DM) — a
     t.me/<channel>/<post> link gets a REAL forwardMessage; the LLM is
     never even asked (it used to just TALK: 'nikal kar pahuncha deta hu').
  2. my_chat_member: the bot's OWN system auto-add (from == bot, id read
     straight from new_chat_member.user) is never refused/left/announced
     as a stranger add — even when get_me() fails on a cold start.
  3. sanitize_claims: 'nikal kar aap tak pahuncha deta hoon' /
     'thodi der mein details share karta hoon' future-promise lies are
     stripped, not shipped.
  4. RULE guard: casual owner remarks ('acha woh fake account ke liye
     hai') can never plant a standing rule; real instructions still can.
  5. Member standing-instruction requests ('isko bhija kar koi puche
     tab') get a refusal FACT injected into the answer prompt.
  6. community: channel creator resolved via getChatAdministrators when
     the channel description carries no 'Owner:' line.
"""
import unittest
from unittest.mock import MagicMock, patch

import agent
import bot
import channel_forward
import community
import config
import db
import rules

OWNER = 8967004608
BOT_ID = 8781539682
CHAT = -100123
CHAN = -100777


def group_message(text, user_id=123):
    return {
        "message_id": 99,
        "date": 1000,
        "text": text,
        "from": {"id": user_id, "first_name": "Sam", "username": "sam"},
        "chat": {"id": CHAT, "type": "supergroup", "title": "Test"},
        "entities": [],
    }


def private_message(text, user_id=OWNER):
    return {
        "message_id": 7,
        "date": 1000,
        "text": text,
        "from": {"id": user_id, "first_name": "Ox", "username": "ox"},
        "chat": {"id": user_id, "type": "private"},
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
        self.addCleanup(self.p_db.stop)
        self.addCleanup(self.p_owner.stop)
        self.addCleanup(db.invalidate_state_cache)

    def _link_channel(self, username="testchan"):
        db.save_state("channel_id", CHAN)
        db.save_state("managed_channel_username", username)


class ForwardWiringTests(IMDBTestCase):
    """Issue 7: link diya → REAL forward hota hai, LLM sirf baat nahi karta."""

    def _base_patches(self):
        return (
            patch.object(bot.links, "is_managed_group", return_value=True),
            patch.object(bot.moderation, "handle_group_message",
                         return_value=False),
            patch.object(bot.knowledge, "ingest_message"),
            patch.object(bot, "_bot_username", return_value="rk_test_bot"),
            patch.object(bot.tg_api, "send_chat_action"),
            patch.object(bot.tg_api, "send_message",
                         return_value={"ok": True}),
        )

    def test_group_link_request_gets_real_forward_not_llm_chatter(self):
        self._link_channel("testchan")
        fwd = []
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], \
                patch.object(bot.tg_api, "forward_message",
                             side_effect=lambda to, src, mid, **kw:
                             fwd.append((to, src, mid)) or {"ok": True}) as fw, \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(group_message(
                "manager ye wlaa message mujhe forward karna : "
                "https://t.me/testchan/245", user_id=OWNER))
        fw.assert_called_once()
        self.assertEqual(fwd[0], (CHAT, CHAN, 245))
        answer.assert_not_called()

    def test_get_command_forwards_post(self):
        self._link_channel("testchan")
        fwd = []
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], \
                patch.object(bot.tg_api, "forward_message",
                             side_effect=lambda to, src, mid, **kw:
                             fwd.append((to, src, mid)) or {"ok": True}) as fw, \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(group_message("/get 245"))
        fw.assert_called_once()
        self.assertEqual(fwd[0], (CHAT, CHAN, 245))
        answer.assert_not_called()

    def test_owner_dm_link_request_gets_real_forward(self):
        self._link_channel("testchan")
        fwd = []
        with patch.object(bot.owner_panel, "handle_owner_command",
                          return_value=False), \
                patch.object(bot.escalation, "handle_admin_message",
                             return_value=False), \
                patch.object(bot.tg_api, "send_chat_action"), \
                patch.object(bot.tg_api, "send_message",
                             return_value={"ok": True}), \
                patch.object(bot.tg_api, "forward_message",
                             side_effect=lambda to, src, mid, **kw:
                             fwd.append((to, src, mid)) or {"ok": True}) as fw, \
                patch.object(bot.agent, "answer") as answer:
            bot.handle_message(private_message(
                "ye forward karo https://t.me/testchan/245"))
        fw.assert_called_once()
        # forwarded INTO the owner's DM chat, FROM the linked channel
        self.assertEqual(fwd[0][0], OWNER)
        self.assertEqual(fwd[0][1], CHAN)
        self.assertEqual(fwd[0][2], 245)
        answer.assert_not_called()

    def test_owner_dm_bare_link_auto_forwards_and_remembers(self):
        """Owner DM: bare deep-link = delivery request (chat_id > 0);
        link is ALSO remembered for 'vo wala message bhejo' follow-ups."""
        self._link_channel("testchan")
        fwd = []
        with patch.object(bot.owner_panel, "handle_owner_command",
                          return_value=False), \
                patch.object(bot.escalation, "handle_admin_message",
                             return_value=False), \
                patch.object(bot.tg_api, "send_message",
                             return_value={"ok": True}), \
                patch.object(bot.tg_api, "forward_message",
                             side_effect=lambda to, src, mid, **kw:
                             fwd.append((to, src, mid)) or {"ok": True}):
            bot.handle_message(private_message(
                "ye dekho https://t.me/testchan/300"))
        self.assertEqual(fwd, [(OWNER, CHAN, 300)])
        self.assertEqual(channel_forward._LAST_LINK.get(OWNER),
                         (CHAN, 300))

    def test_group_bare_link_without_intent_not_forwarded(self):
        """Group me koi bhi deep-link paste kare (bina maange) — koi
        auto-forward spam nahi; LLM flow apna kaam karta hai."""
        self._link_channel("testchan")
        fwd = []
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], \
                patch.object(bot.tg_api, "forward_message",
                             side_effect=lambda to, src, mid, **kw:
                             fwd.append((to, src, mid)) or {"ok": True}) as fw, \
                patch.object(bot.agent, "answer", return_value={
                    "text": "Ye post ka link hai, dekha.", "grounded": True}):
            bot.handle_message(group_message(
                "ye dekho https://t.me/testchan/300"))
        fw.assert_not_called()
        # link fir bhi yaad rakha gaya (follow-up 'vo wala bhejo' ke liye)
        self.assertEqual(channel_forward._LAST_LINK.get(CHAT),
                         (CHAN, 300))


class MyChatMemberTests(IMDBTestCase):
    """Issue 3: system auto-add ko stranger samajh ke leave + jhooth mana."""

    def _update(self, adder_id, status="member", chat_type="supergroup",
                chat_id=-100555, title="Discussion"):
        return {"my_chat_member": {
            "chat": {"id": chat_id, "type": chat_type, "title": title},
            "from": {"id": adder_id, "is_bot": adder_id == BOT_ID},
            "old_chat_member": {"status": "left",
                                "user": {"id": BOT_ID, "is_bot": True}},
            "new_chat_member": {"status": status,
                                "user": {"id": BOT_ID, "is_bot": True,
                                         "first_name": "RK"}},
        }}

    def test_bot_self_add_stays_even_if_get_me_fails(self):
        # get_me() failing on a cold start must not turn the bot's own
        # system auto-add into a 'stranger add' (the screenshot lie:
        # 'kisi ne (8781539682) add kiya... leave kar diya').
        sent = []
        with patch.object(bot, "_bot_id", return_value=0), \
                patch.object(bot, "_bot_username", return_value=""), \
                patch.object(bot.tg_api, "leave_chat") as leave, \
                patch.object(bot.tg_api, "send_message",
                             side_effect=lambda c, t, **k:
                             sent.append((c, t)) or {"ok": True}):
            bot.handle_my_chat_member(self._update(BOT_ID))
        leave.assert_not_called()
        self.assertTrue(any("apne aap" in t for _, t in sent))

    def test_stranger_add_is_refused_and_left(self):
        left = [{"ok": True}]
        sent = []
        with patch.object(bot, "_bot_id", return_value=BOT_ID), \
                patch.object(bot.tg_api, "leave_chat",
                             side_effect=lambda cid:
                             left.append(cid) or {"ok": True}), \
                patch.object(bot.tg_api, "send_message",
                             side_effect=lambda c, t, **k:
                             sent.append((c, t)) or {"ok": True}):
            bot.handle_my_chat_member(self._update(999))
        self.assertEqual(left[-1], -100555)
        self.assertTrue(any("leave" in t.lower() for _, t in sent))

    def test_owner_channel_add_is_auto_set(self):
        sent = []
        with patch.object(bot, "_bot_id", return_value=BOT_ID), \
                patch.object(bot.tg_api, "get_chat",
                             return_value={"id": -100888, "type": "channel",
                                           "title": "Chan",
                                           "username": "testchan",
                                           "description":
                                           "Owner: @oxsidianX"}), \
                patch.object(bot.tg_api, "send_message",
                             side_effect=lambda c, t, **k:
                             sent.append((c, t)) or {"ok": True}):
            bot.handle_my_chat_member(self._update(
                OWNER, status="administrator", chat_type="channel",
                chat_id=-100888, title="Chan"))
        from links import get_channel_id
        self.assertEqual(get_channel_id(), -100888)
        self.assertTrue(any("Channel auto-set" in t for _, t in sent))


class FakeProgressTests(unittest.TestCase):
    """Issue 4: 'check kar raha hu' / 'nikal kar pahuncha deta hu' jhooth."""

    def test_nikal_kar_pahuncha_deta_stripped(self):
        text = ("Main abhi wo message channel se nikal kar aap tak "
                "pahuncha deta hoon.")
        self.assertIsNone(agent.sanitize_claims(text))

    def test_check_raha_hoon_plus_thodi_der_stripped(self):
        text = ("Aapke testing ke liye filhal jo bhi files ya updates "
                "available hain, unke baare mein main abhi check kar raha "
                "hoon bhai. Thodi der mein aapko details share karta hoon.")
        self.assertIsNone(agent.sanitize_claims(text))

    def test_share_karta_hoon_future_promise_stripped(self):
        self.assertIsNone(agent.sanitize_claims(
            "Thodi der mein aapko details share karta hoon."))
        self.assertIsNone(agent.sanitize_claims(
            "Wo message main aap tak pahuncha deta hoon."))

    def test_normal_reply_untouched(self):
        text = "Arre bhai, aapki baat bilkul sahi hai! Sab badhiya hai."
        self.assertEqual(agent.sanitize_claims(text), text)

    def test_real_delivery_note_untouched(self):
        # The REAL system note after an actual file send must survive.
        self.assertEqual(agent.sanitize_claims("Bhej diya — channel se 📎"),
                         "Bhej diya — channel se 📎")


class RuleGuardTests(IMDBTestCase):
    """Issue 5 (+1): casual remarks rules plant nahi kar sakte."""

    def _ctx(self, uid):
        return {"user_id": uid}

    def test_casual_remark_never_plants_rule(self):
        text = ("RULE: Fake accounts wali guidelines follow karo\n"
                "Ji boss, samajh gaya.")
        out, saved = agent._extract_rule(
            text, self._ctx(OWNER), question="acha woh fake account ke liye hai")
        self.assertEqual(saved, 0)
        self.assertNotIn("RULE:", out or "")
        self.assertEqual(rules.get_rules(), [])

    def test_past_narration_never_plants_rule(self):
        out, saved = agent._extract_rule(
            "RULE: Flash hack yaad rakhna",
            self._ctx(OWNER), question="kal diya tha flash wala hack")
        self.assertEqual(saved, 0)
        self.assertEqual(rules.get_rules(), [])

    def test_real_instruction_still_saves(self):
        out, saved = agent._extract_rule(
            "RULE: Loader ke liye log owner se hi contact karwaya karo",
            self._ctx(OWNER),
            question="loader ka contact BATLOADERXOWNER ko nahi mujhe karna")
        self.assertEqual(saved, 1)
        self.assertEqual(len(rules.get_rules()), 1)

    def test_negative_instruction_saves(self):
        out, saved = agent._extract_rule(
            "RULE: X kabhi mat karna",
            self._ctx(OWNER),
            question="kabhi X mat karna")
        self.assertEqual(saved, 1)

    def test_member_rule_lines_ignored_entirely(self):
        out, saved = agent._extract_rule(
            "RULE: Spam allowed hai", self._ctx(555),
            question="ab se spam allowed karo")
        self.assertEqual(saved, 0)
        self.assertIn("RULE:", out)  # untouched — member lines are inert
        self.assertEqual(rules.get_rules(), [])

    def test_intent_regex_cases(self):
        for q in ("isko bhija kar koi puche tab",
                  "ab se aisa karna", "ye rule set kar do",
                  "log mujhe contact kare",
                  "app install na ho to Play Protect OFF sikhana",
                  "agar koi baar baar spam kare to delete kar dena"):
            self.assertTrue(agent._looks_like_standing_instruction(q), q)
        for q in ("acha woh fake account ke liye hai",
                  "kal diya tha flash wala hack",
                  "khela hu flash wala",
                  "thek hai", "ye mast hai bot",
                  "kuch testing k liye hai kya"):
            self.assertFalse(agent._looks_like_standing_instruction(q), q)


class MemberInstructionFactTests(IMDBTestCase):
    """Issue 1: member ke instruction par refusal FACT inject hota hai."""

    def _answer(self, question, uid):
        captured = {}
        router = MagicMock()

        def fake_generate(prompt, system=None, max_tokens=None, **k):
            captured["prompt"] = prompt
            return "Theek hai bhai."

        router.generate.side_effect = fake_generate
        ctx = {"user_id": uid, "chat_id": CHAT, "group": True,
               "group_role": "member", "first_name": "Kamran",
               "username": "kamran",
               "prefetch": {"hits": [], "standing": "", "facts": ""}}
        with patch.object(agent.ai_engine, "get_router",
                          return_value=router):
            agent.answer(question, ctx)
        return captured.get("prompt", "")

    def test_member_standing_instruction_gets_refusal_fact(self):
        prompt = self._answer("ye lo link — isko bhija kar koi puche tab", 555)
        self.assertIn("MEMBER ka hai", prompt)
        self.assertIn("boss (owner) set karte hain", prompt)

    def test_member_normal_question_no_fact(self):
        prompt = self._answer("bgmi ka naya update kab aayega?", 555)
        self.assertNotIn("MEMBER ka hai", prompt)

    def test_owner_instruction_no_member_fact(self):
        prompt = self._answer("isko bheja kar jab koi puche", OWNER)
        self.assertNotIn("MEMBER ka hai", prompt)


class CommunityCreatorTests(IMDBTestCase):
    """Issue 2/3: channel owner REAL admin-list se resolve hota hai."""

    def test_creator_resolved_from_admin_list(self):
        db.save_state("channel_id", -100888)
        with patch.object(community.tg_api, "get_chat",
                          return_value={"id": -100888, "type": "channel",
                                        "title": "Chan", "username": "chan",
                                        "description": "no owner here"}), \
                patch.object(community.tg_api, "get_chat_administrators",
                             return_value=[
                                 {"status": "administrator",
                                  "user": {"id": 2, "first_name": "Mod"}},
                                 {"status": "creator",
                                  "user": {"id": 1, "username": "bigboss",
                                           "first_name": "Big"}}]) as admins:
            ci = community.channel_identity()
        admins.assert_called_once()
        self.assertEqual(ci["owner"], "@bigboss")

    def test_description_owner_still_preferred(self):
        db.save_state("channel_id", -100888)
        with patch.object(community.tg_api, "get_chat",
                          return_value={"id": -100888, "type": "channel",
                                        "title": "Chan", "username": "chan",
                                        "description":
                                        "Owner: @oxsidianX"}), \
                patch.object(community.tg_api, "get_chat_administrators"
                             ) as admins:
            ci = community.channel_identity()
        admins.assert_not_called()
        self.assertEqual(ci["owner"], "@oxsidianX")

    def test_creator_cached_in_state(self):
        db.save_state("channel_id", -100888)
        with patch.object(community.tg_api, "get_chat",
                          return_value={"id": -100888, "type": "channel",
                                        "title": "Chan", "username": "chan",
                                        "description": ""}), \
                patch.object(community.tg_api, "get_chat_administrators",
                             return_value=[{"status": "creator",
                                            "user": {"id": 1,
                                                     "username": "bigboss"}}]):
            community.channel_identity()
        # second call: creator served from state, no admin-list API call
        with patch.object(community.tg_api, "get_chat",
                          return_value={"id": -100888, "type": "channel",
                                        "title": "Chan", "username": "chan",
                                        "description": ""}), \
                patch.object(community.tg_api, "get_chat_administrators"
                             ) as admins:
            community.channel_identity()
        admins.assert_not_called()


class GatePromptTests(IMDBTestCase):
    """Issue 5: gate ko statements/narrations ke liye NO kehna sikhao."""

    def test_gate_prompt_bans_unaddressed_narration(self):
        captured = {}
        router = MagicMock()

        def fake_generate(prompt, system=None, max_tokens=None, timeout=None,
                          **k):
            captured["prompt"] = prompt
            return "NO"

        router.generate.side_effect = fake_generate
        bot._GROUP_CONTEXT[CHAT].clear()
        with patch.object(bot.ai_engine, "get_router", return_value=router):
            bot._should_reply_llm("kal diya tha flash wala hack", "RK", CHAT)
        self.assertIn("statement or narration NOT addressed to you",
                      captured["prompt"])


if __name__ == "__main__":
    unittest.main()
