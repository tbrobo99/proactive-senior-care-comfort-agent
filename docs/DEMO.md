# Demo script

A 3-minute recording that shows the parts a screen recording usually cannot: what happens
after a restart, what happens to a task that was missed, and what happens when one device
in a group cannot comply.

## Before you hit record

```bash
python run.py
```

Then in the app:

1. Delete the existing scheduled tasks so the list starts empty.
2. Turn every device off, so state changes on screen are visible.
3. Type `turn off the living room AC in 2 minutes` — this creates a task that will fire
   while you are still recording. Do **not** let it fire before you start.
4. Type `every night at 11 pm turn on the bedroom AC` — this is the job you will sabotage
   in the recovery beat.
5. Clear the chat, then re-create the two tasks above and stop the server.

Have `README.md` open in a second window for the architecture beat, and a terminal with
`python -m eval.run` ready to paste.

## Beats

**0:00 — the problem (10 s)**

*"Every smart home demo can turn on an AC. The hard part is making it happen at 11 pm when
nobody is holding a phone — and being able to prove that it did."*

**0:10 — immediate control (15 s)**

```
turn on the living room AC
set it to 24 degrees
```

Point at the right-hand card: the temperature number, the mode pills, the room temperature
dropping. Note that `it` resolved to the AC you just named.

**0:25 — one sentence, several devices (20 s)**

```
set everything in the living room to 24 degrees
```

This is one utterance and two appliances, and the purifier cannot hold a temperature. Read
the reply out loud:

```
Done — 1 of 2 devices updated:
  · Living Room AC — set to 24°C
  · Living Room Purifier — skipped: does not support 'temperature'
```

*"The interesting line is the second one. Most agents either drop the device that cannot
comply or fail the whole command. Both hide information from the user."*

Then, for contrast:

```
turn off all the ACs
```

**0:45 — it asks instead of guessing (20 s)**

```
it's stuffy in here
```

The agent replies `Which room?` — there is no device in that sentence.

```
bedroom
```

*"It held the half-finished request and filled in the missing slot. The timeout is two
minutes, and before it fills a slot it re-parses the new text as a fresh request — so an
unrelated question never gets swallowed by a stale prompt."*

Try the failure mode live, it is the convincing part:

```
what's the temperature in the kitchen?
```

The pending question is dropped, the query is answered. Nothing gets hijacked.

**1:05 — the language is the interface (25 s)**

```
every weekday at 7 am turn on the bedroom AC
every Monday and Wednesday at 8 am turn off the living room AC
```

Show the task list filling up. Then:

```
what scheduled tasks do I have?
```

**1:30 — the scheduled run actually fires (20 s)**

Scroll to the task created before recording. Wait for it:

```
⏱ 21:14 Scheduled task ran · Living Room AC · Success: turn off
```

This is the money shot. *"That message is written by the scheduler thread, not by the
model. The task was in SQLite before the message existed."*

**1:50 — the part everyone skips: a run was missed (30 s)**

Kill the server. Change your system clock forward past 23:00 — or just wait, whichever is
easier on camera. Restart.

The conversation now contains:

```
⏱ 1 scheduled task(s) came due while the agent was offline. Nothing was fired late —
one-time tasks were closed out as missed and recurring ones moved to their next slot.
```

*"The naive fix is to replay it on startup. That is a second bug: turning on the bedroom AC
because it was supposed to turn on three hours ago is not what anyone wanted. So nothing is
replayed — the miss is recorded and said out loud. Silence here would be worse than the
failure, because the user would believe the AC came on."*

Point at the task list: the missed one is disabled, the recurring one shows tomorrow's
slot.

**2:20 — architecture (25 s)**

Switch to the README diagram. Two sentences:

1. *"The model translates; a scheduler thread waits and executes. Execution never goes
   through the LLM."*
2. *"Rules handle the common cases deterministically; the LLM only covers the long tail,
   and its output is validated before anything is sent to a device."*

**2:45 — the number (20 s)**

Run it on camera:

```bash
python -m eval.run
```

```
overall              56     56  100.0%
```

*"Fifty-six utterances, a fixed clock, no network, no API key. It runs in CI and fails the
build on regression. It is a guard against my own changes — not proof that the parser
handles a sentence I never thought of. The honest next step is a public benchmark."*

**3:05 — real hardware and close (20 s)**

Show `config.json` with `device_backend: thinq` and `dry_run: true`, plus
`python tools/thinq_check.py devices`.

*"Same pipeline, one config line. Dry-run is on by default so a payload mismatch can never
send a wrong command to an appliance. Voice input needs a browser with Web Speech —
Chrome, Edge or Safari, not Firefox — and the ThinQ control payload has
to be calibrated per model — everything else in this demo runs from a clean clone with no
dependencies."*

## Optional beat — agent mode (30 s, run it if asked "is this really an agent?")

Switch the Assistant tab to **Agent** at the top of the screen. The same sentence now goes
through the model instead of being answered by the rules, and the reply carries the steps
behind it — tap the little line under the bubble to open them.

```
the bedroom is a sauna
→ offer          the rules' reading: turn on, switch to cooling, set to 24°C — Bedroom AC
→ control        mode="cooling"     ✗ Mode must be one of cool, heat, dry, fan, auto
→ control        Bedroom AC — turn on, switch to cooling, set to 24°C
```

Say out loud what those three lines are: the rules *offered*, the model **declined the
offer**, sent a `mode` the device does not have, read the refusal back, and fixed it in the
next step. That is the loop's whole advantage in one screenshot — a plan that cannot be
wrong about the device, because the device said so.

Then flip back to **Cascade** and ask the same thing. Same outcome, ~1 ms, no steps to
show, because code picked the layer and never asked the model anything.

*"The cascade is a workflow: which layer answers is decided in code, from the shape of the
sentence. That is fast and testable, and it is fair to say the model never gets a say. In
agent mode the rules stop deciding and start offering — the parse is handed to the model as
a suggestion, and it either accepts it in one step or plans its own. Everything the model
can still do runs through the same validated toolbox, and if it cannot finish, the
deterministic answer is the one you read."*

Good sentences to use, in this order:

| sentence | what to point at |
|---|---|
| `turn off the living room AC` | it usually *declines* the offer and calls `control` itself — the offer is not an instruction |
| `the bedroom is a sauna` | declines the offer, sends a bad `mode`, reads the error, fixes it — and the cascade answers the same sentence in ~1 ms |
| `cool the bedroom down and shut the living room off` | two calls in one turn, a different action per room |
| `how long can I keep the chicken` | answered instantly, no steps — a lookup has nothing to decide |

That last row is the follow-up question to be ready for: *"Why not everything?"* Because
some answers have no decision in them. A storage answer is an outside rule plus a measured
temperature; the task list is a table. Neither has a tool, so putting them in front of a
model added 8.8 seconds to a 15-millisecond answer and changed nothing.

## Things that look bad on video

- Typing slowly. Paste the sentences.
- Narrating menus. Say what the system *guarantees*, not what you clicked.
- A demo where nothing fails. The skipped purifier and the missed run are the two most
  convincing moments — do not cut them for time.
- Recording at 1x with a 5-second scheduler wait. Pre-create the task instead.
- Claiming 100% means "understands everything". Say what the number measures.
