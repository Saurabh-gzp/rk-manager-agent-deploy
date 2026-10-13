import time
import unittest
from unittest.mock import patch

import db
import filters as filters_mod
import moderation


OWNER = 111
ADMIN = 222
STRANGER = 333
TARGET = 42
BOT_ID = 999
CHAT = -1001


def _msg(text, uid=OWNER, name="Owner", reply_to=None):
    msg = {
        "chat": {"id": CHAT},
        "from": {"id": uid, "first_name": name},
        "message_id": 5,
        "text": text,
    }
    if reply_to is not None:
        msg["reply_to_message"] = reply_to
    return msg


def _user_msg(uid, name, message_id=50):
    return {"message_id": message_id, "from": {"id": uid, "first_name": name}}


BOT_ADMIN = {"status": "administrator", "can_restrict_members": True,
             "can_delete_messages": True}
MEMBER = {"status": "member"}


class ModToolsTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(db, "_DB", db.IMDB()),
            patch.object(moderation.config, "OWNER_CHAT_ID", OWNER),
            patch.object(moderation.config, "ADMIN_CHAT_ID", ADMIN),
            patch.object(moderation.tg_api, "get_me",
                         return_value={"id": BOT_ID}),
            patch.object(moderation.tg_api, "send_message",
                         return_value={"ok": True}),
        ]
        for p in self.patches:
            p.start()
        moderation._BOT_ID = None
        self.sent = moderation.tg_api.send_message

    def tearDown(self):
        for p in self.patches:
            p.stop()
        moderation._BOT_ID = None

    def _member_map(self, extra=None):
        table = {TARGET: MEMBER, BOT_ID: BOT_ADMIN}
        if extra:
            table.update(extra)
        return patch.object(
            moderation.tg_api, "get_chat_member",
            side_effect=lambda chat_id, user_id: table.get(user_id, MEMBER),
        )

    def _last_sent(self):
        return self.sent.call_args.args[1]

    # ---------- parse_duration ----------

    def test_parse_duration_units(self):
        self.assertEqual(moderation.parse_duration("45s"), 45)
        self.assertEqual(moderation.parse_duration("30m"), 1800)
        self.assertEqual(moderation.parse_duration("2h"), 7200)
        self.assertEqual(moderation.parse_duration("1d"), 86400)
        self.assertEqual(moderation.parse_duration("1w"), 604800)
        self.assertEqual(moderation.parse_duration("2H"), 7200)
        self.assertEqual(moderation.parse_duration("  10m  "), 600)

    def test_parse_duration_invalid(self):
        self.assertIsNone(moderation.parse_duration("abc"))
        self.assertIsNone(moderation.parse_duration("10"))
        self.assertIsNone(moderation.parse_duration("0m"))
        self.assertIsNone(moderation.parse_duration("0"))
        self.assertIsNone(moderation.parse_duration(""))
        self.assertIsNone(moderation.parse_duration(None))
        self.assertIsNone(moderation.parse_duration("1.5h"))
        self.assertIsNone(moderation.parse_duration("-5m"))

    def test_parse_duration_over_cap(self):
        self.assertIsNone(moderation.parse_duration("365d"))
        self.assertIsNone(moderation.parse_duration("500d"))
        self.assertIsNone(moderation.parse_duration("100w"))
        self.assertEqual(moderation.parse_duration("364d"), 364 * 86400)

    # ---------- filters (Rose-style: content delivery, never delete) ----------

    def test_filter_add_list_remove(self):
        self.assertEqual(
            moderation.add_filter(
                "Spam", {"kind": "text", "text": "no spam here"}, OWNER),
            "spam")
        flist = moderation.get_filters()
        self.assertIn("spam", flist)
        self.assertEqual(flist["spam"]["text"], "no spam here")
        self.assertEqual(flist["spam"]["by"], OWNER)

        with self._member_map():
            self.assertTrue(moderation.handle_mod_command(
                _msg("/filters"), OWNER, CHAT))
        text = self._last_sent()
        self.assertIn("spam", text)

        self.assertTrue(moderation.remove_filter("SPAM"))
        self.assertNotIn("spam", moderation.get_filters())
        self.assertFalse(moderation.remove_filter("nope"))

    def test_filter_command_flow(self):
        with self._member_map():
            self.assertTrue(moderation.handle_mod_command(
                _msg('/filter "win prize" congrats winner'), OWNER, CHAT))
        self.assertIn("win prize", moderation.get_filters())
        self.assertEqual(
            moderation.get_filters()["win prize"]["text"], "congrats winner")
        self.assertIn("save ho gaya", self._last_sent())

        with self._member_map():
            self.assertTrue(moderation.handle_mod_command(
                _msg('/stop "win prize"'), OWNER, CHAT))
        self.assertNotIn("win prize", moderation.get_filters())
        self.assertIn("hata diya", self._last_sent())

        with self._member_map():
            self.assertTrue(moderation.handle_mod_command(
                _msg("/filter"), OWNER, CHAT))
        self.assertIn("Usage", self._last_sent())

    def test_filter_bare_word_without_content_rejected(self):
        # Rose: a filter MUST have content (typed text or replied message).
        with self._member_map():
            self.assertTrue(moderation.handle_mod_command(
                _msg("/filter ghost"), OWNER, CHAT))
        self.assertNotIn("ghost", moderation.get_filters())
        self.assertIn("Kya save karu", self._last_sent())

    def test_filter_save_from_replied_document(self):
        reply = {
            "message_id": 55,
            "from": {"id": TARGET, "first_name": "Sam"},
            "document": {"file_id": "DOC1", "file_name": "loader.apk"},
            "caption": "BAT loader v2",
        }
        with self._member_map():
            self.assertTrue(moderation.handle_mod_command(
                _msg("/filter loader", reply_to=reply), OWNER, CHAT))
        entry = moderation.get_filters()["loader"]
        self.assertEqual(entry["kind"], "document")
        self.assertEqual(entry["file_id"], "DOC1")
        self.assertEqual(entry["caption"], "BAT loader v2")
        self.assertEqual(entry["src"], {"chat_id": CHAT, "message_id": 55})

    def test_filter_delivery_via_copy_message(self):
        moderation.add_filter("loader", {
            "kind": "document", "file_id": "DOC1",
            "caption": "BAT loader v2",
            "src": {"chat_id": CHAT, "message_id": 55}}, OWNER)
        message = {
            "chat": {"id": CHAT},
            "from": {"id": TARGET, "first_name": "Sam"},
            "message_id": 7,
            "text": "bhai loader bhejo",
        }
        with self._member_map(), \
                patch.object(moderation.tg_api, "copy_message",
                             return_value={"ok": True}) as copy, \
                patch.object(moderation.tg_api, "delete_message") as delete:
            self.assertTrue(moderation.handle_group_message(message))
            copy.assert_called_once_with(CHAT, CHAT, 55,
                                         reply_to_message_id=7)
            delete.assert_not_called()
        actions = list(db.get_db()["mod_actions"].find(
            {"action": "filter_reply"}))
        self.assertEqual(len(actions), 1)

    def test_filter_delivery_file_id_fallback(self):
        # Original message deleted -> copy fails -> file_id resend (Rose way).
        moderation.add_filter("loader", {
            "kind": "document", "file_id": "DOC1", "caption": "BAT v2",
            "src": {"chat_id": CHAT, "message_id": 55}}, OWNER)
        message = {
            "chat": {"id": CHAT},
            "from": {"id": TARGET, "first_name": "Sam"},
            "message_id": 8,
            "text": "loader chahiye",
        }
        with self._member_map(), \
                patch.object(moderation.tg_api, "copy_message",
                             return_value={"ok": False,
                                           "description": "message to copy not found"}), \
                patch.object(moderation.tg_api, "send_document",
                             return_value={"ok": True}) as senddoc, \
                patch.object(moderation.tg_api, "delete_message") as delete:
            self.assertTrue(moderation.handle_group_message(message))
            senddoc.assert_called_once()
            self.assertEqual(senddoc.call_args.args[1], "DOC1")
            delete.assert_not_called()

    def test_filter_text_reply_triggers(self):
        moderation.add_filter(
            "loader", {"kind": "text",
                       "text": "Loader ke liye channel check karo"}, OWNER)
        message = {
            "chat": {"id": CHAT},
            "from": {"id": TARGET, "first_name": "Sam"},
            "message_id": 7,
            "text": "bhai naya loader kahan milega?",
        }
        with self._member_map(), patch.object(
                moderation.tg_api, "delete_message") as delete:
            self.assertTrue(moderation.handle_group_message(message))
            delete.assert_not_called()
        self.sent.assert_called_once()
        args, kwargs = self.sent.call_args
        self.assertEqual(args[1], "Loader ke liye channel check karo")
        self.assertEqual(kwargs.get("reply_to_message_id"), 7)
        actions = list(db.get_db()["mod_actions"].find(
            {"action": "filter_reply"}))
        self.assertEqual(len(actions), 1)

    def test_filter_word_boundary_rose_semantics(self):
        # Rose regex: ( |^|[^\w])word( |$|[^\w]) with IGNORECASE
        moderation.add_filter(
            "ok", {"kind": "text", "text": "THE OK REPLY"}, OWNER)

        def fires(text):
            return moderation.matches_filter(text) is not None

        self.assertTrue(fires("ok"))
        self.assertTrue(fires("OK?"))
        self.assertTrue(fires("theek ok."))
        self.assertTrue(fires("ye ok hai"))
        self.assertFalse(fires("okok"))
        self.assertFalse(fires("book"))
        self.assertFalse(fires("booking"))

    def test_filter_triggers_for_owner_and_admins_too(self):
        # Rose parity: content filters fire for EVERYONE — owner & admins.
        moderation.add_filter(
            "loader", {"kind": "text", "text": "LOADER CONTENT"}, OWNER)
        owner_msg = {
            "chat": {"id": CHAT},
            "from": {"id": OWNER, "first_name": "Owner"},
            "message_id": 31,
            "text": "loader ka update",
        }
        with patch.object(moderation.tg_api, "delete_message") as delete:
            self.assertTrue(moderation.handle_group_message(owner_msg))
            delete.assert_not_called()
        self.sent.assert_called_once()

        admin_msg = {
            "chat": {"id": CHAT},
            "from": {"id": TARGET, "first_name": "AdminGuy"},
            "message_id": 32,
            "text": "loader dedo",
        }
        with self._member_map({TARGET: {"status": "administrator"}}), \
                patch.object(moderation.tg_api, "delete_message") as delete:
            self.assertTrue(moderation.handle_group_message(admin_msg))
            delete.assert_not_called()
        self.assertEqual(self.sent.call_count, 2)

    def test_filter_never_deletes_or_bans(self):
        moderation.add_filter(
            "zzz", {"kind": "text", "text": "Z CONTENT"}, OWNER)
        message = {
            "chat": {"id": CHAT},
            "from": {"id": TARGET, "first_name": "Sam"},
            "message_id": 9,
            "text": "zzz zzz zzz",
        }
        with self._member_map(), \
                patch.object(moderation.tg_api, "delete_message",
                             return_value={"ok": True}) as delete, \
                patch.object(moderation.tg_api, "restrict_chat_member",
                             return_value={"ok": True}) as restrict, \
                patch.object(moderation.tg_api, "ban_chat_member") as ban:
            for mid in (9, 10, 11):
                message["message_id"] = mid
                self.assertTrue(moderation.handle_group_message(message))
            delete.assert_not_called()
            restrict.assert_not_called()
            ban.assert_not_called()

    def test_filter_caption_triggers(self):
        moderation.add_filter(
            "config", {"kind": "text", "text": "CFG"}, OWNER)
        message = {
            "chat": {"id": CHAT},
            "from": {"id": TARGET, "first_name": "Sam"},
            "message_id": 12,
            "caption": "ye config file hai",
        }
        with self._member_map():
            self.assertTrue(moderation.handle_group_message(message))

    def test_legacy_blocklist_filter_is_inert(self):
        # Old-schema {reply: None} auto-delete entries: never fire, removed.
        db.save_state("mod_filters",
                      {"oldword": {"reply": None, "by": 1, "ts": 1}})
        message = {
            "chat": {"id": CHAT},
            "from": {"id": TARGET, "first_name": "Sam"},
            "message_id": 13,
            "text": "ye oldword hai",
        }
        with self._member_map(), patch.object(
                moderation.tg_api, "delete_message") as delete:
            self.assertFalse(moderation.handle_group_message(message))
            delete.assert_not_called()
        self.assertNotIn("oldword", moderation.get_filters())

    def test_bang_prefix_filter_command(self):
        # Rose-style ! prefix (e.g. !filter ping pong reply)
        with self._member_map():
            self.assertTrue(moderation.handle_mod_command(
                _msg("!filter ping pong reply"), OWNER, CHAT))
        self.assertEqual(
            moderation.get_filters()["ping"]["text"], "pong reply")

    def test_album_filter_save_and_group_delivery(self):
        # Album parts arrive -> owner replies to one with /filter pics ->
        # the WHOLE album is saved and delivered regrouped (like Rose).
        part1 = {"chat": {"id": CHAT}, "message_id": 61,
                 "media_group_id": "mg1",
                 "from": {"id": TARGET, "first_name": "Sam"},
                 "photo": [{"file_id": "P1", "file_size": 10}]}
        part2 = {"chat": {"id": CHAT}, "message_id": 62,
                 "media_group_id": "mg1",
                 "from": {"id": TARGET, "first_name": "Sam"},
                 "photo": [{"file_id": "P2", "file_size": 20}]}
        filters_mod.remember_media(CHAT, part1)
        filters_mod.remember_media(CHAT, part2)
        with self._member_map():
            self.assertTrue(moderation.handle_mod_command(
                _msg("/filter pics", reply_to=dict(part2)), OWNER, CHAT))
        entry = moderation.get_filters()["pics"]
        self.assertEqual(entry["kind"], "album")
        self.assertEqual(len(entry["items"]), 2)

        message = {
            "chat": {"id": CHAT},
            "from": {"id": TARGET, "first_name": "Sam"},
            "message_id": 70,
            "text": "pics bhejo",
        }
        with self._member_map(), \
                patch.object(moderation.tg_api, "send_media_group",
                             return_value={"ok": True}) as smg, \
                patch.object(moderation.tg_api, "delete_message") as delete:
            self.assertTrue(moderation.handle_group_message(message))
            smg.assert_called_once()
            media = smg.call_args.args[1]
            self.assertEqual(len(media), 2)
            self.assertEqual({m["media"] for m in media}, {"P1", "P2"})
            self.assertEqual(
                smg.call_args.kwargs.get("reply_to_message_id"), 70)
            delete.assert_not_called()

    # ---------- /mute ----------

    def test_mute_via_reply(self):
        reply = _user_msg(TARGET, "Sam")
        with self._member_map(), patch.object(
                moderation.tg_api, "restrict_chat_member",
                return_value={"ok": True}) as restrict:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/mute spamming", reply_to=reply), OWNER, CHAT))
        restrict.assert_called_once()
        args, kwargs = restrict.call_args
        self.assertEqual(args[0], CHAT)
        self.assertEqual(args[1], TARGET)
        self.assertFalse(args[2]["can_send_messages"])
        self.assertIsNone(kwargs.get("until_date"))
        self.assertIn("mute kar diya", self._last_sent())
        record = db.get_state(f"mod_mute:{CHAT}:{TARGET}")
        self.assertEqual(record["reason"], "spamming")
        self.assertEqual(
            db.get_db()["mod_actions"].count_documents({"action": "mute"}), 1)

    def test_mute_admin_target_refused(self):
        reply = _user_msg(TARGET, "Sam")
        with self._member_map({TARGET: {"status": "administrator"}}), \
                patch.object(moderation.tg_api, "restrict_chat_member") as restrict:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/mute", reply_to=reply), OWNER, CHAT))
            restrict.assert_not_called()
        self.assertIn("admins", self._last_sent())

    def test_mute_without_bot_right_refused(self):
        reply = _user_msg(TARGET, "Sam")
        no_right = {"status": "administrator"}  # bot admin w/o restrict right
        with self._member_map({BOT_ID: no_right}), patch.object(
                moderation.tg_api, "restrict_chat_member") as restrict:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/mute", reply_to=reply), OWNER, CHAT))
            restrict.assert_not_called()
        self.assertIn("Restrict Members", self._last_sent())

    def test_mute_by_id_and_username(self):
        with self._member_map(), patch.object(
                moderation.tg_api, "restrict_chat_member",
                return_value={"ok": True}) as restrict:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/mute 4242"), OWNER, CHAT))
        restrict.assert_called_once()
        self.assertEqual(restrict.call_args.args[1], 4242)

        db.get_db()["messages"].insert_one(
            {"user_id": 777, "username": "GamerX", "date": 100})
        with self._member_map(), patch.object(
                moderation.tg_api, "restrict_chat_member",
                return_value={"ok": True}) as restrict2:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/mute @gamerx"), OWNER, CHAT))
        restrict2.assert_called_once()
        self.assertEqual(restrict2.call_args.args[1], 777)

    def test_tmute_sets_until_date(self):
        reply = _user_msg(TARGET, "Sam")
        before = int(time.time())
        with self._member_map(), patch.object(
                moderation.tg_api, "restrict_chat_member",
                return_value={"ok": True}) as restrict:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/tmute 30m cool down", reply_to=reply), OWNER, CHAT))
        restrict.assert_called_once()
        until = restrict.call_args.kwargs.get("until_date")
        self.assertIsNotNone(until)
        self.assertGreaterEqual(until, before + 1800)
        self.assertLessEqual(until, before + 1800 + 10)
        self.assertIn("30 minute", self._last_sent())
        record = db.get_state(f"mod_mute:{CHAT}:{TARGET}")
        self.assertEqual(record["until"], until)

    def test_tmute_by_id_arg_order(self):
        with self._member_map(), patch.object(
                moderation.tg_api, "restrict_chat_member",
                return_value={"ok": True}) as restrict:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/tmute 4242 2h"), OWNER, CHAT))
        restrict.assert_called_once()
        self.assertEqual(restrict.call_args.args[1], 4242)
        self.assertIn("2 ghante", self._last_sent())

    def test_tmute_invalid_duration(self):
        with self._member_map(), patch.object(
                moderation.tg_api, "restrict_chat_member") as restrict:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/tmute", reply_to=_user_msg(TARGET, "Sam")), OWNER, CHAT))
            restrict.assert_not_called()
        self.assertIn("Usage", self._last_sent())

    def test_unmute_restores_permissions(self):
        reply = _user_msg(TARGET, "Sam")
        with self._member_map(), patch.object(
                moderation.tg_api, "restrict_chat_member",
                return_value={"ok": True}) as restrict:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/unmute", reply_to=reply), OWNER, CHAT))
        restrict.assert_called_once()
        perms = restrict.call_args.args[2]
        self.assertTrue(all(perms.values()))
        self.assertTrue(perms["can_send_messages"])
        self.assertIn("unmute", self._last_sent())

    # ---------- /ban /unban (owner only) ----------

    def test_ban_by_non_owner_refused(self):
        reply = _user_msg(TARGET, "Sam")
        with self._member_map(), patch.object(
                moderation.tg_api, "ban_chat_member") as ban:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/ban", uid=STRANGER, name="Rando", reply_to=reply),
                STRANGER, CHAT))
            ban.assert_not_called()
        self.assertIn("sirf owner", self._last_sent())

    def test_ban_by_admin_refused(self):
        with self._member_map(), patch.object(
                moderation.tg_api, "ban_chat_member") as ban:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/ban 4242", uid=ADMIN, name="Admin"), ADMIN, CHAT))
            ban.assert_not_called()
        self.assertIn("sirf owner", self._last_sent())

    def test_ban_by_owner(self):
        with self._member_map(), patch.object(
                moderation.tg_api, "ban_chat_member",
                return_value={"ok": True}) as ban:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/ban 4242 breaking rules", uid=OWNER), OWNER, CHAT))
        ban.assert_called_once()
        args, kwargs = ban.call_args
        self.assertEqual(args[0], CHAT)
        self.assertEqual(args[1], 4242)
        self.assertIsNone(kwargs.get("until_date"))
        self.assertIn("🔨", self._last_sent())
        self.assertIn("ban kar diya", self._last_sent())
        self.assertEqual(
            db.get_db()["mod_actions"].count_documents({"action": "ban"}), 1)

    def test_unban_owner_only(self):
        with self._member_map(), patch.object(
                moderation.tg_api, "unban_chat_member",
                return_value={"ok": True}) as unban:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/unban 4242"), OWNER, CHAT))
        unban.assert_called_once()
        self.assertEqual(unban.call_args.args[1], 4242)
        self.assertIn("unban kar diya", self._last_sent())

        with self._member_map(), patch.object(
                moderation.tg_api, "unban_chat_member") as unban2:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/unban 4242", uid=STRANGER, name="Rando"), STRANGER, CHAT))
            unban2.assert_not_called()
        self.assertIn("sirf owner", self._last_sent())

    def test_ban_owner_target_refused(self):
        reply = _user_msg(OWNER, "Boss")
        with self._member_map({OWNER: MEMBER}), patch.object(
                moderation.tg_api, "ban_chat_member") as ban:
            # issuer is the admin; target is the owner
            self.assertTrue(moderation.handle_mod_command(
                _msg("/ban", uid=ADMIN, name="Admin", reply_to=reply),
                ADMIN, CHAT))
            ban.assert_not_called()

    # ---------- /delete ----------

    def test_delete_requires_reply(self):
        with self._member_map(), patch.object(
                moderation.tg_api, "delete_message") as delete:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/delete"), OWNER, CHAT))
            delete.assert_not_called()
        self.assertIn("reply karke", self._last_sent())

    def test_delete_removes_replied_message(self):
        reply = _user_msg(TARGET, "Sam", message_id=77)
        with self._member_map(), patch.object(
                moderation.tg_api, "delete_message",
                return_value={"ok": True}) as delete:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/del", reply_to=reply), OWNER, CHAT))
        delete.assert_called_once_with(CHAT, 77)
        self.assertIn("delete kar diya", self._last_sent())

    # ---------- permissions ----------

    def test_non_privileged_denied(self):
        with self._member_map(), patch.object(
                moderation.tg_api, "restrict_chat_member") as restrict:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/mute 4242", uid=STRANGER, name="Rando"), STRANGER, CHAT))
            restrict.assert_not_called()
        self.assertIn("owner/admin", self._last_sent())

    def test_secondary_admin_allowed_for_mute(self):
        with self._member_map(), patch.object(
                moderation.tg_api, "restrict_chat_member",
                return_value={"ok": True}) as restrict:
            self.assertTrue(moderation.handle_mod_command(
                _msg("/mute 4242", uid=ADMIN, name="Admin"), ADMIN, CHAT))
        restrict.assert_called_once()

    def test_stopall_owner_only(self):
        moderation.add_filter("x", {"kind": "text", "text": "y"}, OWNER)
        with self._member_map():
            self.assertTrue(moderation.handle_mod_command(
                _msg("/stopall", uid=ADMIN, name="Admin"), ADMIN, CHAT))
        self.assertIn("x", moderation.get_filters())
        self.assertIn("sirf owner", self._last_sent())

        with self._member_map():
            self.assertTrue(moderation.handle_mod_command(
                _msg("/stopall"), OWNER, CHAT))
        self.assertEqual(moderation.get_filters(), {})

    def test_unknown_command_not_handled(self):
        with self._member_map():
            self.assertFalse(moderation.handle_mod_command(
                _msg("/ask hello"), OWNER, CHAT))
            self.assertFalse(moderation.handle_mod_command(
                _msg("hello there"), OWNER, CHAT))
        self.sent.assert_not_called()

    # ---------- /modlog ----------

    def test_modlog_lists_actions(self):
        with self._member_map(), \
                patch.object(moderation.tg_api, "restrict_chat_member",
                             return_value={"ok": True}), \
                patch.object(moderation.tg_api, "ban_chat_member",
                             return_value={"ok": True}):
            moderation.handle_mod_command(
                _msg("/mute 4242 spam", uid=OWNER), OWNER, CHAT)
            moderation.handle_mod_command(
                _msg("/ban 4343", uid=OWNER), OWNER, CHAT)
            moderation.handle_mod_command(_msg("/modlog"), OWNER, CHAT)
        text = self._last_sent()
        self.assertIn("mute", text)
        self.assertIn("ban", text)
        self.assertIn("user 4242", text)


if __name__ == "__main__":
    unittest.main()
