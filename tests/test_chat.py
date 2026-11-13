"""Bug fixing: conversation, device matching, and the boundary between them.

The Assistant page answers `hello` like an assistant, but only because the
message took the *other door*: a text-only reply with no tools, no intent and
no path to an appliance. These tests pin down both sides of that split:

    a home command  -> rules / whitelist / approval / audited execution
    anything else   -> one short text reply, actions = []

Everything runs offline against the simulator and the scripted FakeLLM, so the
suite stays free, deterministic and usable in CI.
"""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import chat, config, db, nlu  # noqa: E402
from app.agent import Agent  # noqa: E402
from app.devices import MockAdapter  # noqa: E402
from app.devices import mock as mock_mod  # noqa: E402
from tests.fake_llm import FakeLLM, prose  # noqa: E402


def offline_cfg() -> dict:
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["llm"]["enabled"] = False
    cfg["llm"]["api_key"] = ""
    cfg["chat"]["enabled"] = False
    return cfg


def llm_cfg(fake: FakeLLM, mode: str = "agent") -> dict:
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["llm"] = {"enabled": True, "api_key": "test-key", "model": "fake",
                  "base_url": fake.base_url, "timeout": 5}
    cfg["chat"] = {"enabled": True, "temperature": 0.6,
                   "max_tokens": 200, "timeout": 5}
    cfg["agent"]["mode"] = mode
    return cfg


class AgentIsolated(unittest.TestCase):
    """Own database and own simulator state, so no test can leak a device."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._orig = (db.DATA_DIR, db.DB_PATH, mock_mod.STATE_PATH)
        db.DATA_DIR = self.tmp
        db.DB_PATH = self.tmp / "test.db"
        mock_mod.STATE_PATH = self.tmp / "mock_state.json"
        db.init()
        self.adapter = MockAdapter()
        self.cfg = offline_cfg()
        self.agent = Agent(self.adapter, self.cfg)

    def tearDown(self):
        self.adapter.stop()
        (db.DATA_DIR, db.DB_PATH, mock_mod.STATE_PATH) = self._orig
        self._tmp.cleanup()


# ---------------------------------------------------------------- chat layer

class ConversationLayerTest(unittest.TestCase):
    def test_the_four_kinds_are_recognised(self):
        for text, kind in [("hello", "greeting"), ("hi there", "greeting"),
                           ("what can you do?", "capabilities"),
                           ("thank you", "thanks"), ("thanks a lot", "thanks"),
                           ("bye", "farewell"), ("goodbye", "farewell"),
                           ("how are you?", "smalltalk"),
                           ("tell me a joke", "smalltalk")]:
            self.assertEqual(chat.classify(text), kind, text)
            self.assertTrue(chat.is_conversation(text), text)

    def test_a_bare_unrecognised_phrase_is_still_conversation(self):
        # Short, harmless English with no home word: better a short answer
        # and a steer back to the house than "I did not understand".
        self.assertTrue(chat.is_conversation("hmm ok"))
        self.assertTrue(chat.is_conversation("nice"))
        # A home pronoun is deliberately excluded: "it" usually means the
        # appliance from the previous turn and must stay on the command path.
        self.assertFalse(chat.is_conversation("lovely day, isn't it"))

    def test_scenes_are_commands_not_greetings(self):
        # scenes.detect() switches real appliances for these. A chat model
        # must never see them, however much they sound like small talk.
        for text in ["good morning", "good night", "I'm leaving",
                     "going out", "away mode", "wake the house up"]:
            self.assertFalse(chat.is_conversation(text), text)

    def test_home_commands_never_become_small_talk(self):
        for text in ["hi, living room AC 25", "turn on the purifier",
                     "the whole house is too warm", "what's the temperature",
                     "set the bedroom AC to 22", "status of the washer",
                     "what's the weather like", "play some music",
                     "cancel all scheduled tasks please and thank you"]:
            self.assertFalse(chat.is_conversation(text), text)

    def test_empty_is_not_conversation(self):
        self.assertFalse(chat.is_conversation(""))
        self.assertFalse(chat.is_conversation("   "))

    def test_clean_rejects_anything_that_is_not_prose(self):
        self.assertEqual(chat._clean(""), "")
        self.assertEqual(chat._clean('{"intent": "control"}'), "")
        self.assertEqual(chat._clean("```\nhello\n```"), "hello")
        self.assertEqual(chat._clean("I have turned on the AC"), "")
        self.assertEqual(chat._clean("Done — the dryer is now off"), "")
        self.assertEqual(chat._clean("Power: on"), "")
        self.assertEqual(chat._clean('"Hi there!"'), "Hi there!")
        self.assertEqual(chat._clean("Hi   there"), "Hi there")

    def test_offline_reply_always_answers(self):
        cfg = offline_cfg()
        for kind in ("greeting", "capabilities", "thanks", "farewell",
                     "smalltalk", "unknown"):
            self.assertTrue(chat.local_reply(kind).strip())
            self.assertTrue(chat.reply(cfg, "hello").strip())

    def test_the_model_gets_no_tools_and_no_json_mode(self):
        with FakeLLM() as fake:
            fake.script = [prose("Hi! I can help with the appliances.")]
            cfg = llm_cfg(fake)
            got = chat.reply(cfg, "hello")

            self.assertEqual(got, "Hi! I can help with the appliances.")
            self.assertEqual(len(fake.requests), 1)
            body = fake.requests[0]
            self.assertNotIn("tools", body)
            self.assertNotIn("tool_choice", body)
            self.assertNotIn("response_format", body)
            self.assertEqual(len(body["messages"]), 2)
            self.assertEqual(body["messages"][0]["role"], "system")
            self.assertEqual(body["messages"][1]["content"], "hello")

    def test_a_model_that_claims_an_action_is_not_believed(self):
        with FakeLLM() as fake:
            fake.script = [prose("I have switched the cooktop on for you.")]
            got = chat.reply(llm_cfg(fake), "hello")
            self.assertEqual(got, chat.local_reply("greeting"))

    def test_a_broken_endpoint_falls_back_to_local_text(self):
        cfg = offline_cfg()
        cfg["llm"] = {"enabled": True, "api_key": "x", "model": "fake",
                      "base_url": "http://127.0.0.1:9/v1", "timeout": 5}
        cfg["chat"] = {"enabled": True, "timeout": 1}
        self.assertEqual(chat.reply(cfg, "thank you"),
                         chat.local_reply("thanks"))


# ---------------------------------------------------------------- agent split

class ChatAgentTest(AgentIsolated):
    def test_hello_is_answered_like_an_assistant(self):
        r = self.agent.handle("hello")
        self.assertTrue(r["ok"])
        self.assertEqual(r["tier"], "chat")
        self.assertEqual(r["intent_name"], "chat")
        self.assertEqual(r["actions"], [])
        self.assertIn("assistant", r["reply"].lower())
        self.assertNotIn("I can turn devices on and off", r["reply"])

    def test_capabilities_lists_only_what_is_implemented(self):
        r = self.agent.handle("what can you do?")
        self.assertEqual(r["tier"], "chat")
        for word in ("control", "appliances", "schedules", "approval"):
            self.assertIn(word, r["reply"].lower())
        self.assertNotIn("weather", r["reply"].lower())

    def test_thanks_and_farewell_are_answered_normally(self):
        self.assertIn("welcome", self.agent.handle("thank you")["reply"].lower())
        self.assertIn("goodbye", self.agent.handle("bye")["reply"].lower())

    def test_small_talk_answers_briefly_and_steers_back(self):
        r = self.agent.handle("how are you?")
        self.assertEqual(r["tier"], "chat")
        self.assertIn("home", r["reply"].lower())
        self.assertLess(len(r["reply"]), 240)

    def test_chat_never_touches_the_adapter(self):
        with mock.patch.object(self.adapter, "apply",
                               wraps=self.adapter.apply) as apply:
            for text in ("hello", "what can you do?", "thank you", "bye",
                         "how are you?", "tell me a joke"):
                self.agent.handle(text)
        self.assertEqual(apply.call_count, 0)

    def test_chat_writes_no_device_action_to_the_audit_log(self):
        for text in ("hello", "thank you", "bye"):
            self.agent.handle(text)
        events = [row["event_type"] for row in db.recent_audit(limit=200)]
        self.assertFalse([e for e in events if e.startswith("action.")], events)

    def test_chat_does_not_steal_the_last_device(self):
        self.agent.handle("turn on the living room AC")
        self.agent.handle("hello")
        r = self.agent.handle("set it to 22 degrees")
        self.assertEqual(r["intent_name"], "control")
        self.assertEqual(self.adapter.get_state("ac_living")["target_temp"], 22)

    def test_a_scene_is_still_a_command(self):
        r = self.agent.handle("good morning")
        self.assertEqual(r["intent_name"], "scene")
        self.assertNotEqual(r["tier"], "chat")
        self.assertTrue(self.adapter.get_state("ac_living")["power"])

    def test_a_command_with_home_words_stays_on_the_command_path(self):
        r = self.agent.handle("hi, living room AC 25")
        self.assertNotEqual(r["tier"], "chat")
        self.assertEqual(r["actions"], [])

    def test_out_of_scope_topics_keep_their_honest_answer(self):
        r = self.agent.handle("what's the weather like")
        self.assertNotEqual(r["tier"], "chat")
        self.assertIn("cannot see the weather", r["reply"])

    def test_with_llm_enabled_a_greeting_never_reaches_the_tool_loop(self):
        """The strongest form of the boundary: one call, with no tools.

        In agent mode an `unknown` parse would normally go through the
        deliberating tier and then the tool loop. A greeting must be caught
        before either, so FakeLLM sees exactly one plain-text request.
        """
        with FakeLLM() as fake:
            fake.script = [prose("Hello! I can help with the appliances.")]
            agent = Agent(self.adapter, llm_cfg(fake))
            r = agent.handle("hello")

            self.assertEqual(r["tier"], "chat")
            self.assertEqual(r["actions"], [])
            self.assertEqual(len(fake.requests), 1)
            self.assertNotIn("tools", fake.requests[0])
            self.assertEqual(r["reply"], "Hello! I can help with the appliances.")
        self.assertFalse(self.adapter.get_state("ac_living")["power"])


# ---------------------------------------------------------------- device names

class DeviceMatchingTest(AgentIsolated):
    def test_kitchen_temperature_is_not_answered_by_the_range_hood(self):
        for text in ("what's the temperature in the kitchen",
                     "how hot is the kitchen"):
            r = self.agent.handle(text)
            self.assertEqual(r["intent_name"], "query")
            self.assertIn("Kitchen AC", r["reply"], text)
            self.assertNotIn("Range Hood", r["reply"], text)
            self.assertNotIn("Multiple devices matched", r["reply"], text)

    def test_an_unknown_device_is_not_the_last_device(self):
        self.agent.handle("turn on the living room AC")
        # Only the written fields: the simulator drifts current_temp and
        # humidity on a background thread, so comparing the whole state
        # would make the assertion depend on wall-clock time.
        before = self.adapter.get_state("ac_living")
        written_before = (before["power"], before["target_temp"])

        for text in ("set the TV to 24 degrees", "turn on the TV",
                     "set the toaster to 20 degrees", "turn on the banana"):
            r = self.agent.handle(text)
            self.assertNotEqual(r["intent_name"], "control", text)
            self.assertIn("Which device did you mean?", r["reply"], text)

        after = self.adapter.get_state("ac_living")
        self.assertEqual((after["power"], after["target_temp"]), written_before)

    def test_the_unknown_device_is_named_in_the_clarification(self):
        self.agent.handle("turn on the living room AC")
        self.assertIn('"TV"', self.agent.handle("set the TV to 24 degrees")["reply"])
        self.assertIn('"banana"', self.agent.handle("turn on the banana")["reply"])

    def test_an_unknown_device_status_question_does_not_guess(self):
        r = self.agent.handle("is the TV on?")
        self.assertEqual(r["intent_name"], "query")
        self.assertIn('"TV"', r["reply"])
        self.assertNotIn("Living Room AC is", r["reply"])

    def test_a_pronoun_still_refers_to_the_last_device(self):
        self.agent.handle("turn on the bedroom AC")
        r = self.agent.handle("set it to 23 degrees")
        self.assertEqual(r["intent_name"], "control")
        self.assertEqual(self.adapter.get_state("ac_bedroom")["target_temp"], 23)

    def test_a_bare_ac_still_uses_the_default_room(self):
        r = self.agent.handle("turn on the AC")
        self.assertEqual(r["intent_name"], "control")
        self.assertTrue(self.adapter.get_state("ac_living")["power"])


class NluHelperTest(unittest.TestCase):
    def test_names_unknown_device_ignores_food_words(self):
        # "banana" is in the food table, but it is not an appliance here.
        devices = [{"id": "ac_living", "name": "Living Room AC",
                    "type": "air_conditioner", "room": "Living Room"},
                   {"id": "fridge", "name": "Kitchen Fridge",
                    "type": "refrigerator", "room": "Kitchen"}]
        self.assertEqual(nlu.names_unknown_device("turn on the banana", devices),
                         "banana")
        self.assertEqual(nlu.names_unknown_device("set the TV to 24", devices),
                         "TV")
        self.assertIsNone(nlu.names_unknown_device("set the temperature to 24",
                                                   devices))
        self.assertIsNone(nlu.names_unknown_device("turn on the fridge", devices))


if __name__ == "__main__":
    unittest.main()
