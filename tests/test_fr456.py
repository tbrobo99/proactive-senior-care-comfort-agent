"""FR-4/5/6 tests: per-action consent, pre-execution revalidation, cooktop
safeguards, the dryer follow-up notice, and the append-only audit trail.

Everything runs against a temporary SQLite file and a recording adapter, so the
real data/ directory is never touched and CI needs no network.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import audit, db, policy, sensors  # noqa: E402
from app.policy import Candidate  # noqa: E402
from app.proactive import ProactiveEngine  # noqa: E402
from tests.test_phase2 import FakeAdapter, _cfg  # noqa: E402


class FR456Test(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (db.DATA_DIR, db.DB_PATH)
        db.DATA_DIR = Path(self._tmp.name)
        db.DB_PATH = db.DATA_DIR / "test.db"
        db.init()
        self.adapter = FakeAdapter()

    def tearDown(self):
        db.DATA_DIR, db.DB_PATH = self._orig
        self._tmp.cleanup()

    def _engine(self, **proactive):
        e = ProactiveEngine(self.adapter, _cfg(**proactive))
        e.sensors.write_files = False
        return e

    def _oven(self, e):
        """Run the deterministic thermal scenario and index the pending cards."""
        e.sensors.apply_scenario("oven")
        e.tick()
        return {a.action_kind: a for a in e.approvals.pending()}

    def _events(self, event_type=None):
        return db.recent_audit(300, event_type=event_type)

    # ------------------------------------------------------------ FR-6 audit

    def test_audit_log_is_immutable(self):
        db.audit_write("risk.triggered", risk_kind="thermal")
        with self.assertRaises(sqlite3.IntegrityError):
            with db.conn() as c:
                c.execute("UPDATE audit_log SET detail = 'tampered'")
        with self.assertRaises(sqlite3.IntegrityError):
            with db.conn() as c:
                c.execute("DELETE FROM audit_log")
        self.assertEqual(len(self._events("risk.triggered")), 1)

    def test_execute_writes_intent_before_the_adapter_and_outcome_after(self):
        res = audit.execute(self.adapter, "dryer",
                            [{"action": "power", "value": False}])
        self.assertTrue(res["ok"])
        types = [r["event_type"] for r in self._events()]
        # newest first: the outcome must have been written after the intent
        self.assertEqual(types, ["action.executed", "action.intent"])
        self.assertEqual(self.adapter.applied[0][0], "dryer")

    def test_a_failed_intent_write_never_reaches_the_adapter(self):
        original = db.audit_write

        def fail_intent(event_type, **fields):
            if event_type == "action.intent":
                raise OSError("disk full")
            return original(event_type, **fields)

        with mock.patch.object(db, "audit_write", side_effect=fail_intent):
            res = audit.execute(self.adapter, "dryer",
                                [{"action": "power", "value": False}])
        self.assertFalse(res["ok"])
        self.assertTrue(res["audit_blocked"])
        self.assertEqual(self.adapter.applied, [])

    def test_the_full_chain_is_linked_by_approval_id(self):
        e = self._engine()
        ap = self._oven(e)["stop_dryer"]
        self.assertTrue(e.confirm(ap.id)["ok"])
        kinds = {r["event_type"] for r in self._events()}
        self.assertLessEqual({"risk.triggered", "approval.proposed",
                              "approval.confirmed", "action.intent",
                              "action.executed"}, kinds)
        intents = self._events("action.intent")
        self.assertTrue(any(r["approval_id"] == ap.id
                            and r["execution_status"] == "pending"
                            for r in intents))
        outcomes = self._events("action.executed")
        self.assertTrue(any(r["approval_id"] == ap.id
                            and r["execution_status"] == "completed"
                            for r in outcomes))

    def test_an_unknown_action_is_blocked_and_recorded(self):
        e = self._engine()
        e.sensors.apply_scenario("oven")
        invented = Candidate(kind="invented_action", device_id="dryer",
                             device_name="Dryer", label="do something",
                             actions=[{"action": "power", "value": False}])
        with mock.patch.object(policy, "candidates", return_value=[invented]):
            e.tick()
        self.assertEqual(self.adapter.applied, [])
        blocked = self._events("action.blocked")
        self.assertTrue(any("invented_action" in (r["detail"] or "")
                            for r in blocked))

    # ------------------------------------------------------- FR-4 per action

    def test_each_level_2_action_gets_its_own_id_and_one_action(self):
        e = self._engine()
        aps = self._oven(e)
        self.assertEqual(set(aps), {"stop_dryer", "preheat_cooktop"})
        self.assertEqual(aps["stop_dryer"].id, aps["stop_dryer"].id)
        self.assertNotEqual(aps["stop_dryer"].id, aps["preheat_cooktop"].id)
        for ap in aps.values():
            self.assertEqual(len(ap.actions), 1)
            self.assertEqual(ap.actions[0]["kind"], ap.action_kind)
            self.assertEqual(ap.tier, policy.TIER2)
        self.assertNotEqual(aps["stop_dryer"].question,
                            aps["preheat_cooktop"].question)

    def test_a_yes_to_one_action_does_not_execute_the_other(self):
        e = self._engine()
        aps = self._oven(e)
        before = len(self.adapter.applied)
        self.assertTrue(e.confirm(aps["stop_dryer"].id)["ok"])
        applied = {dev for dev, _ in self.adapter.applied[before:]}
        self.assertEqual(applied, {"dryer"})
        self.assertEqual([a.id for a in e.approvals.pending()],
                         [aps["preheat_cooktop"].id])
        # The second card still needs its own answer.
        self.assertTrue(e.confirm(aps["preheat_cooktop"].id)["ok"])
        self.assertIn("cooktop", {dev for dev, _ in self.adapter.applied})

    # ------------------------------------------------- FR-5 revalidation

    def test_a_yes_after_the_risk_clears_is_refused_with_a_reason(self):
        e = self._engine()
        ap = self._oven(e)["stop_dryer"]
        e.sensors.apply_scenario("idle")      # risk gone, dryer still off
        res = e.confirm(ap.id)
        self.assertFalse(res["ok"])
        self.assertTrue(res["blocked"])
        self.assertIn("no longer active", res["reason"])
        self.assertNotIn("dryer", {dev for dev, _ in self.adapter.applied})
        self.assertTrue(any("risk" in (r["detail"] or "")
                            for r in self._events("action.blocked")))

    def test_preheat_cooktop_is_refused_when_nobody_is_home(self):
        e = self._engine()
        ap = self._oven(e)["preheat_cooktop"]
        e.sensors.occupancy["user_home"] = False
        res = e.confirm(ap.id)
        self.assertFalse(res["ok"])
        self.assertIn("nobody is home", res["reason"])
        self.assertNotIn("cooktop", {dev for dev, _ in self.adapter.applied})

    def test_a_device_that_changed_since_the_question_is_not_touched(self):
        e = self._engine()
        ap = self._oven(e)["preheat_cooktop"]
        # Somebody switched the hob on by hand in the meantime.
        e.sensors.note_action("cooktop", [{"action": "power", "value": True}])
        res = e.confirm(ap.id)
        self.assertFalse(res["ok"])
        self.assertIn("device state changed", res["reason"])

    def test_a_confirmation_whose_audit_row_fails_does_not_execute(self):
        e = self._engine()
        ap = self._oven(e)["stop_dryer"]
        original = db.audit_write

        def fail_confirmed(event_type, **fields):
            if event_type == "approval.confirmed":
                raise OSError("disk full")
            return original(event_type, **fields)

        with mock.patch.object(db, "audit_write", side_effect=fail_confirmed):
            res = e.confirm(ap.id)
        self.assertFalse(res["ok"])
        self.assertTrue(res["audit_blocked"])
        self.assertNotIn("dryer", {dev for dev, _ in self.adapter.applied})

    # ------------------------------------------------- FR-5 cooktop safeguards

    def test_no_cooking_detected_switches_the_cooktop_off(self):
        e = self._engine(cooktop_no_cook_timeout=0, cooktop_max_runtime=9999)
        ap = self._oven(e)["preheat_cooktop"]
        self.assertTrue(e.confirm(ap.id)["ok"])
        e.tick()
        self.assertTrue(any(dev == "cooktop"
                            and any(a["action"] == "power" and a["value"] is False
                                    for a in acts)
                            for dev, acts in self.adapter.applied))
        self.assertFalse(e.sensors.snapshot()["appliances"]["cooktop"]["power"])
        self.assertTrue(any(r["action_kind"] == "cooktop_auto_off"
                            for r in self._events("action.executed")))

    def test_maximum_run_time_switches_the_cooktop_off(self):
        e = self._engine(cooktop_no_cook_timeout=9999, cooktop_max_runtime=0)
        ap = self._oven(e)["preheat_cooktop"]
        self.assertTrue(e.confirm(ap.id)["ok"])
        e.tick()
        self.assertFalse(e.sensors.snapshot()["appliances"]["cooktop"]["power"])
        text = " ".join(m["content"] for m in db.recent_messages()
                        if m["role"] == "system")
        self.assertIn("maximum continuous run time", text)

    # ------------------------------------------------- FR-5 follow-up notice

    def test_the_dryer_reminder_comes_once_when_the_risk_clears(self):
        e = self._engine()
        ap = self._oven(e)["stop_dryer"]
        self.assertTrue(e.confirm(ap.id)["ok"])
        e.tick()                       # risk still active: no reminder yet
        self.assertNotIn("dryer_restart_reminder", str(db.recent_messages()))
        e.sensors.apply_scenario("idle")
        e.tick()
        messages = [m for m in db.recent_messages() if m["role"] == "system"]
        notices = [m for m in messages
                   if (m.get("payload") or {}).get("notification")
                   == "dryer_restart_reminder"]
        self.assertEqual(len(notices), 1)
        self.assertIn("have not restarted it", notices[0]["content"])
        self.assertFalse(e.sensors.snapshot()["appliances"]["dryer"]["power"])
        self.assertEqual([j for j in db.list_jobs() if j["device_id"] == "dryer"], [])
        e.tick()
        self.assertEqual(len([m for m in db.recent_messages()
                              if (m.get("payload") or {}).get("notification")
                              == "dryer_restart_reminder"]), 1)

    def test_a_manual_restart_cancels_the_reminder(self):
        e = self._engine()
        ap = self._oven(e)["stop_dryer"]
        self.assertTrue(e.confirm(ap.id)["ok"])
        e.sensors.note_action("dryer", [{"action": "power", "value": True}])
        e.sensors.apply_scenario("idle")
        e.sensors.note_action("dryer", [{"action": "power", "value": True}])
        e.tick()
        self.assertFalse(e._pending_dryer_reminder)
        self.assertNotIn("dryer_restart_reminder", str(db.recent_messages()))


if __name__ == "__main__":
    unittest.main()
