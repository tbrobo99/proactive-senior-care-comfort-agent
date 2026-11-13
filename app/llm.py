"""Optional LLM layer.

Off by default. When enabled it handles phrasing the rule engine cannot parse
("it feels stuffy in here"), but it still emits the same intent structure, so
execution stays in deterministic code.

Any failure — timeout, non-JSON, invalid fields — silently falls back to the
rule engine, so the assistant never becomes unusable.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime

from . import nlu

SYSTEM_PROMPT = """You are a smart-home intent parser. Convert the user's message into JSON.
Output JSON only, no explanation.

Current time: {now}
Controllable devices:
{devices}

Schema:
{{
  "intent": "control" | "schedule" | "query" | "cancel" | "list" | "unknown",
  "device_id": "device id",
  "actions": [{{"action": "power", "value": true}}],
  "rule": null
        | {{"type": "once", "run_at": "YYYY-MM-DDTHH:MM:SS"}}
        | {{"type": "daily", "at": "HH:MM"}}
        | {{"type": "weekday", "at": "HH:MM"}}
        | {{"type": "weekly", "at": "HH:MM", "weekdays": [0,1,2]}}
        | {{"type": "interval", "interval_seconds": 3600}},
  "clarify": null or "one short question to ask the user"
}}

Allowed actions:
- action="power"       value=true/false
- action="temperature" value=integer 16..30
- action="mode"        value="cool"|"heat"|"dry"|"fan"|"auto"
- action="fan"         value="auto"|"low"|"mid"|"high"

Rules:
1. A specific time with no "every" means a one-time task; run_at must be in the future.
2. Weekdays: 0=Monday ... 6=Sunday.
3. If the user does not name a device, pick the most sensible one. Never invent ids.
4. If you cannot tell, use intent="unknown" and ask one short question in clarify."""


def available(cfg: dict) -> bool:
    llm = cfg.get("llm", {})
    return bool(llm.get("enabled") and llm.get("api_key"))


def _device_brief(devices: list[dict]) -> str:
    lines = []
    for d in devices:
        lines.append(
            f"- id={d['id']} name={d['name']} type={d.get('type_label') or d['type']} "
            f"room={d.get('room') or 'unknown'} online={'yes' if d.get('online', True) else 'no'}"
        )
    return "\n".join(lines)


def chat(cfg: dict, messages: list[dict], tools: list[dict] | None = None,
         json_mode: bool = False, temperature: float = 0.1,
         max_tokens: int = 600, timeout: float | None = None) -> dict:
    """One round trip. Returns the assistant message dict unchanged.

    `tools` turns this into the agent-loop call: no JSON mode, and the reply may
    carry `tool_calls` instead of content. A plain-text call passes no tools,
    so the model has no way to ask for a device action.

    `timeout` lets a caller ask for a shorter wait than the configured default.
    A greeting should fall back to its local wording in a couple of seconds,
    not sit behind the 20 s budget a tool loop needs.
    """
    llm = cfg["llm"]
    url = (llm.get("base_url") or "").rstrip("/") + "/chat/completions"
    payload = {
        "model": llm.get("model"),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {llm.get('api_key')}",
    })
    wait = float(timeout if timeout is not None else llm.get("timeout", 20))
    with urllib.request.urlopen(req, timeout=wait) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]


def _call(cfg: dict, messages: list[dict]) -> str:
    return chat(cfg, messages, json_mode=True).get("content") or ""


def _validate(raw: dict, devices: list[dict], now: datetime) -> dict | None:
    if not isinstance(raw, dict):
        return None
    intent = raw.get("intent")
    if intent not in ("control", "schedule", "query", "cancel", "list", "unknown"):
        return None

    by_id = {d["id"]: d for d in devices}
    device = by_id.get(raw.get("device_id"))
    if device is None and devices:
        device, _ = nlu.match_device(str(raw.get("device_id") or ""), devices)
    if device is None and intent in ("control", "schedule"):
        return None

    actions = []
    for a in raw.get("actions") or []:
        if not isinstance(a, dict):
            continue
        name, value = a.get("action"), a.get("value")
        if name == "power":
            actions.append({"action": "power", "value": bool(value)})
        elif name == "temperature":
            try:
                actions.append({"action": "temperature",
                                "value": max(16, min(30, int(float(value))))})
            except (TypeError, ValueError):
                pass
        elif name == "mode" and value in ("cool", "heat", "dry", "fan", "auto"):
            actions.append({"action": "mode", "value": value})
        elif name == "fan" and value in ("auto", "low", "mid", "high"):
            actions.append({"action": "fan", "value": value})

    rule = raw.get("rule")
    if intent == "schedule":
        if not isinstance(rule, dict) or not rule.get("type"):
            return None
        if rule["type"] not in ("once", "daily", "weekday", "weekly", "interval"):
            return None
        if nlu.compute_next(rule, now) is None:
            return None

    return {
        "intent": intent,
        "device_id": device["id"] if device else None,
        "device_name": device["name"] if device else None,
        "actions": actions,
        "rule": rule if intent == "schedule" else None,
        "repeat_label": _label(rule) if intent == "schedule" else "",
        "confidence": 0.8,
        "clarify": raw.get("clarify"),
        "raw_text": "",
        "source": "llm",
    }


def _label(rule: dict) -> str:
    t = rule.get("type")
    at = rule.get("at", "")
    if t == "daily":
        return f"every day at {at}"
    if t == "weekday":
        return f"every weekday at {at}"
    if t == "weekly":
        names = ", ".join(nlu.DAY_NAMES[d] for d in rule.get("weekdays", []))
        return f"every {names} at {at}"
    if t == "interval":
        sec = int(rule.get("interval_seconds", 3600))
        return f"every {sec // 3600} h" if sec >= 3600 else f"every {sec // 60} min"
    if t == "once":
        try:
            return datetime.fromisoformat(rule["run_at"]).strftime("%b %d, %H:%M")
        except Exception:
            return "one time"
    return ""


def plan(cfg: dict, text: str, devices: list[dict], now: datetime,
         default_room: str = "Living Room") -> dict | None:
    """Return an intent dict, or None so the caller falls back to rules."""
    if not available(cfg):
        return None
    system = SYSTEM_PROMPT.format(
        now=now.strftime("%Y-%m-%d %H:%M:%S (%A)"),
        devices=_device_brief(devices),
    )
    try:
        content = _call(cfg, [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ]).strip()
        if content.startswith("```"):
            content = content.strip("`")
            content = content[content.find("{"):]
        raw = json.loads(content)
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError, OSError):
        return None

    return _validate(raw, devices, now)
