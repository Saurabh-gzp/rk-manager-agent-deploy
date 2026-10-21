"""v4.2 REALTIME AGENT (MTProto) tests.

Owner ka brief: 'API ID/Hash use karo taki agent ki power badh jaye —
sabhi channel/group messages realtime, old + new sb kuch, real agent
ki tarah.'

Locks in:
  1. Layer graceful degradation — bina API ID/telethon ke bot 100%
     chalta hai (fetch/verify/scan sab empty, start() False).
  2. Telethon message parse — deleted posts alive=False, file_name/
     file_type detection (document/photo/voice).
  3. Realtime delete-event apply — channel posts + stored group
     messages purge + verify cache dead-mark.
  4. MTProto-first verification — layer connected ho to direct verdict,
     disconnected ho to purana HTTP probe path untouched.
  5. History backfill (ID-range scan) — naye posts ingest, Bot API
     wale rich doc ko downgrade NAHI (entities/file_id safe).
  6. Live reconcile — stored posts batch-verify, deleted => purge,
     unknown par kuch nahi (fail-safe).
  7. Backfilled file posts (file_id=None par file_type hai) file-offer
     eligible — forward-by-id delivery.
  8. Group answers ka LIVE context (fetch_group_context) prefetch me.
"""
import unittest
from unittest.mock import patch, MagicMock

import agent
import bot
import config
import db
import knowledge
import mtproto_layer
import verify

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
        verify.invalidate_cache()
        while mtproto_layer.drain_events(500):
            pass

    def tearDown(self):
        self.p_db.stop()
        self.p_owner.stop()
        db.invalidate_state_cache()
        verify.invalidate_cache()


# ---------------------------------------------------------------------------
# 1. graceful degradation
# ---------------------------------------------------------------------------

class LayerDisabledTests(IMDBTestCase):
    def test_enabled_false_without_api_id(self):
        with patch.object(config, "MTPROTO_ENABLED", 1), \
             patch.object(config, "MTPROTO_API_ID", 0), \
             patch.object(config, "MTPROTO_API_HASH", "x"):
            self.assertFalse(mtproto_layer.enabled())
        with patch.object(config, "MTPROTO_ENABLED", 1), \
             patch.object(config, "MTPROTO_API_ID", 123), \
             patch.object(config, "MTPROTO_API_HASH", ""):
            self.assertFalse(mtproto_layer.enabled())

    def test_enabled_false_when_env_flag_off(self):
        with patch.object(config, "MTPROTO_ENABLED", 0), \
             patch.object(config, "MTPROTO_API_ID", 123), \
             patch.object(config, "MTPROTO_API_HASH", "abc"):
            self.assertFalse(mtproto_layer.enabled())

    def test_fetches_empty_when_not_connected(self):
        self.assertEqual(mtproto_layer.fetch_messages(CHAN, [1, 2]), {})
        self.assertEqual(mtproto_layer.verify_posts(CHAN, [1]), {})
        self.assertEqual(mtproto_layer.scan_channel_window(CHAN, 1, 10), [])
        self.assertEqual(mtproto_layer.fetch_group_context(CHAT, 100), [])

    def test_status_reports_disabled(self):
        st = mtproto_layer.status()
        self.assertIn("enabled", st)
        self.assertIn("connected", st)
        self.assertFalse(st["connected"])


# ---------------------------------------------------------------------------
# 2. message parsing
# ---------------------------------------------------------------------------

class FakeMsg:
    def __init__(self, mid, text="", date=1700000000, media=None,
                 grouped_id=None, author=""):
        self.id = mid
        self.message = text
        self.date = self._dt(date) if date else None
        self.media = media
        self.grouped_id = grouped_id
        self.post_author = author

    @staticmethod
    def _dt(ts):
        import datetime
        return datetime.datetime.fromtimestamp(
            ts, tz=datetime.timezone.utc)


class FakeDocAttr:
    def __init__(self, file_name=None, is_voice=False, is_video=False):
        if file_name is not None:
            self.file_name = file_name
        if is_voice:
            self.voice = True
        if is_video:
            self.round_message = False


class FakeDoc:
    def __init__(self, attributes=None, mime_type="application/zip"):
        self.attributes = attributes or []
        self.mime_type = mime_type


class FakeMediaDoc:
    def __init__(self, doc):
        self.document = doc


class FakeMediaPhoto:
    def __init__(self):
        self.photo = True


class ParseTests(unittest.TestCase):
    def test_live_text_message(self):
        p = mtproto_layer._parse_message(FakeMsg(10, "key on ho gaya"))
        self.assertEqual(p["id"], 10)
        self.assertTrue(p["alive"])
        self.assertEqual(p["text"], "key on ho gaya")

    def test_deleted_message_alive_false(self):
        p = mtproto_layer._parse_message(FakeMsg(10, date=None))
        self.assertEqual(p["id"], 10)
        self.assertFalse(p["alive"])

    def test_none_message_returns_none(self):
        self.assertIsNone(mtproto_layer._parse_message(None))

    def test_document_file_name_detected(self):
        media = FakeMediaDoc(FakeDoc([FakeDocAttr(file_name="loader.apk")]))
        p = mtproto_layer._parse_message(FakeMsg(11, "naya loader", media=media))
        self.assertEqual(p["file_name"], "loader.apk")
        self.assertEqual(p["file_type"], "document")

    def test_photo_detected(self):
        p = mtproto_layer._parse_message(FakeMsg(12, "photo caption",
                                                 media=FakeMediaPhoto()))
        self.assertEqual(p["file_type"], "photo")
        self.assertEqual(p["text"], "photo caption")


# ---------------------------------------------------------------------------
# 3. realtime delete-event apply
# ---------------------------------------------------------------------------

class DeleteEventTests(IMDBTestCase):
    def test_delete_event_purges_post_and_group_message(self):
        knowledge.ingest_mtproto_post(CHAN, {
            "id": 354, "alive": True, "date": 1700000000,
            "text": "GOOD NEWS KAL VIPER AARHA HAI", "file_type": None,
            "file_name": None, "grouped_id": None, "author": ""})
        self._db["messages"].insert_one({
            "chat_id": CHAT, "message_id": 2406, "text": "hello",
            "user_id": 5, "date": 1700000000})
        with patch.object(bot.links, "is_managed_channel",
                          return_value=True):
            bot._apply_mtproto_event("delete", CHAN, [354])
            bot._apply_mtproto_event("delete", CHAT, [2406])
        self.assertFalse(knowledge.post_exists(CHAN, 354))
        self.assertEqual(self._db["messages"].count_documents(
            {"chat_id": CHAT, "message_id": 2406}), 0)

    def test_delete_event_marks_verify_cache_dead(self):
        knowledge.ingest_mtproto_post(CHAN, {
            "id": 400, "alive": True, "date": 1700000000,
            "text": "test post", "file_type": None, "file_name": None,
            "grouped_id": None, "author": ""})
        with patch.object(bot.links, "is_managed_channel",
                          return_value=True):
            bot._apply_mtproto_event("delete", CHAN, [400])
        # verify ab cache se turant False bolega — bina network roundtrip
        with patch.object(verify, "_probe_http") as ph:
            self.assertFalse(verify.verify_post(CHAN, 400))
            ph.assert_not_called()

    def test_new_event_ingests_managed_channel_backup(self):
        with patch.object(bot.links, "is_managed_channel",
                          return_value=True):
            bot._apply_mtproto_event("new", CHAN, {
                "id": 355, "alive": True, "date": 1700000001,
                "text": "naya backup post", "file_type": None,
                "file_name": None, "grouped_id": None, "author": ""})
        self.assertTrue(knowledge.post_exists(CHAN, 355))

    def test_edit_event_updates_text(self):
        knowledge.ingest_mtproto_post(CHAN, {
            "id": 360, "alive": True, "date": 1700000000,
            "text": "pehla text", "file_type": None, "file_name": None,
            "grouped_id": None, "author": ""})
        with patch.object(bot.links, "is_managed_channel",
                          return_value=True):
            bot._apply_mtproto_event("edit", CHAN, {
                "id": 360, "alive": True, "date": 1700000002,
                "text": "edited text", "file_type": None,
                "file_name": None, "grouped_id": None, "author": ""})
        post = knowledge.get_post(CHAN, 360)
        self.assertEqual(post["text"], "edited text")


# ---------------------------------------------------------------------------
# 4. MTProto-first verification
# ---------------------------------------------------------------------------

class VerifyMtprotoTests(IMDBTestCase):
    def test_connected_layer_verdict_wins(self):
        with patch.object(mtproto_layer, "is_connected", return_value=True), \
             patch.object(mtproto_layer, "verify_posts",
                          return_value={555: False}) as vp, \
             patch.object(verify, "_probe_http") as ph:
            self.assertFalse(verify.verify_post(CHAN, 555))
            ph.assert_not_called()
            vp.assert_called_once()

    def test_unknown_verdict_falls_back_to_http(self):
        verify.invalidate_cache()
        with patch.object(mtproto_layer, "is_connected", return_value=True), \
             patch.object(mtproto_layer, "verify_posts",
                          return_value={556: None}), \
             patch.object(verify, "_channel_username",
                          return_value="rkgamersbgmi"), \
             patch.object(verify, "_probe_http", return_value=True) as ph:
            self.assertTrue(verify.verify_post(CHAN, 556))
            ph.assert_called_once()

    def test_disconnected_uses_http_probe(self):
        verify.invalidate_cache()
        with patch.object(mtproto_layer, "is_connected",
                          return_value=False), \
             patch.object(verify, "_channel_username",
                          return_value="rkgamersbgmi"), \
             patch.object(verify, "_probe_http", return_value=False) as ph:
            self.assertFalse(verify.verify_post(CHAN, 999))
            ph.assert_called_once()

    def test_purge_if_dead_with_layer(self):
        knowledge.ingest_mtproto_post(CHAN, {
            "id": 557, "alive": True, "date": 1700000000,
            "text": "dying post", "file_type": None, "file_name": None,
            "grouped_id": None, "author": ""})
        with patch.object(mtproto_layer, "is_connected", return_value=True), \
             patch.object(mtproto_layer, "verify_posts",
                          return_value={557: False}):
            self.assertTrue(verify.purge_if_dead(CHAN, 557))
        self.assertFalse(knowledge.post_exists(CHAN, 557))

    def test_sweep_mtproto_fast_path(self):
        for i in range(3):
            knowledge.ingest_mtproto_post(CHAN, {
                "id": 600 + i, "alive": True, "date": 1700000000 + i,
                "text": f"post {i}", "file_type": None, "file_name": None,
                "grouped_id": None, "author": ""})
        with patch.object(mtproto_layer, "is_connected", return_value=True), \
             patch.object(mtproto_layer, "verify_posts",
                          side_effect=lambda cid, mids: {
                              m: (m != 601) for m in mids}):
            res = verify.sweep_once(limit=10)
        self.assertEqual(res.get("mode"), "mtproto")
        self.assertEqual(res.get("checked"), 3)
        self.assertEqual(res.get("purged"), 1)
        self.assertTrue(knowledge.post_exists(CHAN, 600))
        self.assertFalse(knowledge.post_exists(CHAN, 601))
        self.assertTrue(knowledge.post_exists(CHAN, 602))

    def test_mark_deleted_caches_without_network(self):
        verify.mark_deleted(CHAN, [777])
        with patch.object(mtproto_layer, "is_connected", return_value=True), \
             patch.object(mtproto_layer, "verify_posts") as vp:
            self.assertFalse(verify.verify_post(CHAN, 777))
            vp.assert_not_called()


# ---------------------------------------------------------------------------
# 5-6. backfill + reconcile + ingest protection
# ---------------------------------------------------------------------------

class BackfillTests(IMDBTestCase):
    def test_ingest_mtproto_post_new(self):
        ok = knowledge.ingest_mtproto_post(CHAN, {
            "id": 355, "alive": True, "date": 1700000000,
            "text": "Viper loader https://t.me/x/1", "file_type": "document",
            "file_name": "viper.apk", "grouped_id": None, "author": "RK"})
        self.assertTrue(ok)
        post = knowledge.get_post(CHAN, 355)
        self.assertEqual(post["file_name"], "viper.apk")
        self.assertEqual(post["src"], "mtproto")
        self.assertIn("https://t.me/x/1", post["links"])

    def test_ingest_never_downgrades_bot_api_doc(self):
        # Bot API ne pehle se rich doc daala (file_id + entities)
        self._db["channel_posts"].insert_one({
            "channel_id": CHAN, "message_id": 354, "text": "old text",
            "file_id": "BOTAPIFILE", "file_type": "document",
            "entities": [{"type": "text_link", "offset": 0,
                          "length": 3, "url": "https://key.page"}],
            "date": 1700000000, "links": [], "importance": 0.5,
        })
        knowledge.ingest_mtproto_post(CHAN, {
            "id": 354, "alive": True, "date": 1700000005,
            "text": "new text after edit", "file_type": None,
            "file_name": None, "grouped_id": None, "author": ""})
        post = knowledge.get_post(CHAN, 354)
        self.assertEqual(post["text"], "new text after edit")
        self.assertEqual(post["file_id"], "BOTAPIFILE")  # NOT cleared
        self.assertEqual(post["entities"][0]["url"], "https://key.page")

    def test_backfill_scans_and_ingests(self):
        knowledge.ingest_mtproto_post(CHAN, {
            "id": 354, "alive": True, "date": 1700000000,
            "text": "top stored", "file_type": None, "file_name": None,
            "grouped_id": None, "author": ""})
        fake_posts = [
            {"id": 355, "alive": True, "date": 1700000010,
             "text": "missed post 1", "file_type": None,
             "file_name": None, "grouped_id": None, "author": ""},
            {"id": 356, "alive": True, "date": 1700000020,
             "text": "missed post 2", "file_type": "photo",
             "file_name": None, "grouped_id": None, "author": ""},
        ]
        with patch.object(mtproto_layer, "is_connected", return_value=True), \
             patch.object(mtproto_layer, "scan_channel_window",
                          return_value=fake_posts) as sc:
            res = knowledge.mtproto_backfill(CHAN)
        sc.assert_called_once_with(CHAN, 355, config.MTPROTO_SCAN_WINDOW)
        self.assertEqual(res.get("added"), 2)
        self.assertTrue(knowledge.post_exists(CHAN, 355))
        self.assertTrue(knowledge.post_exists(CHAN, 356))

    def test_backfill_skipped_when_layer_down(self):
        with patch.object(mtproto_layer, "is_connected", return_value=False):
            self.assertEqual(knowledge.mtproto_backfill(CHAN), {"skipped": 1})

    def test_reconcile_purges_deleted_keeps_unknown(self):
        for mid, txt in ((700, "alive"), (701, "dead"), (702, "unknown")):
            knowledge.ingest_mtproto_post(CHAN, {
                "id": mid, "alive": True, "date": 1700000000 + mid,
                "text": txt, "file_type": None, "file_name": None,
                "grouped_id": None, "author": ""})
        verdicts = {700: True, 701: False, 702: None}

        def fake_verify(cid, mids):
            return {m: verdicts[m] for m in mids}

        with patch.object(mtproto_layer, "is_connected", return_value=True), \
             patch.object(mtproto_layer, "verify_posts",
                          side_effect=fake_verify):
            res = knowledge.mtproto_reconcile(CHAN)
        self.assertEqual(res.get("checked"), 2)
        self.assertEqual(res.get("purged"), 1)
        self.assertTrue(knowledge.post_exists(CHAN, 700))
        self.assertFalse(knowledge.post_exists(CHAN, 701))
        self.assertTrue(knowledge.post_exists(CHAN, 702))  # unknown = safe

    def test_managed_channel_ids_from_state(self):
        db.save_state("channel_id", CHAN)
        self.assertIn(CHAN, knowledge.managed_channel_ids())

    def test_purge_group_messages_by_ids(self):
        self._db["messages"].insert_many([
            {"chat_id": CHAT, "message_id": 1, "text": "a", "user_id": 1},
            {"chat_id": CHAT, "message_id": 2, "text": "b", "user_id": 1},
        ])
        self.assertEqual(knowledge.purge_group_messages(CHAT, [1, 2]), 2)
        self.assertEqual(self._db["messages"].count_documents(
            {"chat_id": CHAT}), 0)

    def test_ingest_message_stores_message_id(self):
        knowledge.ingest_message({
            "message_id": 1234, "date": 1700000000,
            "text": "normal group msg",
            "from": {"id": 5, "first_name": "u", "username": "u5"},
            "chat": {"id": CHAT, "type": "supergroup", "title": "G"},
        })
        d = self._db["messages"].find_one({"chat_id": CHAT, "message_id": 1234})
        self.assertIsNotNone(d)


# ---------------------------------------------------------------------------
# 7. backfilled file posts offer-eligible
# ---------------------------------------------------------------------------

class BackfillFileOfferTests(IMDBTestCase):
    def test_file_id_none_but_type_offerable(self):
        hits = [{
            "kind": "post", "text": "viper loader",
            "file_id": None, "file_type": "document",
            "file_name": "viper.apk", "channel_id": CHAN,
            "message_id": 800, "match_score": 3, "date": 1700000000,
        }]
        with patch.object(agent, "_looks_like_file_request",
                          return_value=True):
            offer = agent._best_file_offer(hits, "viper loader bhejo")
        self.assertIsNotNone(offer)
        self.assertEqual(offer["message_id"], 800)

    def test_text_post_without_media_not_offerable(self):
        hits = [{
            "kind": "post", "text": "random info post",
            "file_id": None, "file_type": None, "file_name": None,
            "channel_id": CHAN, "message_id": 801,
            "match_score": 3, "date": 1700000000,
        }]
        with patch.object(agent, "_looks_like_file_request",
                          return_value=True):
            self.assertIsNone(agent._best_file_offer(hits, "random bhejo"))

    def test_find_file_for_backfilled(self):
        knowledge.ingest_mtproto_post(CHAN, {
            "id": 802, "alive": True, "date": 1700000000,
            "text": "arvon config", "file_type": "document",
            "file_name": "arvon.cfg", "grouped_id": None, "author": ""})
        h = knowledge.find_file_for("arvon")
        self.assertIsNotNone(h)
        self.assertEqual(h.get("file_name"), "arvon.cfg")


# ---------------------------------------------------------------------------
# 8. live group context prefetch
# ---------------------------------------------------------------------------

class LiveContextTests(IMDBTestCase):
    def test_prefetch_fills_live_context(self):
        fake = [{"id": 100, "alive": True, "date": 1700000000,
                 "text": "kya hal hai", "file_type": None,
                 "file_name": None, "grouped_id": None, "author": ""}]
        with patch.object(mtproto_layer, "is_connected", return_value=True), \
             patch.object(mtproto_layer, "fetch_group_context",
                          return_value=fake) as fg:
            box = bot._start_answer_prefetch("test question", 5,
                                             chat_id=CHAT, message_id=101)
            import time as _t
            for _ in range(100):
                if box.get("live_context") is not None:
                    break
                _t.sleep(0.02)
        self.assertIn("#100", box.get("live_context") or "")
        fg.assert_called_once()

    def test_prefetch_without_layer_no_live_context(self):
        with patch.object(bot.mtproto_layer, "is_connected",
                          return_value=False):
            box = bot._start_answer_prefetch("q", 5, chat_id=CHAT,
                                             message_id=101)
            import time as _t
            for _ in range(100):
                if box.get("hits") is not None:
                    break
                _t.sleep(0.02)
        self.assertIsNone(box.get("live_context"))


if __name__ == "__main__":
    unittest.main()
