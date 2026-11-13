"""Tests for the English intent parser.

These run without any network, account, or hardware:

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import nlu  # noqa: E402

# Saturday, 18:42 — deliberately after noon so the am/pm inference is exercised.
NOW = datetime(2026, 10, 17, 18, 42)

DEVICES = [
    {"id": "ac_living", "name": "Living Room AC", "type": "air_conditioner",
     "room": "Living Room", "online": True},
    {"id": "ac_bedroom", "name": "Bedroom AC", "type": "air_conditioner",
     "room": "Bedroom", "online": True},
    {"id": "purifier", "name": "Living Room Purifier", "type": "air_purifier",
     "room": "Living Room", "online": True},
    {"id": "washer", "name": "Balcony Washer", "type": "washer",
     "room": "Balcony", "online": True},
]


def parse(text: str, **kwargs) -> dict:
    return nlu.parse(text, DEVICES, NOW, "Living Room", **kwargs)


class DeviceResolutionTest(unittest.TestCase):
    def test_bare_ac_falls_back_to_default_room(self):
        r = parse("turn on the AC")
        self.assertEqual(r["intent"], "control")
        self.assertEqual(r["device_id"], "ac_living")

    def test_explicit_room_wins(self):
        r = parse("turn on the bedroom AC")
        self.assertEqual(r["device_id"], "ac_bedroom")

    def test_type_alias_resolves(self):
        r = parse("turn on the purifier")
        self.assertEqual(r["device_id"], "purifier")

    def test_full_name_match(self):
        r = parse("start the Balcony Washer")
        self.assertEqual(r["device_id"], "washer")

    def test_fallback_device_is_used(self):
        washer = DEVICES[3]
        r = parse("turn it off", fallback_device=washer)
        self.assertEqual(r["device_id"], "washer")


class ActionTest(unittest.TestCase):
    def test_power_on(self):
        r = parse("turn on the living room AC")
        self.assertIn({"action": "power", "value": True}, r["actions"])

    def test_power_off(self):
        r = parse("turn off the living room AC")
        self.assertIn({"action": "power", "value": False}, r["actions"])

    def test_temperature_with_degrees(self):
        r = parse("set the living room AC to 24 degrees")
        self.assertIn({"action": "temperature", "value": 24}, r["actions"])

    def test_temperature_without_degrees(self):
        r = parse("set the AC to 22")
        self.assertIn({"action": "temperature", "value": 22}, r["actions"])

    def test_temperature_out_of_range_is_ignored(self):
        r = parse("set the AC to 45 degrees")
        self.assertFalse([a for a in r["actions"] if a["action"] == "temperature"])

    def test_mode_and_fan(self):
        r = parse("switch the AC to heat and fan low")
        self.assertIn({"action": "mode", "value": "heat"}, r["actions"])
        self.assertIn({"action": "fan", "value": "low"}, r["actions"])

    def test_heat_risk_explanation_is_not_a_heating_command(self):
        r = parse("In one short sentence, explain the current kitchen heat risk.")
        self.assertNotIn({"action": "mode", "value": "heat"}, r["actions"])

    def test_adding_heat_question_is_not_a_heating_command(self):
        r = parse("Why is the dryer adding heat?")
        self.assertNotIn({"action": "mode", "value": "heat"}, r["actions"])

    def test_direct_heat_request_still_selects_heating(self):
        r = parse("heat the bedroom")
        self.assertIn({"action": "mode", "value": "heat"}, r["actions"])

    def test_multiple_actions_in_one_sentence(self):
        r = parse("turn on the AC and set it to 26 degrees")
        kinds = {a["action"] for a in r["actions"]}
        self.assertEqual(kinds, {"power", "temperature"})


class ScheduleTest(unittest.TestCase):
    def test_one_time_tonight(self):
        r = parse("turn off the AC at 11 pm")
        self.assertEqual(r["intent"], "schedule")
        self.assertEqual(r["rule"]["type"], "once")
        self.assertEqual(r["rule"]["run_at"], "2026-10-17T23:00:00")

    def test_daily(self):
        r = parse("turn off the AC every night at 11 pm")
        self.assertEqual(r["rule"], {"type": "daily", "at": "23:00"})

    def test_weekday(self):
        r = parse("every weekday at 7 am turn on the bedroom AC")
        self.assertEqual(r["rule"], {"type": "weekday", "at": "07:00"})

    def test_weekly_multiple_days(self):
        r = parse("every Monday and Wednesday at 8 am turn on the AC")
        self.assertEqual(r["rule"]["type"], "weekly")
        self.assertEqual(r["rule"]["weekdays"], [0, 2])

    def test_relative_minutes(self):
        r = parse("in 30 minutes turn off the washer")
        self.assertEqual(r["rule"]["type"], "once")
        self.assertEqual(r["rule"]["run_at"], "2026-10-17T19:12:00")

    def test_half_past_infers_pm(self):
        r = parse("at half past ten turn off the AC")
        self.assertEqual(r["rule"]["run_at"], "2026-10-17T22:30:00")

    def test_quarter_to_evening(self):
        r = parse("at quarter to eight in the evening turn on the AC")
        self.assertEqual(r["rule"]["run_at"], "2026-10-17T19:45:00")

    def test_tomorrow_morning(self):
        r = parse("at 7 am tomorrow turn on the AC")
        self.assertEqual(r["rule"]["run_at"], "2026-10-18T07:00:00")

    def test_interval(self):
        r = parse("every 2 hours turn on the purifier")
        self.assertEqual(r["rule"], {"type": "interval", "interval_seconds": 7200})

    def test_time_without_action_asks_for_one(self):
        r = parse("at 11 pm")
        self.assertEqual(r["intent"], "need_action")
        self.assertEqual(r["missing"], "action")
        self.assertTrue(r["clarify"])


class QueryTest(unittest.TestCase):
    def test_temperature_question(self):
        r = parse("what's the temperature in the bedroom?")
        self.assertEqual(r["intent"], "query")
        self.assertEqual(r["device_id"], "ac_bedroom")

    def test_is_it_on(self):
        r = parse("is the AC on?")
        self.assertEqual(r["intent"], "query")

    def test_how_hot(self):
        r = parse("how hot is it in the bedroom")
        self.assertEqual(r["intent"], "query")
        self.assertEqual(r["device_id"], "ac_bedroom")

    def test_control_with_word_temperature_is_not_a_query(self):
        # "temperature" appears in QUERY_RE, but a control verb must win.
        r = parse("set the temperature to 24", fallback_device=DEVICES[0])
        self.assertEqual(r["intent"], "control")
        self.assertIn({"action": "temperature", "value": 24}, r["actions"])

    def test_control_without_device_asks_which_one(self):
        r = parse("set the temperature to 24")
        self.assertEqual(r["intent"], "need_target")
        self.assertEqual(r["missing"], "target")
        self.assertIn("Which", r["clarify"])


class HousekeepingTest(unittest.TestCase):
    def test_list(self):
        self.assertEqual(parse("what scheduled tasks do I have?")["intent"], "list")

    def test_cancel_all(self):
        r = parse("cancel all scheduled tasks")
        self.assertEqual(r["intent"], "cancel")
        self.assertTrue(r["cancel_all"])

    def test_unknown(self):
        r = parse("hello there")
        self.assertEqual(r["intent"], "unknown")
        self.assertIn("schedule", r["clarify"])


class ComputeNextTest(unittest.TestCase):
    def test_daily_already_passed_rolls_to_tomorrow(self):
        nxt = nlu.compute_next({"type": "daily", "at": "07:00"}, NOW)
        self.assertEqual(nxt.strftime("%Y-%m-%d %H:%M"), "2026-10-18 07:00")

    def test_daily_still_ahead_stays_today(self):
        nxt = nlu.compute_next({"type": "daily", "at": "23:00"}, NOW)
        self.assertEqual(nxt.strftime("%Y-%m-%d %H:%M"), "2026-10-17 23:00")

    def test_weekly_skips_to_matching_day(self):
        # Saturday 18:42 -> next Monday at 08:00
        nxt = nlu.compute_next({"type": "weekly", "at": "08:00", "weekdays": [0]}, NOW)
        self.assertEqual(nxt.strftime("%Y-%m-%d %H:%M"), "2026-10-19 08:00")

    def test_weekday_skips_weekend(self):
        nxt = nlu.compute_next({"type": "weekday", "at": "08:00"}, NOW)
        self.assertEqual(nxt.strftime("%Y-%m-%d %H:%M"), "2026-10-19 08:00")

    def test_interval(self):
        nxt = nlu.compute_next({"type": "interval", "interval_seconds": 3600}, NOW)
        self.assertEqual(nxt.strftime("%Y-%m-%d %H:%M"), "2026-10-17 19:42")


if __name__ == "__main__":
    unittest.main()

