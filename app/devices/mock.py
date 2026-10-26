"""Simulated device backend.

Lets the whole pipeline run without an LG account, a PAT, or hardware:
- state persists to data/mock_state.json, so a restart keeps it
- a background thread drifts the room temperature toward the target, so
  "what's the temperature?" returns something that actually moves
"""
from __future__ import annotations

import json
import random
import threading
from pathlib import Path

from .base import CAPABILITIES, TYPE_LABELS, DeviceAdapter, temperature_range

ROOT = Path(__file__).resolve().parent.parent.parent
STATE_PATH = ROOT / "data" / "mock_state.json"

DEVICES = [
    {"id": "ac_living", "name": "Living Room AC", "type": "air_conditioner", "room": "Living Room"},
    {"id": "ac_bedroom", "name": "Bedroom AC", "type": "air_conditioner", "room": "Bedroom"},
    {"id": "purifier", "name": "Living Room Purifier", "type": "air_purifier", "room": "Living Room"},
    {"id": "range_hood", "name": "Kitchen Range Hood", "type": "range_hood", "room": "Kitchen"},
    {"id": "oven", "name": "Kitchen Oven", "type": "oven", "room": "Kitchen"},
    {"id": "ac_kitchen", "name": "Kitchen AC", "type": "air_conditioner", "room": "Kitchen"},
    {"id": "cooktop", "name": "Kitchen Cooktop", "type": "cooktop", "room": "Kitchen"},
    {"id": "fridge", "name": "Kitchen Fridge", "type": "refrigerator", "room": "Kitchen"},
    {"id": "washer", "name": "Balcony Washer", "type": "washer", "room": "Balcony"},
    {"id": "dryer", "name": "Balcony Dryer", "type": "dryer", "room": "Balcony"},
    {"id": "dehumidifier", "name": "Balcony Dehumidifier", "type": "dehumidifier", "room": "Balcony"},
]

DEFAULT_STATES = {
    "ac_living": {"power": False, "mode": "cool", "target_temp": 26,
                  "current_temp": 29.4, "humidity": 52, "fan": "auto"},
    "ac_bedroom": {"power": False, "mode": "cool", "target_temp": 27,
                   "current_temp": 28.6, "humidity": 55, "fan": "auto"},
    "purifier": {"power": False, "fan": "auto", "pm25": 42},
    "range_hood": {"power": False, "fan": "auto"},
    "oven": {"power": False, "target_temp": 180},
    # The kitchen's AC is the Tier-1 actuator for "pre-cooling a room before
    # cooking" (poster §II.C). It is not the kitchen's thermometer — an AC
    # reports the setpoint it was told, so the fridge probe below stays the
    # sensor and this stays the hand.
    "ac_kitchen": {"power": False, "mode": "cool", "target_temp": 24,
                   "current_temp": 30.0, "humidity": 54, "fan": "auto"},
    # A hob nobody is standing in front of. Reachable, and deliberately Tier 2.
    "cooktop": {"power": False},
    # LG smart fridges report an external (room) temperature and expose the
    # compressor's energy draw through ThinQ. That pair is the kitchen's real
    # thermal signal — a kitchen in a Korean apartment has no AC of its own.
    "fridge": {"power": True, "fridge_temp": 3, "freezer_temp": -18,
               "external_temp": 29.0, "duty_cycle": 0.35},
    "washer": {"power": False, "state": "standby", "remaining_min": 0,
               "ventilating": False},
    "dryer": {"power": False},
    "dehumidifier": {"power": False, "fan": "auto"},
}


class MockAdapter(DeviceAdapter):
    backend_name = "mock"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states = self._load()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._thread.start()

    # ---------- persistence ----------

    def _load(self) -> dict:
        states = json.loads(json.dumps(DEFAULT_STATES))
        if STATE_PATH.exists():
            try:
                saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                for k, v in saved.items():
                    if k in states:
                        states[k].update(v)
            except Exception:
                pass
        return states

    def _persist(self) -> None:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(
                json.dumps(self._states, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    # ---------- environment simulation ----------

    def _tick_loop(self) -> None:
        while not self._stop.wait(5):
            with self._lock:
                for st in self._states.values():
                    if "current_temp" in st:
                        target = st["current_temp"]
                        if st.get("power") and st.get("mode") in ("cool", "heat"):
                            goal = st.get("target_temp", 26)
                            target += max(-0.35, min(0.35, (goal - target) * 0.18))
                            target += random.uniform(-0.05, 0.05)
                        else:
                            target += random.uniform(-0.12, 0.14)
                        st["current_temp"] = round(min(34.0, max(16.0, target)), 1)

                    # A cooling unit pulls moisture out of the air, which is why
                    # the humidity tile moves in the opposite direction.
                    if st.get("humidity") is not None:
                        drying = st.get("power") and st.get("mode") in ("cool", "dry")
                        goal_h = 45 if drying else 58
                        st["humidity"] += max(-0.6, min(0.6, (goal_h - st["humidity"]) * 0.08))
                        st["humidity"] = round(min(90, max(20, st["humidity"])))

                    if st.get("pm25") is not None:
                        st["pm25"] = max(
                            5, st["pm25"] - (3 if st.get("power") else -2) + random.randint(-1, 1)
                        )

                    # The fridge reports the kitchen it stands in: a hotter room
                    # means a harder-working compressor. This is the signal the
                    # thermal index reads, because the kitchen has no AC.
                    if st.get("external_temp") is not None:
                        st["external_temp"] = round(
                            min(38.0, max(18.0, st["external_temp"] + random.uniform(-0.05, 0.05))), 1)
                        st["duty_cycle"] = round(
                            max(0.15, min(1.0, 0.15 + (st["external_temp"] - 18.0) / 20.0)), 2)
                self._persist()

    def stop(self) -> None:
        self._stop.set()

    # ---------- adapter interface ----------

    def list_devices(self) -> list[dict]:
        return [
            {**d,
             "online": True,
             "capabilities": CAPABILITIES.get(d["type"], ["power"]),
             "type_label": TYPE_LABELS.get(d["type"], d["type"])}
            for d in DEVICES
        ]

    def get_state(self, device_id: str) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._states.get(device_id, {})))

    def apply(self, device_id: str, actions: list[dict]) -> dict:
        if device_id not in self._states:
            return {"ok": False, "applied": [], "error": f"Device {device_id} not found"}

        applied = []
        dev = next((d for d in DEVICES if d["id"] == device_id), {"type": "", "name": device_id})
        caps = CAPABILITIES.get(dev["type"], ["power"])
        with self._lock:
            st = self._states[device_id]
            for act in actions:
                name, value = act.get("action"), act.get("value")
                if name not in caps:
                    return {"ok": False, "applied": applied,
                            "error": f"{dev['name']} does not support '{name}'"}
                if name == "power":
                    st["power"] = bool(value)
                    if device_id.startswith("ac_") and value:
                        st.setdefault("mode", "cool")
                    if device_id == "washer":
                        st["state"] = "washing" if value else "standby"
                        st["remaining_min"] = 45 if value else 0
                elif name == "ventilate":
                    # Drum micro-ventilation: the washer's own fan, safe to run
                    # with the door shut. This is the Tier-1 mold response —
                    # moisture out without opening the door for the user.
                    st["ventilating"] = bool(value)
                elif name == "temperature":
                    lo, hi = temperature_range(dev)
                    try:
                        value = int(round(float(value)))
                    except (TypeError, ValueError):
                        return {"ok": False, "applied": applied, "error": "Invalid temperature"}
                    if not lo <= value <= hi:
                        return {"ok": False, "applied": applied,
                                "error": f"{dev['name']} takes {lo}-{hi}\u00b0C, not {value}"}
                    # a fridge has a compartment setpoint, not a room setpoint
                    if dev["type"] == "refrigerator":
                        st["fridge_temp"] = value
                    else:
                        st["target_temp"] = value
                elif name == "mode":
                    if value in ("cool", "heat", "dry", "fan", "auto"):
                        st["mode"] = value
                    else:
                        return {"ok": False, "applied": applied, "error": f"Unsupported mode {value}"}
                elif name == "fan":
                    if value in ("auto", "low", "mid", "high"):
                        st["fan"] = value
                else:
                    return {"ok": False, "applied": applied, "error": f"Unsupported action {name}"}
                applied.append({"action": name, "value": value})
            self._persist()

        return {"ok": True, "applied": applied, "error": None}
