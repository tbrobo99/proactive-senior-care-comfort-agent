"""Proactive decision engine (Phase 2).

The loop, once a second:

    sensors.tick -> risk.evaluate -> policy.candidates -> DECIDE -> execute / ask

Where the LLM sits in that chain is the whole design. It is not the timer, not
the sensor, and not the safety gate — those are deterministic code, and Phase 1
already argued why a model must never be any of them. The LLM owns exactly the
decisions rules cannot express:

  * which of the candidate actions to take, and which to leave alone
  * in what order, when a mold risk and a thermal risk fire together

Phrasing a Tier-2 question used to be a third job. It is now a template by
default: the model took ~2 s and left out the five-minute deadline every time,
and the template says all four things a senior needs (what is wrong, what will
change, how long they have, what to say). `llm_phrase` turns the model back on
for anyone who wants the richer sentence.

That is a real decision, not polishing: given the same candidates the model may
drop one because the dehumidifier is offline, or abstain entirely because the
trend says the drum is already drying. Its output is validated against a
whitelist before anything moves — it can be wrong about *what* to do, but it
cannot corrupt the house, and a parse failure falls back to the safe default
(Tier 1 executes, Tier 2 is only ever proposed).

Tier-2 execution goes through ApprovalQueue.confirm(), and only the user's
"yes" gets there.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime

from . import approval, db, llm, policy, risk, sensors

DECIDE_SYSTEM = """You are the decision layer of a proactive smart-home agent for a
household that includes a senior. Sensors have flagged a risk; rules have
proposed candidate actions. Your job is to choose.

Choose only from the candidates, and list them in the order they should run.
You may:
- take some and leave others (a candidate whose device is offline or already in
  the wanted state should be left alone),
- reorder them when the order matters,
- take none, if the evidence says the situation is resolving on its own.

You cannot execute anything yourself.

Answer with the kind names you chose, in order, as JSON only and nothing else:
{"chosen": ["candidate_kind", "..."]}

Use an empty list, {"chosen": []}, when the right answer is to do nothing."""

# The paragraph that used to sit above ("Tier-1 actions you choose run
# automatically; Tier-2 actions you choose are put to the user for approval and
# never run without a yes") is gone on purpose. It was true and it was the wrong
# thing to tell the model: given a Tier-2 candidate it would leave it off the
# list, run after run, because asking the user reads as a burden. Measured, 8/8
# runs on the oven scenario: with the sentence, the model chose only the range
# hood; without it, both the hood and the dryer delay. A model that will not
# propose the Tier-2 action is a model that never exercises the product's whole
# point. Nothing about safety depended on that sentence — `_intervene` routes
# Tier-2 to ApprovalQueue and only `confirm()` can execute it, whatever the
# model says.

QUESTION_SYSTEM = """You are writing one short question for an elderly homeowner.
The house wants to take an action that changes their plans, so it must ask
first. Rules:
- one question, at most two short sentences, no technical terms
- name the appliance and what will change, and the deadline for answering
- offer a one-word reply (yes / no)
- warm and plain, never alarming
Output the question text only."""


def _clean(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    return text


def _join(items: list[str]) -> str:
    """'a', 'a and b', 'a, b and c' — the way a person says a list."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    # A label can contain its own "and" (a past-tense report such as
    # "turned the dryer off, and left it for you to restart"). Joining those
    # with another "and" gives one sentence with three of them, which reads as
    # a single long instruction rather than two.
    if any(" and " in i for i in items):
        return "; ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _plain(r: risk.Risk, snap: dict) -> str:
    """The same finding in words a senior can act on.

    The index score, the extrapolation and the wattage belong in the payload,
    where the audit trail and the specification can see them. They do not
    belong in a sentence read aloud to someone who has never heard of an index
    — "Thermal overload 100/100 in the kitchen (31.5 °C, predicted 31.5 °C in
    30 min)" is a report, not a sentence, and it was the wrong thing to say to
    the person the product exists for.
    """
    if r.kind == "mold":
        hours = float(r.drivers.get("sealed_hours") or 0.0)
        if hours >= 1:
            return (f"The washing machine has been shut for {hours:.0f} hours "
                    f"with wet washing still inside it")
        return "The washing machine is shut with wet washing still inside it"

    running = [name for name, key in (("the dryer", "dryer"), ("the oven", "oven"))
               if snap["appliances"].get(key, {}).get("power")]
    if running:
        return (f"The kitchen is at {r.current:.0f}\u00b0C and "
                f"{_join(running)} {'are' if len(running) > 1 else 'is'} adding heat")
    return f"The kitchen is getting warm \u2014 it is {r.current:.0f}\u00b0C now"


def _devices(ap) -> str:
    """The appliances an approval is about, named for a person."""
    names = sorted({a.get("device_name") or a.get("device_id") or "an appliance"
                    for a in (ap.actions or [])})
    return _join(names) if names else "an appliance"


class ProactiveEngine:
    """Watches the simulated house and intervenes under the risk-graded policy."""

    def __init__(self, adapter, cfg: dict) -> None:
        pcfg = cfg.get("proactive", {}) or {}
        self.adapter = adapter
        self.cfg = cfg
        self.tick_seconds = float(pcfg.get("tick_seconds", 1.0))
        self.cooldown_seconds = float(pcfg.get("cooldown_seconds", 180))
        # How long the house stays quiet about a risk the user has already
        # answered — "no", or the fail-safe running out. Without it, always
        # putting the Tier-2 question to the user would mean asking the same
        # question every five minutes for as long as the condition lasts.
        self.quiet_seconds = float(pcfg.get("quiet_seconds", 1800))
        self.llm_decide = bool(pcfg.get("llm_decide", True)) and llm.available(cfg)
        # Off by default. The template question already names the situation, the
        # change, the deadline and the one-word answer; the model's version was
        # measured at 1.6–3.0 s (median 2.0 s, n=4) and consistently dropped the
        # deadline the prompt asks it to include. Two seconds and a worse
        # sentence is not a trade worth making in the path that a fail-safe
        # clock is running against. Flip this on if a richer question is ever
        # worth the wait.
        self.llm_phrase = bool(pcfg.get("llm_phrase", False)) and llm.available(cfg)
        # Cooktop interlock (FR-5). After a confirmed preheat, the hob is
        # switched off by the house if no cooking is detected within the grace
        # period after the scheduled start, and in any case after the maximum
        # continuous run time.
        self.cooktop_grace_seconds = float(pcfg.get("cooktop_grace_seconds", 600))
        self.cooktop_max_run_seconds = float(pcfg.get("cooktop_max_run_seconds", 5400))

        self.sensors = sensors.SensorSimulator(tick_seconds=self.tick_seconds)
        self.approvals = approval.ApprovalQueue(
            ttl_seconds=policy.APPROVAL_TTL_SECONDS)

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cooldown: dict[str, float] = {}     # risk kind -> monotonic deadline
        self._active: dict[str, dict] = {}        # risk kind -> last intervention
        self._last_snapshot: dict = {}
        self._cooktop_guard: dict | None = None   # set after a confirmed preheat
        self._dryer_reminder: dict | None = None  # set after a confirmed dryer stop

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.sensors.start()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="proactive")
        self._thread.start()
        db.log("proactive.start",
               f"engine started (llm_decide={'on' if self.llm_decide else 'off'})")

    def stop(self) -> None:
        self._stop.set()
        self.sensors.stop()

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception as exc:
                # The engine watches the house; it must not be able to break it.
                db.log("proactive.error", f"{type(exc).__name__}: {exc}", "error")

    # ---------------------------------------------------------------- one pass

    def tick(self, now: datetime | None = None) -> dict:
        """One observe-decide-act pass. Returns what happened (for tests/demos)."""
        now = now or datetime.now()
        snap = self.sensors.snapshot()
        self.sensors.record(snap)
        self._last_snapshot = snap

        fired = [r for r in risk.evaluate(snap, self.sensors.history, now)
                 if r.score >= r.threshold]
        events: list[dict] = []

        ev = self._watch_cooktop(snap)
        if ev:
            events.append(ev)
        ev = self._remind_dryer(snap, {r.kind for r in fired})
        if ev:
            events.append(ev)

        pending_kinds = {a.risk_kind for a in self.approvals.pending()}
        for r in fired:
            if self._cooling(r.kind):
                continue
            if r.kind in pending_kinds:
                continue  # already waiting on the user for this risk
            events.append(self._intervene(r, snap, now))

        for ap in self.approvals.expire_due(now):
            events.append({"type": "expired", "approval": ap.id})
            self._cooldown[ap.risk_kind] = time.monotonic() + self.quiet_seconds
            db.add_message("system",
                           f"\u23f3 I asked about {_devices(ap)} and did not hear back, "
                           f"so I left it alone. Anything already done stays as it is.",
                           {"approval": ap.to_dict()})
        return {"ts": snap["ts"], "risks": [r.kind for r in fired], "events": events}

    # ---------------------------------------------------------------- intervene

    def _intervene(self, r: risk.Risk, snap: dict, now: datetime) -> dict:
        _audit_safe("risk.triggered", risk_kind=r.kind, risk_score=round(r.score, 1),
                    decision_source="rules", channel="engine", detail=r.headline)
        cands = policy.candidates(r, snap)
        if not cands:
            return {"type": "no_candidates", "risk": r.kind}

        chosen, decided_by = self._decide(r, cands, snap)
        tier1 = [c for c in chosen if policy.tier_of(c.kind) == policy.TIER1]
        # Tier 2 is not the model's to drop, and this is measured rather than
        # assumed. Given a candidate that switches on a heating appliance, the
        # model leaves it off the list in 15 runs out of 15, across four
        # framings (cooktop, oven, "the resident is standing there", cooking 5
        # or 19 minutes away) and with the caution removed from the prompt. It
        # is not prompt-induced avoidance this time; it is the model's own
        # judgement that lighting a hob is not something a house should offer,
        # and it is not wrong.
        #
        # But whether to *ask* is a policy question, not a model question. If
        # the model's caution decided it, a cooking session on the calendar
        # would be silently ignored, and the product's whole claim — the house
        # asks before it does something that matters — would depend on the
        # model happening to agree. So the rules decide what is put to the user
        # and the user decides what runs; the model keeps the part where its
        # judgement is worth having, which is which Tier-1 actions are worth
        # doing right now. Safety does not move either way: nothing here can
        # execute a Tier-2 action, only `confirm()` can.
        tier2 = [c for c in cands if policy.tier_of(c.kind) == policy.TIER2]

        executed = []
        for c in tier1:
            item = _candidate_dict(c)
            ok, why = policy.revalidate(item, snap, {r.kind: r})
            if not ok:
                outcome = self._block(item, why, tier=policy.TIER1, risk_kind=r.kind,
                                      risk_score=r.score, decision_source=decided_by)
            else:
                outcome = self._execute(item, tier=policy.TIER1, risk_kind=r.kind,
                                        risk_score=r.score, decision_source=decided_by,
                                        channel="engine")
            executed.append(outcome)
            db.log("proactive.tier1", f"{r.kind}: {c.label} -> {outcome['status']}"
                   + (f" ({outcome['reason']})" if outcome["reason"] else ""))

        # FR-3/FR-4: one approval per Level 2 action, each with its own id,
        # question and answer. Confirming one never carries out another.
        approval_ids = []
        for c in tier2:
            question = self._phrase(r, [c], snap)
            ap = self.approvals.propose(
                risk_kind=r.kind,
                question=question,
                summary=c.label,
                actions=[_candidate_dict(c)],
                risk_score=r.score,
                decision_source=decided_by,
            )
            approval_ids.append(ap.id)
            db.add_message("system", question, {"approval": ap.to_dict()})
        approval_id = approval_ids[0] if approval_ids else None

        # Truthful reporting (FR-4): say only what the adapter confirmed.
        done = [o["done"] for o in executed if o["status"] == "completed"]
        failed = [o for o in executed if o["status"] == "failed"]
        head = _plain(r, snap)
        if failed:
            tail = " ".join(f"I tried to {o['label']}, but {o['device_name']} did not "
                            f"respond." for o in failed)
            body = (f"\U0001f6e1 {head}. "
                    + (f"So I {_join(done)}. " if done else "") + tail)
        elif done:
            body = f"\U0001f6e1 {head}. So I {_join(done)}."
        elif tier2:
            body = (f"\U0001f6e1 {head}. Changing that moves your plans, "
                    f"so I am asking you first.")
        else:
            body = f"\U0001f6e1 {head}. I checked \u2014 nothing needs doing right now."
        db.add_message("system", body, {
            "risk": r.kind, "score": round(r.score, 1), "drivers": r.drivers,
            # The technical line stays in the payload, not in the sentence: the
            # spec and the audit trail need the number, the user does not.
            "headline": r.headline,
            "decided_by": decided_by, "approval": approval_id,
            "approvals": approval_ids, "executed": executed,
        })

        self._cooldown[r.kind] = time.monotonic() + self.cooldown_seconds
        self._active[r.kind] = {"score": r.score, "at": now.isoformat(timespec="seconds"),
                                "executed": [o["kind"] for o in executed
                                             if o["status"] == "completed"],
                                "approval": approval_id, "approvals": approval_ids}
        return {"type": "intervened", "risk": r.kind, "decided_by": decided_by,
                "tier1": [c.kind for c in tier1], "tier2": [c.kind for c in tier2],
                "approval": approval_id, "approvals": approval_ids}

    # ---------------------------------------------------------------- execution

    def _block(self, item: dict, reason: str, *, tier: str, risk_kind: str,
               risk_score: float | None, decision_source: str,
               approval_id: str | None = None, channel: str = "engine") -> dict:
        """Record an action that was not carried out, and why."""
        _audit_safe("action.blocked", risk_kind=risk_kind,
                    risk_score=_round(risk_score), action_kind=item.get("kind"),
                    tier=tier, approval_id=approval_id,
                    execution_status="canceled", decision_source=decision_source,
                    channel=channel, detail=reason)
        return _outcome(item, "canceled", reason)

    def _execute(self, item: dict, *, tier: str, risk_kind: str,
                 risk_score: float | None, decision_source: str, channel: str,
                 approval_id: str | None = None) -> dict:
        """Write intent, call the adapter, write the real outcome (FR-6).

        Returns an outcome dict whose `status` is completed, failed or canceled.
        """
        common = dict(risk_kind=risk_kind, risk_score=_round(risk_score),
                      action_kind=item.get("kind"), tier=tier,
                      approval_id=approval_id, decision_source=decision_source,
                      channel=channel)
        if not policy.is_whitelisted(item.get("kind") or "") and tier != "interlock":
            return self._block(item, "not on the action whitelist", tier=tier,
                               risk_kind=risk_kind, risk_score=risk_score,
                               decision_source=decision_source,
                               approval_id=approval_id, channel=channel)
        # Write-before-act. For Level 2 a missing record means no action.
        try:
            db.audit("action.requested", execution_status="requested",
                     detail=json.dumps(item.get("actions"), ensure_ascii=False),
                     **common)
        except Exception as exc:
            db.log("audit.error", f"action.requested {item.get('kind')}: {exc}", "error")
            if tier == policy.TIER2:
                return _outcome(item, "failed",
                                "the house could not record the action, so it did not run")
        try:
            res = self.adapter.apply(item["device_id"], item["actions"])
        except Exception as exc:
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ok = bool(res.get("ok"))
        if ok:
            self.sensors.note_action(item["device_id"], item["actions"])
        _audit_safe("action.executed" if ok else "action.failed",
                    execution_status="completed" if ok else "failed",
                    detail=None if ok else str(res.get("error") or "no response"),
                    **common)
        return _outcome(item, "completed" if ok else "failed",
                        "" if ok else str(res.get("error") or "no response"))

    # ---------------------------------------------------------------- follow-ups (FR-5)

    def _watch_cooktop(self, snap: dict) -> dict | None:
        """Switch a preheated cooktop off when nobody cooks on it, or when it
        has run for the maximum time. Turning heat *off* needs no approval."""
        g = self._cooktop_guard
        if not g:
            return None
        ck = snap["appliances"].get("cooktop", {})
        if not ck.get("power"):
            self._cooktop_guard = None      # switched off by hand: nothing to watch
            return None
        if ck.get("cooking"):
            g["cooking_seen"] = True
        mono = time.monotonic()
        if mono >= g["max_until"]:
            reason = "it reached the longest time it may stay on by itself"
        elif not g.get("cooking_seen") and mono >= g["auto_off_at"]:
            reason = "nobody started cooking on it"
        else:
            return None
        self._cooktop_guard = None
        item = {"kind": "cooktop_auto_off", "device_id": "cooktop",
                "device_name": "Kitchen Cooktop",
                "actions": [{"action": "power", "value": False}],
                "label": "switch the cooktop off", "done": "switched the cooktop off"}
        out = self._execute(item, tier="interlock", risk_kind="thermal", risk_score=None,
                            decision_source="interlock", channel="engine",
                            approval_id=g.get("approval_id"))
        if out["status"] == "completed":
            text = f"\U0001f525 I switched the cooktop off because {reason}."
        else:
            text = (f"⚠ I tried to switch the cooktop off because {reason}, "
                    f"but it did not respond. Please check it.")
        db.add_message("system", text, {"interlock": "cooktop", "executed": [out]})
        return {"type": "cooktop_auto_off", "status": out["status"], "reason": reason}

    def _remind_dryer(self, snap: dict, fired_kinds: set) -> dict | None:
        """After a confirmed dryer stop, say once that it may be restarted,
        when the kitchen overload has cleared."""
        if not self._dryer_reminder or "thermal" in fired_kinds:
            return None
        rem, self._dryer_reminder = self._dryer_reminder, None
        if snap["appliances"].get("dryer", {}).get("power"):
            return None                     # already restarted by hand
        db.add_message("system",
                       "✅ The kitchen has cooled down. You can start the dryer "
                       "again whenever you like — I have left it off.",
                       {"reminder": "dryer_restart"})
        _audit_safe("notify.dryer_restart", risk_kind="thermal", action_kind="stop_dryer",
                    tier=policy.TIER2, approval_id=rem.get("approval_id"),
                    decision_source="rules", channel="engine",
                    detail="thermal risk cleared")
        return {"type": "dryer_reminder"}

    # ---------------------------------------------------------------- the LLM's job

    def _decide(self, r: risk.Risk, cands: list, snap: dict):
        """Choose among candidates. Returns (chosen_in_order, decided_by)."""
        if not self.llm_decide:
            return list(cands), "rules"
        user = (
            f"Risk: {r.headline}\n"
            f"Drivers: {json.dumps(r.drivers, ensure_ascii=False)}\n"
            f"User home: {snap['occupancy'].get('user_home')}\n\n"
            "Candidates:\n" + "\n".join(
                f"- kind={c.kind} tier={policy.tier_of(c.kind)} "
                f"device={c.device_name} does: {c.label} (why: {c.reason})"
                for c in cands)
        )
        # Names, not numbers. Asking for `[1, 3]` instead of the kind names was
        # measured ~0.7 s cheaper and cost the product its whole point: over six
        # runs the model answered "hood only" every time and never proposed the
        # Tier-2 action, where the named schema asked for both. A cheaper wrong
        # decision is not cheaper. max_tokens stays a runaway guard, not a
        # latency lever.
        try:
            msg = llm.chat(self.cfg, [
                {"role": "system", "content": DECIDE_SYSTEM},
                {"role": "user", "content": user},
            ], json_mode=True, temperature=0, max_tokens=80)
            raw = json.loads(_clean(msg.get("content") or ""))
        except Exception as exc:
            db.log("proactive.decide_fallback",
                   f"{type(exc).__name__}: {exc}; took the rule default")
            return list(cands), "rules"

        # A response without the key is a parse we do not trust. A response with
        # the key and an empty list is a decision — the model is saying there is
        # nothing worth doing. Those two must not be confused: the first falls
        # back to the rule default, the second is honoured.
        if not isinstance(raw, dict) or "chosen" not in raw:
            db.log("proactive.decide_fallback",
                   "no 'chosen' key; took the rule default")
            return list(cands), "rules"

        kinds = []
        for item in raw.get("chosen") or []:
            # Two shapes turn up: a kind name (the contract), and {"kind": ...}
            # from an older build. Anything else is not an answer.
            if isinstance(item, dict):
                item = item.get("kind")
            if isinstance(item, str):
                kinds.append(item.strip())

        if not kinds:
            db.log("proactive.decide", f"{r.kind}: abstained (nothing worth doing)")
            return [], "llm"

        by_kind = {c.kind: c for c in cands}
        chosen, seen = [], set()
        for k in kinds:
            if not policy.is_whitelisted(k):
                # FR-3: an invented action is rejected and recorded, never run.
                _audit_safe("action.blocked", risk_kind=r.kind,
                            risk_score=round(r.score, 1), action_kind=k[:64],
                            execution_status="canceled", decision_source="llm",
                            channel="engine", detail="model returned an action "
                                                     "outside the whitelist")
                db.log("proactive.blocked", f"{r.kind}: model proposed unknown action {k!r}")
                continue
            if k in by_kind and k not in seen:
                chosen.append(by_kind[k])
                seen.add(k)
        if not chosen:
            # It answered, but nothing it named is on the table. That is a wrong
            # answer, not a decision.
            db.log("proactive.decide_fallback",
                   "no returned kind matched a candidate; took the rule default")
            return list(cands), "rules"
        db.log("proactive.decide",
               f"{r.kind}: chose {', '.join(c.kind for c in chosen)}")
        return chosen, "llm"

    def _phrase(self, r: risk.Risk, tier2: list, snap: dict) -> str:
        """One plain-language question for the Tier-2 request."""
        wants = _join([c.label for c in tier2])
        # The fallback has to be as plain as the model's answer would be: it is
        # shown whenever the model is off, slow, or unreachable, which is
        # exactly when nobody is around to polish it.
        fallback = (f"{_plain(r, snap)}. I would like to {wants}. "
                    f"Shall I? Reply yes or no \u2014 no answer in 5 minutes "
                    f"keeps everything as it is.")
        if not self.llm_phrase:
            return fallback
        try:
            msg = llm.chat(self.cfg, [
                {"role": "system", "content": QUESTION_SYSTEM},
                {"role": "user", "content":
                    f"Situation: {_plain(r, snap)}. Requested change: {wants}. "
                    f"User is {'home' if snap['occupancy'].get('user_home') else 'out'}."},
            ], temperature=0.2, max_tokens=120)
            text = (msg.get("content") or "").strip()
            if text and len(text) < 400:
                return text
        except Exception:
            pass
        return fallback

    # ---------------------------------------------------------------- user decisions

    def confirm(self, approval_id: str, channel: str = "api") -> dict:
        """The user said yes. Revalidate, then execute this one Tier-2 action."""
        ap = self.approvals.confirm(approval_id, channel=channel)
        if not ap:
            # Two ways to get here, and they are not the same thing. Either
            # there is no such approval, or there was one and this answer
            # arrived after its deadline. The second case is an expiry, so it
            # has to end like a sweep expiry: nothing changes, the user is
            # told, and the risk goes quiet instead of being asked again on
            # the next tick.
            late = self.approvals.take_late_expiry(approval_id)
            if late is None:
                return {"ok": False, "error": "no pending approval with that id"}
            self._cooldown[late.risk_kind] = time.monotonic() + self.quiet_seconds
            db.add_message("system",
                           f"\u23f3 That one closed before I heard back, so I changed "
                           f"nothing. {_devices(late)} are as they were.",
                           {"approval": late.to_dict()})
            return {"ok": False, "expired": True, "approval": late.to_dict()}
        # Pre-execution revalidation (FR-5): the yes was given to the situation
        # the question described. If that situation is gone, nothing runs.
        snap = self.sensors.snapshot()
        active = {r.kind: r for r in risk.evaluate(snap, self.sensors.history)
                  if r.score >= r.threshold}
        executed = []
        for item in ap.actions:
            ok, why = policy.revalidate(item, snap, active)
            if not ok:
                executed.append(self._block(item, why, tier=policy.TIER2,
                                            risk_kind=ap.risk_kind,
                                            risk_score=ap.risk_score,
                                            decision_source="user",
                                            approval_id=ap.id, channel=channel))
                continue
            out = self._execute(item, tier=policy.TIER2, risk_kind=ap.risk_kind,
                                risk_score=ap.risk_score, decision_source="user",
                                channel=channel, approval_id=ap.id)
            executed.append(out)
            if out["status"] == "completed":
                self._arm_follow_ups(item, ap, snap)
        # No job is created here. An approval authorises the action that was
        # asked about, at the moment it is given; it is not a standing
        # permission for the same device to start again hours later.
        status = executed[0]["status"] if executed else "canceled"
        o = executed[0] if executed else {"label": "", "device_name": "", "reason": ""}
        if status == "completed":
            text = f"\u2713 You said yes, so I {o['done']}."
        elif status == "canceled":
            text = (f"You said yes, but I did not go ahead: {o['reason']}. "
                    f"Nothing was changed.")
        else:
            text = (f"\u26a0 You said yes, but {o['device_name']} did not respond, "
                    f"so it is unchanged.")
        db.add_message("system", text, {"approval": ap.to_dict(), "executed": executed,
                                        "status": status})
        # `ok` means the answer was accepted; `status` is what the device did.
        return {"ok": True, "status": status, "approval": ap.to_dict(),
                "executed": executed}

    def _arm_follow_ups(self, item: dict, ap, snap: dict) -> None:
        if item.get("kind") == "stop_dryer":
            self._dryer_reminder = {"approval_id": ap.id}
        elif item.get("kind") == "preheat_cooktop":
            cooking_in = policy.cooking_starts_in_min(snap) or 0
            mono = time.monotonic()
            self._cooktop_guard = {
                "approval_id": ap.id,
                "auto_off_at": mono + float(cooking_in) * 60 + self.cooktop_grace_seconds,
                "max_until": mono + self.cooktop_max_run_seconds,
                "cooking_seen": False,
            }

    def decline(self, approval_id: str, channel: str = "api") -> dict:
        ap = self.approvals.decline(approval_id, channel=channel)
        if not ap:
            return {"ok": False, "error": "no pending approval with that id"}
        # No is an answer. Asking again in three minutes is not respect, it is
        # a machine that did not listen.
        self._cooldown[ap.risk_kind] = time.monotonic() + self.quiet_seconds
        db.add_message("system",
                       f"\u2717 All right \u2014 I left {_devices(ap)} as it was.",
                       {"approval": ap.to_dict()})
        return {"ok": True, "approval": ap.to_dict()}

    # ---------------------------------------------------------------- state

    def _cooling(self, kind: str) -> bool:
        until = self._cooldown.get(kind)
        return until is not None and time.monotonic() < until

    def state(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "llm_decide": self.llm_decide,
            "scenario": self._last_snapshot.get("scenario"),
            "rooms": self._last_snapshot.get("rooms"),
            "appliances": self._last_snapshot.get("appliances"),
            "occupancy": self._last_snapshot.get("occupancy"),
            "active": self._active,
            "pending_approvals": [a.to_dict() for a in self.approvals.pending()],
            # FR-4: the quiet periods are visible, so the user can still see
            # the live risk state while the house is not asking.
            "quiet": {k: round(until - time.monotonic())
                      for k, until in self._cooldown.items()
                      if until > time.monotonic()},
            "cooktop_guard": bool(self._cooktop_guard),
        }


def _candidate_dict(c) -> dict:
    return {"kind": c.kind, "device_id": c.device_id, "device_name": c.device_name,
            "actions": c.actions, "label": c.label, "done": c.done}


def _outcome(item: dict, status: str, reason: str) -> dict:
    return {"kind": item.get("kind"), "device_id": item.get("device_id"),
            "device_name": item.get("device_name") or item.get("device_id") or "the appliance",
            "label": item.get("label") or "", "done": item.get("done") or item.get("label") or "",
            "status": status, "ok": status == "completed", "reason": reason}


def _round(v):
    return None if v is None else round(float(v), 1)


def _audit_safe(event_type: str, **fields) -> None:
    try:
        db.audit(event_type, **fields)
    except Exception as exc:
        db.log("audit.error", f"{event_type}: {exc}", "error")
