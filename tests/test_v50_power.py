"""v5.0 MAX POWER AGENT tests.

Owner ka brief: 'telegram ke hisab se jitna powerful bana sakte ho bana
do — real agent ki tarah, sab tools ke saath.'

Locks in:
  1. TOOL: protocol — parse (TOOL/LOOKUP/WHOIS aliases), unknown tool
     reject, TOOL_MAX_ROUNDS respected.
  2. Tool loop — model TOOL request kare to tool chalta hai, result
     feed-back hota hai, final answer aata hai; max rounds par stop.
  3. Tools execute graceful — SEARCH/GETMSG/RECENT/USER/PINNED/FILES/
     INFO/LINKCHECK sab fail-par TOOL_ERROR, kabhi crash nahi.
  4. FLOOD GUARD — burst threshold pe warn, repeat pe mute, admin
     exempt, force=True direct test mode.
  5. REMINDERS — '/remind 30m text' + spoken '10 min baad yaad dila'
     parse; Mongo-persist; due delivery.
  6. WELCOME — naya member welcome (cooldown respected), bots skip,
     ban alert.
  7. INVITE/POLL commands — admin-only gating + TG api calls.
  8. MEMBER CARDS — card_update counters, member_card_block text.
  9. WATCHDOG — unanswered help-sawal queue + still-unanswered logic
     (human reply aa jaye to rescue nahi).
  10. RADAR — radar ingest dedupe (Mongo me already to skip), context
      remember hamesha.
  11. MTProto latest-id tracker — note_message_id monotonic.
  12. Admin log describe — known action types human-readable.
"""
import time
import unittest
from unittest.mock import patch, MagicMock

import agent
import agent_tools
import bot
import config
import db
import knowledge
import mtproto_layer
import powers


OWNER = 8967004608
CHAT = -1004303033609
CHAN = -1003956726834


class IMDBTestCase(unittest.TestCase):
    def setUp(self):
        self._db = db.IMDB()
        self.p_db = patch.object(db, "_DB", self._db)
        self.p_db.start()
        db.invalidate_state_cache()
        self.p_owner = patch.object(config, "OWNER_CHAT_ID", OWNER)
        self.p_owner.start()
        # flood/watchdog/welcome state reset
        powers._FLOOD_TS.clear()
        powers._FLOOD_STRIKES.clear()
        powers._WELCOMED.clear()
        powers._JOIN_TRACK.clear()
        bot._WATCHDOG_QUEUE.clear()
        bot._WATCHDOG_FIRED.clear()
        bot._GROUP_CONTEXT.clear()
        bot._UID_NAMES.clear()
        agent_tools.TOOL_STATS["calls"] = 0
        agent_tools.TOOL_STATS["rounds"] = 0

    def tearDown(self):
        self.p_db.stop()
        self.p_owner.stop()
        db.invalidate_state_cache()
        powers._FLOOD_TS.clear()
        powers._FLOOD_STRIKES.clear()
        bot._WATCHDOG_QUEUE.clear()
        bot._WATCHDOG_FIRED.clear()


# ---------------------------------------------------------------------------
# 1) TOOL: protocol parsing
# ---------------------------------------------------------------------------

class ToolParseTests(IMDBTestCase):
    def test_tool_parse_basic(self):
        self.assertEqual(agent_tools.parse_tool_request("TOOL: SEARCH naya loader"),
                         ("SEARCH", "naya loader"))
        self.assertEqual(agent_tools.parse_tool_request("TOOL:USER rich"),
                         ("USER", "rich"))
        self.assertIsNone(agent_tools.parse_tool_request("seedha jawab do"))

    def test_lookup_alias_maps_to_search(self):
        self.assertEqual(agent_tools.parse_tool_request("LOOKUP: key kha milega"),
                         ("SEARCH", "key kha milega"))

    def test_whois_alias(self):
        self.assertEqual(agent_tools.parse_tool_request("TOOL: WHOIS @rich"),
                         ("USER", "@rich"))

    def test_unknown_tool_rejected(self):
        self.assertIsNone(agent_tools.parse_tool_request("TOOL: TELEPORT home"))

    def test_multiline_reply_uses_first_tool_line(self):
        m = agent_tools.parse_tool_request("Ruko bhai\nTOOL: RECENT 10\nrest")
        self.assertEqual(m, ("RECENT", "10"))


# ---------------------------------------------------------------------------
# 2) tool loop driver
# ---------------------------------------------------------------------------

class ToolLoopTests(IMDBTestCase):
    def _gen_script(self, replies):
        calls = []

        def gen(prompt, system, ctx):
            calls.append(prompt)
            # pehla followup-gen -> replies[1] (final), aage sab last
            return replies[min(len(calls), len(replies) - 1)]
        return gen, calls

    def test_no_tool_direct_return(self):
        gen, calls = self._gen_script(["final jawab"])
        out, interim = agent_tools.run_tool_loop(
            "q?", "final jawab", {}, gen, "sys", notify_interim=None)
        self.assertEqual(out, "final jawab")
        self.assertIsNone(interim)
        self.assertEqual(calls, [])  # koi follow-up generate nahi

    def test_tool_round_then_final(self):
        gen, calls = self._gen_script(
            ["TOOL: SEARCH crown loader", "final: mil gaya"])
        notified = []
        out, interim = agent_tools.run_tool_loop(
            "crown loader?", "TOOL: SEARCH crown loader", {},
            gen, "sys", notify_interim=lambda: notified.append(1))
        self.assertEqual(out, "final: mil gaya")
        self.assertEqual(interim, "sent")      # pehle round par interim gaya
        self.assertEqual(notified, [1])        # sirf EK baar
        self.assertEqual(agent_tools.TOOL_STATS["calls"], 1)
        self.assertIn("TOOL SEARCH result", calls[0])  # result feed-back
        self.assertIn("crown loader", calls[0])       # original sawal zinda

    def test_max_rounds_stop(self):
        # model har baar TOOL hi maangta hai — 3 rounds ke baad rukna hai
        gen, calls = self._gen_script(["TOOL: SEARCH x"] * 10)
        out, interim = agent_tools.run_tool_loop(
            "q?", "TOOL: SEARCH x", {}, gen, "sys")
        self.assertEqual(len(calls), agent_tools.TOOL_MAX_ROUNDS)

    def test_legacy_lookup_via_agent_runs_tools(self):
        """agent.answer path: pehla reply LOOKUP: ho to tool loop chalta hai."""
        with patch.object(agent, "_generate_answer",
                          side_effect=["LOOKUP: crown loader",
                                       "Crown loader post #12 me hai."]), \
             patch.object(agent, "_looks_like_file_request", return_value=False), \
             patch.object(knowledge, "search", return_value=[]), \
             patch.object(tg_api_mod(agent), "send_message") as sm:
            res = agent.answer("crown loader konsa?", {
                "user_id": 1, "chat_id": CHAT, "group": True,
                "group_id": CHAT, "group_role": "member",
                "prefetch": {"hits": [], "standing": None, "facts": None},
            })
        self.assertIn("Crown loader", (res.get("text") or ""))


def tg_api_mod(mod):
    import tg_api
    return tg_api


# ---------------------------------------------------------------------------
# 3) tool implementations — graceful
# ---------------------------------------------------------------------------

class ToolExecTests(IMDBTestCase):
    def test_search_no_records(self):
        with patch.object(knowledge, "search", return_value=[]):
            out = agent_tools.execute("SEARCH", "kuch nahi", {})
        self.assertIn("no records", out)

    def test_getmsg_needs_id(self):
        out = agent_tools.execute("GETMSG", "abc", {"chat_id": CHAT})
        self.assertIn("TOOL_ERROR", out)

    def test_getmsg_layer_down(self):
        out = agent_tools.execute("GETMSG", "123", {"chat_id": CHAT})
        self.assertIn("TOOL_ERROR", out)

    def test_recent_layer_down(self):
        with patch.object(mtproto_layer, "is_connected", return_value=False):
            out = agent_tools.execute("RECENT", "5", {"chat_id": CHAT})
        self.assertIn("TOOL_ERROR", out)

    def test_user_unknown_member(self):
        out = agent_tools.execute("USER", "ghumakkad", {"chat_id": CHAT})
        self.assertIn("no member", out)

    def test_user_with_card(self):
        powers.card_update(77, {"text": "crown loader kaise download karu"},
                           is_question=True, name="Rich")
        with patch.object(bot.tg_api, "get_chat_member",
                          return_value={"status": "member",
                                        "joined_date": 1700000000}):
            out = agent_tools.execute("USER", "Rich", {"chat_id": CHAT})
        self.assertIn("Rich", out)
        self.assertIn("member", out)
        self.assertIn("questions asked: 1", out)

    def test_files_tool(self):
        db.get_db()["channel_posts"].insert_one({
            "channel_id": CHAN, "message_id": 21, "file_id": "f1",
            "file_name": "com_twitter_android.apkm", "file_type": "document",
            "text": "", "caption": "", "date": 1700000000,
        })
        out = agent_tools.execute("FILES", "twitter", {})
        self.assertIn("com_twitter_android", out)
        self.assertIn("post #21", out)

    def test_pinned_none(self):
        with patch.object(bot.tg_api, "get_chat", return_value={}) as gc:
            out = agent_tools.execute("PINNED", "", {"chat_id": CHAT})
        self.assertIn("no pinned", out)

    def test_pinned_found(self):
        with patch.object(bot.tg_api, "get_chat", return_value={
                "pinned_message": {"message_id": 55, "text": "Rules yahan",
                                   "from": {"first_name": "Boss"}}}):
            out = agent_tools.execute("PINNED", "", {"chat_id": CHAT})
        self.assertIn("#55", out)
        self.assertIn("Rules yahan", out)

    def test_unknown_tool_name(self):
        out = agent_tools.execute("FOO", "", {})
        self.assertIn("TOOL_ERROR", out)
        self.assertIn("unknown tool", out)

    def test_adminlog_graceful_down(self):
        with patch.object(mtproto_layer, "is_connected", return_value=False), \
             patch.object(agent_tools.links, "get_channel_id", return_value=CHAN):
            out = agent_tools.execute("ADMINLOG", "10", {"chat_id": CHAT})
        self.assertIn("TOOL_ERROR", out)


# ---------------------------------------------------------------------------
# 4) FLOOD GUARD
# ---------------------------------------------------------------------------

class FloodGuardTests(IMDBTestCase):
    def _msg(self, n=1):
        for _ in range(n):
            yield

    def test_admin_exempt(self):
        r = powers.flood_check(CHAT, 999, is_admin=True, force=True)
        self.assertFalse(r)

    def test_burst_warn_then_mute(self):
        uid = 555
        # 8 messages burst window me — 9th par flood warn
        fired = None
        for i in range(9):
            fired = powers.flood_check(CHAT, uid, is_admin=False, force=True)
        self.assertTrue(fired)  # warn strike
        # repeat burst → mute path
        powers._FLOOD_TS[(CHAT, uid)].clear()
        with patch.object(powers.tg_api, "send_message") as sm, \
             patch.object(powers.moderation, "manual_mute",
                          return_value={"ok": True}) as mm:
            for i in range(9):
                r = powers.flood_check(CHAT, uid, is_admin=False, force=True)
            self.assertTrue(r)
        mm.assert_called()  # mute bhi hua

    def test_normal_pace_ok(self):
        uid = 556
        r = None
        for i in range(5):
            r = powers.flood_check(CHAT, uid, is_admin=False, force=True)
        self.assertFalse(r)


# ---------------------------------------------------------------------------
# 5) REMINDERS
# ---------------------------------------------------------------------------

class ReminderTests(IMDBTestCase):
    def test_parse_command_30m(self):
        pr = powers.parse_reminder("/remind 30m match khelna hai")
        self.assertIsNotNone(pr)
        self.assertEqual(pr[0], 1800)
        self.assertIn("match", pr[1])

    def test_parse_command_2h(self):
        pr = powers.parse_reminder("/remind 2h key event")
        self.assertEqual(pr[0], 7200)

    def test_parse_spoken(self):
        pr = powers.parse_reminder("bhai 10 min baad yaad dila dena loader")
        self.assertIsNotNone(pr)
        self.assertEqual(pr[0], 600)

    def test_parse_spoken_hours(self):
        pr = powers.parse_reminder("2 ghante baad yaad dila dena")
        self.assertEqual(pr[0], 7200)

    def test_no_reminder_intent(self):
        self.assertIsNone(powers.parse_reminder("kya aap bot ho?"))
        self.assertIsNone(powers.parse_reminder("/remind"))
        self.assertIsNone(powers.parse_reminder("yaad dila dena"))

    def test_schedule_and_due(self):
        doc = powers.schedule_reminder(CHAT, 42, 10, "test note")
        self.assertIsNotNone(doc)
        self.assertIn("rid", doc)          # IMDB-safe stable id
        self.assertEqual(len(powers.pending_reminders(CHAT)), 1)
        # due nahi abhi
        self.assertEqual(powers._due_reminders(), [])
        # backdate karke due bana do
        db.get_db()["reminders"].update_one(
            {"rid": doc["rid"]}, {"$set": {"due_at": int(time.time()) - 5}})
        due = powers._due_reminders()
        self.assertEqual(len(due), 1)
        # mark done → ab due nahi
        powers._mark_done(doc["rid"])
        self.assertEqual(powers._due_reminders(), [])

    def test_human_delay(self):
        self.assertEqual(powers._human_delay(600), "10 minute")
        self.assertEqual(powers._human_delay(7200), "2 ghante")
        self.assertEqual(powers._human_delay(172800), "2 din")


# ---------------------------------------------------------------------------
# 6) WELCOME
# ---------------------------------------------------------------------------

class WelcomeTests(IMDBTestCase):
    def _join_update(self, uid=31337, name="NewBhai"):
        return {"chat_member": {
            "chat": {"id": CHAT, "type": "supergroup", "title": "RK"},
            "from": {"id": OWNER},
            "old_chat_member": {"status": "left",
                                "user": {"id": uid, "is_bot": False}},
            "new_chat_member": {"status": "member",
                                "user": {"id": uid, "first_name": name,
                                         "is_bot": False}},
        }}

    def test_new_member_welcomed(self):
        with patch.object(bot.links, "is_managed_group", return_value=True), \
             patch.object(bot.tg_api, "send_message") as sm:
            powers.handle_chat_member(self._join_update())
        sm.assert_called()
        sent = sm.call_args.args[1]
        self.assertIn("Welcome NewBhai", sent)

    def test_welcome_cooldown(self):
        upd = self._join_update()
        with patch.object(bot.links, "is_managed_group", return_value=True), \
             patch.object(bot.tg_api, "send_message") as sm:
            powers.handle_chat_member(upd)
            powers.handle_chat_member(upd)  # turant dobara join
        self.assertEqual(sm.call_count, 1)  # cooldown → ek hi welcome

    def test_bot_member_not_welcomed(self):
        upd = self._join_update(uid=999, name="OtherBot")
        upd["chat_member"]["new_chat_member"]["user"]["is_bot"] = True
        with patch.object(bot.links, "is_managed_group", return_value=True), \
             patch.object(bot.tg_api, "send_message") as sm:
            powers.handle_chat_member(upd)
        sm.assert_not_called()

    def test_ban_alerts_owner(self):
        upd = self._join_update()
        upd["chat_member"]["old_chat_member"]["status"] = "member"
        upd["chat_member"]["new_chat_member"]["status"] = "kicked"
        with patch.object(bot.links, "is_managed_group", return_value=True), \
             patch.object(bot.tg_api, "send_message") as sm:
            powers.handle_chat_member(upd)
        sm.assert_called()
        self.assertIn("ban", sm.call_args.args[1].lower())

    def test_service_message_welcome(self):
        msg = {"chat": {"id": CHAT, "type": "supergroup"},
               "message_id": 9,
               "new_chat_members": [{"id": 4242, "first_name": "Hero",
                                     "is_bot": False}]}
        with patch.object(bot.links, "is_managed_group", return_value=True), \
             patch.object(bot.tg_api, "send_message") as sm:
            powers.handle_new_chat_members_service(msg)
        sm.assert_called()
        self.assertIn("Welcome Hero", sm.call_args.args[1])


# ---------------------------------------------------------------------------
# 7) INVITE / POLL commands
# ---------------------------------------------------------------------------

class InvitePollTests(IMDBTestCase):
    def test_invite_admin_only(self):
        msg = {"text": "/invite", "message_id": 5}
        with patch.object(bot.tg_api, "send_message") as sm:
            r = powers.handle_invite_command(msg, 111, CHAT, is_admin=False)
        self.assertTrue(r)
        self.assertIn("owner/admin", sm.call_args.args[1])

    def test_invite_creates_link(self):
        msg = {"text": "/invite 10 24", "message_id": 6}
        with patch.object(bot.tg_api, "create_chat_invite_link",
                          return_value={"result": {"invite_link": "t.me/+x"}}) as ci, \
             patch.object(bot.tg_api, "send_message") as sm:
            r = powers.handle_invite_command(msg, OWNER, CHAT, is_admin=True)
        self.assertTrue(r)
        ci.assert_called_once()
        self.assertEqual(ci.call_args.kwargs.get("member_limit"), 10)
        self.assertEqual(ci.call_args.kwargs.get("expire_hours"), 24)
        self.assertIn("t.me/+x", sm.call_args.args[1])

    def test_poll_command(self):
        msg = {"text": "/poll best gun | m416 | akm", "message_id": 7}
        with patch.object(bot.tg_api, "send_poll",
                          return_value={"ok": True}) as sp:
            r = powers.handle_poll_command(msg, OWNER, CHAT, is_admin=True)
        self.assertTrue(r)
        self.assertEqual(sp.call_args.args[1], "best gun")
        self.assertEqual(sp.call_args.args[2], ["m416", "akm"])

    def test_poll_member_refused(self):
        msg = {"text": "/poll q | a | b", "message_id": 8}
        with patch.object(bot.tg_api, "send_message") as sm:
            r = powers.handle_poll_command(msg, 123, CHAT, is_admin=False)
        self.assertTrue(r)
        self.assertIn("sirf owner/admin", sm.call_args.args[1])

    def test_spoken_poll_parse(self):
        q, opts = powers.parse_spoken_poll("poll banao best gun | m416 | akm | ump")
        self.assertEqual(q, "best gun")
        self.assertEqual(opts, ["m416", "akm", "ump"])

    def test_spoken_invite_request_detected(self):
        self.assertIsNotNone(bot._INVITE_REQUEST_RE.search("bhai invite link banao"))
        self.assertIsNotNone(bot._INVITE_REQUEST_RE.search("invite system banao"))
        self.assertIsNone(bot._INVITE_REQUEST_RE.search("invite code kya hai"))


# ---------------------------------------------------------------------------
# 8) MEMBER CARDS
# ---------------------------------------------------------------------------

class MemberCardTests(IMDBTestCase):
    def test_card_update_counters(self):
        powers.card_update(808, {"text": "crown loader kaise lu"},
                           is_question=True, name="Rich", username="rich1")
        powers.card_update(808, {"text": "hello"}, name="Rich")
        block = powers.member_card_block(808)
        self.assertIn("Rich", block)
        self.assertIn("questions", block)

    def test_card_none_for_anon(self):
        powers.card_update(None, {})
        self.assertIsNone(powers.get_card(None))
        self.assertEqual(powers.member_card_block(0), "")

    def test_topics_capped(self):
        powers.card_update(909, {"text": "twitter facebook instagram snapchat"},
                           is_question=True)
        card = powers.get_card(909)
        self.assertLessEqual(len(card.get("topics") or []), 3)


# ---------------------------------------------------------------------------
# 9) WATCHDOG
# ---------------------------------------------------------------------------

class WatchdogTests(IMDBTestCase):
    def test_enqueue(self):
        bot._watchdog_enqueue(CHAT, 500, 111, "A", "key kha milega")
        self.assertEqual(len(bot._WATCHDOG_QUEUE), 1)

    def test_disabled_noop(self):
        with patch.object(bot.config, "WATCHDOG_ENABLED", 0):
            bot._watchdog_enqueue(CHAT, 501, 111, "A", "key?")
        self.assertEqual(len(bot._WATCHDOG_QUEUE), 0)

    def test_still_unanswered_true(self):
        item = {"chat_id": CHAT, "msg_id": 600, "uid": 1, "ts": time.time(),
                "text": "key kha milega"}
        self.assertTrue(bot._watchdog_still_unanswered(item))

    def test_human_replied_rescues_cancel(self):
        bot._remember(CHAT, "member", "yahan se lo", uid=222)
        item = {"chat_id": CHAT, "msg_id": 600, "uid": 1,
                "ts": time.time() - 1, "text": "key kha milega"}
        self.assertFalse(bot._watchdog_still_unanswered(item))

    def test_bot_replied_cancels(self):
        bot._remember(CHAT, "member", "key kha milega", uid=222)
        time.sleep(0.01)
        bot._remember(CHAT, "RK Manager", "channel post #197 me hai", is_bot=True)
        item = {"chat_id": CHAT, "msg_id": 600, "uid": 1,
                "ts": time.time() - 0.005, "text": "key kha milega"}
        self.assertFalse(bot._watchdog_still_unanswered(item))


# ---------------------------------------------------------------------------
# 10) RADAR
# ---------------------------------------------------------------------------

class RadarTests(IMDBTestCase):
    def test_radar_ingest_dedupe(self):
        # pehle Bot API ne ingest kiya
        knowledge.ingest_message({
            "chat": {"id": CHAT}, "from": {"id": 222, "is_bot": False},
            "message_id": 900, "date": 1700000000,
            "text": "radar test msg", "caption": None,
        })
        # radar ne wahi dekha
        bot._radar_ingest(CHAT, {"id": 900, "text": "radar test msg",
                                 "sender_id": 222, "file_type": None,
                                 "file_name": None, "date": 1700000000})
        docs = list(db.get_db()["messages"].find({"chat_id": CHAT}))
        self.assertEqual(len(docs), 1)  # dedupe — ek hi copy
        # par context me yaad ho gaya
        ctx = list(bot._GROUP_CONTEXT[CHAT])
        self.assertEqual(len(ctx), 1)

    def test_radar_ingest_new_message(self):
        bot._radar_ingest(CHAT, {"id": 950, "text": "privacy-mode msg",
                                 "sender_id": 333, "file_type": None,
                                 "file_name": None, "date": 1700000001})
        docs = list(db.get_db()["messages"].find(
            {"chat_id": CHAT, "message_id": 950}))
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["user_id"], 333)

    def test_media_label_remembered(self):
        bot._radar_ingest(CHAT, {"id": 960, "text": "",
                                 "sender_id": 333, "file_type": "video",
                                 "file_name": "clip.mp4", "date": 1})
        ctx = list(bot._GROUP_CONTEXT[CHAT])
        self.assertTrue(any("clip.mp4" in c["text"] for c in ctx))

    def test_bootstrap_id(self):
        knowledge.ingest_message({
            "chat": {"id": CHAT}, "from": {"id": 222, "is_bot": False},
            "message_id": 777, "date": 1, "text": "hi", "caption": None,
        })
        self.assertEqual(bot._radar_bootstrap_id(CHAT), 777)

    def test_uid_name_cache(self):
        bot._remember(CHAT, "Rocky", "hello", uid=444)
        self.assertEqual(bot._uid_name(444), "Rocky")
        self.assertEqual(bot._uid_name(55555), "member")


# ---------------------------------------------------------------------------
# 11) MTProto latest-id tracker
# ---------------------------------------------------------------------------

class LatestIdTests(IMDBTestCase):
    def test_note_monotonic(self):
        mtproto_layer.note_message_id(CHAT, 100)
        mtproto_layer.note_message_id(CHAT, 50)   # purana — ignore
        mtproto_layer.note_message_id(CHAT, 200)
        self.assertEqual(mtproto_layer.latest_group_message_id(CHAT), 200)

    def test_parse_message_sender_id(self):
        import datetime
        class FakeMsg:
            id = 300
            date = datetime.datetime(2026, 10, 21, 12, 0, 0,
                                     tzinfo=datetime.timezone.utc)
            message = "hi"
            media = None
            grouped_id = None
            post_author = None
            sender_id = 12345
        p = mtproto_layer._parse_message(FakeMsg())
        self.assertEqual(p["sender_id"], 12345)
        self.assertTrue(p["alive"])


# ---------------------------------------------------------------------------
# 12) admin log describe
# ---------------------------------------------------------------------------

class AdminLogDescribeTests(IMDBTestCase):
    def test_known_actions(self):
        # asli Telethon class names se describe
        Del = type("ChannelAdminLogEventActionDeleteMessage", (), {})
        Pin = type("ChannelAdminLogEventActionPinMessage", (), {})
        Slow = type("ChannelAdminLogEventActionToggleSlowMode", (), {"new_value": 10})
        self.assertEqual(mtproto_layer._describe_admin_action(Del()),
                         "deleted a message")
        self.assertEqual(mtproto_layer._describe_admin_action(Pin()),
                         "pinned a message")
        self.assertIn("slow mode", mtproto_layer._describe_admin_action(Slow()))
        self.assertTrue(len(mtproto_layer._describe_admin_action(None)) > 0)


if __name__ == "__main__":
    unittest.main()
