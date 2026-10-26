"""End-to-end tests for the agent, the simulator, and the scheduler.

Uses a temporary database and a temporary simulator state file, so the real
data/ directory is never touched.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import build, config, db  # noqa: E402
from app.agent import Agent  # noqa: E402
from app.devices import MockAdapter  # noqa: E402
from app.devices import mock as mock_mod  # noqa: E402
from app.scheduler import Scheduler  # noqa: E402


class AgentTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

        # isolate persistence
        self._orig_data_dir = db.DATA_DIR
        self._orig_db_path = db.DB_PATH
        self._orig_state_path = mock_mod.STATE_PATH
        db.DATA_DIR = self.tmp
        db.DB_PATH = self.tmp / "test.db"
        mock_mod.STATE_PATH = self.tmp / "mock_state.json"

        db.init()
        self.adapter = MockAdapter()
        cfg = dict(config.DEFAULTS)
        cfg["agent"] = {"default_room": "Living Room"}
        self.agent = Agent(self.adapter, cfg)

    def tearDown(self):
        self.adapter.stop()
        db.DATA_DIR = self._orig_data_dir
        db.DB_PATH = self._orig_db_path
        mock_mod.STATE_PATH = self._orig_state_path
        self._tmp.cleanup()


class ControlTest(AgentTestBase):
    def test_turn_on_reports_state(self):
        r = self.agent.handle("turn on the living room AC")
        self.assertTrue(r["ok"])
        self.assertIn("Living Room AC is on", r["reply"])
        self.assertTrue(self.adapter.get_state("ac_living")["power"])

    def test_reference_resolves_to_last_device(self):
        self.agent.handle("turn on the bedroom AC")
        r = self.agent.handle("set it to 22 degrees")
        self.assertTrue(r["ok"])
        self.assertEqual(self.adapter.get_state("ac_bedroom")["target_temp"], 22)

    def test_unsupported_capability_is_rejected(self):
        r = self.agent.handle("set the purifier to 24 degrees")
        self.assertFalse(r["ok"])
        self.assertIn("does not support", r["reply"])

    def test_temperature_is_clamped_by_device(self):
        self.agent.handle("turn on the living room AC")
        self.agent.handle("set it to 30 degrees")
        self.assertEqual(self.adapter.get_state("ac_living")["target_temp"], 30)


class QueryTest(AgentTestBase):
    def test_query_off_device(self):
        r = self.agent.handle("what's the temperature in the bedroom?")
        self.assertTrue(r["ok"])
        self.assertIn("Bedroom AC", r["reply"])

    def test_query_after_turning_on(self):
        self.agent.handle("turn on the living room AC")
        r = self.agent.handle("what's the temperature?")
        self.assertIn("running", r["reply"])


class ScheduleTest(AgentTestBase):
    def test_daily_job_is_persisted(self):
        r = self.agent.handle("turn off the AC every night at 11 pm")
        self.assertTrue(r["ok"])
        jobs = db.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["rule"], {"type": "daily", "at": "23:00"})
        self.assertEqual(jobs[0]["device_id"], "ac_living")

    def test_list_and_cancel(self):
        self.agent.handle("turn off the AC every night at 11 pm")
        r = self.agent.handle("what scheduled tasks do I have?")
        self.assertIn("scheduled task", r["reply"])
        r = self.agent.handle("cancel all scheduled tasks")
        self.assertIn("Cleared", r["reply"])
        self.assertEqual(db.list_jobs(), [])

    def test_cancel_specific_device(self):
        self.agent.handle("turn off the AC every night at 11 pm")
        self.agent.handle("turn off the washer at 11 pm")
        r = self.agent.handle("cancel the washer schedule")
        self.assertTrue(r["ok"])
        self.assertEqual(len(db.list_jobs()), 1)


class SchedulerTest(AgentTestBase):
    def _make_job(self, rule: dict, actions=None, seconds_ahead=0):
        run_at = (datetime.now() + timedelta(seconds=seconds_ahead)).isoformat(timespec="seconds")
        rule = dict(rule)
        if rule["type"] == "once":
            rule["run_at"] = run_at
        return db.add_job("washer", "Balcony Washer",
                          actions or [{"action": "power", "value": True}],
                          rule, run_at, "test", "test")

    def test_one_time_job_runs_and_disables(self):
        job_id = self._make_job({"type": "once"})
        sched = Scheduler(self.adapter)
        sched._run_job(db.get_job(job_id), datetime.now())

        job = db.get_job(job_id)
        self.assertEqual(job["enabled"], 0)
        self.assertIn("Success", job["last_result"])
        self.assertTrue(self.adapter.get_state("washer")["power"])

    def test_recurring_job_stays_enabled_and_advances(self):
        job_id = self._make_job({"type": "daily", "at": "23:00"})
        before = db.get_job(job_id)["next_run_at"]
        sched = Scheduler(self.adapter)
        sched._run_job(db.get_job(job_id), datetime.now())

        job = db.get_job(job_id)
        self.assertEqual(job["enabled"], 1)
        self.assertNotEqual(job["next_run_at"], before)

    def test_run_is_pushed_into_chat(self):
        job_id = self._make_job({"type": "once"})
        Scheduler(self.adapter)._run_job(db.get_job(job_id), datetime.now())
        messages = db.recent_messages(5)
        self.assertTrue(any(m["role"] == "system" for m in messages))

    def test_offline_device_is_recorded_as_failure(self):
        job_id = self._make_job({"type": "once"}, actions=[{"action": "power", "value": True}])
        self.adapter.list_devices = lambda: []  # pretend the device vanished
        Scheduler(self.adapter)._run_job(db.get_job(job_id), datetime.now())
        self.assertIn("Failed", db.get_job(job_id)["last_result"])


class GroupControlTest(AgentTestBase):
    def test_room_level_command_hits_every_device_in_it(self):
        self.agent.handle("turn on the living room AC")
        self.agent.handle("turn on the purifier")
        r = self.agent.handle("turn off everything in the living room")
        self.assertTrue(r["ok"])
        self.assertFalse(self.adapter.get_state("ac_living")["power"])
        self.assertFalse(self.adapter.get_state("purifier")["power"])
        self.assertIn("2 of 2", r["reply"])

    def test_partial_failure_is_reported_per_device(self):
        # the purifier has no temperature capability
        r = self.agent.handle("set everything in the living room to 24 degrees")
        self.assertIn("1 of 2", r["reply"])
        self.assertIn("skipped", r["reply"])
        self.assertEqual(self.adapter.get_state("ac_living")["target_temp"], 24)

    def test_skip_reason_does_not_repeat_the_device_name(self):
        r = self.agent.handle("set everything in the living room to 24 degrees")
        purifier_line = next(
            ln for ln in r["reply"].splitlines() if "Purifier" in ln)
        self.assertEqual(purifier_line.count("Purifier"), 1, purifier_line)
        self.assertIn("does not support", purifier_line)

    def test_group_schedule_creates_one_job_per_device(self):
        self.agent.handle("turn off everything in the living room at 11 pm")
        jobs = db.list_jobs()
        self.assertEqual(len(jobs), 2)
        self.assertEqual({j["device_id"] for j in jobs}, {"ac_living", "purifier"})


class DialogueStateTest(AgentTestBase):
    def test_comfort_without_device_asks_then_completes(self):
        r = self.agent.handle("it's stuffy in here")
        self.assertEqual(r["intent_name"], "need_target")
        self.assertIn("Which", r["reply"])

        r2 = self.agent.handle("bedroom")
        self.assertTrue(r2["ok"])
        state = self.adapter.get_state("ac_bedroom")
        self.assertTrue(state["power"])
        self.assertEqual(state["mode"], "cool")
        self.assertEqual(state["target_temp"], 24)

    def test_missing_action_is_filled_by_next_turn(self):
        r = self.agent.handle("set a timer for the washer at 11 pm")
        self.assertEqual(r["intent_name"], "need_action")

        r2 = self.agent.handle("turn it off")
        self.assertTrue(r2["ok"])
        jobs = db.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["device_id"], "washer")
        self.assertEqual(jobs[0]["actions"], [{"action": "power", "value": False}])

    def test_pending_request_can_be_cancelled(self):
        self.agent.handle("it's stuffy in here")
        r = self.agent.handle("never mind")
        self.assertTrue(r["ok"])
        self.assertIn("dropped", r["reply"].lower())

    def test_pending_state_is_not_leaked_into_unrelated_turns(self):
        self.agent.handle("it's stuffy in here")
        self.agent.handle("what's the temperature in the bedroom?")
        # the pending comfort request must not hijack an unrelated question
        self.assertFalse(self.adapter.get_state("ac_bedroom")["power"])


class CatchUpTest(AgentTestBase):
    def test_overdue_one_time_job_is_closed_not_fired(self):
        past = (datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds")
        job_id = db.add_job("washer", "Balcony Washer",
                            [{"action": "power", "value": True}],
                            {"type": "once", "run_at": past}, past, "past", "test")

        Scheduler(self.adapter)._catch_up()

        job = db.get_job(job_id)
        self.assertEqual(job["enabled"], 0)
        self.assertIn("Missed", job["last_result"])
        self.assertFalse(self.adapter.get_state("washer")["power"])

    def test_overdue_recurring_job_is_rescheduled(self):
        past = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
        job_id = db.add_job("ac_living", "Living Room AC",
                            [{"action": "power", "value": False}],
                            {"type": "daily", "at": "07:00"}, past, "daily", "test")

        Scheduler(self.adapter)._catch_up()

        job = db.get_job(job_id)
        self.assertEqual(job["enabled"], 1)
        self.assertIn("Missed", job["last_result"])
        self.assertGreater(job["next_run_at"],
                           datetime.now().isoformat(timespec="seconds"))

    def test_future_jobs_are_untouched(self):
        future = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
        job_id = db.add_job("ac_living", "Living Room AC",
                            [{"action": "power", "value": False}],
                            {"type": "once", "run_at": future}, future, "later", "test")

        Scheduler(self.adapter)._catch_up()

        job = db.get_job(job_id)
        self.assertEqual(job["enabled"], 1)
        self.assertEqual(job["next_run_at"], future)


class PhrasingTest(unittest.TestCase):
    def test_single_action_keeps_the_device_name(self):
        txt = Agent.phrase("Living Room AC", [{"action": "power", "value": False}])
        self.assertEqual(txt, "turn off the Living Room AC")

    def test_multiple_actions_name_the_device_once(self):
        txt = Agent.phrase("Living Room AC", [
            {"action": "temperature", "value": 24},
            {"action": "fan", "value": "low"},
        ])
        self.assertEqual(txt.count("Living Room AC"), 1, txt)
        self.assertIn("24\u00b0C", txt)
        self.assertIn("low", txt)

    def test_mode_and_fan_read_as_one_sentence(self):
        txt = Agent.phrase("Living Room AC", [
            {"action": "mode", "value": "heat"},
            {"action": "fan", "value": "low"},
        ])
        self.assertEqual(txt, "switch the Living Room AC to heating, fan low")


class GroupOffTest(AgentTestBase):
    """A flat action list cannot say "except the fridge", so the exception lives
    in one table. Without it, "everything off" switched the fridge off while the
    scene layer spared it — the same sentence meaning two things."""

    def test_everything_off_spares_the_fridge(self):
        r = self.agent.handle("turn off everything")

        self.assertFalse(self.adapter.get_state("ac_living")["power"])
        self.assertTrue(self.adapter.get_state("fridge")["power"])
        self.assertIn("Fridge stays on", r["reply"])

    def test_naming_the_fridge_still_switches_it_off(self):
        """The exception is about the word "everything", not about the fridge."""
        self.agent.handle("turn off the fridge")
        self.assertFalse(self.adapter.get_state("fridge")["power"])

    def test_a_room_group_is_unaffected(self):
        r = self.agent.handle("turn off everything in the living room")
        self.assertNotIn("stays on", r["reply"])
        self.assertFalse(self.adapter.get_state("purifier")["power"])


class SmellTest(AgentTestBase):
    """A smell is an air problem, and the air is the purifier's job.

    The bug this covers was destructive rather than cosmetic: "the kitchen
    smells" resolved to the fridge, because the fridge is the only device in the
    kitchen, and the tool loop switched the fridge off to help. Two runs in three.
    """

    def test_a_smelly_room_turns_on_the_purifier(self):
        r = self.agent.handle("the kitchen smells")

        self.assertEqual(r["intent_name"], "control")
        self.assertTrue(self.adapter.get_state("purifier")["power"])
        self.assertEqual(self.adapter.get_state("purifier")["fan"], "high")

    def test_the_fridge_is_not_the_answer_to_a_smell(self):
        before = self.adapter.get_state("fridge")["power"]
        self.agent.handle("the kitchen smells")
        self.assertEqual(self.adapter.get_state("fridge")["power"], before)

    def test_an_appliance_that_smells_is_not_ours_to_fix(self):
        """ "The fridge smells" is about the fridge. Turning on a purifier would
        be answering a question the user did not ask."""
        r = self.agent.handle("the fridge smells")
        self.assertNotEqual(r["intent_name"], "control")

    def test_an_explicit_off_still_wins(self):
        self.adapter.apply("purifier", [{"action": "power", "value": True}])
        r = self.agent.handle("turn off the purifier, it stinks")

        self.assertEqual(r["intent_name"], "control")
        self.assertFalse(self.adapter.get_state("purifier")["power"])


class ComfortDialogueTest(AgentTestBase):
    def test_clammy_is_a_humidity_complaint(self):
        """Dry, not cool. The loop answered this with `mode=auto`, which is not
        wrong so much as not the point."""
        r = self.agent.handle("it feels clammy in the bedroom")

        self.assertEqual(r["intent_name"], "control")
        self.assertEqual(self.adapter.get_state("ac_bedroom")["mode"], "dry")

    def test_stuffy_asks_which_room_and_then_acts(self):
        """The documented multi-turn flow, and the reason a comfort request is
        not handed to the loop: only the room is missing, and no tool can read
        which room someone is standing in."""
        first = self.agent.handle("it's stuffy in here")
        self.assertEqual(first["intent_name"], "need_target")
        self.assertIn("Which room", first["reply"])

        second = self.agent.handle("the bedroom")
        self.assertEqual(second["intent_name"], "control")
        self.assertTrue(self.adapter.get_state("ac_bedroom")["power"])
        self.assertEqual(self.adapter.get_state("ac_bedroom")["mode"], "cool")

    def test_a_room_named_as_a_sauna_is_cooled(self):
        """The sentence this project's own pitch uses as its example. It is a
        comparison, not a feeling, and none of the feeling words are in it —
        so it fell through all three tiers to the generic help text, which
        reads as "I did not understand"."""
        r = self.agent.handle("the bedroom is a sauna")

        self.assertEqual(r["intent_name"], "control")
        state = self.adapter.get_state("ac_bedroom")
        self.assertTrue(state["power"])
        self.assertEqual(state["mode"], "cool")
        self.assertEqual(state["target_temp"], 24)

    def test_an_oven_you_cook_in_is_not_a_room_that_is_hot(self):
        """The "is/like a" prefix is what keeps cooking out. An oven you cook in
        is "in the oven"; were the bare noun enough, every cooking sentence would
        try to cool the room it was spoken in."""
        r = self.agent.handle("put the chicken in the oven at 7 pm")

        self.assertNotEqual(r["intent_name"], "control")
        self.assertFalse(self.adapter.get_state("ac_living")["power"])

    def test_a_room_with_no_air_conditioner_is_not_the_fridge(self):
        """Resolved by room alone, a complaint about a room with no AC used to
        land on the Kitchen Fridge — the only appliance in that room — and the
        command layer refused `mode`, which reads as a broken assistant and is
        the last guardrail before a refrigerator is asked to hold 24 °C. The
        kitchen has an AC now, so the balcony — washer, dryer, dehumidifier, and
        none of them able to cool a room — is the room to test."""
        r = self.agent.handle("the balcony is stuffy")

        self.assertEqual(r["intent_name"], "control")
        self.assertIn("no air conditioner", r["reply"])
        self.assertEqual(self.adapter.get_state("fridge")["fridge_temp"], 3)

    def test_the_comparison_does_not_reach_the_purifier(self):
        """A hot room is a cooling job, not an air-quality one — the two rules
        share the word "air" in conversation but not in intent."""
        r = self.agent.handle("the kitchen is an oven")

        self.assertEqual(r["intent_name"], "control")
        self.assertNotIn("purifier", r["reply"].lower())
        self.assertFalse(self.adapter.get_state("purifier")["power"])


class Tier2ClarifyTest(AgentTestBase):
    def test_successful_plan_does_not_carry_the_rule_fallback(self):
        """`hi, living room Ac 25` missed the rules, so tier 2 answered it.

        The rule layer's clarify on a miss is its help text. Inheriting it
        printed "Done -- set the Living Room AC to 25C" followed by "I can turn
        devices on and off..." -- one reply contradicting itself.
        """
        cfg = dict(config.DEFAULTS)
        cfg["llm"] = dict(config.DEFAULTS["llm"], enabled=True, api_key="test-key")
        cfg["agent"] = {"default_room": "Living Room"}
        agent = Agent(self.adapter, cfg)
        plan = {"intent": "control", "device_id": "ac_living",
                "device_name": "Living Room AC",
                "targets": [self.adapter.get_device("ac_living")],
                "actions": [{"action": "temperature", "value": 25}], "rule": None}

        with mock.patch("app.llm.plan", return_value=plan):
            r = agent.handle("hi, living room Ac 25")

        self.assertEqual(r["tier"], "llm")
        self.assertTrue(r["ok"])
        self.assertIn("25", r["reply"])
        self.assertNotIn("I can turn devices on and off", r["reply"])


class OutOfScopeTest(AgentTestBase):
    def test_weather_names_the_missing_data(self):
        """The generic help text reads as "I did not understand". This sentence
        was understood; the house simply has no thermometer outside."""
        r = self.agent.handle("what's the weather like tomorrow")

        self.assertEqual(r["tier"], "rules")
        self.assertIn("weather", r["reply"])
        self.assertNotIn("I can turn devices on and off", r["reply"])

    def test_music_is_refused_by_name(self):
        r = self.agent.handle("play some music")

        self.assertIn("music", r["reply"])

    def test_a_command_that_mentions_rain_is_still_a_command(self):
        """The table is checked last, after every control path, so a passing
        mention of the weather does not turn an order into a refusal."""
        r = self.agent.handle("turn on the purifier, it's raining")

        self.assertEqual(r["intent_name"], "control")
        self.assertTrue(self.adapter.get_state("purifier")["power"])

    def test_loop_is_told_it_cannot_see_outside(self):
        from app.agent_loop import SYSTEM_PROMPT

        self.assertIn("cannot see the weather", SYSTEM_PROMPT)


class TypoToleranceTest(AgentTestBase):
    def test_misspelt_room_still_targets_that_room(self):
        """The dangerous typo: a room the parser cannot match is not a miss, it
        is a tie between two ACs that breaks towards the default room -- so the
        wrong appliance gets set, in a millisecond, with a confident reply."""
        r = self.agent.handle("set the bedrom ac to 22 degres")

        self.assertEqual(r["tier"], "rules")
        self.assertEqual(self.adapter.get_state("ac_bedroom")["target_temp"], 22)
        self.assertEqual(self.adapter.get_state("ac_living")["target_temp"], 26)

    def test_turn_of_is_read_as_turn_off(self):
        self.agent.handle("turn on the purifier")
        r = self.agent.handle("plese swich of the purifire")

        self.assertEqual(r["tier"], "rules")
        self.assertFalse(self.adapter.get_state("purifier")["power"])

    def test_a_room_split_in_two_is_still_the_room(self):
        """Seen live: "the bed room is too hot" asked "Which room?" for a
        sentence that had already said which room. The phone keyboard wrote the
        room as two words, and the only thing between the parser and the answer
        was a space."""
        r = self.agent.handle("the bed room is too hot")

        self.assertEqual(r["intent_name"], "control")
        self.assertEqual(self.adapter.get_state("ac_bedroom")["mode"], "cool")

    def test_two_words_that_only_look_like_a_room_are_not_one(self):
        """Joining words is only safe because the room name has to survive as a
        contiguous run: "put the bed in the room" is not "bedroom"."""
        r = self.agent.handle("put the bed in the room")

        self.assertNotEqual(r["intent_name"], "control")
        self.assertFalse(self.adapter.get_state("ac_bedroom")["power"])
        self.assertFalse(self.adapter.get_state("ac_living")["power"])

    def test_correct_sentences_are_left_exactly_as_typed(self):
        from app import nlu

        devices = self.adapter.list_devices()
        for sentence in ["turn off the living room ac",
                         "set the bedroom ac to 22 degrees",
                         "what is the temperature in the kitchen",
                         "the room of the house"]:
            self.assertEqual(nlu.repair(sentence, devices), sentence)


class BuildStampTest(AgentTestBase):
    """Which build is answering.

    A page and a server can be different builds — a tab left open, a preview
    panel that never reloaded, a second process holding the port. The wording of
    a reply is a poor way to tell them apart, and it cost real time twice.
    """

    def test_the_snapshot_carries_the_stamp_of_the_code_that_answers(self):
        stamp = self.agent.snapshot()["build"]
        self.assertEqual(stamp, build.STAMP)
        self.assertRegex(stamp, r"^[0-9a-f]{7}$")

    def test_the_stamp_is_stable_for_the_same_source(self):
        self.assertEqual(build._digest(), build._digest())

    def test_a_stored_message_carries_the_build_that_wrote_it(self):
        """A reply outlives the code that wrote it. The stamp travels with the
        payload, so the interface can mark a row that a build which no longer
        runs produced — three times today an old row was read as the answer to
        a new question."""
        self.agent.handle("turn on the purifier")

        rows = db.recent_messages(2)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["payload"].get("build"), build.STAMP)


if __name__ == "__main__":
    unittest.main()