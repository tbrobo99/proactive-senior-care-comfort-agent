"""Device adapter abstraction.

The agent and the scheduler only know this interface. They do not care whether
the devices are real LG appliances in the cloud or the local simulator.

Actions are normalised to {"action": "...", "value": ...} so they can be stored
in SQLite, replayed, and audited.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

# device type -> supported capabilities
CAPABILITIES: dict[str, list[str]] = {
    "air_conditioner": ["power", "temperature", "mode", "fan"],
    "air_purifier": ["power", "fan"],
    "washer": ["power", "ventilate"],
    "dryer": ["power"],
    "refrigerator": ["power", "temperature"],
    "dish_washer": ["power"],
    "water_heater": ["power", "temperature"],
    "dehumidifier": ["power", "fan"],
    "range_hood": ["power", "fan"],
    "oven": ["power", "temperature"],
    "cooktop": ["power"],
}

# A setpoint is not one range. 22 °C is a comfortable room and a fridge disaster;
# a fridge at 3 °C is correct and an AC at 3 °C is nonsense. Keeping a single
# 16–30 window in the tool schema made "the fridge is too warm" unanswerable:
# the model had no valid value to send, so it sent something else.
TEMPERATURE_RANGE: dict[str, tuple[int, int]] = {
    "air_conditioner": (16, 30),
    "refrigerator": (1, 7),
    "water_heater": (30, 60),
    "oven": (50, 250),
}
DEFAULT_TEMPERATURE_RANGE = (16, 30)


def temperature_range(device: dict) -> tuple[int, int]:
    return TEMPERATURE_RANGE.get(device.get("type", ""), DEFAULT_TEMPERATURE_RANGE)


def capabilities_of(device: dict) -> list[str]:
    return list(device.get("capabilities")
                or CAPABILITIES.get(device.get("type", ""), ["power"]))

TYPE_LABELS = {
    "air_conditioner": "Air Conditioner",
    "air_purifier": "Air Purifier",
    "washer": "Washer",
    "dryer": "Dryer",
    "refrigerator": "Refrigerator",
    "dish_washer": "Dishwasher",
    "water_heater": "Water Heater",
    "dehumidifier": "Dehumidifier",
    "styler": "Styler",
    "robot_cleaner": "Robot Cleaner",
    "oven": "Oven",
    "range_hood": "Kitchen Range Hood",
    "cooktop": "Cooktop",
}

MODE_LABELS = {
    "cool": "cooling",
    "heat": "heating",
    "dry": "dehumidifying",
    "fan": "fan only",
    "auto": "auto",
}

FAN_LABELS = {"auto": "auto", "low": "low", "mid": "medium", "high": "high"}


class DeviceAdapter(ABC):
    """Every device backend implements these four methods."""

    backend_name = "unknown"

    @abstractmethod
    def list_devices(self) -> list[dict]:
        """Return [{"id","name","type","room","online","capabilities"}]."""

    @abstractmethod
    def get_state(self, device_id: str) -> dict:
        """Return the current state, always including a `power` field."""

    @abstractmethod
    def apply(self, device_id: str, actions: list[dict]) -> dict:
        """Apply a list of actions. Returns {"ok","applied","error"}."""

    def get_device(self, device_id: str) -> dict | None:
        for d in self.list_devices():
            if d["id"] == device_id:
                return d
        return None


def normalize_action(action: dict) -> dict:
    """Fold action aliases into the canonical names used internally."""
    name = str(action.get("action", "")).strip().lower()
    alias = {
        "power_on": ("power", True),
        "power_off": ("power", False),
        "on": ("power", True),
        "off": ("power", False),
        "temp": ("temperature", action.get("value")),
        "target_temp": ("temperature", action.get("value")),
    }
    if name in alias:
        k, v = alias[name]
        return {"action": k, "value": v}
    return {"action": name, "value": action.get("value")}


def describe_action(action: dict) -> str:
    """Human-readable form of a single action, used in replies and logs."""
    name = action.get("action")
    v = action.get("value")
    if name == "power":
        return "turn on" if v else "turn off"
    if name == "temperature":
        return f"set to {v}\u00b0C"
    if name == "mode":
        return f"switch to {MODE_LABELS.get(v, v)}"
    if name == "fan":
        return f"fan {FAN_LABELS.get(v, v)}"
    return f"{name}={v}"
