import unittest

import triggers


class TriggerTests(unittest.TestCase):
    def test_greetings_and_chatter_do_not_trigger(self):
        for text in ("hyy", "hello!", "good morning", "thanks", "ok"):
            with self.subTest(text=text):
                self.assertFalse(triggers.is_question_or_request(text))

    def test_questions_and_requests_trigger(self):
        for text in (
            "What time is the event?",
            "Kya tum admin ho",
            "please help me",
            "APK bhej do",
            "group scan karo",
        ):
            with self.subTest(text=text):
                self.assertTrue(triggers.is_question_or_request(text))

    def test_commands_are_not_natural_triggers(self):
        self.assertFalse(triggers.is_question_or_request("/start"))
        self.assertFalse(triggers.is_question_or_request("/ask hi"))

    def test_live_group_scan_routing(self):
        self.assertTrue(triggers.is_group_scan_request("Are you admin?"))
        self.assertTrue(triggers.is_group_scan_request("group permissions check"))
        self.assertFalse(triggers.is_group_scan_request("What is an admin?"))


if __name__ == "__main__":
    unittest.main()