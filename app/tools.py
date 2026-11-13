"""Tools the model may call when the rule engine gives up.

Three rules hold this layer together, and all three exist because of a failure
mode that actually happened while building it:

1. **The model never supplies a timestamp.** `schedule` takes the user's own
   words and hands them to `nlu.build_rule`, which is deterministic and covered
   by the benchmark. A model that invents "23:30" cannot reach the job table.
2. **An invalid device id is a recoverable error, not an exception.** Asked to
   turn off the bedroom AC, `glm-4-flash` answered `bedroom_ac` when the real id
   is `ac_bedroom`. The tool replies with the valid ids so the next step can fix
   itself — which is what makes it a loop instead of a pipeline.
3. **Every call is validated here, not in the adapter.** The adapter's job is
   talking to appliances; the toolbox's job is refusing nonsense before it gets
   that far.
"""
from __future__ import annotations

import re
from datetime import datetime

from . import audit, db, nlu
from .devices.base import (FAN_LABELS, MODE_LABELS, capabilities_of,
                           describe_action, normalize_action, temperature_range)

ALLOWED_ACTIONS = ("power", "temperature", "mode", "fan")
_TRUE = {"true", "on", "yes", "1"}
_FALSE = {"false", "off", "no", "0"}

# Small models sometimes echo the tool name into its own argument
# ("finish\nThe AC is on"), and sometimes into prose instead of calling the
# tool at all. Cheap to clean, ugly to show a user.
_ECHOED_NAME = re.compile(r"^(?:finish|ask_user)\b[\s:：\-]*", re.I)


def clean_reply(text: str) -> str:
    """Strip a tool name the model pasted in front of its own sentence.

    Also unwraps a fully quoted reply. A small model that has been asked for
    "one short sentence" sometimes returns the sentence in quotes, and a chat
    bubble showing `"The purifier has been turned off."` reads like the agent
    is quoting somebody else.
    """
    out = _ECHOED_NAME.sub("", (text or "").strip()).strip()
    pairs = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}
    while len(out) >= 2 and out[0] in pairs and out[-1] == pairs[out[0]]:
        out = out[1:-1].strip()
    return out

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_devices",
            "description": "List every controllable device with its id, room and "
                           "whether it is currently on. Call this first if you are "
                           "not sure which device the user means.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_state",
            "description": "Read the current state of one device: power, target "
                           "temperature, mode, fan speed, room temperature.",
            "parameters": {
                "type": "object",
                "properties": {"device_id": {"type": "string"}},
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control",
            "description": "Change a device right now. Use one call per device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": list(ALLOWED_ACTIONS)},
                                "value": {
                                    "description": "true/false for power, an integer "
                                                   "in the device's temperature_range "
                                                   "for temperature (see list_devices), "
                                                   "a string for mode or fan",
                                },
                            },
                            "required": ["action", "value"],
                        },
                    },
                },
                "required": ["device_id", "actions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule",
            "description": "Create a task that runs later. Pass the user's own words "
                           "for the time — do not convert it to a clock time yourself, "
                           "a deterministic parser does that.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": list(ALLOWED_ACTIONS)},
                                "value": {},
                            },
                            "required": ["action", "value"],
                        },
                    },
                    "when_text": {
                        "type": "string",
                        "description": "The time as the user said it, e.g. "
                                       "'every night at 11 pm' or 'tomorrow at 7'",
                    },
                },
                "required": ["device_id", "actions", "when_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Stop and ask one short question when the request is "
                           "ambiguous or missing something you cannot look up.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "End the turn with a short sentence for the user. Call this "
                           "after the tools have done their work.",
            "parameters": {
                "type": "object",
                "properties": {"reply": {"type": "string"}},
                "required": ["reply"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adopt_parse",
            "description": "Accept the parser's reading of this sentence, shown in the "
                           "system prompt, and carry it out in one step. Use it when the "
                           "reading is right. Do not use it when the reading is wrong, "
                           "incomplete, or the request needs something the reading does "
                           "not cover — plan those yourself.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class ToolBox:
    """Executes tool calls and accumulates what the turn actually did."""

    def __init__(self, adapter, cfg: dict, now: datetime, default_room: str = "",
                 rule: dict | None = None, rule_label: str = "",
                 adopt=None) -> None:
        self.adapter = adapter
        self.cfg = cfg
        self.now = now
        self.default_room = default_room
        # A time the rule engine already read out of the user's own sentence.
        # When it is set, acting now is wrong no matter how the model reads the
        # request — "in 20 minutes" is not "now".
        self.rule = rule
        self.rule_label = rule_label
        # In agent mode the parser does not act; it makes an offer. The model
        # calls `adopt_parse` to accept it or ignores it and plans itself, which
        # is the difference between a workflow and an agent that has a fast path.
        self.adopt = adopt
        self.adopted = False
        # The offer the model was shown, kept so the trace can say what it
        # turned down. A trace that only lists the calls a model made cannot
        # distinguish "considered and chose otherwise" from "never told".
        self.offer: dict | None = None
        # What the turn did, step by step, so the interface can show the work
        # instead of asserting that work happened.
        self.trace: list[dict] = []

        self.actions: list[dict] = []
        self.targets: list[dict] = []
        self.reply: str | None = None
        self.question: str | None = None
        self.finished = False
        self.calls = 0

    # ------------------------------------------------ dispatch

    def call(self, name: str, args: dict) -> dict:
        self.calls += 1
        handler = getattr(self, "_t_" + str(name), None)
        if handler is None:
            return {"error": f"no such tool: {name}",
                    "available": ["list_devices", "get_state", "control",
                                  "schedule", "ask_user", "finish", "adopt_parse"]}
        if not isinstance(args, dict):
            return {"error": "arguments must be an object"}
        try:
            out = handler(**args)
        except TypeError as exc:
            return {"error": f"bad arguments: {exc}"}
        except Exception as exc:  # a tool must never take the turn down with it
            return {"error": f"{type(exc).__name__}: {exc}"}
        self.trace.append({"tool": name, "args": args, "result": _trace_line(out)})
        return out

    def _devices(self) -> list[dict]:
        return self.adapter.list_devices()

    def _state(self, device_id: str) -> dict:
        try:
            return self.adapter.get_state(device_id) or {}
        except Exception:
            return {}

    def _find(self, device_id: str) -> dict | None:
        for d in self._devices():
            if d["id"] == device_id:
                return d
        return None

    def _unknown_device(self, device_id: str) -> dict:
        known = [{"id": d["id"], "name": d["name"]} for d in self._devices()]
        return {"error": f"no device with id {device_id!r}", "devices": known}

    def _clean_actions(self, actions, device: dict) -> tuple[list[dict], str | None]:
        """Coerce what the model sends into the shapes the adapter accepts.

        Small models routinely emit `"power": "false"` or `"temperature": "24"`.
        Rejecting those would burn a step and teach nothing, so they are coerced;
        only values that cannot be read unambiguously are refused.

        The temperature window comes from the device, not from a constant. A
        fridge holds 1–7 °C and an AC 16–30 °C, and validating both against one
        range is what made "the fridge is too warm" impossible to answer.
        """
        if not isinstance(actions, list) or not actions:
            return [], "actions must be a non-empty array"
        lo, hi = temperature_range(device)
        caps = capabilities_of(device)
        out = []
        for raw in actions:
            if not isinstance(raw, dict):
                return [], "each action must be an object"
            act = normalize_action(raw)
            name, value = act["action"], act.get("value")

            if name not in ALLOWED_ACTIONS:
                return [], (f"unknown action {name!r}; use one of "
                            f"{', '.join(ALLOWED_ACTIONS)}")
            if name not in caps:
                return [], (f"{device.get('name', 'this device')} does not support "
                            f"{name!r}; it supports {', '.join(caps)}")

            if name == "power":
                if isinstance(value, bool):
                    pass
                elif isinstance(value, str) and value.strip().lower() in _TRUE:
                    value = True
                elif isinstance(value, str) and value.strip().lower() in _FALSE:
                    value = False
                elif isinstance(value, (int, float)) and value in (0, 1):
                    value = bool(value)
                else:
                    return [], "power must be true or false"

            elif name == "temperature":
                try:
                    value = int(round(float(value)))
                except (TypeError, ValueError):
                    return [], "temperature must be a whole number"
                if not lo <= value <= hi:
                    return [], (f"temperature for this device must be between "
                                f"{lo} and {hi} \u00b0C")

            elif name == "mode":
                value = str(value).strip().lower()
                if value not in MODE_LABELS:
                    return [], f"mode must be one of {', '.join(MODE_LABELS)}"

            elif name == "fan":
                value = str(value).strip().lower()
                if value not in FAN_LABELS:
                    return [], f"fan must be one of {', '.join(FAN_LABELS)}"

            out.append({"action": name, "value": value})
        return out, None

    # ------------------------------------------------ tools

    def _t_list_devices(self) -> dict:
        out = []
        for d in self._devices():
            state = self._state(d["id"])
            caps = capabilities_of(d)
            item = {
                "id": d["id"],
                "name": d["name"],
                "room": d.get("room", ""),
                "type": d.get("type_label") or d.get("type"),
                "online": d.get("online", True),
                "power": state.get("power"),
                "supports": caps,
            }
            if "temperature" in caps:
                lo, hi = temperature_range(d)
                item["temperature_range"] = f"{lo}-{hi} \u00b0C"
            out.append(item)
        return {"devices": out}

    def _t_get_state(self, device_id: str = "") -> dict:
        d = self._find(device_id)
        if not d:
            return self._unknown_device(device_id)
        caps = capabilities_of(d)
        out = {
            "device": d["name"],
            "room": d.get("room", ""),
            "online": d.get("online", True),
            "supports": caps,
            "state": self._state(d["id"]),
        }
        if "temperature" in caps:
            lo, hi = temperature_range(d)
            out["temperature_range"] = f"{lo}-{hi} \u00b0C"
        return out

    def _t_control(self, device_id: str = "", actions=None) -> dict:
        # The second half of the timestamp guardrail. `schedule` refuses a
        # model-supplied clock time; this refuses the opposite mistake — acting
        # immediately on a sentence that named a time. Measured: "put the living
        # room to sleep in 20 minutes" ran the scene on the spot, twice, and the
        # user was told it had been scheduled.
        if self.rule:
            return {"error": f"this request has a time ({self.rule_label!r}), so "
                             f"nothing may happen now",
                    "use": "schedule",
                    "hint": f"call schedule with when_text={self.rule_label!r}"}

        d = self._find(device_id)
        if not d:
            return self._unknown_device(device_id)
        if not d.get("online", True):
            return {"error": f"{d['name']} is offline, nothing was sent"}

        clean, err = self._clean_actions(actions, d)
        if err:
            return {"error": err, "supports": capabilities_of(d),
                    "temperature_range": "-".join(
                        str(x) for x in temperature_range(d)) + " \u00b0C"}

        res = audit.execute(self.adapter, d["id"], clean)
        if not res.get("ok"):
            return {"error": res.get("error") or "the device rejected the command",
                    "device": d["name"]}

        self.actions.extend({**a, "device_id": d["id"], "device_name": d["name"]}
                            for a in clean)
        self.targets.append({"id": d["id"], "name": d["name"], "actions": clean})
        db.log("tool.control", f"{d['name']} {clean} -> ok")
        return {
            "ok": True,
            "device": d["name"],
            "did": ", ".join(describe_action(a) for a in clean),
            "state": self._state(d["id"]),
        }

    def _t_schedule(self, device_id: str = "", actions=None, when_text: str = "") -> dict:
        d = self._find(device_id)
        if not d:
            return self._unknown_device(device_id)

        clean, err = self._clean_actions(actions, d)
        if err:
            return {"error": err, "supports": capabilities_of(d),
                    "temperature_range": "-".join(
                        str(x) for x in temperature_range(d)) + " \u00b0C"}

        # The guardrail: the model hands over the words, not a timestamp.
        rule, label = nlu.build_rule(str(when_text or ""), self.now)
        if not rule:
            return {
                "error": f"could not read a time from {when_text!r}",
                "hint": "pass the user's own words, e.g. 'every night at 11 pm', "
                        "'tomorrow at 7 am', 'in 30 minutes'",
            }

        nxt = nlu.compute_next(rule, self.now)
        if nxt is None:
            return {"error": "that time is in the past"}

        job_id = db.add_job(
            device_id=d["id"],
            device_name=d["name"],
            actions=clean,
            rule=rule,
            next_run_at=nxt.isoformat(timespec="seconds"),
            repeat_label=label,
            source_text=str(when_text or ""),
        )
        self.actions.extend({**a, "device_id": d["id"], "device_name": d["name"]}
                            for a in clean)
        self.targets.append({"id": d["id"], "name": d["name"], "actions": clean,
                             "scheduled": job_id})
        db.log("tool.schedule", f"{d['name']} {clean} {rule} -> job {job_id}")
        return {
            "ok": True,
            "job_id": job_id,
            "device": d["name"],
            "repeat": label or "once",
            "next_run": nxt.strftime("%b %d, %H:%M"),
        }

    def _t_ask_user(self, question: str = "") -> dict:
        self.question = clean_reply(question) or "Which device do you mean?"
        self.finished = True
        return {"ok": True, "asked": self.question}

    def _t_finish(self, reply: str = "") -> dict:
        self.reply = clean_reply(reply) or None
        self.finished = True
        return {"ok": True, "replied": self.reply or ""}

    def _t_adopt_parse(self) -> dict:
        """Carry out the parser's reading. The model chose to trust it."""
        if self.adopt is None:
            return {"error": "there is no parser reading for this turn"}
        out = self.adopt() or {}
        self.adopted = True
        self.reply = out.get("reply")
        self.actions = out.get("actions") or []
        self.targets = out.get("targets") or []
        self.finished = True
        return {"ok": bool(out.get("ok")),
                "done": self.reply,
                "actions": self.actions}

    # ------------------------------------------------ result

    def summary(self) -> str:
        """What to say when the model stopped without a `finish` call.

        Phrased per device. A whole-home request produces one identical action
        per room, and flattening those gave "turn off, turn off, turn off" —
        which names neither the devices nor the rooms.
        """
        if not self.targets:
            return ""

        names = [t["name"] for t in self.targets]
        plans = [t.get("actions") or [] for t in self.targets]

        # the usual whole-home case: one shared action, so say it once
        if plans[0] and all(p == plans[0] for p in plans):
            did = ", ".join(_short(a) for a in plans[0])
            return f"Done \u2014 {_and(names)}: {did}."

        parts = []
        for name, acts in zip(names, plans):
            tail = ", ".join(_short(a) for a in acts) if acts else "done"
            parts.append(f"{name} {tail}")
        return "Done \u2014 " + "; ".join(parts) + "."


def _trace_line(out: dict) -> str:
    """One line for the turn trace: what a tool *did*, not what it returned.

    Shown to the user, so it reads as a decision, not as a JSON blob. The
    failure branch is the one that earns the most — a step that came back
    `{"error": ...}` is the loop recovering from itself, and hiding it makes
    the agent look like a pipeline that never fails.
    """
    if not isinstance(out, dict):
        return ""
    if out.get("error"):
        head = str(out["error"])
        return head[:1].upper() + head[1:]
    if "devices" in out:
        return f"listed {len(out['devices'])} devices"
    if "did" in out:
        return f"{out.get('device')} \u2014 {out['did']}"
    if "job_id" in out:
        return f"scheduled {out.get('device')} ({out.get('repeat')})"
    if "asked" in out:
        return "asked a question"
    if "replied" in out:
        return "replied to the user"
    if "done" in out:
        done = str(out["done"] or "").strip().replace("\n", " / ")
        return f"adopted the parser's reading \u2014 {done[:60]}"
    if "state" in out:
        state = out.get("state") or {}
        power = state.get("power")
        want = "on" if power else ("off" if power is not None else "unknown")
        return f"read {out.get('device')} \u2014 {want}"
    return "done"


def _short(a: dict) -> str:
    """One action as a noun-ish fragment: "off", "24°C", "cool", "fan low"."""
    name, value = a.get("action"), a.get("value")
    if name == "power":
        return "on" if value else "off"
    if name == "temperature":
        return f"{value}\u00b0C"
    if name == "mode":
        return MODE_LABELS.get(value, str(value))
    if name == "fan":
        return f"fan {FAN_LABELS.get(value, value)}"
    return f"{name}={value}"


def _and(items: list[str]) -> str:
    """'a, b and c' — a comma before the last name reads wrong in a sentence."""
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]

