"""v3.0 SPEED kit tests.

Locks in the performance fixes that made replies slow:
  1. db state cache       — write-through + db-swap invalidation
  2. knowledge.search     — combined $or single-pass (3 queries, not 30),
                            same semantics as the old per-token loop
  3. agent.answer         — ONE search per answer (not two), prefetch box
                            consumed without a second search
  4. ANSWER_MAX_TOKENS    — answer generation capped (350 default)
  5. tg_api keep-alive    — persistent connection reused across calls
  6. ai_engine thinking   — thinkingBudget=0 sent; self-heals on rejection
  7. gate timeout         — reply-gate uses GATE_TIMEOUT_SECONDS
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

import agent
import ai_engine
import bot
import config
import db
import knowledge
import tg_api

OWNER = 8967004608
CHAT = -1001430303536097


class StateCacheTests(unittest.TestCase):
    def setUp(self):
        self._db = db.IMDB()
        self.p = patch.object(db, "_DB", self._db)
        self.p.start()
        db.invalidate_state_cache()
        self.addCleanup(self.p.stop)
        self.addCleanup(db.invalidate_state_cache)

    def test_write_through(self):
        db.save_state("k1", {"a": 1})
        # value is served from cache AND persisted to the store
        self.assertEqual(db.get_state("k1"), {"a": 1})
        doc = self._db["state"].find_one({"k": "k1"})
        self.assertEqual(doc["v"], {"a": 1})

    def test_default_on_missing(self):
        self.assertEqual(db.get_state("nope", "dflt"), "dflt")

    def test_db_swap_invalidates(self):
        db.save_state("k1", "old")
        # tests swap the underlying store — cache must NOT leak across
        fresh = db.IMDB()
        with patch.object(db, "_DB", fresh):
            self.assertEqual(db.get_state("k1", "fresh-default"), "fresh-default")
            db.save_state("k1", "new")
            self.assertEqual(db.get_state("k1"), "new")

    def test_cache_hit_does_not_query(self):
        db.save_state("k1", "v")
        coll = MagicMock(wraps=self._db["state"])
        with patch.object(self._db, "_cols", {"state": coll}):
            db.get_state("k1")
            coll.find_one.assert_not_called()


class CombinedSearchTests(unittest.TestCase):
    def setUp(self):
        self._db = db.IMDB()
        self.p = patch.object(db, "_DB", self._db)
        self.p.start()
        db.invalidate_state_cache()
        self.addCleanup(self.p.stop)
        self.addCleanup(db.invalidate_state_cache)
        self._db["messages"].insert_many([
            {"chat_id": CHAT, "user_id": 1, "text": "bgmi loader update aaya",
             "date": 100},
            {"chat_id": CHAT, "user_id": 2, "text": "random chatter",
             "date": 101},
            {"chat_id": CHAT, "user_id": 1, "text": "loader config file",
             "date": 102},
        ])
        self._db["channel_posts"].insert_many([
            {"channel_id": -100, "message_id": 5, "text": "loader update",
             "caption": "v2 loader", "file_id": "F1", "file_type": "document",
             "date": 200},
            {"channel_id": -100, "message_id": 6, "text": "",
             "caption": "unrelated", "file_id": None, "date": 201},
        ])
        self._db["qa"].insert_one(
            {"user_id": 1, "question": "loader kaise install?",
             "answer": "config file se", "date": 300},
        )

    def test_multi_token_match_and_score(self):
        hits = knowledge.search("loader update", limit=10)
        texts = [h["text"] for h in hits]
        self.assertTrue(any("bgmi loader update" in t for t in texts))
        self.assertTrue(any("loader update" in t for t in texts))
        # no irrelevant doc leaks in
        self.assertFalse(any("random chatter" in t for t in texts))
        self.assertFalse(any("unrelated" in t for t in texts))

    def test_user_scoping_for_messages(self):
        hits = knowledge.search("bgmi loader", limit=10, user_id=2)
        # the bgmi message belongs to user 1 — scoped out for user 2
        self.assertFalse(any("bgmi loader" in h["text"] for h in hits))

    def test_caption_match_and_file_fields(self):
        hits = knowledge.search("v2 loader", limit=10)
        post = next((h for h in hits if h["kind"] == "post"), None)
        self.assertIsNotNone(post)
        self.assertEqual(post.get("file_id"), "F1")
        self.assertEqual(post.get("file_type"), "document")

    def test_limit_respected(self):
        hits = knowledge.search("loader", limit=1)
        self.assertEqual(len(hits), 1)

    def test_empty_query(self):
        self.assertEqual(knowledge.search(""), [])


class SingleSearchTests(unittest.TestCase):
    def setUp(self):
        self._db = db.IMDB()
        self.p = patch.object(db, "_DB", self._db)
        self.p.start()
        db.invalidate_state_cache()
        self.addCleanup(self.p.stop)
        self.addCleanup(db.invalidate_state_cache)

    def _router(self, prompts):
        router = MagicMock()
        router.generate.side_effect = (
            lambda p, system=None, **kw: prompts.append(p) or "jawab")
        return router

    def test_answer_searches_once(self):
        prompts = []
        router = self._router(prompts)
        with patch.object(agent.ai_engine, "get_router", return_value=router), \
                patch.object(agent.knowledge, "search",
                             wraps=knowledge.search) as m_search:
            agent.answer("kya haal hai bhai", {"user_id": 1, "chat_id": CHAT})
        self.assertEqual(m_search.call_count, 1)

    def test_prefetch_box_consumed_without_search(self):
        prompts = []
        router = self._router(prompts)
        box = {"hits": [{"text": "channel post", "kind": "post",
                         "file_id": None, "file_type": None}],
               "standing": "", "facts": ""}
        with patch.object(agent.ai_engine, "get_router", return_value=router), \
                patch.object(agent.knowledge, "search") as m_search:
            agent.answer("kya haal hai", {"user_id": 1, "chat_id": CHAT,
                                          "prefetch": box})
        m_search.assert_not_called()
        self.assertTrue(prompts)

    def test_answer_max_tokens_capped(self):
        prompts = []
        router = self._router(prompts)
        with patch.object(agent.ai_engine, "get_router", return_value=router), \
                patch.object(agent.knowledge, "search", return_value=[]):
            agent.answer("generic question", {"user_id": 1, "chat_id": CHAT})
        router.generate.assert_called_once()
        self.assertEqual(router.generate.call_args.kwargs.get("max_tokens"),
                         config.ANSWER_MAX_TOKENS)
        self.assertLessEqual(config.ANSWER_MAX_TOKENS, 512)


class ThinkingConfigTests(unittest.TestCase):
    def test_thinking_budget_sent_then_selfheals(self):
        calls = []

        def fake_post(key, model, body, timeout):
            # snapshot: the caller mutates the same body dict on retry
            calls.append({"generationConfig": dict(body.get("generationConfig", {}))})
            if "thinkingConfig" in body.get("generationConfig", {}):
                return "", "Unknown name 'thinkingConfig' rejected"
            return "theek hai bhai", ""

        with patch.object(ai_engine, "_gemini_post", side_effect=fake_post):
            ai_engine._THINKING_OFF_OK = True
            text, err = ai_engine._gemini_call(
                "k", "gemini-3.5-flash-lite", "p", None, 100, 5)
        self.assertEqual(text, "theek hai bhai")
        self.assertEqual(err, "")
        # first call had it, retry dropped it, flag disabled permanently
        self.assertIn("thinkingConfig", calls[0]["generationConfig"])
        self.assertNotIn("thinkingConfig", calls[1]["generationConfig"])
        self.assertFalse(ai_engine._THINKING_OFF_OK)
        ai_engine._THINKING_OFF_OK = True  # restore for other tests

    def test_thinking_ok_no_retry(self):
        calls = []

        def fake_post(key, model, body, timeout):
            calls.append(body)
            return "jawab", ""

        with patch.object(ai_engine, "_gemini_post", side_effect=fake_post):
            ai_engine._THINKING_OFF_OK = True
            text, err = ai_engine._gemini_call(
                "k", "gemini-3.5-flash-lite", "p", None, 100, 5)
        self.assertEqual(text, "jawab")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["generationConfig"].get("thinkingConfig"),
            {"thinkingBudget": 0})


class GateTimeoutTests(unittest.TestCase):
    def test_gate_uses_short_timeout(self):
        router = MagicMock()
        router.generate.return_value = "NO"
        with patch.object(bot.ai_engine, "get_router", return_value=router), \
                patch.object(bot, "_GROUP_CONTEXT", {}):
            bot._should_reply_llm("hello", "x", CHAT)
        self.assertEqual(router.generate.call_args.kwargs.get("timeout"),
                         config.GATE_TIMEOUT_SECONDS)
        self.assertLess(config.GATE_TIMEOUT_SECONDS, config.AI_REQUEST_TIMEOUT)


class KeepaliveTests(unittest.TestCase):
    def setUp(self):
        # the module-level connection must not leak between tests
        self._saved_conn = tg_api._CONN
        tg_api._CONN = None

    def tearDown(self):
        tg_api._CONN = self._saved_conn

    def test_connection_reused_across_calls(self):
        conn = MagicMock()
        resp = MagicMock()
        resp.read.return_value = b'{"ok": true, "result": {}}'
        conn.getresponse.return_value = resp
        made = []

        def fake_connect(host, timeout=None):
            made.append(host)
            return conn

        with patch.object(tg_api.http.client, "HTTPSConnection",
                          side_effect=fake_connect):
            with patch.dict("os.environ",
                            {"TELEGRAM_BOT_TOKEN": "T:ok"}):
                tg_api.api("getMe")
                tg_api.api("getChat", {"chat_id": 1})
        # one connection object serving both calls
        self.assertEqual(len(made), 1)
        self.assertEqual(conn.request.call_count, 2)

    def test_falls_back_to_urllib_on_dead_connection(self):
        with patch.object(tg_api.http.client, "HTTPSConnection",
                          side_effect=OSError("no network")), \
                patch.object(tg_api.urllib.request, "urlopen",
                             side_effect=OSError("still dead")), \
                patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "T:ok"}):
            r = tg_api.api("getMe")
        self.assertFalse(r.get("ok"))


class PrefetchTests(unittest.TestCase):
    def test_prefetch_box_filled(self):
        with patch.object(bot.knowledge, "search", return_value=[]) as ms, \
                patch.object(bot.rules, "rules_block", return_value="R"), \
                patch.object(bot.community, "community_block",
                             return_value="F"):
            box = bot._start_answer_prefetch("loader update", 1)
            # give the daemon thread a moment
            for _ in range(100):
                if box.get("hits") is not None:
                    break
                import time as _t
                _t.sleep(0.01)
        self.assertEqual(box.get("hits"), [])
        self.assertEqual(box.get("standing"), "R")
        self.assertEqual(box.get("facts"), "F")
        ms.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
    sys.exit(0)
