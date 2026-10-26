"""Predictive risk assessment (Phase 2): MRI and TOI.

Two indices, both 0..100, both explainable — every point of a score names the
driver that earned it, because a senior-care product that cannot say *why* it
is worried is not a product anyone should ship.

MRI — Mold Risk Index. Humidity trapped in a sealed post-wash drum, corrected
for how long it has been sealed and how warm the room is. The prediction is a
least-squares slope over the recent drum-humidity history, projected 30 minutes
ahead: the intervention is worth making when the drum will be worse later, not
only when it is bad now.

TOI — Thermal Overload Index. Heat-generating load (oven, cooktop, dryer,
dishwasher) measured against the room's ability to shed it, plus a calendar
term: a cooking session scheduled within the next 30 minutes is a load that has
not arrived yet, and cooling or ventilating before it does is only useful ahead
of time.

The kitchen's temperature is read through the fridge, not through its air
conditioner: an AC reports the setpoint it was told, so it can say what it is
doing and never what the room is. An LG smart fridge reports an external
temperature plus the compressor's duty cycle through ThinQ, and a compressor
working harder is a kitchen getting hotter — the fridge is the appliance that
pays for it, which is the whole reason this index exists.

Neither index is a black box and neither is trained. They are the cheap,
deterministic part of the decision loop: they run every tick without an LLM in
the way, and their only job is to raise a flag the decision layer then acts on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

MOLD_THRESHOLD = 50.0
THERMAL_THRESHOLD = 50.0
PREDICT_MINUTES = 30

# A cooking session this close counts as present load: cooling or ventilating
# after the fact is just a fan running for nothing.
CALENDAR_LOOKAHEAD_MIN = 30


@dataclass
class Risk:
    kind: str                 # "mold" | "thermal"
    score: float              # 0..100
    metric: str               # the headline number, e.g. "drum_humidity"
    current: float            # its value now
    predicted: float          # its value PREDICT_MINUTES from now
    threshold: float
    drivers: dict = field(default_factory=dict)

    @property
    def headline(self) -> str:
        if self.kind == "mold":
            return (f"Mold risk {self.score:.0f}/100 on the washer drum "
                    f"({self.current:.0f}% humidity, predicted {self.predicted:.0f}% "
                    f"in {PREDICT_MINUTES} min)")
        return (f"Thermal overload {self.score:.0f}/100 in the kitchen "
                f"({self.current:.1f}\u00b0C, predicted {self.predicted:.1f}\u00b0C "
                f"in {PREDICT_MINUTES} min)")


def slope_per_second(series) -> float:
    """Least-squares slope of (timestamp, value) points, in units/second."""
    pts = [(t, v) for t, v in series if v is not None]
    if len(pts) < 3:
        return 0.0
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() for t, _ in pts]
    ys = [v for _, v in pts]
    n = len(pts)
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return 0.0
    return num / den


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def mold_risk(snapshot: dict, history, now: datetime | None = None) -> Risk | None:
    """MRI over the washer drum. None when there is nothing sealed to worry about."""
    now = now or datetime.now()
    w = snapshot["appliances"]["washer"]
    if w.get("cycle") != "done" or not w.get("door_closed"):
        return None

    humidity = float(w.get("drum_humidity") or 0.0)
    sealed_hours = snapshot.get("sealed_hours", 0.0)
    room_temp = snapshot["rooms"].get("Balcony", {}).get("temp", 26.0)

    # Humidity carries most of the score; sealed time and warmth are multipliers
    # on the same physical story — wet, warm, trapped.
    score = _clamp(
        (humidity - 70.0) * 3.2
        + sealed_hours * 6.0
        + max(0.0, room_temp - 24.0) * 1.5
    )
    series = history.get("washer.drum_humidity") if history else None
    slope = slope_per_second(series) if series else 0.0
    predicted = _clamp(humidity + slope * PREDICT_MINUTES * 60, 0, 100)

    return Risk(
        kind="mold",
        score=score,
        metric="drum_humidity",
        current=humidity,
        predicted=predicted,
        threshold=MOLD_THRESHOLD,
        drivers={
            "drum_humidity": round(humidity, 1),
            "sealed_hours": round(sealed_hours, 2),
            "balcony_temp": round(room_temp, 1),
            "humidity_trend_per_min": round(slope * 60, 3),
            "user_home": snapshot["occupancy"].get("user_home", True),
        },
    )


def thermal_risk(snapshot: dict, history, now: datetime | None = None) -> Risk | None:
    """TOI over the kitchen. None when no heat source is on or imminent."""
    now = now or datetime.now()
    heat_w = int(snapshot.get("heat_load_w") or 0)
    kitchen = snapshot["rooms"].get("Kitchen", {})
    kitchen_temp = float(kitchen.get("temp") or 0.0)
    # The fridge is the kitchen's sensor node. Its probe lags the room, so a
    # rising duty cycle is the earliest honest signal that heat is accumulating.
    fridge = snapshot["appliances"].get("fridge", {})
    fridge_external = float(fridge.get("external_temp") or kitchen_temp)
    fridge_duty = float(fridge.get("duty_cycle") or 0.0)

    cooking_soon = False
    soonest = None
    for entry in snapshot.get("calendar") or []:
        try:
            start = datetime.fromisoformat(entry["start"])
        except (KeyError, ValueError):
            continue
        delta = (start - now).total_seconds() / 60.0
        if 0 <= delta <= CALENDAR_LOOKAHEAD_MIN and (soonest is None or delta < soonest):
            soonest, cooking_soon = delta, True

    if heat_w <= 0 and not cooking_soon:
        return None

    # Load ratio: 2500 W is a full kitchen; each degree above 27 °C is a room
    # that has stopped shedding heat; a compressor past 60% duty is a fridge
    # paying for the kitchen's heat; a scheduled oven is load in advance.
    score = _clamp(
        heat_w / 25.0
        + max(0.0, kitchen_temp - 27.0) * 6.0
        + max(0.0, fridge_duty - 0.6) * 60.0
        + (15.0 if cooking_soon else 0.0)
    )
    series = history.get("Kitchen.temp") if history else None
    slope = slope_per_second(series) if series else 0.0
    predicted = kitchen_temp + slope * PREDICT_MINUTES * 60

    return Risk(
        kind="thermal",
        score=score,
        metric="kitchen_temp",
        current=kitchen_temp,
        predicted=round(predicted, 1),
        threshold=THERMAL_THRESHOLD,
        drivers={
            "heat_load_w": heat_w,
            "kitchen_temp": round(kitchen_temp, 1),
            "fridge_external_temp": round(fridge_external, 1),
            "fridge_duty_cycle": round(fridge_duty, 2),
            "cooking_scheduled_in_min": round(soonest) if soonest is not None else None,
            "temp_trend_per_min": round(slope * 60, 3),
            "user_home": snapshot["occupancy"].get("user_home", True),
        },
    )


def evaluate(snapshot: dict, history, now: datetime | None = None) -> list[Risk]:
    """Every risk that currently applies, highest score first."""
    now = now or datetime.now()
    out = []
    for fn in (mold_risk, thermal_risk):
        try:
            r = fn(snapshot, history, now)
        except Exception:
            r = None
        if r is not None:
            out.append(r)
    out.sort(key=lambda r: r.score, reverse=True)
    return out
