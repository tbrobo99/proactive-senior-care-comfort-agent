"""Configuration: config.json overrides the defaults below."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

DEFAULTS: dict = {
    "server": {"host": "127.0.0.1", "port": 8848},
    # mock = built-in simulator (default, no account needed)
    # thinq = real LG appliances via the official ThinQ Open API
    "device_backend": "mock",
    "thinq": {
        "access_token": "",    # personal access token (PAT)
        "country_code": "US",  # must match the region of your LG account
        "client_id": "",       # auto-generated and written back if empty
        "dry_run": True,       # build the payload and log it, do not send
    },
    "llm": {
        "enabled": False,
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "timeout": 20,
    },
    # FR-5 safety windows, in seconds. The simulator has no stove sensor: an
    # active cooking session on the local calendar, or a running oven, is the
    # only honest "cooking detected" signal. These are wall-clock limits, not
    # a number of API calls or ticks.
    "proactive": {
        "cooktop_require_presence": True,
        "cooktop_no_cook_timeout": 300,
        "cooktop_max_runtime": 1800,
    },
    "agent": {
        # used when the user does not name a room
        "default_room": "Living Room",
        # cascade = code picks the tier from the shape of the request (the
        #           benchmark's mode; deterministic, ~1 ms for the rules)
        # agent   = the model sees the rules' reading and keeps it or throws it
        #           away; one extra round trip, and a decision the model makes
        "mode": "cascade",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides(cfg: dict) -> dict:
    """Environment beats config.json.

    A hosted copy must never need a key committed to the repository, and the
    hosting platform decides the port. Both arrive as environment variables.
    """
    e = os.environ
    if e.get("LLM_API_KEY"):
        cfg["llm"]["api_key"] = e["LLM_API_KEY"]
        cfg["llm"]["enabled"] = True
    if e.get("LLM_BASE_URL"):
        cfg["llm"]["base_url"] = e["LLM_BASE_URL"]
    if e.get("LLM_MODEL"):
        cfg["llm"]["model"] = e["LLM_MODEL"]
    if e.get("PORT"):
        try:
            cfg["server"]["port"] = int(e["PORT"])
        except ValueError:
            pass
    if e.get("HOST"):
        cfg["server"]["host"] = e["HOST"]
    return cfg


def load() -> dict:
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            user_cfg = {}
        return _env_overrides(_deep_merge(DEFAULTS, user_cfg))
    return _env_overrides(copy.deepcopy(DEFAULTS))


def save(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def ensure_file() -> None:
    if not CONFIG_PATH.exists():
        save(load())


def update(**patch) -> dict:
    cfg = _deep_merge(load(), patch)
    save(cfg)
    return cfg
