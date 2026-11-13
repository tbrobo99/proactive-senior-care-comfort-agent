"""Simulated IoT sensor streams (Phase 2).

The proposal's NFR requires every sensor reading and calendar entry to be
generated locally as JSON/CSV. This module is that generator: it owns a virtual
house — rooms with temperature and humidity, appliances with drum state and
power draw, an occupancy flag, and a calendar — and ticks once a second,
appending each snapshot to data/sensors/readings.jsonl and rewriting
data/sensors/latest.csv.

It is deliberately separate from the device adapter. Sensors *observe* the
environment; the adapter *actuates* appliances. The oven heating the kitchen is
a sensor fact; the range hood exhausting it is an adapter action. Keeping the
two apart is what lets the proactive loop close a real loop: act, watch the room
answer, act again.

The kitchen's thermal state is read through the fridge, not through its air
conditioner: an AC reports the setpoint it was told, not the room it stands in.
LG smart fridges report an external (room) temperature and expose the
compressor's energy draw through ThinQ, and a compressor working harder is a
kitchen getting hotter. So the fridge is the sensor and the kitchen AC is one of
the actuators — which is the arrangement the poster describes, where the air
conditioner gets a signal ahead of the cooking instead of after it.

Physics here is simple and explainable on purpose. Every number the risk model
consumes can be traced back to a rule in `_tick` — the point of Phase 2 is the
decision layer, not a credible building simulator.
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

# The repository root. Two levels up from app/sensors.py — this was three, which
# put the generated sensor files in the *parent* of the repo while the SQLite
# database stayed in data/. Both live in <repo>/data now, which is what the
# module docstring and NFR III.B both claim.
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or (ROOT / "data"))
SENSOR_DIR = DATA_DIR / "sensors"

# NFR III.B: calendar *inputs* are a local file too, not a live account. The
# file is a JSON array of {"title", "start", "end"}; `start` is ISO 8601. The
# thermal index reads it to treat an oven scheduled within the next half hour
# as load that has not arrived yet — which is the only moment ventilating
# ahead of time is worth doing.
CALENDAR_FILE = DATA_DIR / "calendar.json"

ROOMS = ("Living Room", "Kitchen", "Bedroom", "Balcony")

# Nameplate draw of the heat-generating appliances, watts. The thermal risk
# index is a load ratio, so these numbers are the units it is measured in.
HEAT_LOAD_W = {"oven": 2200, "dryer": 1800, "dish_washer": 1200, "washer": 500,
               "cooktop": 1800}

ROOM_BASE = {
    "Living Room": {"temp": 29.0, "humidity": 52.0},
    "Kitchen": {"temp": 30.0, "humidity": 54.0},
    "Bedroom": {"temp": 28.5, "humidity": 55.0},
    "Balcony": {"temp": 28.0, "humidity": 60.0},
}

# A wash cycle in the simulator. Short on purpose: the mold scenario needs the
# cycle to finish while the demo is still running.
CYCLE_SECONDS = 240


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class SensorSimulator:
    """A virtual house that produces a sensor stream, one tick at a time."""

    def __init__(self, tick_seconds: float = 1.0, history_len: int = 300,
                 write_files: bool = True) -> None:
        self.tick_seconds = tick_seconds
        self.write_files = write_files
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.rooms: dict[str, dict[str, float]] = {
            r: {"temp": b["temp"], "humidity": b["humidity"]} for r, b in ROOM_BASE.items()
        }
        self.appliances: dict[str, dict] = {
            "washer": {"power": False, "cycle": "standby", "door_closed": True,
                       "door_closed_since": None, "drum_humidity": 40.0,
                       "ventilating": False, "remaining_sec": 0},
            "dryer": {"power": False},
            "oven": {"power": False, "target_temp": 180},
            "dehumidifier": {"power": False},
            "range_hood": {"power": False, "fan": "auto"},
            # The purifier draws nothing worth counting as heat, but its state
            # is a sensor fact like any other: the mold branch proposes it when
            # spores are the problem, and the loop must not propose it twice.
            "purifier": {"power": False, "fan": "auto"},
            # The cooktop is the one heating appliance the house can reach but
            # must not decide about alone: a hob left on with nobody in front of
            # it is the reason Tier 2 exists (poster §II.C, "warming up
            # cooktops"). Its state is a sensor fact like any other.
            "cooktop": {"power": False},
            # The fridge is the kitchen's sensor node: what its external probe
            # reads and how hard its compressor works. duty_cycle is derived
            # from external_temp, exactly as the real appliance's draw tracks
            # the room it stands in.
            "fridge": {"power": True, "external_temp": 29.0, "duty_cycle": 0.35},
        }
        self.ac_targets: dict[str, dict] = {}  # room -> {"target_temp", "mode", "power"}
        self.occupancy = {"user_home": True}
        self.calendar: list[dict] = self._load_calendar()

        # (timestamp, value) per signal, for the trend extrapolation in risk.py
        self.history: dict[str, deque] = {
            f"{r}.temp": deque(maxlen=history_len) for r in ROOMS
        }
        self.history.update({f"{r}.humidity": deque(maxlen=history_len) for r in ROOMS})
        self.history["washer.drum_humidity"] = deque(maxlen=history_len)
        self.history["home.heat_w"] = deque(maxlen=history_len)

        self._scenario = "idle"
        self._scenario_since = time.monotonic()

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sensors")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_seconds):
            try:
                with self._lock:
                    self._tick()
                self.record(self.snapshot())
            except Exception:
                # A broken sensor tick must never take the agent down; the
                # proactive loop simply sees a stale snapshot.
                pass

    # ---------------------------------------------------------------- physics

    def _tick(self) -> None:
        dt = self.tick_seconds
        rooms = self.rooms
        ap = self.appliances

        # Oven: a 2.2 kW element in a closed kitchen. Fast enough to see inside
        # a demo, slow enough that ventilation has a point.
        if ap["oven"]["power"]:
            rooms["Kitchen"]["temp"] = _clamp(rooms["Kitchen"]["temp"] + 0.05 * dt, 16, 42)

        # Cooktop: a smaller element than the oven, and it only heats while
        # something is on it. It is in the load table, so switching it on raises
        # the thermal index by itself — which is exactly why the policy asks
        # before it does.
        if ap.get("cooktop", {}).get("power"):
            rooms["Kitchen"]["temp"] = _clamp(rooms["Kitchen"]["temp"] + 0.03 * dt, 16, 42)

        # Range hood: the only kitchen actuator there is. It exhausts hot air,
        # so the kitchen sheds heat toward the outdoor baseline instead of
        # accumulating it.
        if ap["range_hood"]["power"]:
            rooms["Kitchen"]["temp"] -= _clamp(
                (rooms["Kitchen"]["temp"] - 26.0) * 0.03 * dt, 0, 0.3 * dt)

        # AC: drifts its room toward the setpoint, exactly like the mock
        # adapter does for device-reported temperature.
        for room, ac in self.ac_targets.items():
            if room not in rooms:
                continue
            if ac.get("power") and ac.get("mode") in ("cool", "heat"):
                goal = ac.get("target_temp", 26)
                rooms[room]["temp"] += _clamp((goal - rooms[room]["temp"]) * 0.02 * dt,
                                              -0.35 * dt, 0.35 * dt)
            else:
                rooms[room]["temp"] += 0.01 * dt  # drifts back up when off

        # Dryer dumps heat and dry air onto the balcony.
        if ap["dryer"]["power"]:
            rooms["Balcony"]["temp"] = _clamp(rooms["Balcony"]["temp"] + 0.01 * dt, 16, 42)
            rooms["Balcony"]["humidity"] = _clamp(rooms["Balcony"]["humidity"] - 0.05 * dt, 20, 95)

        # Dehumidifier pulls the balcony down toward 45%; without one it drifts
        # back toward the outdoor baseline.
        dh = 0.6 if ap["dehumidifier"]["power"] else 0.05
        rooms["Balcony"]["humidity"] += _clamp((45 if ap["dehumidifier"]["power"] else 62)
                                               - rooms["Balcony"]["humidity"], -dh * dt, dh * dt)

        # The fridge reads the kitchen it stands in, with lag: its probe sits on
        # the body, not in the air. duty_cycle follows the probe — a hotter
        # kitchen is a compressor that never gets to rest. This is the kitchen's
        # only honest temperature channel, since the kitchen has no AC.
        fridge = ap["fridge"]
        if fridge.get("power", True):
            goal = rooms["Kitchen"]["temp"]
            fridge["external_temp"] += _clamp((goal - fridge["external_temp"]) * 0.01 * dt,
                                              -0.2 * dt, 0.2 * dt)
            fridge["external_temp"] = round(min(38.0, max(18.0, fridge["external_temp"])), 1)
            fridge["duty_cycle"] = round(
                max(0.15, min(1.0, 0.15 + (fridge["external_temp"] - 18.0) / 20.0)), 2)

        # Washer drum: the mold model. A finished cycle leaves the drum wet;
        # a shut door traps it; micro-ventilation bleeds it off slowly.
        w = ap["washer"]
        if w["cycle"] == "washing":
            w["remaining_sec"] = max(0, w["remaining_sec"] - dt)
            if w["remaining_sec"] == 0:
                w["cycle"] = "done"
                w["power"] = False
                w["door_closed"] = True
                w["door_closed_since"] = datetime.now()
                w["drum_humidity"] = 90.0
        elif w["cycle"] == "done":
            if w["door_closed"]:
                if w["door_closed_since"] is None:
                    w["door_closed_since"] = datetime.now()
                rate = -0.05 * dt if w["ventilating"] else 0.02 * dt
                w["drum_humidity"] = _clamp(w["drum_humidity"] + rate, 30, 98)
            else:
                w["door_closed_since"] = None
                w["drum_humidity"] = _clamp(
                    w["drum_humidity"] - (0.5 + (0.1 if w["ventilating"] else 0)) * dt, 30, 98)

        # Rooms drift gently toward their baseline so nothing is frozen.
        for room, base in ROOM_BASE.items():
            rooms[room]["temp"] += _clamp((base["temp"] - rooms[room]["temp"]) * 0.002 * dt,
                                          -0.05 * dt, 0.05 * dt)

    # ---------------------------------------------------------------- adapter feedback

    def note_action(self, device_id: str, actions: list[dict]) -> None:
        """Let the environment answer an actuator command.

        Without this the proactive loop would act into a void: the range hood
        would "ventilate" a kitchen whose temperature ignores it, and the mold
        intervention would never show the drum drying.
        """
        with self._lock:
            for act in actions:
                name, value = act.get("action"), act.get("value")
                if device_id.startswith("ac_"):
                    room = next((d_room for d_id, d_room in
                                 (("ac_living", "Living Room"), ("ac_bedroom", "Bedroom"),
                                  ("ac_kitchen", "Kitchen"))
                                 if d_id == device_id), None)
                    if room:
                        ac = self.ac_targets.setdefault(room, {"power": False, "mode": "cool",
                                                               "target_temp": 26})
                        if name == "power":
                            ac["power"] = bool(value)
                        elif name == "mode":
                            ac["mode"] = value
                        elif name == "temperature":
                            ac["target_temp"] = value
                elif device_id == "washer":
                    w = self.appliances["washer"]
                    if name == "power" and value:
                        w["cycle"] = "washing"
                        w["remaining_sec"] = CYCLE_SECONDS
                        w["door_closed"] = True
                        w["drum_humidity"] = 60.0
                    elif name == "ventilate":
                        w["ventilating"] = bool(value)
                elif device_id in self.appliances and name in ("power", "fan"):
                    self.appliances[device_id][name] = value
                elif device_id == "oven" and name == "temperature":
                    self.appliances["oven"]["target_temp"] = value

    # ---------------------------------------------------------------- calendar

    @staticmethod
    def _load_calendar(path: Path | None = None) -> list[dict]:
        """Read the day's appointments from the local JSON file.

        A missing or malformed file is an empty day, not a crash: the house
        must keep working when a fixture is wrong, and a calendar that cannot
        be read is the same as a calendar with nothing on it.
        """
        p = Path(path) if path is not None else CALENDAR_FILE
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        out = []
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("start"):
                continue
            out.append({
                "title": str(entry.get("title") or "appointment"),
                "start": str(entry["start"]),
                "end": str(entry.get("end") or ""),
            })
        return out

    def reload_calendar(self) -> list[dict]:
        """Re-read the file, so a demo can edit the day without a restart."""
        with self._lock:
            self.calendar = self._load_calendar()
            return list(self.calendar)

    # ---------------------------------------------------------------- scenarios

    def apply_scenario(self, name: str, now: datetime | None = None) -> None:
        """Load a deterministic starting state for demos and tests."""
        now = now or datetime.now()
        with self._lock:
            self._scenario = name
            self._scenario_since = time.monotonic()
            # reset
            for room, base in ROOM_BASE.items():
                self.rooms[room] = {"temp": base["temp"], "humidity": base["humidity"]}
            self.appliances = {
                "washer": {"power": False, "cycle": "standby", "door_closed": True,
                           "door_closed_since": None, "drum_humidity": 40.0,
                           "ventilating": False, "remaining_sec": 0},
                "dryer": {"power": False},
                "oven": {"power": False, "target_temp": 180},
                "dehumidifier": {"power": False},
                "range_hood": {"power": False, "fan": "auto"},
                "purifier": {"power": False, "fan": "auto"},
                "cooktop": {"power": False},
                "fridge": {"power": True, "external_temp": 29.0, "duty_cycle": 0.35},
            }
            self.ac_targets.clear()
            # Scenarios start from the real day and then override it. A demo
            # fixture is still a fixture, but the baseline is the file.
            self.calendar = self._load_calendar()
            self.occupancy = {"user_home": True}

            if name == "mold":
                # Cycle finished two hours ago, door never opened, nobody home
                # to notice — the exact situation from the poster.
                self.occupancy["user_home"] = False
                w = self.appliances["washer"]
                w["cycle"] = "done"
                w["door_closed"] = True
                w["door_closed_since"] = now - timedelta(hours=2)
                w["drum_humidity"] = 90.0
                self.rooms["Balcony"]["humidity"] = 66.0
            elif name in ("oven", "oven_running"):
                self.calendar = [{
                    "title": "Dinner bake",
                    "start": (now + timedelta(minutes=20)).isoformat(timespec="minutes"),
                    "end": (now + timedelta(minutes=80)).isoformat(timespec="minutes"),
                }]
                self.appliances["dryer"]["power"] = True
                # The kitchen is already hot and the fridge is feeling it: the
                # probe lags the room, and the duty cycle is what that heat
                # costs. This is the kitchen's only temperature channel.
                self.rooms["Kitchen"]["temp"] = 31.5
                self.appliances["fridge"]["external_temp"] = 31.0
                self.appliances["fridge"]["duty_cycle"] = 0.8
                if name == "oven_running":
                    self.appliances["oven"]["power"] = True

    # ---------------------------------------------------------------- output

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "scenario": self._scenario,
                "rooms": {r: dict(v) for r, v in self.rooms.items()},
                "appliances": json.loads(json.dumps(self.appliances, default=str)),
                "ac_targets": {r: dict(v) for r, v in self.ac_targets.items()},
                "occupancy": dict(self.occupancy),
                "calendar": list(self.calendar),
                "heat_load_w": self._heat_load(),
                "sealed_hours": self.sealed_hours(),
            }

    def _heat_load(self) -> int:
        return sum(HEAT_LOAD_W[k] for k, v in self.appliances.items()
                   if v.get("power") and k in HEAT_LOAD_W)

    def sealed_hours(self, now: datetime | None = None) -> float:
        """How long the washer drum has been shut and wet. 0.0 if it is not."""
        now = now or datetime.now()
        w = self.appliances["washer"]
        if w["cycle"] != "done" or not w["door_closed"] or not w["door_closed_since"]:
            return 0.0
        since = w["door_closed_since"]
        if isinstance(since, str):
            try:
                since = datetime.fromisoformat(since)
            except ValueError:
                return 0.0
        return max(0.0, (now - since).total_seconds() / 3600.0)

    def record(self, snap: dict) -> None:
        """Append to readings.jsonl and rewrite latest.csv (NFR: local JSON/CSV)."""
        if not self.write_files:
            return
        try:
            SENSOR_DIR.mkdir(parents=True, exist_ok=True)
            with open(SENSOR_DIR / "readings.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(snap, ensure_ascii=False, default=str) + "\n")
            rows = []
            for room, vals in snap["rooms"].items():
                rows.append([snap["ts"], f"room.{room}", "temp", f"{vals['temp']:.1f}"])
                rows.append([snap["ts"], f"room.{room}", "humidity", f"{vals['humidity']:.1f}"])
            w = snap["appliances"]["washer"]
            if w["cycle"] == "done":
                rows.append([snap["ts"], "washer", "drum_humidity", f"{w['drum_humidity']:.1f}"])
            rows.append([snap["ts"], "home", "heat_load_w", str(snap["heat_load_w"])])
            with open(SENSOR_DIR / "latest.csv", "w", encoding="utf-8", newline="") as f:
                csv.writer(f).writerows([["ts", "source", "metric", "value"]] + rows)
        except Exception:
            pass
