"""Tests for the specification requirements FR-3 to FR-6
(docs/project_specifications_final.tex):

  FR-3  one approval per Level 2 action; unknown actions blocked and logged
  FR-4  truthful outcome reporting; quiet periods visible in state
  FR-5  pre-execution revalidation, cooktop safeguards, dryer restart reminder
  FR-6  append-only audit_log, write-before-act

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from tests.fake_llm import FakeLLM  # noqa: E402
from tests.test_phase2 import Base, _ask, _cfg  # noqa: E402


def _events(approval_id=None):
    return [r["event_type"] for r in reversed(db.recent_audit(500, approval_id))]


def _applied(adapter, device):
    return [a for dev, acts in adapter.applied if dev == device for a in acts]


class FailingAdapterMixin:
    def apply(self, device_id, actions):
        self.applied.append((device_id, [dict(a) for a in actions]))
        return {"ok": False, "applied": [], "error": "device offline"}


# ---------------------------------------------------------------- FR-6

class AuditLogTest(Base):
    def test_audit_log_is_append_only(self):
        rid = db.audit("risk.triggered", risk_kind="mold", risk_score=80.0)
        with db.conn() as c:
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute("UPDATE audit_log SET risk_score=0 WHERE id=?", (rid,))
        with db.conn() as c:
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute("DELETE FROM audit_log WHERE id=?", (rid,))
        self.assertEqual(db.recent_audit(5)[0]["risk_score"], 80.0)

    def test_timestamps_are_timezone_aware(self):
        db.audit("risk.triggered", risk_kind="mold")
        ts = db.recent_audit(1)[0]["ts"]
        self.assertRegex(ts, r"[+-]\d\d:\d\d$|Z$")

    def test_unknown_audit_field_is_rejected(self):
        with self.assertRaises(ValueError):
            db.audit("risk.triggered", nonsense=1)

    def test_one_approval_is_traceable_end_to_end(self):
        e = self._engine()
        e.sensors.apply_scenario("oven")
        e.tick()
        ap = _ask(e, "stop_dryer")
        e.confirm(ap.id, channel="ui")
        self.assertEqual(_events(ap.id), ["approval.proposed", "approval.confirmed",
                                          "action.requested", "action.executed"])
        row = db.recent_audit(1, ap.id)[0]
        self.assertEqual(row["action_kind"], "stop_dryer")
        self.assertEqual(row["tier"], "tier2")
        self.assertEqual(row["execution_status"], "completed")
        self.assertEqual(row["channel"], "ui")
        self.assertIn("risk.triggered", _events())

    def test_level2_does_not_run_if_intent_cannot_be_recorded(self):
        e = self._engine()
        e.sensors.apply_scenario("oven")
        e.tick()
        ap = _ask(e, "stop_dryer")
        real = db.audit

        def broken(event_type, **f):
            if event_type == "action.requested":
                raise sqlite3.OperationalError("disk full")
            return real(event_type, **f)

        with mock.patch.object(db, "audit", side_effect=broken):
            res = e.confirm(ap.id)
        self.assertEqual(res["status"], "failed")
        self.assertEqual(_applied(self.adapter, "dryer"), [])


# ---------------------------------------------------------------- FR-3

class PerActionApprovalTest(Base):
    def test_confirming_one_action_does_not_run_the_other(self):
        e = self._engine()
        e.sensors.apply_scenario("oven")
        e.tick()
        e.confirm(_ask(e, "stop_dryer").id)
        self.assertTrue(_applied(self.adapter, "dryer"))
        self.assertEqual(_applied(self.adapter, "cooktop"), [])
        self.assertEqual([a.action_kind for a in e.approvals.pending()],
                         ["preheat_cooktop"])

    def test_unknown_action_from_the_model_is_blocked_and_logged(self):
        with FakeLLM() as fake:
            fake.prose_reply = '{"chosen": ["open_front_door", "ventilate_washer"]}'
            cfg = _cfg(llm={"enabled": True, "base_url": fake.base_url,
                            "api_key": "k", "model": "m", "timeout": 5})
            e = self._engine(cfg)
            e.sensors.apply_scenario("mold")
            e.tick()
        blocked = [r for r in db.recent_audit(50) if r["event_type"] == "action.blocked"]
        self.assertEqual([r["action_kind"] for r in blocked], ["open_front_door"])
        self.assertTrue(_applied(self.adapter, "washer"))
        self.assertNotIn("front_door", {dev for dev, _ in self.adapter.applied})


# ---------------------------------------------------------------- FR-4 / FR-5

class RevalidationTest(Base):
    def test_dryer_already_off_cancels_the_confirmed_stop(self):
        e = self._engine()
        e.sensors.apply_scenario("oven")
        e.tick()
        ap = _ask(e, "stop_dryer")
        e.sensors.note_action("dryer", [{"action": "power", "value": False}])
        res = e.confirm(ap.id)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "canceled")
        self.assertEqual(_applied(self.adapter, "dryer"), [])
        self.assertIn("action.blocked", _events(ap.id))
        last = db.recent_messages(1)[0]["content"]
        self.assertIn("Nothing was changed", last)
        self.assertNotIn("so I", last)

    def test_cooktop_needs_someone_at_home(self):
        e = self._engine()
        e.sensors.apply_scenario("oven")
        e.tick()
        ap = _ask(e, "preheat_cooktop")
        e.sensors.occupancy["user_home"] = False
        res = e.confirm(ap.id)
        self.assertEqual(res["status"], "canceled")
        self.assertIn("nobody is home", res["executed"][0]["reason"])
        self.assertEqual(_applied(self.adapter, "cooktop"), [])

    def test_cooktop_preheat_still_runs_after_the_overload_clears(self):
        """Stopping the dryer usually clears the thermal index on its own.
        Preheating is preparation for the meal, so it does not depend on it."""
        e = self._engine()
        e.sensors.apply_scenario("oven")
        e.tick()
        e.confirm(_ask(e, "stop_dryer").id)
        with e.sensors._lock:
            e.sensors.rooms["Kitchen"]["temp"] = 25.0
            e.sensors.appliances["fridge"]["duty_cycle"] = 0.3
        res = e.confirm(_ask(e, "preheat_cooktop").id)
        self.assertEqual(res["status"], "completed")
        self.assertTrue(_applied(self.adapter, "cooktop"))

    def test_cooktop_needs_cooking_on_the_calendar(self):
        e = self._engine()
        e.sensors.apply_scenario("oven")
        e.tick()
        ap = _ask(e, "preheat_cooktop")
        e.sensors.calendar = []
        res = e.confirm(ap.id)
        self.assertEqual(res["status"], "canceled")
        self.assertIn("no cooking", res["executed"][0]["reason"])

    def test_a_failed_adapter_call_is_reported_as_failed(self):
        self.adapter.__class__ = type("Failing", (FailingAdapterMixin,
                                                  self.adapter.__class__), {})
        e = self._engine()
        e.sensors.apply_scenario("oven")
        e.tick()
        res = e.confirm(_ask(e, "stop_dryer").id)
        self.assertEqual(res["status"], "failed")
        self.assertIn("action.failed", _events())
        last = db.recent_messages(1)[0]["content"]
        self.assertIn("did not respond", last)
        # and the sensor model was not told the dryer stopped
        self.assertTrue(e.sensors.snapshot()["appliances"]["dryer"]["power"])

    def test_failed_tier1_is_not_reported_as_done(self):
        self.adapter.__class__ = type("Failing", (FailingAdapterMixin,
                                                  self.adapter.__class__), {})
        e = self._engine()
        e.sensors.apply_scenario("mold")
        e.tick()
        body = [m for m in db.recent_messages(20) if "washing machine" in m["content"]][-1]
        self.assertIn("did not respond", body["content"])
        self.assertNotIn("So I started", body["content"])


class CooktopInterlockTest(Base):
    def _preheated(self, **proactive):
        e = self._engine(_cfg(**proactive))
        e.sensors.apply_scenario("oven")
        e.tick()
        self.assertEqual(e.confirm(_ask(e, "preheat_cooktop").id)["status"], "completed")
        self.assertTrue(e.state()["cooktop_guard"])
        return e

    def _offs(self):
        return [a for a in _applied(self.adapter, "cooktop")
                if a["action"] == "power" and a["value"] is False]

    def test_auto_off_when_no_cooking_is_detected(self):
        e = self._preheated()
        e._cooktop_guard["auto_off_at"] = 0
        out = e.tick()
        self.assertIn("cooktop_auto_off", [ev["type"] for ev in out["events"]])
        self.assertEqual(len(self._offs()), 1)
        self.assertFalse(e.sensors.snapshot()["appliances"]["cooktop"]["power"])
        self.assertIn("interlock", {r["decision_source"] for r in db.recent_audit(20)})

    def test_cooking_keeps_it_on_until_the_maximum_run_time(self):
        e = self._preheated()
        e.sensors.note_action("cooktop", [{"action": "cooking", "value": True}])
        e._cooktop_guard["auto_off_at"] = 0
        e.tick()
        self.assertEqual(self._offs(), [])            # someone is cooking
        e._cooktop_guard["max_until"] = 0
        e.tick()
        self.assertEqual(len(self._offs()), 1)        # hard limit still applies

    def test_switched_off_by_hand_ends_the_watch(self):
        e = self._preheated()
        e.sensors.note_action("cooktop", [{"action": "power", "value": False}])
        e.tick()
        self.assertFalse(e.state()["cooktop_guard"])
        self.assertEqual(self._offs(), [])


class DryerReminderTest(Base):
    def test_reminder_once_the_overload_clears(self):
        e = self._engine()
        e.sensors.apply_scenario("oven")
        e.tick()
        e.confirm(_ask(e, "stop_dryer").id)
        e.tick()                                      # still hot: no reminder yet
        self.assertFalse(any("start the dryer again" in m["content"]
                             for m in db.recent_messages(50)))
        e.sensors.apply_scenario("idle")
        e.sensors.calendar = []
        e.tick()
        e.tick()
        hits = [m for m in db.recent_messages(50) if "start the dryer again" in m["content"]]
        self.assertEqual(len(hits), 1)
        self.assertIn("notify.dryer_restart", _events())


class QuietPeriodStateTest(Base):
    def test_quiet_period_is_visible_in_state(self):
        e = self._engine()
        e.sensors.apply_scenario("oven")
        e.tick()
        for ap in e.approvals.pending():
            e.decline(ap.id)
        self.assertGreater(e.state()["quiet"].get("thermal", 0), 1700)


if __name__ == "__main__":
    unittest.main()
