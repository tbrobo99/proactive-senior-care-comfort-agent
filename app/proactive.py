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
    # A label can contain its own "and" ("turn the dryer off and restart it at
    # 11 PM"). Joining those with another "and" gives one sentence with three
    # of them, which reads as a single long instruction rather than two.
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

        self.sensors = sensors.SensorSimulator(tick_seconds=self.tick_seconds)
        self.approvals = approval.ApprovalQueue(
            ttl_seconds=policy.APPROVAL_TTL_SECONDS)

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cooldown: dict[str, float] = {}     # risk kind -> monotonic deadline
        self._active: dict[str, dict] = {}        # risk kind -> last intervention
        self._last_snapshot: dict = {}

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
                           f"so I changed nothing. Everything is as it was.",
                           {"approval": ap.to_dict()})
        return {"ts": snap["ts"], "risks": [r.kind for r in fired], "events": events}

    # ---------------------------------------------------------------- intervene

    def _intervene(self, r: risk.Risk, snap: dict, now: datetime) -> dict:
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
            res = self.adapter.apply(c.device_id, c.actions)
            self.sensors.note_action(c.device_id, c.actions)
            ok = bool(res.get("ok"))
            executed.append({"kind": c.kind, "label": c.label, "ok": ok,
                             "error": res.get("error")})
            db.log("proactive.tier1",
                   f"{r.kind}: {c.label} -> {'ok' if ok else res.get('error')}")

        approval_id = None
        if tier2:
            question = self._phrase(r, tier2, snap)
            ap = self.approvals.propose(
                risk_kind=r.kind,
                question=question,
                summary="; ".join(c.label for c in tier2),
                actions=[_candidate_dict(c) for c in tier2],
            )
            approval_id = ap.id
            db.add_message("system", question, {"approval": ap.to_dict()})

        done = [c.done for c in tier1]
        head = _plain(r, snap)
        if done:
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
            "executed": executed,
        })

        self._cooldown[r.kind] = time.monotonic() + self.cooldown_seconds
        self._active[r.kind] = {"score": r.score, "at": now.isoformat(timespec="seconds"),
                                "executed": [c.kind for c in tier1],
                                "approval": approval_id}
        return {"type": "intervened", "risk": r.kind, "decided_by": decided_by,
                "tier1": [c.kind for c in tier1], "tier2": [c.kind for c in tier2],
                "approval": approval_id}

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

    def confirm(self, approval_id: str) -> dict:
        """The user said yes. Execute the Tier-2 actions now."""
        ap = self.approvals.confirm(approval_id)
        if not ap:
            return {"ok": False, "error": "no pending approval with that id"}
        executed = []
        for item in ap.actions:
            res = self.adapter.apply(item["device_id"], item["actions"])
            self.sensors.note_action(item["device_id"], item["actions"])
            executed.append({"device_id": item["device_id"], "ok": bool(res.get("ok")),
                             "error": res.get("error")})
            job = item.get("job")
            if job and res.get("ok"):
                db.add_job(job["device_id"], item.get("device_name", job["device_id"]),
                           job["actions"], job["rule"], job["rule"]["run_at"],
                           repeat_label=job.get("repeat_label", ""),
                           source_text=f"approved:{approval_id}")
        done = _join([it.get("done") or it.get("label") or it.get("device_id", "")
                      for it in ap.actions])
        restarted = any(it.get("job") for it in ap.actions)
        db.add_message("system",
                       f"\u2713 You said yes, so I {done}."
                       + (" It will start again later, as we agreed." if restarted else ""),
                       {"approval": ap.to_dict(), "executed": executed})
        return {"ok": True, "approval": ap.to_dict(), "executed": executed}

    def decline(self, approval_id: str) -> dict:
        ap = self.approvals.decline(approval_id)
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
        }


def _candidate_dict(c) -> dict:
    return {"kind": c.kind, "device_id": c.device_id, "device_name": c.device_name,
            "actions": c.actions, "label": c.label, "done": c.done, "job": c.job}
