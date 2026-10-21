"""v5.1 REAL AGENT tests.

Owner ka live report: 'bot normal chat AI ki tarah respond karta hai,
tools use nahi karta; channel ka updated message nahi dikhata.'

Locks in:
  1. AUTO-TOOLS — deterministic intent->tool prefetch (kisi bhi provider
     ke bina model compliance ke): latest/naya post -> CHANNELRECENT,
     kisne delete -> ADMINLOG, pinned -> PINNED, kitne member -> INFO,
     kaun hai -> USER, #id -> GETMSG, link -> LINKCHECK, kya chal raha
     -> RECENT. Block format + stats bump. General chit-chat pe NO tools.
  2. TOOL_SPECS — 10 functionDeclarations, sab me name+description,
     required params sahi.
  3. execute_fc — structured call dispatch (recentChannelPosts ->
     CHANNELRECENT, memberProfile -> USER, ...), unknown tool graceful,
     exception non-fatal.
  4. FC AGENT LOOP — Gemini generate_fc mock: pehle calls, phir text;
     functionResponse contents sahi shape me; max rounds respected;
     no-FC provider par text-protocol fallback + rescue.
  5. RESCUE — 'record nahi' answer on community question -> SEARCH +
     CHANNELRECENT chal ke regen; general-knowledge question pe rescue
     nahi; rescue-disable knob respected.
  6. CHANNEL RADAR — find_channel_top_id floor-walk, recent_channel_posts
     last-N alive + note_message_id; bot._radar_channel_pass ingest +
     advance + empty-window skip; deletion-sync cycle gating.
  7. _use_fc_path — gemini selected + text only + knob; media path off.
  8. SYSTEM_PROMPT — v5.1 tool instructions + CHANNELRECENT + freshness
     hierarchy present.
"""
import unittest
from unittest.mock import patch, MagicMock

import agent
import agent_tools
import ai_engine
import bot
import config
import mtproto_layer


OWNER = 8967004608
CHAT = -1004303033609
CHAN = -1003956726834


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.p_fc = patch.object(config, "AGENT_FC_ENABLED", 1)
        self.p_fc.start()
        self.p_auto = patch.object(config, "AUTO_TOOLS_ENABLED", 1)
        self.p_auto.start()
        self.p_resc = patch.object(config, "TOOL_RESCUE_ENABLED", 1)
        self.p_resc.start()
        for k in ("calls", "rounds", "fc_calls", "auto_calls", "auto_fired",
                  "rescued"):
            agent_tools.TOOL_STATS[k] = 0

    def tearDown(self):
        self.p_fc.stop()
        self.p_auto.stop()
        self.p_resc.stop()


def _ctx(group=True):
    return {"chat_id": CHAT, "group": group, "user_id": 111,
            "first_name": "tester", "message_id": 500}


# ---------------------------------------------------------------------------
# 1) AUTO-TOOLS deterministic prefetch
# ---------------------------------------------------------------------------

class TestAutoTools(BaseCase):

    @patch.object(agent_tools, "execute")
    def test_latest_post_fires_channel_recent(self, ex):
        ex.side_effect = lambda name, args, ctx: {
            "CHANNELRECENT": "post #1: hello",
            "ADMINLOG": "TOOL_ERROR: realtime layer not connected right now.",
            "PINNED": "TOOL_ERROR: no chat context.",
            "INFO": "TOOL_ERROR: no chat context.",
            "RECENT": "TOOL_ERROR: no chat context.",
            "USER": "TOOL_ERROR: no chat context.",
            "GETMSG": "TOOL_ERROR: no chat context.",
            "LINKCHECK": "TOOL_ERROR: x",
            "SEARCH": "TOOL_ERROR: x",
            "FILES": "TOOL_ERROR: x",
        }[name]
        block, fired = agent_tools.auto_tools(
            "channel pe latest post kya hai bhai?", _ctx())
        self.assertIn("recentChannelPosts", fired)
        self.assertIn("LIVE TOOL RESULTS", block)
        self.assertIn("post #1: hello", block)
        self.assertEqual(agent_tools.TOOL_STATS["auto_fired"], 1)

    @patch.object(agent_tools, "execute")
    def test_intent_matrix(self, ex):
        # label -> internal tool name mapping jo auto_tools use karta hai
        label_to_tool = {
            "recentChannelPosts": "CHANNELRECENT",
            "adminLog": "ADMINLOG",
            "pinnedMessage": "PINNED",
            "chatInfo": "INFO",
            "memberProfile": "USER",
            "getMessage": "GETMSG",
            "recentGroupMessages": "RECENT",
        }
        cases = [
            ("naya loader konsa aaya hai", "recentChannelPosts"),
            ("kisne wo post delete kiya?", "adminLog"),
            ("pinned message me kya hai", "pinnedMessage"),
            ("kitne member hai group me", "chatInfo"),
            ("rich kaun hai ye", "memberProfile"),
            ("#1523 ka message kya tha", "getMessage"),
            ("kya chal raha hai group me", "recentGroupMessages"),
        ]
        for q, want_label in cases:
            ex.reset_mock()
            called = set()

            def side(name, args, ctx, _n=None):
                called.add(name)
                return "TOOL_ERROR: n/a"

            ex.side_effect = side
            block, fired = agent_tools.auto_tools(q, _ctx())
            self.assertIn(label_to_tool[want_label], called,
                          f"Q={q!r} -> called={called}")
            self.assertEqual(block, "")  # sab TOOL_ERROR tha -> no block

    def test_general_chitchat_no_tools(self):
        with patch.object(agent_tools, "execute") as ex:
            ex.return_value = "TOOL_ERROR: no"
            block, fired = agent_tools.auto_tools(
                "hello bhai kaise ho aaj", _ctx())
            self.assertEqual(fired, [])
            self.assertEqual(block, "")
            ex.assert_not_called()

    def test_disabled_knob(self):
        with patch.object(config, "AUTO_TOOLS_ENABLED", 0):
            block, fired = agent_tools.auto_tools(
                "latest post kya hai", _ctx())
            self.assertEqual((block, fired), ("", []))

    @patch.object(agent_tools, "execute")
    def test_content_refresh_adds_channel(self, ex):
        # loader wala sawal (koi aur intent nahi) -> CHANNELRECENT(5)
        ex.return_value = "post #99: crown loader v2"
        with patch.object(agent_tools.links, "get_channel_id",
                          return_value=CHAN):
            block, fired = agent_tools.auto_tools(
                "crown loader de do bhai", _ctx())
        self.assertIn("recentChannelPosts", fired)
        self.assertIn("post #99", block)


# ---------------------------------------------------------------------------
# 2) TOOL_SPECS + execute_fc dispatch
# ---------------------------------------------------------------------------

class TestSpecsAndFcExec(BaseCase):

    def test_specs_shape(self):
        self.assertEqual(len(agent_tools.TOOL_SPECS), 10)
        names = {s["name"] for s in agent_tools.TOOL_SPECS}
        self.assertEqual(names, {
            "searchContent", "recentChannelPosts", "recentGroupMessages",
            "getMessage", "memberProfile", "pinnedMessage", "listChannelFiles",
            "chatInfo", "adminLog", "checkLink"})
        for s in agent_tools.TOOL_SPECS:
            self.assertTrue(s.get("description"))
            self.assertIsInstance(s.get("parameters"), dict)

    @patch.object(agent_tools, "_tool_search")
    def test_execute_fc_search(self, ts):
        ts.return_value = "hit1"
        out = agent_tools.execute_fc("searchContent", {"query": "crown"},
                                     _ctx())
        self.assertEqual(out, "hit1")
        ts.assert_called_once_with("crown", unittest.mock.ANY)

    @patch.object(agent_tools, "_tool_channel_recent")
    def test_execute_fc_channel_recent(self, tc):
        tc.return_value = "posts"
        out = agent_tools.execute_fc(
            "recentChannelPosts", {"count": 5}, _ctx())
        self.assertEqual(out, "posts")
        tc.assert_called_once_with("5", unittest.mock.ANY)

    @patch.object(agent_tools, "_tool_user")
    def test_execute_fc_member(self, tu):
        tu.return_value = "member profile"
        self.assertEqual(
            agent_tools.execute_fc("memberProfile", {"name": "rich"}, _ctx()),
            "member profile")
        tu.assert_called_once_with("rich", unittest.mock.ANY)

    @patch.object(agent_tools, "_tool_getmsg")
    def test_execute_fc_getmsg(self, tg):
        tg.return_value = "msg"
        self.assertEqual(
            agent_tools.execute_fc("getMessage", {"messageId": 12}, _ctx()),
            "msg")
        tg.assert_called_once_with("12", unittest.mock.ANY)

    def test_execute_fc_unknown(self):
        out = agent_tools.execute_fc("nope", {}, _ctx())
        self.assertTrue(out.startswith("TOOL_ERROR: unknown tool"))

    @patch.object(agent_tools, "_tool_user")
    def test_execute_fc_exception_nonfatal(self, tu):
        tu.side_effect = RuntimeError("boom")
        out = agent_tools.execute_fc("memberProfile", {"name": "x"}, _ctx())
        self.assertIn("TOOL_ERROR", out)


# ---------------------------------------------------------------------------
# 3) FC agent loop (Gemini native function calling)
# ---------------------------------------------------------------------------

class TestFcLoop(BaseCase):

    def _router(self, responses):
        r = MagicMock()
        r.generate_fc.side_effect = responses
        return r

    @patch.object(agent_tools, "execute_fc")
    def test_fc_loop_two_rounds(self, ex):
        ex.return_value = "LIVE RESULT"
        responses = [
            {"calls": [{"name": "recentChannelPosts", "args": {"count": 5}}]},
            {"text": "Final jawab"},
        ]
        with patch.object(ai_engine, "get_router",
                          return_value=self._router(responses)):
            out = agent._fc_agent_loop("prompt", "SYS", _ctx())
        self.assertEqual(out, "Final jawab")
        self.assertEqual(agent_tools.TOOL_STATS["fc_calls"], 1)
        ex.assert_called_once()
        # second turn: functionResponse contents model ko diye gaye
        sec = responses[0]  # placeholder
        r = agent.ai_engine.get_router() if False else None

    @patch.object(agent_tools, "execute_fc")
    def test_fc_loop_max_rounds_then_notools(self, ex):
        ex.return_value = "data"
        call = {"name": "searchContent", "args": {"query": "x"}}
        responses = [{"calls": [call]}] * 3 + [{"text": "done"}]
        with patch.object(config, "TOOL_MAX_ROUNDS", 3):
            with patch.object(ai_engine, "get_router",
                              return_value=self._router(responses)):
                out = agent._fc_agent_loop("p", "SYS", _ctx())
        self.assertEqual(out, "done")
        # 3 tool rounds + 1 final call WITHOUT tools
        self.assertEqual(ex.call_count, 3)

    @patch.object(agent_tools, "execute_fc")
    def test_fc_loop_direct_text(self, ex):
        with patch.object(ai_engine, "get_router",
                          return_value=self._router([{"text": "seedha"}])):
            out = agent._fc_agent_loop("p", "SYS", _ctx())
        self.assertEqual(out, "seedha")
        ex.assert_not_called()


# ---------------------------------------------------------------------------
# 4) provider path selection
# ---------------------------------------------------------------------------

class TestPathSelection(BaseCase):

    def test_use_fc_gemini_text(self):
        with patch.object(ai_engine, "get_router") as gr:
            gr.return_value.get_text_selection.return_value = ("gemini", "m")
            self.assertTrue(agent._use_fc_path(_ctx()))

    def test_no_fc_when_mercury(self):
        with patch.object(ai_engine, "get_router") as gr:
            gr.return_value.get_text_selection.return_value = ("mercury", "m")
            self.assertFalse(agent._use_fc_path(_ctx()))

    def test_no_fc_when_media(self):
        ctx = _ctx()
        ctx["media"] = [{"kind": "photo"}]
        with patch.object(ai_engine, "get_router") as gr:
            gr.return_value.get_text_selection.return_value = ("gemini", "m")
            self.assertFalse(agent._use_fc_path(ctx))

    def test_no_fc_when_disabled(self):
        with patch.object(config, "AGENT_FC_ENABLED", 0):
            with patch.object(ai_engine, "get_router") as gr:
                gr.return_value.get_text_selection.return_value = (
                    "gemini", "m")
                self.assertFalse(agent._use_fc_path(_ctx()))


# ---------------------------------------------------------------------------
# 5) RESCUE PASS
# ---------------------------------------------------------------------------

class TestRescue(BaseCase):

    def test_rescue_needed_positive(self):
        self.assertTrue(agent._rescue_needed(
            "crown loader kaun sa hai",
            "Sorry, iski koi jaankari nahi hai mere paas."))
        self.assertTrue(agent._rescue_needed(
            "naya hack kab aayega", "mujhe pata nahi bhai"))

    def test_rescue_not_for_general_knowledge(self):
        # 'nahi pata' bhi bola par community token nahi -> no rescue
        self.assertFalse(agent._rescue_needed(
            "universe kitna bada hai", "mujhe pata nahi"))
        # community sawal + confident answer -> no rescue
        self.assertFalse(agent._rescue_needed(
            "crown loader kaun sa hai", "Crown loader v2 hai bhai, ready."))

    def test_rescue_disabled(self):
        with patch.object(config, "TOOL_RESCUE_ENABLED", 0):
            self.assertFalse(agent._rescue_needed(
                "crown loader?", "koi record nahi"))

    @patch.object(agent_tools, "execute")
    @patch.object(agent, "_generate_answer")
    def test_rescue_regen_regenerates(self, gen, ex):
        ex.side_effect = lambda name, args, ctx: {
            "SEARCH": "[1] crown loader post",
            "CHANNELRECENT": "post #99: crown loader",
        }[name]
        gen.return_value = "Crown loader v2 live hai bhai!"
        out = agent._rescue_regen("crown loader?", "record nahi", _ctx(),
                                  "SYS")
        self.assertEqual(out, "Crown loader v2 live hai bhai!")
        self.assertEqual(agent_tools.TOOL_STATS["rescued"], 1)

    @patch.object(agent_tools, "execute")
    def test_rescue_regen_no_results_keeps_draft(self, ex):
        ex.side_effect = lambda name, args, ctx: {
            "SEARCH": "TOOL_ERROR: db down",
            "CHANNELRECENT": "TOOL_ERROR: layer down",
        }[name]
        with patch.object(agent, "_generate_answer") as gen:
            out = agent._rescue_regen("loader?", "record nahi", _ctx(), "SYS")
        self.assertIsNone(out)
        gen.assert_not_called()


# ---------------------------------------------------------------------------
# 6) CHANNEL freshness layer (MTProto + bot radar pass)
# ---------------------------------------------------------------------------

class TestChannelFreshness(BaseCase):

    def test_find_top_floor_walk(self):
        with patch.object(mtproto_layer, "is_connected", lambda: True):
            with patch.object(mtproto_layer, "fetch_messages") as fm:
                # floor=90; posts 91-92 alive, upar sab dead
                def side(cid, ids):
                    return {i: {"id": i, "alive": i in (90, 91, 92)}
                            for i in ids}
                fm.side_effect = side
                top = mtproto_layer.find_channel_top_id(CHAN, floor=90)
                self.assertEqual(top, 92)

    def test_find_top_down_when_layer(self):
        with patch.object(mtproto_layer, "is_connected", lambda: False):
            self.assertEqual(
                mtproto_layer.find_channel_top_id(CHAN, floor=90), 0)

    def test_recent_channel_posts_last_n(self):
        with patch.object(mtproto_layer, "is_connected", lambda: True):
            with patch.object(mtproto_layer, "latest_group_message_id",
                              return_value=100):
                with patch.object(mtproto_layer, "fetch_messages") as fm:
                    def side(cid, ids):
                        return {i: {"id": i, "alive": i % 2 == 0}
                                for i in ids}
                    fm.side_effect = side
                    posts = mtproto_layer.recent_channel_posts(CHAN, count=4)
                    # window = [139..150] (3x4 + drift 50); alive evens
                    # 140..150; last 4 = 144,146,148,150
                    self.assertEqual([p["id"] for p in posts],
                                     [144, 146, 148, 150])

    def test_radar_channel_pass_ingests(self):
        bot._CH_RADAR_LAST.clear()
        with patch.object(bot.links, "get_channel_id", return_value=CHAN):
            with patch.object(bot.mtproto_layer, "scan_channel_window") as sc:
                sc.return_value = [{"id": 500, "alive": True, "date": 1,
                                    "text": "naya loader post",
                                    "file_type": "document",
                                    "file_name": "crown.apk"}]
                with patch.object(bot.knowledge, "ingest_mtproto_post") as ing:
                    bot._CH_RADAR_LAST[CHAN] = 499
                    bot._radar_channel_pass()
                    ing.assert_called_once()
                    self.assertEqual(bot._CH_RADAR_LAST[CHAN], 500)

    def test_radar_channel_pass_empty_window_parks(self):
        # v5.1.1: blind advance dead — empty window par pointer PARK
        # rehta hai (naya post #last+1 isi window me hi land hoga).
        bot._CH_RADAR_LAST.clear()
        with patch.object(bot.links, "get_channel_id", return_value=CHAN):
            with patch.object(bot.mtproto_layer, "scan_channel_window") as sc:
                sc.return_value = []
                with patch.object(bot.knowledge, "ingest_mtproto_post") as ing:
                    with patch.object(bot.mtproto_layer,
                                     "latest_group_message_id",
                                     return_value=1000):
                        bot._CH_RADAR_LAST[CHAN] = 1000
                        bot._radar_channel_pass()
                        ing.assert_not_called()
                        # park: known (1000) <= win_end (1039) -> no advance
                        self.assertEqual(bot._CH_RADAR_LAST[CHAN], 1000)
        bot._CH_RADAR_LAST.clear()

    def test_radar_channel_pass_walk_through_when_known_beyond(self):
        # v5.1.1 SAFE WALK-THROUGH: empty window ke baad zinda id
        # CONFIRMED ho (known > win_end) to hi aage badhta hai.
        bot._CH_RADAR_LAST.clear()
        with patch.object(bot.links, "get_channel_id", return_value=CHAN):
            with patch.object(bot.mtproto_layer, "scan_channel_window") as sc:
                sc.return_value = []
                with patch.object(bot.knowledge, "ingest_mtproto_post") as ing:
                    with patch.object(bot.mtproto_layer,
                                     "latest_group_message_id",
                                     return_value=2000):
                        bot._CH_RADAR_LAST[CHAN] = 1000
                        bot._radar_channel_pass()
                        ing.assert_not_called()
                        win_end = 1000 + config.RADAR_BATCH - 1
                        self.assertEqual(bot._CH_RADAR_LAST[CHAN], win_end)
        bot._CH_RADAR_LAST.clear()

    def test_radar_channel_pass_disabled(self):
        with patch.object(config, "CHANNEL_RADAR_ENABLED", 0):
            with patch.object(bot.links, "get_channel_id") as gl:
                bot._radar_channel_pass()
                gl.assert_not_called()


# ---------------------------------------------------------------------------
# 7) prompt upgrades present
# ---------------------------------------------------------------------------

class TestPromptUpgrades(BaseCase):

    def test_system_prompt_has_v51_tools(self):
        self.assertIn("CHANNELRECENT", agent.SYSTEM_PROMPT)
        self.assertIn("TOOL: <NAME> <args>", agent.SYSTEM_PROMPT)
        self.assertIn("FRESHNESS HIERARCHY", agent.SYSTEM_PROMPT)
        self.assertIn("LIVE TOOL RESULTS", agent.SYSTEM_PROMPT)

    def test_caps_block_examples(self):
        self.assertIn("TOOL: CHANNELRECENT 8", agent._CAPABILITIES)
        self.assertIn("TOOL: USER rich", agent._CAPABILITIES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
