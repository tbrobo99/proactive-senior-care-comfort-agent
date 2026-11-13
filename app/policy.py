"""Risk-graded action policy (Phase 2).

The proposal's central safety claim: actions are split into two tiers, and no
Tier-2 action ever executes without explicit user approval. This module is
where that claim is enforced — the only place in the codebase that decides
whether a proposed action may run on its own.

Tier 1 (automatic, logged, reported): drum micro-ventilation, dehumidifier,
air purifier, kitchen exhaust, and pre-cooling the kitchen before cooking.
Reversible, low-power, and — the property that actually matters — worst case is
a slightly dry room or a fan running for nothing, never a ruined garment, a
delayed meal, or a hot surface nobody is watching.

Tier 2 (approval required): anything that changes what the user planned or
touches a *heating* appliance. Stopping a running dryer is one case; switching
the cooktop on ahead of a meal is the other. Both change a plan or put heat
where a person is not standing, so the user decides. Nothing here schedules a
restart: the user starts the appliance again by hand.

`candidates()` turns a risk into the actions worth considering. It deliberately
produces a *set*, not a plan: the decision layer chooses among these, in the
order it defends. Rules propose; the agent disposes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

TIER1 = "tier1"
TIER2 = "tier2"

# The whitelist. An action kind that is not in this table does not exist as far
# as the policy layer is concerned — nothing downstream can invent one.
#
# The table is exactly what `candidates()` can produce, and a test enforces
# that: an entry no rule ever proposes is a capability the code claims and does
# not have. `reschedule_washer` was removed for that reason — there is no rule
# that would ever want to move the washer, so the entry was a claim with nothing
# behind it. `preheat_cooktop` was removed for the same reason when the
# appliance set had no cooktop; it is back, and so is the cooktop.
ACTION_TIERS: dict[str, str] = {
    "ventilate_washer": TIER1,
    "dehumidifier_on": TIER1,
    "purifier_on": TIER1,
    "ventilate_kitchen": TIER1,
    "precool_kitchen": TIER1,
    "stop_dryer": TIER2,
    "preheat_cooktop": TIER2,
}

# How long a Tier-2 request waits before the fail-safe timeout (poster §II.E).
APPROVAL_TTL_SECONDS = 300


@dataclass
class Candidate:
    kind: str
    device_id: str
    device_name: str
    actions: list[dict]
    label: str            # one line, shown to the user and the LLM
    tier: str = ""
    # Past-tense form for the report the user reads *after* the action ran:
    # "I started drum micro-ventilation", not "I start drum micro-ventilation".
    done: str = ""
    reason: str = ""      # why the rule proposed it — evidence, not decoration

    def __post_init__(self) -> None:
        if not self.tier:
            self.tier = tier_of(self.kind)
        if not self.done:
            self.done = self.label


def tier_of(kind: str) -> str:
    return ACTION_TIERS.get(kind, TIER2)  # unknown kinds default to the safe tier


def is_whitelisted(kind: str) -> bool:
    """FR-3: an action outside the table is blocked and logged, never executed."""
    return kind in ACTION_TIERS


# Which risk each action answers. Revalidation checks that risk is still active.
ACTION_RISK: dict[str, str] = {
    "ventilate_washer": "mold",
    "dehumidifier_on": "mold",
    "purifier_on": "mold",
    "ventilate_kitchen": "thermal",
    "precool_kitchen": "thermal",
    "stop_dryer": "thermal",
    "preheat_cooktop": "thermal",
}


def revalidate(item: dict, snapshot: dict, active: dict) -> tuple[bool, str]:
    """FR-5: check, immediately before the adapter call, that an action still
    makes sense. `item` is a candidate dict, `active` maps risk kind -> Risk for
    every risk currently at or above its threshold.

    Returns (ok, reason). The reason is plain language: it is shown to the user
    when a confirmed action is not carried out.
    """
    kind = item.get("kind") or ""
    if not is_whitelisted(kind):
        return False, "that action is not on the list the house may perform"
    if kind == "preheat_cooktop":
        # Preheating prepares a meal; it does not relieve the overload. So it
        # is not tied to the thermal index still being high (stopping the
        # dryer alone usually clears it) — only to the meal, the person and
        # the cooktop itself.
        if snapshot.get("appliances", {}).get("cooktop", {}).get("power"):
            return False, "the cooktop is already on"
        if not snapshot.get("occupancy", {}).get("user_home"):
            return False, "nobody is home to watch the cooktop"
        if cooking_starts_in_min(snapshot) is None:
            return False, "there is no cooking on the calendar any more"
        return True, ""
    rk = ACTION_RISK.get(kind)
    if rk not in active:
        what = "the washer is no longer at risk of mould" if rk == "mold" \
            else "the kitchen is no longer overheating"
        return False, what

    ap = snapshot.get("appliances", {})
    if kind == "ventilate_washer" and ap.get("washer", {}).get("ventilating"):
        return False, "the washer is already being aired"
    if kind == "dehumidifier_on" and ap.get("dehumidifier", {}).get("power"):
        return False, "the dehumidifier is already on"
    if kind == "purifier_on" and ap.get("purifier", {}).get("power"):
        return False, "the air purifier is already on"
    if kind == "ventilate_kitchen" and ap.get("range_hood", {}).get("power"):
        return False, "the range hood is already on"
    if kind == "precool_kitchen" and \
            (snapshot.get("ac_targets") or {}).get("Kitchen", {}).get("power"):
        return False, "the kitchen air conditioner is already on"
    if kind == "stop_dryer" and not ap.get("dryer", {}).get("power"):
        return False, "the dryer is already off"
    return True, ""


def cooking_starts_in_min(snapshot: dict, now: datetime | None = None,
                          lookahead_min: float = 30) -> float | None:
    """Minutes until the soonest calendar entry starting within the lookahead
    window (the same window the thermal index uses), or None."""
    now = now or datetime.now()
    soonest = None
    for entry in snapshot.get("calendar") or []:
        try:
            start = datetime.fromisoformat(entry["start"])
        except (KeyError, TypeError, ValueError):
            continue
        delta = (start - now).total_seconds() / 60.0
        if 0 <= delta <= lookahead_min and (soonest is None or delta < soonest):
            soonest = delta
    return soonest


def candidates(risk, snapshot: dict) -> list[Candidate]:
    """Propose actions for a fired risk. Order is a suggestion, not a decision."""
    ap = snapshot["appliances"]
    rooms = snapshot["rooms"]
    out: list[Candidate] = []

    if risk.kind == "mold":
        w = ap["washer"]
        if not w.get("ventilating"):
            out.append(Candidate(
                kind="ventilate_washer",
                device_id="washer",
                device_name="Balcony Washer",
                actions=[{"action": "ventilate", "value": True}],
                label="start drum micro-ventilation on the washer",
                done="started drum micro-ventilation on the washer",
                reason=f"drum at {risk.current:.0f}% humidity, sealed {risk.drivers.get('sealed_hours', 0):.1f} h",
            ))
        balcony = rooms.get("Balcony", {}).get("humidity", 60)
        if balcony > 55 and not ap["dehumidifier"].get("power"):
            out.append(Candidate(
                kind="dehumidifier_on",
                device_id="dehumidifier",
                device_name="Balcony Dehumidifier",
                actions=[{"action": "power", "value": True}],
                label="turn on the balcony dehumidifier",
                done="turned on the balcony dehumidifier",
                reason=f"balcony humidity {balcony:.0f}%",
            ))
        # A damp drum is also an airborne problem: the flat breathes what the
        # balcony grows. The purifier is the cheapest reversible answer, and
        # the proposal names it explicitly as a Tier-1 action.
        if not ap.get("purifier", {}).get("power"):
            out.append(Candidate(
                kind="purifier_on",
                device_id="purifier",
                device_name="Living Room Purifier",
                actions=[{"action": "power", "value": True}],
                label="turn on the living room air purifier to catch mould spores",
                done="turned on the living room air purifier",
                reason=(f"mould risk {risk.score:.0f}/100; a wet drum that has been "
                        f"sealed for hours is shedding spores into the flat"),
            ))

    elif risk.kind == "thermal":
        if ap["dryer"].get("power"):
            # Stop only. An earlier version also scheduled a restart at 23:00,
            # which claimed three things we cannot keep: that 23:00 is cheaper
            # (no evidence), that restoring power resumes the drying cycle (it
            # depends on the appliance), and that an approval given now still
            # applies to a machine that starts itself hours later. The user
            # restarts it, and is told so before approving.
            out.append(Candidate(
                kind="stop_dryer",
                device_id="dryer",
                device_name="Balcony Dryer",
                actions=[{"action": "power", "value": False}],
                label="turn the dryer off now; you would start it again yourself",
                done="turned the dryer off, and left it for you to restart",
                tier=TIER2,
                reason=f"{snapshot.get('heat_load_w', 0)} W of heat load, kitchen at {risk.current:.1f}\u00b0C",
            ))
        # The fridge duty is the evidence: past 60% the compressor is paying
        # for the heat. Exhausting is the cheapest thing that helps right now.
        hood = ap.get("range_hood", {})
        if not hood.get("power"):
            out.append(Candidate(
                kind="ventilate_kitchen",
                device_id="range_hood",
                device_name="Kitchen Range Hood",
                actions=[{"action": "power", "value": True},
                         {"action": "fan", "value": "high"}],
                label="turn on the kitchen range hood to exhaust the heat",
                done="turned on the kitchen range hood to pull the heat out",
                reason=(f"fridge compressor at "
                        f"{snapshot['appliances'].get('fridge', {}).get('duty_cycle', 0):.0%} duty, "
                        f"predicted {risk.predicted:.1f}\u00b0C in "
                        f"{risk.drivers.get('cooking_scheduled_in_min') or 30} min"),
            ))

        # A cooking session is on the calendar. Two things follow from it, and
        # they sit on opposite sides of the line on purpose: cooling the room is
        # cheap, reversible and invisible, so the house does it; lighting a hob
        # is a heating appliance with nobody standing next to it, so the house
        # asks. Same event, different stakes — which is the whole thesis.
        cooking_in = risk.drivers.get("cooking_scheduled_in_min")
        if cooking_in is not None:
            kitchen_ac = (snapshot.get("ac_targets") or {}).get("Kitchen", {})
            if not kitchen_ac.get("power"):
                out.append(Candidate(
                    kind="precool_kitchen",
                    device_id="ac_kitchen",
                    device_name="Kitchen AC",
                    actions=[{"action": "power", "value": True},
                             {"action": "mode", "value": "cool"},
                             {"action": "temperature", "value": 24}],
                    label="cool the kitchen to 24\u00b0C before the cooking starts",
                    done="cooled the kitchen to 24\u00b0C ahead of the cooking",
                    reason=(f"cooking starts in {cooking_in} min and the kitchen "
                            f"is at {risk.current:.1f}\u00b0C, so the heat would "
                            f"arrive into a room already carrying it"),
                ))
            if not ap.get("cooktop", {}).get("power"):
                # The reason is evidence for the decision layer, not a warning
                # label. Saying "a hob nobody is watching" here made the model
                # drop this candidate in 5 runs out of 5 — the same failure as
                # the old "this one needs approval" sentence in DECIDE_SYSTEM.
                # The safety argument belongs in the tier table, which is
                # enforced in code, not in the text the model reads.
                out.append(Candidate(
                    kind="preheat_cooktop",
                    device_id="cooktop",
                    device_name="Kitchen Cooktop",
                    actions=[{"action": "power", "value": True}],
                    label="switch the cooktop on now so it is hot when you start",
                    done="switched the cooktop on ahead of the cooking",
                    tier=TIER2,
                    reason=(f"cooking starts in {cooking_in} min and the cooktop "
                            f"is off; it takes about five minutes to reach heat"),
                ))

    return out
