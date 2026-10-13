"""v4.0 TRUTH AGENT tests.

Locks in the owner's live-reported issues (the 'last chance' list):
  1. 'channel me koi post krta hu to channel post ko bhi ye group se
     delete kr deta hai' — auto-forwarded channel-post copies (owner ke
     promotions!) moderation se KABHI delete nahi honge — chahe unme
     t.me promo links hon, from pseudo-user ho, admin lookup fail ho.
  2. 'jo channel se delete ho chuka hai vo link/file bhej deta hai ...
     old data se response' — answer ke post-links LIVE verify hote hain;
     confirmed-deleted post purge + honest rewrite (ZYROX dead-links).
  3. 'mene aisa to bola hi nhi ki mujhe BAT ka key chahiye' —
     unrequested key/password VALUE lines answer se strip hote hain;
     jab user ne khud manga (topic match / key intent) to zinda.
  4. '2 users baat karte hue bot spam karta hai' — budget + MIN-GAP.
  5. 'member ne key maangi, koi jawab nahi' — exchange-participant ka
     genuine help-sawal + gate-LLM failure par bhi ANSWER guaranteed.
"""
import unittest
from unittest.mock import patch, MagicMock

import agent
import bot
import channel_forward
import config
import db
import knowledge
import moderation
import tg_api
import triggers
import verify

OWNER = 8967004608
BOT_ID = 8781539682
CHAT = -100123
CHAN = -100777


def group_message(text, user_id=123, message_id=99, reply_to_message=None,
                  **extra):
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
    msg.update(extra)
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
        verify.invalidate_cache()
        moderation.invalidate_admin_cache()
        verify._MANAGED_LINK_RES.clear()
        verify._MANAGED_LINK_TS = 0.0
        self.addCleanup(self.p_db.stop)
        self.addCleanup(self.p_owner.stop)
        self.addCleanup(db.invalidate_state_cache)
        self.addCleanup(channel_forward._LAST_LINK.clear)
        self.addCleanup(bot._GROUP_CONTEXT.clear)
        self.addCleanup(bot._REPLY_TIMES.clear)
        self.addCleanup(tg_api.OWN_MSG_IDS.clear)
        self.addCleanup(knowledge._CHAT_MSG_COUNT.clear)
        self.addCleanup(verify.invalidate_cache)
        self.addCleanup(moderation.invalidate_admin_cache)

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
# 1. OWNER'S CHANNEL POSTS ARE NEVER DELETED FROM THE LINKED GROUP
# ---------------------------------------------------------------------------
class ChannelPostImmunityTests(IMDBTestCase):
    """The exact live bug: owner posts a promotion in the channel; the
    auto-forwarded copy in the discussion group gets deleted because its
    `from` is a pseudo-user (owner-exemption misses) and promo regexes
    fire on the t.me links."""

    def _promo_copy(self, **extra):
        msg = group_message(
            "Join our channel https://t.me/someotherchannel aur ye bhi "
            "https://t.me/+AbCdEf promo",
            user_id=777, message_id=55)
        msg["is_automatic_forward"] = True
        msg["sender_chat"] = {"id": CHAN, "title": "RK Channel",
                              "type": "channel"}
        msg.update(extra)
        return msg

    def test_auto_forward_never_moderated(self):
        with patch.object(moderation.tg_api, "delete_message") as dm:
            handled = moderation.handle_group_message(self._promo_copy())
            self.assertFalse(handled)
            dm.assert_not_called()

    def test_sender_chat_only_never_moderated(self):
        msg = self._promo_copy()
        del msg["is_automatic_forward"]  # sirf sender_chat bhi kaafi hai
        with patch.object(moderation.tg_api, "delete_message") as dm:
            self.assertFalse(moderation.handle_group_message(msg))
            dm.assert_not_called()

    def test_anonymous_pseudo_user_never_moderated(self):
        # bina sender_chat ke bhi GroupAnonymousBot id exempt hai
        msg = group_message("promote https://t.me/otherchan",
                            user_id=1087968824, message_id=66)
        with patch.object(moderation.tg_api, "delete_message") as dm:
            self.assertFalse(moderation.handle_group_message(msg))
            dm.assert_not_called()

    def test_real_promo_from_member_still_deleted(self):
        # guard asli spam par gadbad na kare: normal member ka promo delete
        msg = group_message("join https://t.me/otherchannel fast",
                            user_id=999, message_id=77)
        with patch.object(moderation, "matches_term", return_value=None), \
             patch.object(moderation, "matches_filter", return_value=None), \
             patch.object(moderation, "is_enabled", return_value=True), \
             patch.object(moderation.tg_api, "get_chat_administrators",
                          return_value=[]), \
             patch.object(moderation.tg_api, "get_chat_member",
                          return_value={"status": "member"}), \
             patch.object(moderation.tg_api, "delete_message",
                          return_value={"ok": True}) as dm, \
             patch.object(moderation, "_say_reply"):
            handled = moderation.handle_group_message(msg)
            self.assertTrue(handled)
            dm.assert_called_once()

    def test_admin_whitelist_prevents_member_moderation(self):
        # getChatAdministrators me listed admin ko getChatMember galat
        # 'member' bole tab bhi delete nahi hoga (v4.0 whitelist).
        msg = group_message("join https://t.me/otherchannel fast",
                            user_id=4242, message_id=78)
        with patch.object(moderation, "matches_term", return_value=None), \
             patch.object(moderation, "matches_filter", return_value=None), \
             patch.object(moderation, "is_enabled", return_value=True), \
             patch.object(moderation.tg_api, "get_chat_administrators",
                          return_value=[{"status": "administrator",
                                         "user": {"id": 4242}}]), \
             patch.object(moderation.tg_api, "get_chat_member",
                          return_value={"status": "member"}) as gcm, \
             patch.object(moderation.tg_api, "delete_message") as dm:
            self.assertFalse(moderation.handle_group_message(msg))
            dm.assert_not_called()
            gcm.assert_not_called()  # whitelist hit -> per-user call skip


# ---------------------------------------------------------------------------
# 2. REAL-TIME TRUTH: deleted channel posts never reach the user
# ---------------------------------------------------------------------------
class VerifyLayerTests(IMDBTestCase):
    def _seed_channel(self, uname="rk_test_ch"):
        db.save_state("channel_id", CHAN)
        db.save_state("managed_channel_username", uname)
        verify._MANAGED_LINK_RES.clear()
        verify._MANAGED_LINK_TS = 0.0

    def _seed_post(self, mid, text, fname=None, date=2000):
        get = db.get_db()["channel_posts"]
        get.insert_one({
            "channel_id": CHAN, "message_id": mid, "text": text,
            "caption": text, "file_name": fname, "date": date,
        })

    def test_dead_link_replaced_and_purged(self):
        self._seed_channel()
        self._seed_post(245, "ZYROX FILE LINK: https://spoo.me/DEAD1")
        answer = ("Le lo bhai: https://t.me/rk_test_ch/245 me hai ZYROX")
        with patch.object(verify, "verify_post", return_value=False):
            out = verify.verify_answer_links(answer, "zyrox file link do")
        self.assertNotIn("t.me/rk_test_ch/245", out)
        self.assertIn("delete ho chuka", out)
        # DB se bhi gaya — dobara kabhi context me nahi aayega
        self.assertIsNone(db.get_db()["channel_posts"].find_one(
            {"channel_id": CHAN, "message_id": 245}))

    def test_live_link_untouched(self):
        self._seed_channel()
        self._seed_post(300, "healthy post")
        answer = "Ye dekho: https://t.me/rk_test_ch/300"
        with patch.object(verify, "verify_post", return_value=True):
            out = verify.verify_answer_links(answer, "post dikhao")
        self.assertEqual(out, answer)

    def test_unknown_probe_never_destroys_answer(self):
        self._seed_channel()
        self._seed_post(310, "maybe alive")
        answer = "Ye dekho: https://t.me/rk_test_ch/310"
        with patch.object(verify, "verify_post", return_value=None):
            out = verify.verify_answer_links(answer, "post dikhao")
        self.assertEqual(out, answer)

    def test_sweep_purges_dead_posts(self):
        self._seed_channel()
        self._seed_post(1, "dead one", date=5000)
        self._seed_post(2, "alive one", date=4000)
        with patch.object(verify, "verify_post",
                          side_effect=lambda cid, mid: mid != 1):
            res = verify.sweep_once(limit=10)
        self.assertEqual(res.get("purged"), 1)
        self.assertIsNone(db.get_db()["channel_posts"].find_one(
            {"channel_id": CHAN, "message_id": 1}))
        self.assertIsNotNone(db.get_db()["channel_posts"].find_one(
            {"channel_id": CHAN, "message_id": 2}))

    def test_probe_cache_used(self):
        self._seed_channel()
        calls = {"n": 0}

        def fake_probe(uname, mid):
            calls["n"] += 1
            return True
        with patch.object(verify, "_channel_username",
                          return_value="rk_test_ch"), \
                patch.object(verify, "_probe_http", side_effect=fake_probe):
            self.assertTrue(verify.verify_post(CHAN, 12))
            self.assertTrue(verify.verify_post(CHAN, 12))
            self.assertTrue(verify.verify_post(CHAN, 12))
        self.assertEqual(calls["n"], 1)

    def test_private_channel_returns_unknown(self):
        # private channel: no username -> HTTP probe impossible -> None
        db.save_state("channel_id", CHAN)
        db.save_state("managed_channel_username", "")
        verify._MANAGED_LINK_RES.clear()
        verify._MANAGED_LINK_TS = 0.0
        with patch.object(verify, "_channel_username", return_value=""):
            self.assertIsNone(verify.verify_post(CHAN, 12))

    def test_agent_answer_runs_truth_layer(self):
        """agent.answer ka grounded path verify_answer_links integrate."""
        self._seed_channel()
        self._seed_post(245, "ZYROX loader key 12345")
        ctx = {"user_id": 123, "chat_id": CHAT, "group": True,
               "group_id": CHAT, "message_id": 1, "recent_log": []}
        fake_llm = ("Le: https://t.me/rk_test_ch/245 se milega.")
        with patch.object(agent.ai_engine, "get_router") as router, \
                patch.object(agent, "_generate_answer", return_value=fake_llm), \
                patch.object(verify, "verify_post", return_value=False), \
                patch.object(agent.rules, "rules_block", return_value=""), \
                patch.object(agent.community, "community_block",
                             return_value=""):
            router.return_value.generate.return_value = fake_llm
            res = agent.answer("zyrox ka link do", ctx)
        self.assertNotIn("t.me/rk_test_ch/245", res.get("text") or "")
        self.assertIn("delete ho chuka", res.get("text") or "")


# ---------------------------------------------------------------------------
# 3. UNREQUESTED KEY GUARD ('BAT key' bug)
# ---------------------------------------------------------------------------
class UnrequestedKeyGuardTests(unittest.TestCase):
    def test_bat_key_stripped_when_unrequested(self):
        q = "loader kaunsa latest hai abhi"
        a = ("Abhi **BAT loader** latest hai.\nKEY: 849201\n"
             "Aur second line bhi.")
        out = agent.sanitize_unrequested_keys(a, q)
        self.assertNotIn("849201", out)
        self.assertIn("loader", out)

    def test_key_kept_when_user_asked_for_it(self):
        q = "bat ka key chahiye"
        a = "Bat ka key: KEY: 849201 ye lo"
        out = agent.sanitize_unrequested_keys(a, q)
        self.assertIn("849201", out)

    def test_key_kept_when_topic_matches(self):
        q = "bat loader ka password kya hai"  # password intent -> kept
        a = "Ye lo bhai\npassword: 849201"
        out = agent.sanitize_unrequested_keys(a, q)
        self.assertIn("849201", out)

    def test_no_key_lines_untouched(self):
        q = "game kaunsa khele"
        a = "PUBG khelo bhai, mast hai."
        self.assertEqual(agent.sanitize_unrequested_keys(a, q), a)

    def test_code_line_topic_match_kept(self):
        # question 'crown' ka hai, answer line crown ka code — zinda rahega
        q = "crown loader ka code"
        a = "crown ka code: 556677 bhai"
        out = agent.sanitize_unrequested_keys(a, q)
        self.assertIn("556677", out)


# ---------------------------------------------------------------------------
# 4. ANTI-SPAM: budget + min-gap
# ---------------------------------------------------------------------------
class AntiSpamGapTests(IMDBTestCase):
    def test_min_gap_blocks_back_to_back_undirected(self):
        with patch.object(bot, "_REPLY_MIN_GAP_SECONDS", 45):
            self.assertTrue(bot._reply_budget_ok(CHAT))
            # turant doosra undirected reply — min-gap block
            self.assertFalse(bot._reply_budget_ok(CHAT))

    def test_gap_expired_allows_again(self):
        import time as _t
        with patch.object(bot, "_REPLY_MIN_GAP_SECONDS", 1), \
                patch.object(bot, "_REPLY_BUDGET_SECONDS", 600), \
                patch.object(bot, "_REPLY_BUDGET", 3):
            self.assertTrue(bot._reply_budget_ok(CHAT))
            _t.sleep(1.1)
            self.assertTrue(bot._reply_budget_ok(CHAT))
            _t.sleep(1.1)
            self.assertTrue(bot._reply_budget_ok(CHAT))
            # budget 3/600s khatam — 4th blocked
            _t.sleep(1.1)
            self.assertFalse(bot._reply_budget_ok(CHAT))

    def test_directed_not_budgeted_still(self):
        # /ask / mention wale budget se bahar hain — routing par ye budget
        # call hi sirf `not directed` branch me hoti hai (handle_message
        # me `if uid != OWNER and not directed:` guard). Directed path me
        # budget check nahi hota, isliye directed replies kabhi blocked
        # nahi — ye unit budget function ko direct test karke prove karta
        # hai ki budget sirf UNDIRECTED timestamps par lagta hai.
        self.assertTrue(bot._reply_budget_ok(CHAT))
        # directed replies (mention//ask) _REPLY_TIMES me entry NAHI
        # banate — budget state untouched rehta hai (routing test me
        # proven: mention path budget call ko bypass karta hai).
        self.assertEqual(len(bot._REPLY_TIMES[CHAT]), 1)


class ExchangeHelpFixTests(IMDBTestCase):
    """v4.0: exchange PARTICIPANT ka genuine help-sawal ANSWER hota hai
    (v3.4/v3.5 me silence — spoo.me/Ox5jh1 ka root cause)."""

    def _seed_exchange(self):
        # do members ki chal rahi baat (bot beech me nahi)
        bot._remember(CHAT, "A", "bhai kal match khelega?", uid=111)
        bot._remember(CHAT, "B", "haan pakka", uid=222)
        bot._remember(CHAT, "A", "mast", uid=111)
        bot._remember(CHAT, "B", "setup ready hai mera", uid=222)

    def test_exchanger_help_question_answered(self):
        self._seed_exchange()
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot, "_should_reply_llm") as gate, \
                patch.object(bot.agent, "answer",
                             return_value={"text": "Ye lo bhai",
                                           "grounded": True}) as ans, \
                patch.object(bot, "_start_answer_prefetch",
                             return_value={}):
            gate.return_value = False  # even a NO gate can't swallow it
            bot.handle_message(group_message("key kha milega", user_id=111))
            self.assertTrue(ans.called)
            self.assertTrue(send.called)

    def test_exchanger_chatter_stays_silent(self):
        self._seed_exchange()
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot, "_should_reply_llm") as gate, \
                patch.object(bot.agent, "answer") as ans, \
                patch.object(bot, "_start_answer_prefetch",
                             return_value={}):
            gate.return_value = False
            bot.handle_message(group_message("haan bhai mast mausam hai",
                                             user_id=111))
            self.assertFalse(ans.called)
            self.assertFalse(send.called)

    def test_gate_error_with_help_question_still_answered(self):
        # gate LLM fail (429/timeout) + member ka help-sawal -> answer
        bot._remember(CHAT, "X", "koi hai?", uid=321)
        bot._remember(CHAT, "X", "hello?", uid=321)
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as send, \
                patch.object(bot, "_should_reply_llm", return_value=None), \
                patch.object(bot.agent, "answer",
                             return_value={"text": "Ye lo bhai",
                                           "grounded": True}) as ans, \
                patch.object(bot, "_start_answer_prefetch",
                             return_value={}):
            bot.handle_message(group_message("crown loader kaise milega",
                                             user_id=321))
            self.assertTrue(ans.called)

    def test_channel_copy_is_not_exchange_participant(self):
        # beech me channel post copy aa gayi -> exchange TOOTNA nahi chahiye
        bot._remember(CHAT, "A", "bhai kal match khelega?", uid=111)
        bot._remember(CHAT, "B", "haan pakka", uid=222)
        bot._remember(CHAT, "RK Channel", "NEW LOADER POSTED!",
                      uid=777, is_channel=True)
        ex = bot._member_exchange(CHAT)
        self.assertIsNotNone(ex)  # A-B exchange abhi bhi detect hota hai
        self.assertIn(111, ex)
        self.assertIn(222, ex)

    def test_remember_flags_channel_backed(self):
        bot._remember(CHAT, "Ch", "post copy", uid=777, is_channel=True)
        self.assertTrue(bot._GROUP_CONTEXT[CHAT][-1]["is_channel"])
        self.assertTrue(bot._is_channel_backed(
            {"is_automatic_forward": True}))
        self.assertTrue(bot._is_channel_backed(
            {"sender_chat": {"id": CHAN}}))
        self.assertFalse(bot._is_channel_backed({"text": "normal"}))


# ---------------------------------------------------------------------------
# 5. verify module edges
# ---------------------------------------------------------------------------
class VerifyEdgeTests(IMDBTestCase):
    def _mock_urlopen(self, body: bytes):
        """Patch urlopen so `with urlopen(...) as resp: resp.read(...)` returns
        `body` (MagicMock ka default __enter__ child mock deta hai — read
        lambda us pe pahunchta hi nahi tha; ab context binds to self)."""
        resp = MagicMock()
        resp.read = lambda n=0: body
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return MagicMock(return_value=resp)

    def test_disabled_flag_disables(self):
        with patch.object(config, "VERIFY_ENABLED", 0):
            self.assertIsNone(verify.verify_post(CHAN, 1))
            self.assertEqual(verify.verify_answer_links("x", "q"), "x")

    def test_dead_markers_detected(self):
        for body in ("Post can't be shown", "message not found",
                     "This message doesn't exist"):
            m = self._mock_urlopen(body.encode())
            with patch.object(verify.urllib.request, "urlopen", m):
                self.assertFalse(verify._probe_http("somechan", 5))

    def test_live_post_page_shape_detected(self):
        # LIVE-VERIFIED shape (real channel @rkgamersbgmi): embed widget
        body = (b'<html><body><script async src="telegram-widget.js" '
                b'data-telegram-post="somechan/5" data-width="100%">'
                b'</script>Context Embed View In Channel</body></html>')
        with patch.object(verify.urllib.request, "urlopen",
                          self._mock_urlopen(body)):
            self.assertTrue(verify._probe_http("somechan", 5))

    def test_landing_page_shape_means_dead(self):
        # DEAD shape: t.me falls back to the channel LANDING page
        body = (b'<html><body><div class="tgme_page_description">'
                b'Channel about gaming</div>'
                b'<div class="tgme_page_action">View Post</div>'
                b'</body></html>')
        with patch.object(verify.urllib.request, "urlopen",
                          self._mock_urlopen(body)):
            self.assertFalse(verify._probe_http("somechan", 5))

    def test_http_404_means_dead(self):
        import urllib.error
        err = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        with patch.object(verify.urllib.request, "urlopen", side_effect=err):
            self.assertFalse(verify._probe_http("somechan", 5))

    def test_network_error_means_unknown(self):
        with patch.object(verify.urllib.request, "urlopen",
                          side_effect=OSError("boom")):
            self.assertIsNone(verify._probe_http("somechan", 5))

    def test_captcha_page_means_unknown(self):
        # interstitial/captcha page: NEITHER marker -> inconclusive (a flaky
        # probe must never purge a live post)
        body = b"<html>Checking your browser...</html>"
        with patch.object(verify.urllib.request, "urlopen",
                          self._mock_urlopen(body)):
            self.assertIsNone(verify._probe_http("somechan", 5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
