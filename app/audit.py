"""Append-only audit trail and the single gate for device actions (FR-6).

Every path that changes an appliance goes through `execute()`. The intent is
written first, the adapter runs second, and the outcome is written last. If the
intent cannot be persisted the adapter is never called — write-before-act, and
it fails in the safe direction.

`db.audit_log` is protected by BEFORE UPDATE / BEFORE DELETE triggers, so a row
cannot be rewritten or removed from application code.
"""
from __future__ import annotations

from . import db

_CONTEXT_FIELDS = ("risk_kind", "risk_score", "action_kind", "tier",
                   "approval_id", "approval_status", "decision_source", "channel")


def record(event_type: str, **fields) -> int:
    """Append an event. Raises when persistence fails (see execute())."""
    return db.audit_write(event_type, **fields)


def _base(context: dict, device_id: str) -> dict:
    out = {k: context.get(k) for k in _CONTEXT_FIELDS}
    out.setdefault("channel", "device")
    out.setdefault("decision_source", "rules")
    out["device_id"] = device_id
    return out


def blocked(reason: str, *, context: dict | None = None, device_id: str = "") -> None:
    """A decision the engine refused to carry out. Never reaches the adapter."""
    try:
        record("action.blocked", **_base(dict(context or {}), device_id),
               execution_status="blocked", detail=reason)
    except Exception:
        # A blocked action is already safe; failing to write about it must not
        # turn into an exception inside the 1 Hz loop.
        pass


def execute(adapter, device_id: str, actions: list[dict], *,
            context: dict | None = None) -> dict:
    """Run one device action with intent and outcome on either side of it.

    Returns the adapter's own result unchanged when all goes well, so every
    caller keeps the shape it already expects (`ok`, `applied`, `error`).
    """
    base = _base(dict(context or {}), device_id)

    # 1. Intent, before anything moves. A failure here aborts the action.
    try:
        record("action.intent", **base, execution_status="pending",
               detail="write-before-act")
    except Exception as exc:
        return {"ok": False, "audit_blocked": True,
                "error": f"audit write failed: {exc}"}

    # 2. The only adapter call in this path.
    try:
        res = adapter.apply(device_id, actions)
    except Exception as exc:
        try:
            record("action.failed", **base, execution_status="failed",
                   detail=f"adapter raised {type(exc).__name__}: {exc}")
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}

    # 3. Outcome, immediately after.
    ok = bool(res.get("ok"))
    try:
        record("action.executed" if ok else "action.failed", **base,
               execution_status="completed" if ok else "failed",
               detail=str(res.get("error") or ""))
    except Exception:
        pass
    return res
