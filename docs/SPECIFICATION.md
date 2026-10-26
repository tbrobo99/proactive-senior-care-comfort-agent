# Phase 2 — Software Specification

**Project** Proactive Senior Care & Comfort Agent: Risk-Graded Autonomous Home Orchestration
**Team** Ha Yun Kim, SooYoung Song, Ruitan Sun, Tang Bin
**Course** Information Systems — Phase 2 deliverable
**Status** Team draft / merge skeleton — **not the submission document**

> **Read this first.** This file is a scaffold for the team document, not a
> deliverable. Its technical sections are duplicated in
> [`DECISION-ENGINE-SPEC.md`](DECISION-ENGINE-SPEC.md), which is the
> **authoritative** version — it is checked against the implementation and the
> test suite. If the two disagree on a number, the annex is right and this file
> is stale.
>
> Ownership:
>
> | Sections | Owner | Note |
> |---|---|---|
> | §2–§7, §9, §10 | Tang Bin | in the annex |
> | §1 | Tang Bin drafts, 김하윤 frames the overall architecture narrative | the annex version is scoped to the decision engine, not the whole system |
> | §8 | **split** — timing/cooldown is Tang Bin's (annex §8); voice and senior-facing presentation is 송수영 / ruitan | |
> | §11, §12 | all | joint decision |
>
> Missing entirely from this draft, and required by the course: user personas,
> care scenarios, competitive positioning. Those are not technical sections and
> cannot be filled in from the code.

---

## 0. How to read this document

Phase 1 produced the **requirement baseline**: what the system must do, in the
client's words. This document is the **specification**: the same requirements
restated at the level of detail needed to build and to verify them.

The distinction is the one drawn in class — a requirement says *"display a set
of images"*, a specification says *"display image 1 at x=40..640, y=90..470,
maximum 600×380 px, for 10 seconds, then fade over 0.3 s"*. Every clause below
carries a number, a unit, and a way to check it. Where a clause cannot be
expressed that way yet, it is listed in §9 as open, not silently omitted.

Each section states: the **requirement** it serves (R-n), then the
**specification** (S-n.m) that satisfies it.

---

## 1. System boundary

| Layer | Responsibility | Deterministic? |
|---|---|---|
| Sensor layer | produce a 1 Hz reading of the simulated house | yes |
| Risk layer | score two indices, 0–100, with named drivers | yes |
| Policy layer | propose candidate actions; assign a tier to each | yes |
| Decision layer | choose among candidates, order them, or abstain | **LLM** |
| Approval layer | hold Tier-2 requests until the user answers | yes |
| Actuation layer | apply actions to the device adapter | yes |
| Presentation | web UI, voice in/out | yes |

**S-1.1** The LLM appears in exactly one place: the decision layer. It is not
the clock, not the sensor, not the safety gate, and not the actuator. It cannot
address a device directly; it returns *kinds* that are looked up in a whitelist.

**S-1.2** Every deterministic layer must remain fully functional with the LLM
disabled (`proactive.llm_decide = false`). In that mode the rule default — take
all candidates, in the order the policy proposed them — is used. This is the
behaviour the system degrades to, not an error state.

---

## 2. Units and quantities

**S-2.1** Temperatures are °C, one decimal. Humidities and drum humidity are
percent, 0–100. Heat load is watts, integer. Times are local time, ISO 8601;
durations are seconds unless suffixed.

**S-2.2** Rooms are the four keys `Living Room`, `Kitchen`, `Bedroom`, `Balcony`. Appliances:
`ac_living`, `ac_bedroom`, `ac_kitchen`, `purifier`, `range_hood`, `oven`,
`cooktop`, `fridge`, `washer`, `dryer`, `dehumidifier`.

**S-2.3** *The kitchen is observed through its fridge, not through its AC.* An
air conditioner reports the setpoint it was told; the fridge reports the room it
stands in — `external_temp` plus the compressor's `duty_cycle`, both of which a
real LG fridge exposes through ThinQ. The AC (`ac_kitchen`) is an actuator, not
a thermometer: it is the Tier-1 answer to "pre-cool a room before cooking", while
the range hood (`range_hood`) is the answer to heat that has already arrived.
Any clause that reads the kitchen's temperature from an AC is wrong.

---

## 3. Sensor layer

**R-1** The system observes the home continuously without user input.

**S-3.1 Sampling.** One reading per second (`tick_seconds = 1.0`). The reading
is a snapshot dict; the loop never waits for the LLM.

**S-3.2 History.** Each signal keeps the most recent **300 samples** (5 minutes
at 1 Hz) in a fixed-length buffer. A signal with fewer than 3 samples has a
trend of exactly `0.0` — never `None`, never an exception.

**S-3.3 Baseline.** Starting room state for the `idle` scenario:

| Room | Temp °C | Humidity % |
|---|---|---|
| Living Room | 29.0 | 52 |
| Kitchen | 30.0 | 54 |
| Bedroom | 28.5 | 55 |
| Balcony | 28.0 | 60 |

**S-3.4 Nameplate heat load.** The thermal index is a *load ratio*, so these
values are its unit of measure:

| Appliance | W |
|---|---|
| oven | 2200 |
| dryer | 1800 |
| dishwasher | 1200 |
| washer | 500 |

**S-3.5 Fridge as the kitchen's sensor node.** The fridge reports
`external_temp` (ambient at the cabinet) and `duty_cycle` in `[0.15, 1.00]`,
derived as `0.15 + (external_temp − 18) / 20`, clamped. A compressor working
harder is a kitchen getting hotter, and the fridge is the appliance that pays
for it.

**S-3.6 Range hood as the kitchen's actuator.** With the hood on, kitchen heat is
removed at `(kitchen_temp − 26) × 0.03 × dt`, capped at `0.3 × dt` per second.
Below 26 °C the hood removes nothing.

**S-3.7 Persistence.** Every tick appends to `data/sensors/readings.jsonl` and
rewrites `data/sensors/latest.csv`. Both are local files; nothing leaves the
machine.

**S-3.8 Calendar input is a local file.** NFR III.B names "sensor data **and
calendar inputs**". The calendar is `data/calendar.json` — a JSON array of
`{"title", "start", "end"}`, `start` in ISO 8601 — read at construction and on
`reload_calendar()`. A missing or malformed file is an *empty day*, not an
exception: a calendar that cannot be read is the same as a calendar with
nothing on it. The `oven` scenarios install their own entry on top of the file
after it is loaded, because a demo fixture is a fixture and the baseline is the
file.

**S-3.9 Scenarios.** Four deterministic starting states, selectable at launch:
`idle`, `mold`, `oven`, `oven_running`. `mold` = cycle finished 2 h ago, door
still closed, user out, drum at 90%, balcony at 66% RH. `oven` and
`oven_running` = a bake scheduled 20 min out, dryer running, kitchen already hot.

---

## 4. Risk layer

**R-2** The system predicts trouble before it happens, and can say why.

**S-4.1 Two indices, both 0–100, both clamped.** A risk is *fired* when
`score ≥ 50.0`. Each index returns `None` — not a zero score — when it does not
apply. This matters: "no risk" and "no evidence" are different states and the
UI shows them differently.

**S-4.2 Trend.** Least-squares slope over the buffered series, in units per
second, projected **30 minutes** ahead. Fewer than 3 points ⇒ slope 0.

### 4.3 MRI — Mold Risk Index

**S-4.4** Applies **only** when the washer cycle is `done` **and** the door is
closed. An open door is a problem that solves itself.

```
MRI = clamp( (drum_humidity − 70) × 3.2
           + sealed_hours        × 6.0
           + max(0, balcony_temp − 24) × 1.5 ,  0, 100 )
```

| Driver | Weight | Reads as |
|---|---|---|
| drum humidity above 70% | 3.2 / percentage point | wet |
| hours sealed | 6.0 / hour | trapped |
| balcony above 24 °C | 1.5 / °C | warm |

Predicted drum humidity = `clamp(humidity + slope × 1800, 0, 100)`.

**S-4.5 Worked example — the `mold` scenario.** Drum 90%, sealed 2.0 h, balcony
28.0 °C:

```
(90 − 70) × 3.2 = 64.0
        2.0 × 6.0 = 12.0
  (28 − 24) × 1.5 =  6.0
                  ------
                    82.0   → fires (≥ 50)
```

Measured value from `apply_scenario("mold")`: **82.0**. The `headline` the index
produces is *"Mold risk 82/100 on the washer drum (90% humidity, predicted 90% in
30 min)"* — that string goes into the message **payload** and the audit trail,
not onto the screen. What the user reads is the plain sentence in §8.6. The
predicted value equals the current one because a scenario loaded this instant
has no trend yet — after a few ticks the slope term becomes non-zero.

### 4.6 TOI — Thermal Overload Index

**S-4.7** Applies **only** when `heat_load_w > 0` **or** an oven is scheduled
within the next **30 minutes** (`0 ≤ Δ ≤ 30`). An oven scheduled later than
30 min is not yet a load.

```
TOI = clamp( heat_load_w / 25
           + max(0, kitchen_temp − 27)     × 6.0
           + max(0, fridge_duty − 0.60)    × 60.0
           + (15.0 if oven_scheduled_within_30min else 0) ,  0, 100 )
```

| Driver | Weight | Reads as |
|---|---|---|
| heat load | 1 / 25 W (2500 W = 100) | load |
| kitchen above 27 °C | 6.0 / °C | room stopped shedding heat |
| fridge duty above 60% | 60 / unit | the fridge is paying for it |
| oven within 30 min | flat 15 | load that has not arrived |

Predicted kitchen temp = `kitchen_temp + slope × 1800` (not clamped; a
prediction of 34.2 °C is information, and clipping it would hide it).

**S-4.8 Worked example — the `oven_running` scenario.** Dryer 1800 W + oven
2200 W = 4000 W, kitchen 31.5 °C, fridge duty 0.80, bake scheduled in 19 min:

```
4000 / 25          = 160.0
(31.5 − 27) × 6.0  =  27.0
(0.80 − 0.60) × 60 =  12.0
oven within 30 min =  15.0
                    -------
                      214.0   → clamped to 100
```

Measured value: **100.0**. Headline (payload only, not shown to the user):
*"Thermal overload 100/100 in the kitchen (31.5 °C, predicted 31.5 °C in
30 min)"*.

**S-4.9 The clamp is load-bearing, and it hides something.** The raw sum is 214,
so the index saturates well before the other terms matter: a 4000 W load alone
already exceeds 100. Above roughly 2500 W the score stops discriminating between
"hot" and "much hotter". This is acceptable for the decision it drives — *is
there a heat problem worth exhausting?* is a yes/no question, not a magnitude
one — but it is a calibration limit, not a feature. Recorded as O-4.

---

## 5. Policy layer — candidates and tiers

**R-3** Reversible, low-power actions may run on their own. Anything that
changes the user's plans must be asked first.

**S-5.1 `candidates(risk, snapshot)` returns a *set*, not a plan.** The order is
a suggestion. The decision layer may take some, reorder, or take none.

**S-5.2 Candidate generation.**

| Risk | Condition | Kind | Tier |
|---|---|---|---|
| mold | washer not already ventilating | `ventilate_washer` | 1 |
| mold | balcony humidity > 55% and dehumidifier off | `dehumidifier_on` | 1 |
| mold | purifier off | `purifier_on` | 1 |
| thermal | dryer is running | `delay_dryer` | 2 |
| thermal | range hood off | `ventilate_kitchen` | 1 |
| thermal | a cooking session starts within 30 min and the kitchen AC is off | `precool_kitchen` | 1 |
| thermal | a cooking session starts within 30 min and the cooktop is off | `preheat_cooktop` | 2 |

**S-5.3 `delay_dryer` carries a restart job**: dryer off now, back on at the
next **23:00** local, one-shot. The job is created **only if the user confirms**
— an unconfirmed approval must not leave a scheduled job behind.

**S-5.4 Tier table — the whitelist.** Exactly what S-5.2 can produce; a test
fails if the two drift apart.

| Tier 1 — automatic | Tier 2 — approval required |
|---|---|
| `ventilate_washer` | `delay_dryer` |
| `dehumidifier_on` | `preheat_cooktop` |
| `purifier_on` | |
| `ventilate_kitchen` | |
| `precool_kitchen` | |

The two examples the proposal gives for §II.C are both realised, and they come
from the same calendar event: `precool_kitchen` is the "pre-cooling a room before
cooking" the house does on its own, `preheat_cooktop` is the "warming up
cooktops" it asks about. `reschedule_washer` is still gone — no rule proposed
it, so it could never fire.

**S-5.5 Default deny.** Any action kind not in the table is **Tier 2**. A typo,
a future kind, or a hallucinated kind cannot become an automatic action by
being unknown.

**S-5.6 Why the line falls here.** Tier 1 worst case is a slightly dry room or a
fan running for nothing. Tier 2 worst case is a ruined garment, a delayed meal,
or a hot surface with nobody in front of it. The boundary is *reversibility and
who is standing there*, not *power draw*.

---

## 6. Decision layer (the LLM contract)

**R-4** The system decides which actions to take, not merely which thresholds
were crossed.

**S-6.1 Input.** Risk headline, drivers (JSON), `user_home`, and the candidate
list as `kind / tier / device / does / why`. Nothing else — no raw sensor
stream, no device handles.

**S-6.2 Output.** JSON only, and as short as the decision allows:

```json
{"chosen": ["ventilate_kitchen"]}
```

A bare list of kinds, in the order they should run. Deliberately no
per-candidate reasons and no log sentence: at the measured decode rate
(~36 ms per output token) those fields cost about 1.8 s, and the earlier
schema's `reason` was parsed and then discarded — the engine never read it.

**S-6.3 Call parameters.** `temperature = 0`, `max_tokens = 80` (a runaway
guard, not a latency lever), JSON mode.

**S-6.4 Validation.** Every returned `kind` is looked up in the *candidate* set.
Unknown kinds are dropped. Duplicates are dropped. Order is preserved as
returned. The model can be wrong about *what* to do; it cannot corrupt the house.

**S-6.5 Fallback — the safe default.** On exception, or on a response that is
not an object with a `chosen` key (a parse we do not trust), the engine takes
**all** candidates in rule order and marks the decision `decided_by = "rules"`.
Tier 1 then runs and Tier 2 is only proposed — so a degraded decision is still a
safe one.

**S-6.6 Abstention is legal; silence is not.** `{"chosen": []}` — the key
present, the list empty — is a real decision: the model judged the situation is
resolving on its own. Nothing executes. A response **without** the `chosen` key
is a parse we do not trust and falls back to the rule default (S-6.5). "The
model said do nothing" and "we could not read the model" must never collapse
into the same branch.

**S-6.7 The LLM never executes.** Chosen Tier-1 candidates are applied by the
engine. Chosen Tier-2 candidates become an approval request. There is no code
path from model output to actuator that does not pass one of those two doors.

**It also does not get to decide whether to ask.** Tier-2 candidates reach the
approval queue whether or not the model kept them, because whether to *ask* is a
policy question and the user, not the model, decides what runs. Measured: given
a candidate that switches on a heating appliance, `glm-4-flash` left it off the
list in **15 runs out of 15** — across four framings (cooktop, oven, "the
resident is standing there", cooking five or nineteen minutes away) and with the
caution removed from the prompt. That is the model's own judgement rather than a
prompt artefact, and it is not wrong; but if it decided the question, a cooking
session on the calendar would be silently ignored and the product's claim would
depend on the model happening to agree. The model keeps the part where its
judgement earns its keep: *which* Tier-1 actions are worth doing now. A "no", or
the fail-safe running out, buys `quiet_seconds = 1800` of silence for that risk
kind — asking again three minutes later is not respect.

---

## 7. Approval layer and the fail-safe

**R-5** No Tier-2 action executes without an explicit yes. Silence is a no.

**S-7.1 TTL = 300 s** (5 minutes), from the poster's §II.E.

**S-7.2 State machine.** `pending → confirmed | declined | expired`. Only a
`pending` approval can transition. A second confirm on the same id returns
`ok: false`; it does not re-execute.

**S-7.3 Deadline precision.** `expires_at` is written with **microsecond**
precision. A deadline truncated to whole seconds and compared against a
microsecond `now` expires up to one second early, and a fail-safe that fires
early is a fail-safe that lies.

**S-7.4 Expiry executes nothing.** The house is left exactly as it was, and the
user is told: *"I asked about the Balcony Dryer and did not hear back, so I
changed nothing. Everything is as it was."*

**S-7.5 Confirm executes, then schedules.** Actions are applied first; the
restart job is created only if the action succeeded.

**S-7.6 In memory, deliberately.** Pending approvals are not persisted. A
restart drops them, which under this policy means nothing executes — the safe
direction to fail in. Phase 1 made *scheduled jobs* durable because a lost job
is silent; a lost approval fails loud, because the user simply never sees the
question.

**S-7.7 One question per risk.** While an approval is pending for a risk kind,
that risk does not raise a second question. Re-asking the same thing every
second for five minutes is not persistence, it is harassment.

---

## 8. Loop timing, cooldown, and presentation

**S-8.1 Loop.** Once per second: `snapshot → record → evaluate → for each fired
risk not cooling and not pending → intervene → expire due approvals`. An
exception inside a tick is caught, logged, and the loop continues. The engine
watches the house; it must not be able to break it.

**S-8.2 Cooldown = 180 s per risk kind.** After an intervention, that kind is
silent for 3 minutes. This bounds LLM calls to at most **40 per hour** (2 kinds
× 3600/180), and bounds them to *events*, not ticks.

**S-8.3 Latency, measured** (GLM-4-Flash, Korean network, with the output schema
reduced to a bare list of kinds and the Tier-2 question rendered from a
template):

| Path | Measured | n |
|---|---|---|
| sensor tick + record | < 20 ms | — |
| risk evaluation (both indices) | < 5 ms | — |
| candidate generation | < 1 ms | — |
| **proactive tick, no risk fired** | **< 30 ms** | — |
| **Tier-1 dispatch, 3 actions** (mold: decide + auto-execute) | **median 2.72 s**, range 2.04–4.17 s | 6 |
| **Tier-1 + Tier-2 dispatch** (thermal: decide, execute, post the question) | **median 2.95 s**, range 1.60–4.16 s | 6 |

Latency is the model call and nothing else: `_decide` alone measures 2.36 s
median while the whole deterministic half of the tick measures under 30 ms. An
earlier run recorded 1.43 s for the Tier-1 path; that was before `purifier_on`
joined the mold branch, so the model answered with two kinds instead of three.
At ~36 ms per output token, the third kind is worth about a second.

**S-8.4 What blocks, and what does not.** The sensor simulator runs on its own
thread, so sampling continues at 1 Hz regardless of the model. The *proactive
loop* is single-threaded and calls the model synchronously: while a decision is
in flight, no other risk is evaluated and approval expiry is not checked.

Bounded, not unbounded: cooldown (S-8.2) plus one-question-per-risk (S-7.7) cap
model calls at ~40 per hour; at ~2.8 s each that is under 120 s of blocked loop
per hour (~3% duty). Worst-case delay to an expiry check is one dispatch
against a 300 s TTL — it cannot make a fail-safe fire early, only ~3 s late.

**S-8.5 NFR A — "less than 2.0 seconds per check" — resolved.**

| Path | Meets 2.0 s? | Evidence |
|---|---|---|
| deterministic check, every tick | **yes, by three orders of magnitude** | < 30 ms |
| dispatch when a risk fires (model in the loop) | **no** | 2.72 s / 2.95 s median |
| dispatch with `llm_decide: false` | **yes** | rules take every candidate, < 30 ms |

"Per check" is not "per intervention". The loop checks once a second and the
check is deterministic; the model is consulted only when an index crosses its
threshold, and then at most once per risk kind per 180 s cooldown — about 40
times an hour, not 3600. The 2.0 s bound is met exactly when the model is out
of the dispatch path (`llm_decide: false`, < 30 ms), and the two-tier policy,
the approval queue and the fail-safe all still run in that configuration; what
is lost is only the choice *among* candidates. With the model in the loop the
bound is a target, not a guarantee, because tail latency belongs to the model
provider — single runs of the *same* minimal request were observed at 3.04 s
and 3.51 s.

**S-8.6 Presentation is a template, not a generation.** Two sentences reach the
user, and neither is written by the model:

| Message | Shape |
|---|---|
| Tier-1 report | *"The washing machine has been shut for 2 hours with wet washing still inside it. So I started drum micro-ventilation on the washer and turned on the balcony dehumidifier."* |
| Tier-2 question | *"The kitchen is at 32 °C and the dryer is adding heat. I would like to turn the dryer off now and restart it at 11 PM tonight; switch the cooktop on now so it is hot when you start. Shall I? Reply yes or no — no answer in 5 minutes keeps everything as it is."* |

The index score, the extrapolation and the wattage stay in the message payload,
where the audit trail and this document can read them. They were in the
sentence once — *"Mold risk 82/100 on the washer drum (90% humidity, predicted
90% in 30 min). I start drum micro-ventilation…"* — a report, read aloud to a
person who has never heard of an index, in the present tense, about an action
that had already happened. Proposal §II.D asks for "plain, helpful language";
that sentence was neither.

Generating the Tier-2 question with the model is implemented and available
(`llm_phrase: true`), and off by default. It was measured against the template
and lost on both counts: 1.6–3.0 s per call (median 2.0 s, n=4), and it dropped
the five-minute deadline the prompt asked it to include, every time. The
template names the situation, the change, the deadline and the one-word answer,
always, in 0 ms — and it is the text that runs when the model is unreachable,
which is exactly when nobody is available to improve it.

No cloud LLM can promise a hard ceiling: single runs of the *same* minimal
request were observed at 3.04 s and 3.51 s — provider-side queueing, not our
code. Recommended wording for the submission:

> The monitoring loop completes in under 30 ms per tick (p100). When a risk
> index crosses its threshold the agent consults the language model; that
> decision-and-dispatch path completes in a median of 2.7 s (mold, three
> actions, n=6) and 3.0 s (thermal, one automatic action plus one approval
> request, n=6), with a largest single observation of 4.2 s. The 2.0 s bound in
> the requirement is met by the deterministic path and by the rules-only
> configuration (`llm_decide: false`, < 30 ms); with the model in the loop the
> bound is a target, not a guarantee, because tail latency belongs to the model
> provider.

Weaker-sounding, stronger in fact: every number in it is reproducible.

**S-8.7 Voice.** Output only for Tier-2 questions — a question the user did not
hear is a question the user did not answer, so the Tier-2 prompt is **always
spoken**, regardless of the read-aloud setting. Input via the Web Speech API;
default locale `en-US`, switchable to `ko-KR`. Text input is always available
and is the fallback, not a lesser path.

**S-8.5 Presentation — senior-first.**

| Property | Value |
|---|---|
| root font size | 18 px |
| minimum touch target | 56 px |
| device power button | 60 px |
| talk bar height | 62 px |
| Tier-2 answer area min-height | 76 px |
| theme | light, high contrast |
| Tier-2 answer | two buttons, "Yes" / "No", plus a live countdown in seconds |

One primary action per screen. Three tabs: home, schedule, assistant.

---

## 9. HTTP interface

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/snapshot` | devices + room state |
| GET | `/api/proactive` | risk state, active interventions, pending approvals |
| GET | `/api/approvals` | pending approvals only |
| POST | `/api/approvals/<id>/confirm` | the user said yes |
| POST | `/api/approvals/<id>/decline` | the user said no |
| POST | `/api/chat` | `{"text": "..."}` → reply, actions, tier, trace |
| GET/POST | `/api/mode` | rule-only vs LLM-assisted |
| GET | `/api/messages`, `/api/logs` | household log, audit trail |
| POST | `/api/control` | direct device control |
| POST | `/api/jobs/clear`, `/api/messages/clear` | housekeeping |

**S-9.1** `POST /api/approvals/<id>/confirm` on an expired id returns
`{"ok": false}` and changes nothing.

---

## 10. Acceptance criteria

Each row is an executable check. All currently pass (237 tests total; the 25
Phase-2 cases are named).

| # | Given | Then | Test |
|---|---|---|---|
| A-1 | `mold` scenario: drum 90%, sealed 2 h, door closed | MRI fires, score ≥ 50, prediction ≥ current | `test_mold_risk_fires_on_sealed_wet_drum` |
| A-2 | door open | MRI returns `None` | `test_mold_risk_silent_when_door_open` |
| A-3 | no cycle run | MRI returns `None` | `test_mold_risk_silent_when_no_cycle` |
| A-4 | dryer running + a bake scheduled within 30 min | TOI fires, `heat_load_w == 1800`, `cooking_scheduled_in_min` present | `test_thermal_risk_fires_with_dryer_and_scheduled_oven` |
| A-5 | nothing running, no calendar | TOI returns `None` | `test_thermal_risk_silent_when_no_heat` |
| A-6 | mold risk | all candidates are Tier 1 | `test_mold_candidates_are_tier1` |
| A-7 | thermal risk | candidates split across both tiers | `test_thermal_candidates_split_across_tiers` |
| A-8 | unknown action kind | treated as Tier 2 | `test_unknown_action_defaults_to_tier2` |
| A-9 | approval expires | nothing executed | `test_expiry_executes_nothing` |
| A-10 | mold scenario, LLM off | only Tier 1 runs, no question asked | `test_mold_scenario_runs_tier1_only` |
| A-11 | thermal scenario | Tier 1 runs **and** Tier 2 is asked | `test_thermal_scenario_executes_tier1_and_asks_tier2` |
| A-12 | user confirms | actions applied + restart job created | `test_confirmed_approval_executes_and_schedules_restart` |
| A-13 | user declines | house unchanged | `test_declined_approval_changes_nothing` |
| A-14 | user never answers | house unchanged | `test_expired_approval_changes_nothing` |
| A-15 | two ticks while pending | exactly one question | `test_second_tick_does_not_duplicate_a_pending_ask` |
| A-16 | tick immediately after intervening | cooldown suppresses it | `test_cooldown_blocks_immediate_repeat` |
| A-17 | LLM chooses a subset | only that subset runs | `test_llm_subset_is_respected` |
| A-18 | LLM returns `{"chosen": []}` | nothing runs — an abstention is a decision | `test_llm_abstention_does_nothing` |
| A-19 | LLM returns garbage | rule default, still safe | `test_llm_garbage_falls_back_to_rule_default` |
| A-20 | LLM chooses only Tier 1 | no question is asked | `test_llm_choosing_only_tier1_skips_the_question` |
| A-21 | JSON with no `chosen` key | **not** an abstention; falls back to rule default | `test_llm_missing_chosen_key_is_not_an_abstention` |

---

## 11. Deviation from the requirement baseline

Two examples in the Phase 1 proposal assumed a kitchen air conditioner. The
deployment does not have one.

| Proposal text | Specification reality |
|---|---|
| "pre-cooling a room before cooking" | **pre-ventilating** the kitchen before cooking — the range hood exhausts, it does not cool |
| "I started cooling the kitchen" | "I started the kitchen exhaust" |

The architecture, the two-tier policy, the fail-safe, the NFRs and the novelty
claim are unaffected: the thesis is *risk-graded autonomy*, and the kitchen is
still observed (through the fridge) and still actuated (through the hood). Only
the two illustrative sentences change.

---

## 12. Open items

| # | Question | Blocks |
|---|---|---|
| O-1 | Mobile deliverable: one codebase with a native shell, or two native codebases generated from this specification? | Phase 2 submission scope — awaiting the second set of course instructions |
| O-2 | Multi-occupant approval: whose yes counts when two people are home? | out of scope unless raised |
| O-3 | Real ThinQ integration: the adapter interface is fixed, the transport is mocked | out of scope for Phase 2 |
| O-4 | TOI saturates above ~2500 W (see S-4.9). Should the load term be normalised against a reference rather than divided by 25? | affects how much the score can say; does not affect any decision currently made |
