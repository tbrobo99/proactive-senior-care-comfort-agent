"""Tier-2 approval queue (Phase 2).

The fail-safe is the default: an approval that is not explicitly confirmed is
never executed. Expiry closes it as "expired" and leaves the house exactly as
it was — a request the user did not answer is a request the user did not make.

Pending approvals live in memory only, and that is deliberate. A restart drops
them, which under this policy means nothing executes: the safe direction to
fail in. (Phase 1 made *scheduled jobs* durable because a lost job is silent;
a lost approval fails loud — the user simply never sees the question.)
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from . import db


@dataclass
class Approval:
    id: str
    risk_kind: str
    question: str                 # what the user is asked, in plain language
    summary: str                  # one-line audit line
    # One Level 2 action per approval (FR-3/FR-4: per-action consent). The
    # list shape is kept so callers and the UI read it the same way, but it
    # always holds exactly one candidate.
    actions: list = field(default_factory=list)   # Candidate dicts
    created_at: str = ""
    expires_at: str = ""
    status: str = "pending"       # pending | confirmed | declined | expired
    decided_by: str = ""
    risk_score: float | None = None
    channel: str = ""             # where the answer came from (ui, api, ...)

    @property
    def action_kind(self) -> str:
        return (self.actions[0].get("kind") if self.actions else "") or ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "risk_kind": self.risk_kind, "question": self.question,
            "summary": self.summary, "actions": self.actions,
            "action_kind": self.action_kind, "risk_score": self.risk_score,
            "created_at": self.created_at, "expires_at": self.expires_at,
            "status": self.status, "decided_by": self.decided_by,
            "channel": self.channel,
        }


def _audit(event_type: str, ap: "Approval", **extra) -> None:
    """Record an approval transition. A failure here is logged, not raised:
    write-before-act is enforced where the device is touched, in the engine."""
    try:
        db.audit(event_type, risk_kind=ap.risk_kind, risk_score=ap.risk_score,
                 action_kind=ap.action_kind, tier="tier2", approval_id=ap.id,
                 approval_status=ap.status, **extra)
    except Exception as exc:
        db.log("audit.error", f"{event_type} {ap.id}: {exc}", "error")


class ApprovalQueue:
    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._items: dict[str, Approval] = {}
        # Answers that arrived after the deadline. Kept so the caller can react
        # the way it reacts to a sweep expiry -- silence the risk, tell the
        # user nothing changed. A bare None would be read as "no such
        # approval", and the risk would be asked about again on the next tick.
        self._late: dict[str, Approval] = {}

    # ---------------------------------------------------------------- propose

    def propose(self, *, risk_kind: str, question: str, summary: str,
                actions: list, ttl_seconds: int | None = None,
                risk_score: float | None = None,
                decision_source: str = "rules") -> Approval:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        now = datetime.now()
        ap = Approval(
            id=uuid.uuid4().hex[:10],
            risk_kind=risk_kind,
            question=question,
            summary=summary,
            actions=actions,
            created_at=now.isoformat(timespec="seconds"),
            # Full precision on purpose: a seconds-truncated deadline compared
            # against a microsecond `now` expires up to a second early, and a
            # fail-safe that fires early is a fail-safe that lies.
            expires_at=(now + _seconds(ttl)).isoformat(),
            risk_score=None if risk_score is None else round(float(risk_score), 1),
        )
        with self._lock:
            self._items[ap.id] = ap
        db.log("approval.propose", f"{ap.id} {risk_kind}: {summary} (ttl {ttl}s)")
        _audit("approval.proposed", ap, decision_source=decision_source,
               channel="engine", detail=f"ttl {ttl}s; expires {ap.expires_at}")
        return ap

    # ---------------------------------------------------------------- decide

    def confirm(self, approval_id: str, channel: str = "api") -> Approval | None:
        """Mark confirmed. The caller executes the actions; this only decides."""
        late = None
        with self._lock:
            ap = self._items.get(approval_id)
            if not ap or ap.status != "pending":
                return None
            # The sweep runs on the tick, and the tick can be held up by a model
            # call. Without this check a late answer slips through in the gap
            # between two sweeps and executes past its deadline — which is the
            # one thing this whole mechanism exists to prevent. Expiring here
            # instead means the deadline holds no matter when the answer lands.
            try:
                if datetime.fromisoformat(ap.expires_at) <= datetime.now():
                    ap.status = "expired"
                    ap.channel = channel
                    self._late[approval_id] = ap
                    late = ap
            except ValueError:
                pass  # no deadline recorded: nothing to enforce
            if late is None:
                ap.status, ap.decided_by, ap.channel = "confirmed", "user", channel
        if late is not None:
            db.log("approval.expired",
                   f"{approval_id} {late.risk_kind}: answered after the "
                   f"window; nothing was changed")
            _audit("approval.expired", late, decision_source="timeout",
                   channel=channel, execution_status="canceled",
                   detail="answer arrived after the deadline")
            return None
        db.log("approval.confirm", f"{approval_id} {ap.risk_kind}")
        _audit("approval.confirmed", ap, decision_source="user", channel=channel)
        return ap

    def decline(self, approval_id: str, channel: str = "api") -> Approval | None:
        with self._lock:
            ap = self._items.get(approval_id)
            if not ap or ap.status != "pending":
                return None
            ap.status, ap.decided_by, ap.channel = "declined", "user", channel
        db.log("approval.decline", f"{approval_id} {ap.risk_kind}")
        _audit("approval.declined", ap, decision_source="user", channel=channel,
               execution_status="canceled")
        return ap

    # ---------------------------------------------------------------- expire

    def take_late_expiry(self, approval_id: str) -> Approval | None:
        """Was this id closed by an answer that arrived too late?

        Pop-on-read: the caller asks once, right after a `confirm()` returned
        None, and acts on it the way it acts on a sweep expiry.
        """
        with self._lock:
            return self._late.pop(approval_id, None)

    def expire_due(self, now: datetime | None = None) -> list[Approval]:
        """Close everything past its deadline. Nothing is executed; that is the point."""
        now = now or datetime.now()
        expired: list[Approval] = []
        with self._lock:
            for ap in self._items.values():
                if ap.status != "pending":
                    continue
                try:
                    due = datetime.fromisoformat(ap.expires_at)
                except ValueError:
                    continue
                if due <= now:
                    ap.status = "expired"
                    expired.append(ap)
        for ap in expired:
            db.log("approval.expired",
                   f"{ap.id} {ap.risk_kind}: no response within the window; nothing was changed")
            _audit("approval.expired", ap, decision_source="timeout",
                   channel="engine", execution_status="canceled",
                   detail="no response within the window")
        return expired

    # ---------------------------------------------------------------- read

    def pending(self) -> list[Approval]:
        with self._lock:
            return [a for a in self._items.values() if a.status == "pending"]

    def get(self, approval_id: str) -> Approval | None:
        with self._lock:
            return self._items.get(approval_id)

    def all(self) -> list[Approval]:
        with self._lock:
            return list(self._items.values())


def _seconds(n: float):
    from datetime import timedelta
    return timedelta(seconds=n)
