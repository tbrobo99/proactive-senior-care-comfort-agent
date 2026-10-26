# Decision Engine — Implementation Specification

**Subsystem owner** Tang Bin (software developer)
**Part of** Phase 2 specification — *Proactive Senior Care & Comfort Agent: Risk-Graded Autonomous Home Orchestration*
**Scope** Sensor layer, risk layer, policy layer, decision layer, approval layer, timing, HTTP interface, acceptance criteria
**Status** Implementation contract, derived from running code (`lg-home-agent`, branch `phase2`)

---

## 0. What this document is, and what it is not

This is a **technical annex**, not the project's specification document. It covers
one subsystem — the part that observes the house, scores risk, chooses actions,
and asks permission. It is written at the level of detail where every clause
carries a number, a unit, and a way to check it, because these numbers already
exist in the code and anyone restating them from memory will get them wrong.

**It deliberately does not cover** the sections that belong to other members:

| Not here | Owner | Why |
|---|---|---|
| User personas, care scenarios, the case for the product | 송수영 (customer), ruitan (user) | a usability argument, not a technical fact |
| Voice interaction design, senior-facing UI rationale | 송수영, ruitan | user research |
| Overall architecture narrative, system context | 김하윤 (dev manager) | team-level framing |
| Project management plan, milestones, division of work | 김하윤 | SPMP |
| Deviation notes vs the Phase 1 proposal | all | joint decision |

**Authority.** Where this annex and any other version of the specification
disagree on a number, this document is correct — it is checked against the
implementation, and the implementation is checked by tests (§9). The other
document should be updated to match, not the reverse.

**Known uncertainty.** The course has announced that the Phase 2 specification
format "will be explained with the second set of instructions" (lecture,
43:20). The *content* here is stable regardless of format; the *structure* may
need reshaping when those instructions arrive. Numbers will not change.

---

## 0.1 Mapping to the required submission structure

The course document (`phase2-project-II-2026.pdf`) requires section **IV.
Specifications** to be organised **per requirement**, each with a
"detailed description of implementation process (e.g., pseudocode level)" and,
recommended, a graphical representation. It is *not* organised per module.

The Phase 1 document already numbered the requirements. This annex maps onto
them one-to-one — nothing here needs rewriting, only re-cutting:

| Required section | Phase 1 requirement | This annex | Pseudocode needed |
|---|---|---|---|
| IV.A | II.A Sensor Data Collection | §3 | yes — the 1 Hz tick |
| IV.B | II.B Predictive Risk Assessment | §4 | yes — MRI, TOI, slope |
| IV.C | II.C Two-Tiered Action Framework | §5, §6 | yes — candidate generation, tier routing, decide |
| IV.D | II.D Senior-Friendly Conversational Interface | §9.2, §9.3, §9.4 | no — interface contract, but a diagram helps |
| IV.E | II.E Fail-Safe Default Policy | §7 | yes — the approval state machine |
| — | III.A Processing Latency | §8 | no — measurement table |
| — | III.B Simulation Containment | §3.7, §3.8, §3.9 | no |
| — | III.C System Reliability | §7.7, S-5.5, S-6.7 | no |

**Two things this annex does not yet contain and the submission requires:**
pseudocode (§0.1 column above) and a graphical representation (architecture
diagram, approval state machine, and one sequence diagram for the Tier-2 path).
Both are Tang Bin's to write for IV.A–IV.E; the document owner assembles them.

---

## 1. System boundary

| Layer | Responsibility | Deterministic? |
|---|---|---|
| Sensor | produce a 1 Hz reading of the house | yes |
| Risk | score two indices, 0–100, with named drivers | yes |
| Policy | propose candidate actions, assign a tier to each | yes |
| Decision | choose among candidates, order them, or abstain | **LLM** |
| Approval | hold Tier-2 requests until the user answers | yes |
| Actuation | apply actions to the device adapter | yes |

**S-1.1** The LLM appears in exactly one place: the decision layer. It is not
the clock, not the sensor, not the safety gate, and not the actuator. It cannot
address a device directly; it returns *kinds* that are looked up in a whitelist.

**S-1.2** Every deterministic layer remains fully functional with the LLM
disabled (`proactive.llm_decide = false`). In that mode the rule default — take
all candidates, in the order the policy proposed them — is used. That is the
degraded behaviour, not an error state.

---

## 2. Units and quantities

**S-2.1** Temperatures are °C, one decimal. Humidities are percent, 0–100. Heat
load is watts, integer. Times are local, ISO 8601; durations are seconds.

**S-2.2** Rooms: `Living Room`, `Kitchen`, `Bedroom`, `Balcony`. Appliances:
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

**S-3.1 Sampling.** One reading per second (`tick_seconds = 1.0`). The loop
never waits for the LLM.

**S-3.2 History.** Each signal keeps the most recent **300 samples** (5 minutes
at 1 Hz). A signal with fewer than 3 samples has a trend of exactly `0.0` —
never `None`, never an exception.

**S-3.3 Baseline (idle scenario).**

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
`external_temp` and `duty_cycle` in `[0.15, 1.00]`, derived as
`0.15 + (external_temp − 18) / 20`, clamped. A compressor working harder is a
kitchen getting hotter — and the fridge is the appliance that pays for it.

**S-3.6 Range hood as the kitchen's actuator.** With the hood on, kitchen heat
is removed at `(kitchen_temp − 26) × 0.03 × dt`, capped at `0.3 × dt` per
second. Below 26 °C the hood removes nothing.

**S-3.7 Persistence.** Every tick appends to `data/sensors/readings.jsonl` and
rewrites `data/sensors/latest.csv`. Local files only.

**S-3.8 Calendar input is a local file, not a live account.** NFR III.B says
"all sensor data **and calendar inputs** run locally using generated JSON/CSV
files". The calendar is `data/calendar.json` — a JSON array of
`{"title", "start", "end"}` with `start` in ISO 8601 — read at construction and
on `reload_calendar()`. A missing or malformed file is an *empty day*, not an
exception: a calendar that cannot be read is the same as a calendar with
nothing on it, and the house must keep working.

The `oven` scenario still installs its own entry after the file is loaded. A
demo fixture is a fixture; the baseline is the file. Covered by
`test_calendar_is_read_from_a_local_json_file`,
`test_missing_or_broken_calendar_file_is_an_empty_day` and
`test_calendar_entry_drives_the_thermal_index`.

**S-3.9 Data directory.** `<repo>/data`. The sensor module computed this as
`__file__.parent.parent.parent` — one level too high — so readings landed in the
*parent* of the repository while the SQLite database stayed in `data/`. Both
live under `data/` now, which is what this section and NFR III.B claim.

**S-3.10 Scenarios.** Four deterministic starting states: `idle`, `mold`, `oven`,
`oven_running`. `mold` = cycle finished 2 h ago, door closed, user out, drum at
90%, balcony at 66% RH. `oven` / `oven_running` = a bake scheduled 20 min out,
dryer running, kitchen already hot.

---

## 4. Risk layer

**S-4.1 Two indices, both 0–100, both clamped.** A risk *fires* at
`score ≥ 50.0`. Each index returns `None` — not a zero score — when it does not
apply. "No risk" and "no evidence" are different states and the UI shows them
differently.

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

Measured: **82.0**. The predicted value equals the current one because a
scenario loaded this instant has no trend yet; after a few ticks the slope term
becomes non-zero.

### 4.6 TOI — Thermal Overload Index

**S-4.7** Applies **only** when `heat_load_w > 0` **or** an oven is scheduled
within the next **30 minutes** (`0 ≤ Δ ≤ 30`). An oven scheduled later is not
yet a load.

```
TOI = clamp( heat_load_w / 25
           + max(0, kitchen_temp − 27)   × 6.0
           + max(0, fridge_duty − 0.60)  × 60.0
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
2200 W = 4000 W, kitchen 31.5 °C, fridge duty 0.80, bake in 19 min:

```
4000 / 25          = 160.0
(31.5 − 27) × 6.0  =  27.0
(0.80 − 0.60) × 60 =  12.0
oven within 30 min =  15.0
                    -------
                      214.0   → clamped to 100
```

Measured: **100.0**.

**S-4.9 The clamp is load-bearing, and it hides something.** The raw sum is 214,
so the index saturates well before the other terms matter: a 4000 W load alone
already exceeds 100. Above roughly 2500 W the score stops discriminating between
"hot" and "much hotter". Acceptable for the decision it drives — *is there a
heat problem worth exhausting?* is a yes/no question, not a magnitude one — but
it is a calibration limit, not a feature. Recorded as O-4.

---

## 5. Policy layer — candidates and tiers

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

**S-5.4 Tier table — the whitelist.** The table is exactly what S-5.2 can
produce; a test (`test_whitelist_is_exactly_what_the_rules_can_propose`) fails
if the two ever drift apart. An entry no rule proposes is a capability the code
claims and does not have.

| Tier 1 — automatic | Tier 2 — approval required |
|---|---|
| `ventilate_washer` | `delay_dryer` |
| `dehumidifier_on` | `preheat_cooktop` |
| `purifier_on` | |
| `ventilate_kitchen` | |
| `precool_kitchen` | |

The two examples the proposal gives for §II.C are both realised here, and they
come from the same calendar event: `precool_kitchen` is the "pre-cooling a room
before cooking" the house does on its own, `preheat_cooktop` is the "warming up
cooktops" it asks about. One kind from an earlier draft is still gone:
`reschedule_washer` — no rule would ever want to move the washer, so the entry
was a claim with nothing behind it.

**S-5.5 Default deny.** Any action kind not in the table is **Tier 2**. A typo,
a future kind, or a hallucinated kind cannot become automatic by being unknown.

**S-5.6 Why the line falls here.** Tier 1 worst case is a slightly dry room or a
fan running for nothing. Tier 2 worst case is a ruined garment, a delayed meal,
or a hot surface with nobody in front of it. The boundary is *reversibility and
who is standing there*, not power draw — a 2.2 kW oven the user asked for is
Tier 1 territory, and a hob switched on by nobody is not.

---

## 6. Decision layer (the LLM contract)

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
schema's `reason` was parsed and then discarded — the engine never read it. The
household-facing message is composed deterministically (S-6.8), not generated
by the model.

**S-6.3 Call parameters.** `temperature = 0`, `max_tokens = 80` (a runaway
guard, not a latency lever — latency tracks the tokens actually produced), JSON
mode.

**S-6.4 Validation.** Every returned `kind` is looked up in the *candidate* set.
Unknown kinds dropped, duplicates dropped, order preserved as returned. The
model can be wrong about *what* to do; it cannot corrupt the house.

**S-6.5 Fallback — the safe default.** On exception, or on a response that is
not an object with a `chosen` key (a parse we do not trust), the engine takes
**all** candidates in rule order and marks `decided_by = "rules"`. Tier 1 then
runs and Tier 2 is only proposed — a degraded decision is still a safe one.

**S-6.6 Abstention is legal; silence is not.** `{"chosen": []}` — the key
present, the list empty — is a real decision: the model judged the situation is
resolving on its own. Nothing executes, and the household log says so.

A response **without** the `chosen` key is not an abstention. It is a parse we
do not trust, and it falls back to the rule default (S-6.5). The distinction has
to be structural, because "the model said do nothing" and "we could not read the
model" must never collapse into the same branch — one is a decision, the other
is a failure, and only the second should run the house on rules.

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

**S-6.8 Wording is a template, not a generation.** Two sentences are shown to
the user, and neither is written by the model:

| Message | Shape |
|---|---|
| Tier-1 report | *"The washing machine has been shut for 2 hours with wet washing still inside it. So I started drum micro-ventilation on the washer and turned on the balcony dehumidifier."* |
| Tier-2 question | *"The kitchen is at 32 °C and the dryer is adding heat. I would like to turn the dryer off now and restart it at 11 PM tonight; switch the cooktop on now so it is hot when you start. Shall I? Reply yes or no — no answer in 5 minutes keeps everything as it is."* |

The index score, the extrapolation and the wattage stay in the message
*payload*, where the audit trail and this document can read them. They were in
the sentence once: *"Mold risk 82/100 on the washer drum (90% humidity,
predicted 90% in 30 min). I start drum micro-ventilation…"* — a report, read
aloud to a person who has never heard of an index, in the present tense, about
an action that had already happened.

Phrasing the Tier-2 question with the model was implemented and is still
available (`llm_phrase: true`). It is **off by default**, because it was
measured against the template and lost on both counts: 1.6–3.0 s per call
(median 2.0 s, n=4), and it dropped the five-minute deadline the prompt
explicitly asked it to include, every time. The template names the situation,
the change, the deadline and the one-word answer, always, in 0 ms — and it is
the text that runs when the model is unreachable, which is exactly when nobody
is available to improve it.

**S-6.9 What the model is *not* told.** The prompt does not say that a chosen
Tier-2 action will be put to the user for approval. It used to, and the model
then left the Tier-2 candidate off its list — run after run — because asking a
person reads as a burden. Ten consecutive engine runs on the thermal scenario
with the sentence removed: the Tier-2 candidate was chosen 10/10. Nothing about
safety depended on the sentence; S-6.7 and §7 enforce that in code. The lesson
generalises: a model told about a cost will optimise it away, so state
constraints in code and give the model only the facts it needs to decide.

---

## 7. Approval layer and the fail-safe

**S-7.1 TTL = 300 s** (5 minutes), from the proposal's §II.E.

**S-7.2 State machine.** `pending → confirmed | declined | expired`. Only a
`pending` approval can transition. A second confirm on the same id returns
`ok: false`; it does not re-execute.

**S-7.3 Deadline precision.** `expires_at` is written with **microsecond**
precision. A deadline truncated to whole seconds and compared against a
microsecond `now` expires up to one second early — and a fail-safe that fires
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
that risk does not raise a second question. Re-asking every second for five
minutes is not persistence, it is harassment.

---

## 8. Loop timing and latency budget

**S-8.1 Loop.** Once per second: `snapshot → record → evaluate → for each fired
risk not cooling and not pending → intervene → expire due approvals`. An
exception inside a tick is caught, logged, and the loop continues. The engine
watches the house; it must not be able to break it.

**S-8.2 Cooldown = 180 s per risk kind.** After an intervention that kind is
silent for 3 minutes. This bounds LLM calls to at most **40 per hour**
(2 kinds × 3600 / 180), and bounds them to *events*, not ticks.

**S-8.3 Latency, measured.** GLM-4-Flash, from a Korean network, with the output
schema reduced to a bare list of kinds (S-6.2) and the Tier-2 question rendered
from a template rather than generated (S-6.5).

| Path | Measured | n |
|---|---|---|
| sensor tick + record | < 20 ms | — |
| risk evaluation (both indices) | < 5 ms | — |
| candidate generation | < 1 ms | — |
| **proactive tick, no risk fired** | **< 30 ms** | — |
| **Tier-1 dispatch, 3 actions** (mold: decide, then auto-execute) | **median 2.72 s**, range 2.04–4.17 s | 6 |
| **Tier-1 + Tier-2 dispatch** (oven: decide, execute, post the question) | **median 2.95 s**, range 1.60–4.16 s | 6 |

Latency is the model call and nothing else: `_decide` measured 2.36 s median
while the entire deterministic half of the tick (snapshot, record, both risk
indices, candidate generation) measured under 30 ms. The two paths are close
because Tier 2 no longer makes a second model call.

Two numbers moved and the reason matters. An earlier measurement recorded a
1.43 s median for the Tier-1 path; that was before `purifier_on` was added to
the mold branch (S-5.2), so the model answered with two kinds instead of three.
Roughly 36 ms per output token, and the third kind is worth about a second.

Re-running `python -m eval.latency` on a different evening moves the median by
up to a second in either direction — 1.9 s and 3.5 s have both been observed for
the same mold dispatch, same prompt, same model. The provider is the variable,
not the code, which is why the requirement is proposed as a budget (S-8.5)
rather than as a ceiling.

**S-8.4 What blocks, and what does not.** The sensor simulator runs on its own
thread, so sampling continues at 1 Hz regardless of the model. The *proactive
loop* is single-threaded and calls the model synchronously: while a decision is
in flight, no other risk is evaluated and approval expiry is not checked.

This is bounded, not unbounded. Cooldown (S-8.2) plus one-question-per-risk
(S-7.7) cap the model calls at ~40 per hour; at ~2.8 s each that is under 120 s
of blocked loop per hour, about 3% duty. The worst-case delay to an expiry
check is one dispatch against a 300 s TTL — it cannot make a fail-safe fire
early, only up to ~3 s late.

**S-8.5 NFR A — "less than 2.0 seconds per check" — resolved.**

| Path | Meets 2.0 s? | Evidence |
|---|---|---|
| deterministic check, every tick | **yes, by three orders of magnitude** | < 30 ms |
| dispatch when a risk fires (LLM in the loop) | **no** | 2.72 s / 2.95 s median |
| dispatch with `llm_decide: false` | **yes** | rules take every candidate, < 30 ms |

Three things are true and should all be stated in the submission:

1. **"Per check" is not the same as "per intervention".** The loop checks once a
   second and the check is deterministic. The model is consulted only when an
   index crosses its threshold, and then at most once per risk kind per 180 s
   cooldown — roughly 40 times an hour, not 3600.
2. **The requirement is met exactly when the model is out of the dispatch
   path.** `llm_decide: false` makes the rules take every candidate: 30 ms, and
   the two-tier policy, the approval queue and the fail-safe all still run. What
   is lost is only the choice *among* candidates.
3. **No cloud LLM can promise a hard ceiling.** Single runs of the *same*
   minimal request were observed at 3.04 s and 3.51 s, and one dispatch in six
   took **16.7 s** while choosing exactly what the other five chose — a stall,
   not a different answer. That is provider-side queueing, not our code. An NFR
   written as an absolute upper bound on a remote service is not a
   specification, it is a hope.

**The two budgets, in one breath.** The single figure is the mistake, not the
number. Three sentences carry the argument, and the third is the one that
reframes it — five seconds stops being a relaxation and becomes a different
quantity from the one the requirement was measuring:

> The check runs every second. The decision runs once, when something is wrong.
> So we measure them separately: check under 50 milliseconds, decision under 5
> seconds.
> The decision has a five-minute deadline anyway — five seconds is under two
> percent of it.

5 / 300 = **1.7%**. Against the measured medians (< 30 ms and 1.9–3.5 s) both
budgets hold with room to spare, and `python -m eval.latency` prints both rather
than asking anyone to take them on trust. Note where the fail-safe clock starts:
`approval.propose()` writes `expires_at` *after* the model has returned, so a
slow decision costs responsiveness and never eats into the user's five minutes.

Recommended wording for the Phase 2 document:

> We propose measuring the two paths separately. The monitoring loop completes
> in under 30 ms per tick (p100) and never touches the network; the decision
> path — one model call, taken only when a risk index crosses its threshold, at
> most once per risk kind per three minutes — completes in a median of 1.9–3.5 s
> across runs, with a 5 s budget. The decision is followed by a 300 s fail-safe
> whose clock starts when the question is asked, so the budget governs
> responsiveness, not safety. The single 2.0 s bound is met by the deterministic
> path and by the rules-only configuration (`llm_decide: false`, < 30 ms).

This is a weaker-sounding but stronger claim than "less than 2.0 seconds per
check", because every number in it can be reproduced.

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
| GET / POST | `/api/mode` | rule-only vs LLM-assisted |
| GET | `/api/messages`, `/api/logs` | household log, audit trail |
| POST | `/api/control` | direct device control |
| POST | `/api/jobs/clear`, `/api/messages/clear` | housekeeping |

**S-9.1** `POST /api/approvals/<id>/confirm` on an expired id returns
`{"ok": false}` and changes nothing.

### 9.2 What the presentation layer gets from this subsystem

This is the hand-off surface for whoever builds the UI and the voice channel.

| Need | Source | Shape |
|---|---|---|
| "is something happening?" | `GET /api/proactive` → `active` | `{risk_kind: {score, at, executed[], approval}}` |
| "am I being asked something?" | `GET /api/proactive` → `pending_approvals[]` | `{id, question, summary, expires_at, status}` |
| "how long do I have?" | `expires_at` | ISO timestamp, microsecond precision — compute the countdown client-side, never re-derive it from a poll interval |
| "what did the house do?" | `GET /api/messages` | `role: "system"` entries carry `risk`, `score`, `drivers`, `executed[]` |
| answer a question | `POST /api/approvals/<id>/confirm\|decline` | `{ok: bool}` — `false` means the window already closed |

**S-9.3** The `question` string is already plain language, generated for a
senior reader: at most two sentences, names the appliance, states the deadline,
offers a one-word reply. The UI should display it **verbatim** — rewording it
in the presentation layer would undo the phrasing rules in §6.2.

**S-9.4** A Tier-2 question must be **spoken as well as shown**. A question the
user did not hear is a question the user did not answer, and the fail-safe would
then punish them for it.

---

## 10. Acceptance criteria

Each row is an executable check. All currently pass — 237 tests total, 25 of
them Phase 2.

| # | Given | Then | Test |
|---|---|---|---|
| A-1 | `mold` scenario: drum 90%, sealed 2 h, door closed | MRI fires, score ≥ 50, prediction ≥ current | `test_mold_risk_fires_on_sealed_wet_drum` |
| A-2 | door open | MRI returns `None` | `test_mold_risk_silent_when_door_open` |
| A-3 | no cycle run | MRI returns `None` | `test_mold_risk_silent_when_no_cycle` |
| A-4 | dryer running + bake within 30 min | TOI fires, `heat_load_w == 1800`, `cooking_scheduled_in_min` present | `test_thermal_risk_fires_with_dryer_and_scheduled_oven` |
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
| A-16 | tick right after intervening | cooldown suppresses it | `test_cooldown_blocks_immediate_repeat` |
| A-17 | LLM chooses a subset | only that subset runs | `test_llm_subset_is_respected` |
| A-18 | LLM returns `{"chosen": []}` | nothing runs — an abstention is a decision | `test_llm_abstention_does_nothing` |
| A-19 | LLM returns garbage | rule default, still safe | `test_llm_garbage_falls_back_to_rule_default` |
| A-20 | LLM chooses only Tier 1 | no question is asked | `test_llm_choosing_only_tier1_skips_the_question` |
| A-21 | LLM returns JSON with no `chosen` key | **not** treated as abstention; falls back to rule default | `test_llm_missing_chosen_key_is_not_an_abstention` |

---

## 11. Open items owned by this subsystem

| # | Question | Impact |
|---|---|---|
| O-1 | TOI saturates above ~2500 W (S-4.9). Normalise the load term against a reference instead of dividing by 25? | affects how much the score can say; affects no decision currently made |
| O-2 | Real ThinQ integration: the adapter interface is fixed, the transport is mocked | out of scope for Phase 2 |
| O-3 | Multi-occupant approval: whose yes counts when two people are home? | out of scope unless raised |

Items that are *not* mine and are still open elsewhere: the mobile deliverable
form (one codebase with a native shell vs two native codebases) and the
deviation note against the Phase 1 proposal. Both are team decisions.
