"use strict";

/* Senior-first front end. Three rules this file follows:
   1. Every screen answers one question: "is anything wrong, and what do I do?"
   2. The house speaks first. A Tier-2 request takes the top of the screen and
      waits there with two big answers and an honest clock.
   3. Voice is a channel, not the only one: the text box is always there. */

const $ = (sel) => document.querySelector(sel);

const ICON = {
  air_conditioner: "i-ac",
  air_purifier: "i-purifier",
  washer: "i-washer",
  dryer: "i-washer",
  dish_washer: "i-washer",
  refrigerator: "i-fridge",
  water_heater: "i-fridge",
  dehumidifier: "i-purifier",
  range_hood: "i-hood",
  oven: "i-hood",
  cooktop: "i-cooktop",
};

const BACKEND_LABEL = { mock: "Simulator", thinq: "LG ThinQ" };
const MODE_LABEL = { cool: "Cool", heat: "Heat", dry: "Dry", fan: "Fan" };
const MODES = ["cool", "heat", "dry", "fan"];

let snapshot = null;   // /api/snapshot: devices, jobs, mode
let pro = null;        // /api/proactive: risks, approvals, sensor state
let activeRoom = "All";
let currentTab = "home";
let sheetId = null;
let chipsSig = "";
let askSig = "";          // signature of the pending approval queue
let askBusy = false;
let spokenAsks = new Set();
let auditRows = null;     // /api/audit, FR-6 trail

/* ---------- helpers ---------- */

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...(opts || {}),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function findDevice(id) {
  return snapshot ? (snapshot.devices || []).find((d) => d.id === id) : null;
}

function fmtWhen(iso) {
  if (!iso) return "—";
  const when = new Date(iso);
  if (isNaN(when)) return iso;
  const now = new Date();
  const time = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (when.toDateString() === now.toDateString()) return `today ${time}`;
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  if (when.toDateString() === tomorrow.toDateString()) return `tomorrow ${time}`;
  return `${when.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

function cap(text) {
  const s = String(text == null ? "" : text);
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

function repeatShort(label) {
  const s = String(label || "").trim();
  if (!/every/i.test(s)) return "once";
  return s.replace(/\s+at\s+\d{1,2}:\d{2}\s*$/, "").trim();
}

/* ---------- voice ---------- */

/* The browser listens (Web Speech API), the text box stays permanent: a
   misheard word is fixed by typing, never by repeating yourself at a machine.
   English first because the course, the demo and the examiner are English;
   Korean is one tap away because the household is not. */
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const VOICE_LANGS = ["en-US", "ko-KR"];
const VOICE_PAUSE_MS = 2000;   // a pause this long means "I have finished"
const VOICE_TAIL_QUIET_MS = 900;   // a "final" this long after any audio is a guess
const VOICE_MAX_RESTARTS = 8;
let voiceLang = VOICE_LANGS[0];
let recognition = null;
let listening = false;
let ttsOn = false;
let voiceTimer = null;         // the pause that ends the turn
let voiceRestarts = 0;
let voiceBase = "";            // words finalised before the current session
let voiceSaid = "";            // finalised words so far in this turn
let voiceWarned = false;       // the "I heard nothing" hint, once per turn
let voiceDone = false;         // this turn has already been handed to send()
let voiceFinals = 0;           // finalised phrases the engine has handed back
let voiceInterim = "";         // the last thing the engine was still guessing at
let voiceLastHeard = 0;        // when any text last arrived from the engine

function speak(text, force) {
  if (!(ttsOn || force) || !("speechSynthesis" in window)) return;
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (!clean) return;
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(clean);
  utter.lang = voiceLang;
  utter.rate = 0.95;  // the audience is not in a hurry
  window.speechSynthesis.speak(utter);
}

function setListening(on) {
  listening = on;
  const btn = $("#mic");
  if (btn) {
    btn.classList.toggle("listening", on);
    btn.setAttribute("aria-pressed", String(on));
  }
  // Say out loud that the turn is still open. Without this the only signal that
  // the microphone is done is the words appearing in the box, and a pause of a
  // second and a half looks identical to having been cut off.
  const input = $("#input");
  if (input) {
    if (on) input.dataset.ph = input.dataset.ph || input.placeholder;
    input.placeholder = on ? "Listening — pause when you're done" : (input.dataset.ph || "Or type here…");
  }
}

/* The engine hands back each phrase without the space that belonged between
   them: "turn on the living" and "room AC" are two phrases, and gluing them
   with + makes one word out of two. */
function glue(a, b) {
  if (!a) return b || "";
  if (!b) return a;
  if (/[\s-]$/.test(a) || /^[\s,.;:!?]/.test(b)) return a + b;
  return `${a} ${b}`;
}

/* End the turn: take whatever is in the box and send it. Guarded by voiceDone so
   the three ways a turn can end — the pause timer, a second tap, the engine
   giving up — cannot send the same sentence twice. */
function voiceFinish() {
  if (voiceDone) return;
  voiceDone = true;
  clearTimeout(voiceTimer);
  voiceTimer = null;
  const text = ($("#input").value || voiceSaid).replace(/\s+/g, " ").trim();
  setListening(false);
  try { if (recognition) recognition.stop(); } catch (_) { /* already stopped */ }
  if (text) send(text);
}

/* The pause, not the engine, decides when the sentence is over. Every result
   pushes it back, so a breath in the middle of a sentence costs nothing. */
function voiceArmPause() {
  clearTimeout(voiceTimer);
  voiceTimer = setTimeout(voiceFinish, VOICE_PAUSE_MS);
}

function startVoice() {
  if (!SpeechRecognition) {
    toast("Voice needs Chrome or Safari — please type.");
    return;
  }
  if (listening) { voiceFinish(); return; }   // a second tap means "send now"
  if (!recognition) {
    recognition = new SpeechRecognition();
    recognition.interimResults = true;
    // continuous, because the default ends the session at the first short
    // pause: "turn on the living room AC … and set it to 24" arrived as two
    // half sentences, and the first half was already on its way to the model
    // before the second half was spoken.
    recognition.continuous = true;
    recognition.onresult = (event) => {
      // Recompute from index 0 rather than from resultIndex: with continuous
      // on, the results list is the whole turn, and rebuilding it is idempotent
      // where appending to it is not.
      const now = Date.now();
      let finalText = "";
      let interim = "";
      let finals = 0;
      for (let i = 0; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) {
          finals++;
          finalText = glue(finalText, result[0].transcript);
        } else if (String(result[0].transcript || "").trim()) {
          interim = glue(interim, result[0].transcript);
        }
      }
      if (interim) voiceInterim = interim;

      if (finals > voiceFinals) {
        // Speech is announced before it is confirmed: while a person is
        // talking, the engine keeps handing back interim text, and the final
        // phrase lands as the confirmation of something it was already
        // guessing at. A phrase that arrives with nothing interim behind it,
        // more than a second after the last audio, was not spoken — the engine
        // filled a silence. "turn on living room" came back as "turn on living
        // room they say": the tail was invented, and because every result
        // pushed the pause timer back, a slow speaker's turn never ended on
        // their own schedule.
        const invented = voiceSaid && !voiceInterim &&
                         now - voiceLastHeard > VOICE_TAIL_QUIET_MS;
        if (invented) return;        // drop it; the pause timer keeps running
        voiceFinals = finals;
        voiceInterim = "";
      } else if (!interim) {
        return;                      // nothing new: do not extend the turn
      }

      voiceLastHeard = now;
      voiceSaid = glue(voiceBase, finalText).trim();
      // Live in the box: the user sees what was heard before it is sent, which
      // is the difference between trusting a microphone and guessing at one.
      $("#input").value = glue(voiceSaid, interim).trim();
      voiceArmPause();
    };
    recognition.onerror = (event) => {
      if (voiceDone) return;
      if (event.error === "not-allowed") {
        toast("Microphone permission denied — please type.");
        voiceDone = true;      // stop onend from restarting a denied mic
      } else if (event.error === "no-speech") {
        // Silence, not a pause: nothing at all was captured this turn. On a
        // laptop this is almost always the wrong input device — Windows keeps
        // the headset-jack microphone selected, which is a microphone that
        // exists and hears nothing unless something is plugged into it. Saying
        // so is the difference between "the app is broken" and "pick the other
        // microphone".
        if (!voiceWarned && !voiceSaid && !$("#input").value.trim()) {
          voiceWarned = true;
          toast("I heard nothing — check which microphone Windows is using.");
        }
        return;                // onend decides; nothing was said yet
      } else if (event.error !== "aborted") {
        toast("Voice stopped — please type.");
      }
    };
    recognition.onend = () => {
      if (voiceDone) { setListening(false); return; }
      // Safari ends the session at every pause even with continuous = true.
      // Start it again and keep what has been heard, so the pause timer — not
      // the engine — is what ends the turn. Delayed slightly to avoid spinning.
      if (listening && voiceRestarts < VOICE_MAX_RESTARTS) {
        voiceRestarts++;
        setTimeout(() => {
          if (voiceDone || !listening) return;
          // A new session starts with an empty results list, so bank what the
          // last one heard before asking for more.
          voiceBase = voiceSaid;
          voiceFinals = 0;
          voiceInterim = "";
          try { recognition.start(); } catch (_) { voiceFinish(); }
        }, 250);
        return;
      }
      voiceFinish();
    };
  }
  recognition.lang = voiceLang;
  voiceBase = "";
  voiceSaid = "";
  voiceWarned = false;
  voiceDone = false;
  voiceRestarts = 0;
  voiceFinals = 0;
  voiceInterim = "";
  voiceLastHeard = Date.now();
  clearTimeout(voiceTimer);
  $("#input").value = "";
  try {
    recognition.start();
    setListening(true);
  } catch (err) {
    setListening(false);  // already running; the tap is a stop
  }
}

function renderLangButton() {
  const btn = $("#lang");
  if (btn) btn.textContent = voiceLang.startsWith("ko") ? "KO" : "EN";
}

/* ---------- status: the one thing the house has to say ---------- */

function houseStatus() {
  const pending = (pro && pro.pending_approvals) || [];
  if (pending.length) {
    return { tone: "ask",
             line: pending.length === 1
               ? "The house is waiting for your answer."
               : `The house is waiting for ${pending.length} answers.`,
             detail: "Each question has its own Yes / No — silence changes nothing." };
  }
  // Live scores, not the last intervention: a risk that cleared minutes ago
  // must not still be reported as happening (FR-4).
  const risks = (pro && pro.risks) || {};
  const firing = Object.values(risks).filter((r) => r.firing);
  if (firing.length) {
    const names = firing.map((r) => RISK_TEXT[r.kind].short);
    return { tone: "watch",
             line: `The house is handling ${names.join(" and ")}.`,
             detail: "Small fixes run on their own; anything bigger is asked first." };
  }
  return { tone: "ok", line: "Everything is fine.", detail: "No risks to deal with." };
}

function renderStatus() {
  const s = houseStatus();
  $("#status").dataset.tone = s.tone;
  $("#statusLine").textContent = s.line;
  $("#statusDetail").textContent = s.detail;
}

/* ---------- the ask: one card per Level-2 action (FR-4) ---------- */

function renderAsk() {
  const list = (pro && pro.pending_approvals) || [];
  const box = $("#askQueue");
  const sig = list.map((a) => a.id).join("|");
  if (sig !== askSig) {
    askSig = sig;
    box.innerHTML = list.map((ap, i) => {
      return `
        <div class="ask" data-ask="${esc(ap.id)}" role="group" aria-label="Question ${i + 1} of ${list.length}">
          <p class="ask-kicker">The house is asking
            <span class="ask-index">${i + 1} of ${list.length}</span></p>
          <p class="ask-action">${esc(askDevice(ap))}<span class="ask-id">request ${esc(ap.id)}</span></p>
          <p class="ask-question">${esc(ap.question || ap.summary || "")}</p>
          <div class="ask-answers">
            <button class="answer yes" data-answer="yes" data-id="${esc(ap.id)}">
              <svg viewBox="0 0 24 24"><use href="#i-check"/></svg>
              <span>Yes<small>네</small></span>
            </button>
            <button class="answer no" data-answer="no" data-id="${esc(ap.id)}">
              <svg viewBox="0 0 24 24"><use href="#i-x"/></svg>
              <span>No<small>아니요</small></span>
            </button>
          </div>
          <p class="ask-clock" data-clock="${esc(ap.id)}"></p>
        </div>`;
    }).join("");
    // Spoken prompts are supplementary (FR-4). Two text-to-speech questions
    // in a row cut each other off, so announce the first new card per batch.
    if (list.length && !spokenAsks.has(list[0].id)) {
      spokenAsks.add(list[0].id);
      speak(list[0].question || list[0].summary, true);
    }
  }
  for (const ap of list) {
    const el = box.querySelector(`[data-clock="${ap.id}"]`);
    if (!el) continue;
    const left = Math.max(0, (new Date(ap.expires_at).getTime() - Date.now()) / 1000);
    el.textContent = left > 0
      ? `Answer within ${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, "0")} — no answer changes nothing.`
      : "Time is up — nothing was changed.";
  }
}

/* ---------- risks, cooktop guard, energy (FR-2, FR-5) ---------- */

const RISK_TEXT = {
  mold: { title: "Washer mold", short: "washer mold risk",
          what: "Wet laundry sealed in the drum" },
  thermal: { title: "Kitchen heat", short: "kitchen heat",
             what: "Heat from appliances and cooking" },
};

const ACTION_TEXT = {
  ventilate_washer: "Air the washer drum",
  dehumidifier_on: "Dehumidifier on",
  purifier_on: "Air purifier on",
  ventilate_kitchen: "Range hood on",
  precool_kitchen: "Pre-cool the kitchen",
  stop_dryer: "Stop the dryer",
  preheat_cooktop: "Preheat the cooktop",
  cooktop_auto_off: "Cooktop safety shut-off",
};

const AUDIT_TEXT = {
  "risk.triggered": { text: "Risk detected", tone: "warn" },
  "approval.proposed": { text: "Asked you", tone: "ask" },
  "approval.confirmed": { text: "You said yes", tone: "ok" },
  "approval.declined": { text: "You said no", tone: "plain" },
  "approval.expired": { text: "No answer — nothing done", tone: "plain" },
  "action.intent": { text: "About to act", tone: "plain" },
  "action.executed": { text: "Done", tone: "ok" },
  "action.failed": { text: "Device did not respond", tone: "bad" },
  "action.blocked": { text: "Not carried out", tone: "plain" },
  "notification.dryer_restart": { text: "Told you the dryer can restart", tone: "plain" },
  "scenario.reset": { text: "Demo scenario loaded", tone: "plain" },
};

function riskWhy(r) {
  const d = r.drivers || {};
  if (r.kind === "mold") {
    if (d.drum_humidity == null) return "Washer is empty or open.";
    const parts = [`Drum ${Math.round(d.drum_humidity)}% humid`];
    if (d.sealed_hours) parts.push(`shut for ${d.sealed_hours < 1
      ? `${Math.round(d.sealed_hours * 60)} min` : `${d.sealed_hours.toFixed(1)} h`}`);
    return parts.join(", ") + ".";
  }
  if (d.kitchen_temp == null) return "No heat source on or coming up.";
  const parts = [`Kitchen ${d.kitchen_temp.toFixed(1)}°C`];
  if (d.heat_load_w) parts.push(`${(d.heat_load_w / 1000).toFixed(1)} kW of heat`);
  if (d.fridge_duty_cycle != null) parts.push(`fridge working ${Math.round(d.fridge_duty_cycle * 100)}%`);
  if (d.cooking_scheduled_in_min != null) parts.push(`cooking in ${d.cooking_scheduled_in_min} min`);
  return parts.join(", ") + ".";
}

function riskPrediction(r) {
  if (r.predicted == null || r.current == null) return "";
  const unit = r.kind === "mold" ? "%" : "°C";
  const delta = r.predicted - r.current;
  if (Math.abs(delta) < 0.2) return `Steady over the next 30 min.`;
  return `${delta > 0 ? "Rising" : "Falling"} to ${r.predicted}${unit} in 30 min.`;
}

function fmtMin(sec) {
  const m = Math.ceil(sec / 60);
  return m <= 1 ? "under a minute" : `${m} min`;
}

function renderRisks() {
  const box = $("#risks");
  const risks = (pro && pro.risks) || {};
  const quiet = (pro && pro.quiet) || {};
  // While a question about a risk is open, the house *is* asking about it.
  const asking = new Set(((pro && pro.pending_approvals) || []).map((a) => a.risk_kind));
  box.innerHTML = ["mold", "thermal"].map((kind) => {
    const r = risks[kind] || { kind, score: 0, threshold: 50, drivers: {} };
    const t = RISK_TEXT[kind];
    const score = Math.round(r.score || 0);
    const level = r.firing ? "high" : score >= r.threshold * 0.6 ? "watch" : "low";
    const label = { high: "Acting", watch: "Watching", low: "Fine" }[level];
    const q = quiet[kind] > 0 && !asking.has(kind)
      ?`<p class="risk-quiet">Won’t ask about this again for ${fmtMin(quiet[kind])}.</p>` : "";
    return `<article class="risk" data-level="${level}">
      <header class="risk-head">
        <h3>${t.title}</h3>
        <span class="risk-label">${label}</span>
      </header>
      <p class="risk-score"><strong>${score}</strong><span>/100</span></p>
      <div class="risk-bar" role="meter" aria-label="${t.title} risk"
           aria-valuemin="0" aria-valuemax="100" aria-valuenow="${score}">
        <span class="risk-fill" style="width:${Math.min(100, score)}%"></span>
        <span class="risk-line" style="left:${r.threshold}%" title="acts at ${r.threshold}"></span>
      </div>
      <p class="risk-why">${esc(riskWhy(r))}</p>
      <p class="risk-pred">${esc(riskPrediction(r))}</p>
      ${q}
    </article>`;
  }).join("");
}

function renderGuard() {
  const box = $("#guard");
  const g = pro && pro.cooktop_guard;
  if (!g) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  let line;
  if (g.cooking) line = "Cooking in progress.";
  else if (g.meal_in_min != null) line = `Waiting for cooking to start in ${Math.round(g.meal_in_min)} min.`;
  else line = `Switches off in ${fmtMin(g.idle_off_in)} if no cooking starts.`;
  box.innerHTML = `<svg viewBox="0 0 24 24"><use href="#i-cooktop"/></svg>
    <div><strong>The cooktop is on and being watched.</strong>
    <span>${esc(line)} It switches off by itself after ${fmtMin(g.max_off_in)} at most.</span></div>`;
}

const FULL_KITCHEN_W = 2500;   // risk.py: 2500 W counts as a full kitchen

function renderEnergy() {
  const box = $("#energy");
  const w = (pro && pro.heat_load_w) || 0;
  const pct = Math.min(100, Math.round((w / FULL_KITCHEN_W) * 100));
  const level = w >= FULL_KITCHEN_W ? "high" : w >= FULL_KITCHEN_W * 0.6 ? "watch" : "low";
  const app = (pro && pro.appliances) || {};
  const on = [["oven", "oven"], ["cooktop", "cooktop"], ["dryer", "dryer"],
              ["dish_washer", "dishwasher"], ["washer", "washer"]]
    .filter(([k]) => app[k] && app[k].power).map(([, n]) => n);
  box.dataset.level = level;
  box.innerHTML = `<div class="energy-head">
      <span>Heat-producing load</span>
      <strong>${(w / 1000).toFixed(1)} kW</strong>
    </div>
    <div class="energy-bar" role="meter" aria-label="Heat-producing load"
         aria-valuemin="0" aria-valuemax="${FULL_KITCHEN_W}" aria-valuenow="${w}">
      <span style="width:${pct}%"></span>
    </div>
    <p class="energy-note">${on.length ? `Running: ${esc(on.join(", "))}.` : "No heating appliances running."}</p>`;
}

async function loadScenario(name) {
  try {
    await api("/api/scenario", { method: "POST", body: JSON.stringify({ name }) });
    toast("Scenario loaded.");
    askSig = "";
    await refresh();
  } catch (err) {
    toast(`Could not load the scenario: ${err.message}`);
  }
}

function askDevice(ap) {
  const item = (ap.actions && ap.actions[0]) || {};
  const kind = ap.action_kind || item.kind;
  return `${item.device_name || "Appliance"} · ${ACTION_TEXT[kind] || kind || "action"}`;
}

/* ---------- the append-only trail (FR-6) ---------- */

function renderAudit() {
  const box = $("#auditLog");
  if (!box) return;
  const rows = auditRows || [];
  if (!rows.length) {
    box.innerHTML = '<p class="audit-empty">No recorded events yet.</p>';
    return;
  }
  box.innerHTML = rows.slice(0, 8).map((r) => {
    const ev = AUDIT_TEXT[r.event_type] || { text: r.event_type, tone: "plain" };
    const what = ACTION_TEXT[r.action_kind] || r.action_kind || "";
    const meta = [r.approval_id ? `request ${r.approval_id}` : "", r.detail]
      .filter((x) => x && x !== "write-before-act").join(" · ");
    return `<div class="audit-row" data-tone="${ev.tone}">
      <span class="audit-time">${esc(String(r.ts || "").slice(11, 19))}</span>
      <span class="audit-event">${esc(ev.text)}${what ? ` <span class="audit-what">${esc(what)}</span>` : ""}</span>
      <span class="audit-code" title="event type">${esc(r.event_type)}</span>
      ${meta ? `<span class="audit-meta">${esc(meta)}</span>` : ""}
    </div>`;
  }).join("");
}

async function answer(yes, id) {
  if (askBusy || !id) return;
  askBusy = true;
  try {
    const res = await api(
      `/api/approvals/${encodeURIComponent(id)}/${yes ? "confirm" : "decline"}`,
      { method: "POST", body: JSON.stringify({ channel: "ui" }) });
    // The house reports `expired` when the deadline passed while the answer
    // was in flight, and `blocked` with a reason when revalidation refused
    // the action. Saying "Done." in either case would be a lie about devices.
    if (res && res.expired) toast("Too late — nothing was changed.");
    else if (res && res.ok === false) toast(res.reason || res.error || "That request had already closed.");
    else toast(yes ? "Done." : "Nothing changed.");
    await refresh();
    loadMessages().catch(() => {});
  } catch (err) {
    toast(`Could not reach the house: ${err.message}`);
  } finally {
    askBusy = false;
  }
}

/* ---------- rooms ---------- */

const ROOM_ORDER = ["Living Room", "Kitchen", "Bedroom", "Balcony"];

function roomStats(devices) {
  const map = new Map();
  const rooms = (pro && pro.rooms) || null;

  // Room-level readings come from the sensor stream when it is there: it knows
  // all four rooms, while the device list only knows rooms that happen to hold
  // an appliance reporting a temperature. Without this the kitchen showed "—%"
  // and the balcony "—°" — the data existed, the card just never asked for it.
  if (rooms) {
    for (const name of ROOM_ORDER) {
      const r = rooms[name] || {};
      map.set(name, {
        name,
        temps: r.temp == null ? [] : [r.temp],
        hums: r.humidity == null ? [] : [r.humidity],
        running: 0,
        total: 0,
      });
    }
  }

  for (const d of devices) {
    const room = d.room || "Other";
    if (!map.has(room)) map.set(room, { name: room, temps: [], hums: [], running: 0, total: 0 });
    const s = map.get(room);
    const st = d.state || {};
    s.total += 1;
    if (st.power) s.running += 1;
    // Only fall back to the device's own sensor when the room stream is absent.
    if (!s.temps.length && st.current_temp != null) s.temps.push(st.current_temp);
    if (!s.temps.length && st.external_temp != null) s.temps.push(st.external_temp);
    if (!s.hums.length && st.humidity != null) s.hums.push(st.humidity);
  }
  return [...map.values()];
}

function renderRooms() {
  const stats = roomStats((snapshot && snapshot.devices) || []);
  $("#rooms").innerHTML = stats.map((s) => {
    const t = s.temps.length
      ? (s.temps.reduce((a, b) => a + b, 0) / s.temps.length).toFixed(1)
      : "—";
    const h = s.hums.length
      ? Math.round(s.hums.reduce((a, b) => a + b, 0) / s.hums.length)
      : "—";
    return `<button class="room${activeRoom === s.name ? " active" : ""}" data-room="${esc(s.name)}">
      <h3>${esc(s.name)}</h3>
      <span class="temp">${t}°</span>
      <span class="meta">
        <svg viewBox="0 0 24 24"><use href="#i-drop"/></svg>${h}%
        <span>·</span>${s.running}/${s.total} on
      </span>
    </button>`;
  }).join("");
}

/* ---------- devices ---------- */

function cardSub(d) {
  const st = d.state || {};
  const on = !!st.power;
  switch (d.type) {
    case "air_conditioner":
      return on
        ? [MODE_LABEL[st.mode] || st.mode || "On",
           st.target_temp != null ? `${st.target_temp}°C` : ""].filter(Boolean).join(" ")
        : "Off";
    case "air_purifier":
      return on ? `PM2.5 ${st.pm25 == null ? "—" : st.pm25}` : "Off";
    case "washer":
      return on ? `Washing · ${st.remaining_min == null ? "?" : st.remaining_min} min`
                : (st.state === "done" ? "Cycle finished" : "Standby");
    case "refrigerator":
      return `Fridge ${st.fridge_temp == null ? "—" : st.fridge_temp}°C · ` +
             `Kitchen ${st.external_temp == null ? "—" : st.external_temp + "°C"} · ` +
             `compressor ${st.duty_cycle == null ? "—" : Math.round(st.duty_cycle * 100) + "%"}`;
    case "range_hood":
      return on ? `Exhausting · fan ${st.fan || "auto"}` : "Off";
    case "oven":
      return on ? `Heating · ${st.target_temp == null ? "—" : st.target_temp + "°C"}` : "Off";
    default:
      return on ? "On" : "Off";
  }
}

function renderDevices() {
  const all = (snapshot && snapshot.devices) || [];
  const list = activeRoom === "All" ? all : all.filter((d) => (d.room || "") === activeRoom);
  $("#deviceHead").textContent = activeRoom === "All" ? "Devices" : `Devices · ${activeRoom}`;
  const box = $("#devices");
  if (!list.length) {
    box.innerHTML = `<p class="empty">Nothing in this room.</p>`;
    return;
  }
  box.innerHTML = list.map((d) => {
    const st = d.state || {};
    const on = !!st.power;
    const icon = ICON[d.type] || "i-ac";
    return `<article class="device${on ? " on" : ""}${d.online === false ? " offline" : ""}"
                     data-open="${esc(d.id)}">
      <svg class="dev-icon" viewBox="0 0 24 24"><use href="#${icon}"/></svg>
      <div class="dev-text">
        <h3>${esc(d.name)}</h3>
        <p>${esc(cardSub(d))}</p>
      </div>
      <button class="pwr" data-power="${esc(d.id)}" data-on="${on ? 1 : 0}"
              aria-label="${on ? "Turn off" : "Turn on"} ${esc(d.name)}">
        <svg viewBox="0 0 24 24"><use href="#i-power"/></svg>
      </button>
    </article>`;
  }).join("");
}

/* ---------- quick actions ---------- */

const SCENES = [
  { key: "off", icon: "i-power", label: "All off",
    say: (room) => room === "All" ? "turn off everything in the living room"
                                  : `turn off everything in the ${room}` },
  { key: "cool", icon: "i-temp", label: "Cool 24°",
    say: (room) => room === "All"
      ? "turn on the living room AC and set it to 24 degrees"
      : `turn on the ${room} AC and set it to 24 degrees` },
  { key: "stuffy", icon: "i-drop", label: "It's stuffy",
    // Name the room. Without it the agent answers "Which room?" — a question
    // the button gives no way to answer, which is a dead end, not a dialog.
    say: (room) => room === "All" ? "it's stuffy in the living room"
                                  : `it's stuffy in the ${room}` },
];

function renderQuick() {
  $("#quick").innerHTML = SCENES.map((s) =>
    `<button data-scene="${s.key}">
      <svg viewBox="0 0 24 24"><use href="#${s.icon}"/></svg>
      <span>${esc(s.label)}</span>
    </button>`).join("");
}

/* ---------- schedule ---------- */

function renderJobs() {
  const jobs = (snapshot && snapshot.jobs) || [];
  const box = $("#jobs");
  if (!jobs.length) {
    box.innerHTML = `<p class="empty">Nothing scheduled.<br>Ask the assistant to set something.</p>`;
    return;
  }
  box.innerHTML = jobs.map((j) => `
    <div class="job${j.enabled ? "" : " disabled"}">
      <div class="job-main">
        <strong>${esc(cap(j.summary || j.device_name || "Scheduled task"))}</strong>
        <span>${esc(repeatShort(j.repeat_label) || "once")} ·
          ${j.enabled ? "next " + esc(fmtWhen(j.next_run_at)) : "disabled"}</span>
      </div>
      <button class="del" data-del="${esc(j.id)}" aria-label="Delete task">×</button>
    </div>`).join("");
}

/* ---------- chat ---------- */

function liveBuild() {
  return (snapshot && snapshot.build) || "";
}

/* ---------- chat scrolling ----------
   The transcript is its own scroll container now (see .chat in style.css).
   Nothing here touches the document: on a phone the browser scrolls the
   nearest scrollable ancestor to reveal a focused field, and the field lives
   in the talk bar, so scrolling the page was what made the transcript jump. */
function chatBox() { return $("#chat"); }

function nearBottom(el, slack = 60) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= slack;
}

function scrollChatToEnd(force = false) {
  const el = chatBox();
  if (!el) return;
  // Never yank someone who scrolled up to re-read a message.
  if (!force && !nearBottom(el)) return;
  el.scrollTop = el.scrollHeight;
}

function addMsg(role, text, message, opts = {}) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.textContent = text;
  const tag = tagFor(message);
  if (tag) {
    const stamp = document.createElement("span");
    stamp.className = "stamp";
    stamp.textContent = tag;
    el.appendChild(stamp);
  }
  const trace = (message && message.payload && message.payload.trace) || [];
  if (trace.length) {
    const det = document.createElement("details");
    det.className = "trace";
    const sum = document.createElement("summary");
    const calls = trace.filter((s) => s.tool !== "offer").length;
    sum.textContent = (message.payload && message.payload.adopted)
      ? `took the rules' reading · ${calls} ${calls === 1 ? "step" : "steps"}`
      : `declined the reading · ${calls} ${calls === 1 ? "step" : "steps"}`;
    det.appendChild(sum);
    const ol = document.createElement("ol");
    trace.forEach((s) => {
      const li = document.createElement("li");
      li.textContent = s.result || s.tool;
      if (s.tool === "offer") li.className = "offer";
      ol.appendChild(li);
    });
    det.appendChild(ol);
    el.appendChild(det);
  }
  chatBox().appendChild(el);
  scrollChatToEnd(opts.force);

  // Read replies aloud when asked to — and always read a Tier-2 question,
  // because that is the one message that must not be missed. A transcript
  // rebuild is not a new message and must stay silent.
  const approval = message && message.payload && message.payload.approval;
  if (!opts.silent && (ttsOn || approval)) speak(text);
  return el;
}

function tagFor(msg) {
  if (!msg) return null;
  const rows = [];
  const when = msg.ts || msg.created_at;
  if (when) {
    const t = new Date(when);
    rows.push(isNaN(t) ? String(when).slice(11, 16) : t.toTimeString().slice(0, 5));
  }
  // A reply written by a build that no longer runs is a historical answer;
  // marking it is the difference between reading a transcript and trusting it.
  // Only a message that carries a build can be called stale: an optimistic user
  // bubble has no payload, and stamping it "older build" a second after it was
  // typed is a false alarm — the one thing a diagnostic must never do.
  const b = (msg.payload || {}).build;
  if (b && liveBuild() && b !== liveBuild()) rows.push(`old build ${b}`);
  return rows.length ? rows.join(" · ") : null;
}

function renderChips() {
  const devices = (snapshot && snapshot.devices) || [];
  const primary = devices.find((d) => d.type === "air_conditioner") || devices[0];
  if (!primary) return;
  const name = primary.name;
  const room = primary.room || "";
  const on = !!(primary.state || {}).power;

  const chips = [
    [`Turn ${on ? "off" : "on"} the ${name}`, on ? "Turn off" : "Turn on"],
    [`Set the ${name} to 24 degrees`, "24°C"],
    ["It's stuffy in here", "It's stuffy"],
    ["What scheduled tasks do I have?", "Schedules"],
    ["거실 에어컨 꺼줘", "한국어 예시"],
  ];
  if (room) chips.push([`Turn off everything in the ${room}`, "Whole room"]);

  const sig = chips.map((c) => c[0]).join("|");
  if (sig === chipsSig) return;
  chipsSig = sig;
  $("#chips").innerHTML = chips
    .map(([say, label]) => `<button data-say="${esc(say)}">${esc(label)}</button>`)
    .join("");
}

async function send(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  await switchTab("assistant");
  addMsg("user", trimmed, { ts: new Date().toISOString() }, { force: true });
  $("#input").value = "";
  // The agent tier takes 4–8 s on a real model. Silence for that long reads as
  // a dead app, so say something immediately and replace it when the answer
  // lands. Built by hand rather than through addMsg: a placeholder should not
  // be read aloud.
  const pending = document.createElement("div");
  pending.className = "msg assistant pending";
  pending.textContent = "Working on it…";
  chatBox().appendChild(pending);
  scrollChatToEnd(true);
  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ text: trimmed }),
    });
    pending.remove();
    addMsg("assistant", data.reply || "…", { ts: new Date().toISOString(), payload: data });
    showReply(data.reply || "");
    await refresh();
    return data.reply || "";
  } catch (err) {
    pending.remove();
    addMsg("system", `Request failed: ${err.message}`);
    return "";
  }
}

let chatSig = "";

async function loadMessages(force = false) {
  const data = await api("/api/messages");
  const list = data.messages || [];
  // Rebuilding the transcript for an unchanged list is not free: it resets the
  // scroll position, and with read-aloud on it re-reads every message. Compare
  // a signature first and leave the DOM alone when nothing has arrived.
  const sig = list
    .map((m, i) => `${i}:${m.role}:${(m.content || "").length}:${(m.content || "").slice(0, 24)}`)
    .join("|");
  if (!force && sig === chatSig && chatBox().children.length) return;
  chatSig = sig;
  const box = chatBox();
  box.innerHTML = "";
  for (const m of list) addMsg(m.role, m.content, m, { silent: true });
  if (!list.length) {
    addMsg("system", "Say something, or tap a suggestion below.", null, { silent: true });
  }
  requestAnimationFrame(() => scrollChatToEnd(true));
}

/* ---------- decision mode ---------- */

async function setMode(mode) {
  try {
    const data = await api("/api/mode", { method: "POST", body: JSON.stringify({ mode }) });
    if (data.snapshot) {
      snapshot = data.snapshot;
      renderAll();
    }
  } catch (err) {
    toast(`Failed: ${err.message}`);
  }
}

function renderMode() {
  const mode = (snapshot && snapshot.mode) || "cascade";
  document.querySelectorAll("[data-agent-mode]").forEach((b) => {
    const on = b.dataset.agentMode === mode;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  });
}

/* ---------- toast ---------- */

let toastTimer = null;

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

/* ---------- the house's last answer ---------- */

/* Kept on screen until the next one. The old path showed a reply as a toast for
   2.6 s and cut it to its first line — the answer to "it's stuffy" arrived,
   was shown in 15 px type, and was gone before anyone could read it. */
function showReply(text) {
  const clean = String(text || "").trim();
  if (!clean) return;
  $("#replyText").textContent = clean;
  $("#reply").hidden = false;
  $("#reply").classList.remove("pending");
  speak(clean);
}

/* Something on screen the moment the button is pressed. A 5-second wait with
   no visible change is indistinguishable from a broken app. */
function showPending(label) {
  $("#replyText").textContent = label || "Working on it…";
  $("#reply").hidden = false;
  $("#reply").classList.add("pending");
}

async function runScene(scene) {
  const phrase = scene.say(activeRoom);
  showPending();
  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ text: phrase }),
    });
    if (data.snapshot) {
      snapshot = data.snapshot;
      renderAll();
    }
    showReply(data.reply || "Done.");
  } catch (err) {
    showReply("Sorry — I could not reach the house just now.");
    toast(`Could not reach the house: ${err.message}`);
  }
}

/* ---------- device sheet ---------- */

function renderSheet() {
  const device = findDevice(sheetId);
  if (!device) return closeSheet();

  const st = device.state || {};
  const on = !!st.power;
  const caps = device.capabilities || [];
  const sub = [device.room || device.type_label, cap(device.brief)].filter(Boolean).join(" · ");

  let body = `<h2>${esc(device.name)}</h2><p class="sub">${esc(sub)}</p>`;

  if (caps.includes("temperature")) {
    const target = st.target_temp == null ? 26 : st.target_temp;
    body += `<div class="stepper">
      <button data-step="-1" ${on ? "" : "disabled"}>−</button>
      <span class="target">${target}°C</span>
      <button data-step="1" ${on ? "" : "disabled"}>+</button>
    </div>`;
  }
  if (caps.includes("mode")) {
    body += `<div class="pills">${MODES.map((mode) =>
      `<button data-mode="${mode}" class="${st.mode === mode ? "active" : ""}">${MODE_LABEL[mode]}</button>`
    ).join("")}</div>`;
  }
  body += `<button class="sheet-power${on ? " on" : ""}" data-sheet-power="${on ? 0 : 1}">
    ${on ? "Turn off" : "Turn on"}</button>`;

  $("#sheetBody").innerHTML = body;
}

function openSheet(id) {
  if (!findDevice(id)) return;
  sheetId = id;
  renderSheet();
  $("#scrim").hidden = false;
  $("#sheet").hidden = false;
}

function closeSheet() {
  sheetId = null;
  $("#scrim").hidden = true;
  $("#sheet").hidden = true;
}

async function control(deviceId, actions) {
  try {
    const data = await api("/api/control", {
      method: "POST",
      body: JSON.stringify({ device_id: deviceId, actions }),
    });
    if (data.snapshot) {
      snapshot = data.snapshot;
      renderAll();
    }
  } catch (err) {
    addMsg("system", `Control failed: ${err.message}`);
  }
}

/* ---------- render ---------- */

function renderAll() {
  if (!snapshot) return;
  $("#stamp").textContent =
    `${BACKEND_LABEL[snapshot.backend] || snapshot.backend || ""} · ${snapshot.build || ""}`;
  renderStatus();
  renderAsk();
  renderRisks();
  renderGuard();
  renderEnergy();
  renderAudit();
  renderRooms();
  renderDevices();
  if (!$("#quick").children.length) renderQuick();
  renderJobs();
  renderChips();
  renderMode();
  if (sheetId) renderSheet();
}

async function refresh() {
  try {
    const [snap, state, trail] = await Promise.all([
      api("/api/snapshot"),
      api("/api/proactive").catch(() => null),
      api("/api/audit?limit=12").catch(() => null),
    ]);
    snapshot = snap;
    pro = state;
    auditRows = trail && trail.audit;
    renderAll();
  } catch (err) {
    /* keep the last good snapshot rather than blanking the screen */
  }
}

/* ---------- navigation ---------- */

const TABS = ["home", "schedule", "assistant"];

async function switchTab(name) {
  if (!TABS.includes(name)) name = "home";
  const changed = name !== currentTab;
  currentTab = name;
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === name);
  });
  for (const key of TABS) {
    const view = $(`#view-${key}`);
    if (view) view.hidden = key !== name;
  }
  if (history.replaceState && location.hash !== `#${name}`) {
    history.replaceState(null, "", `#${name}`);
  }
  // Only when the tab actually changes. send() calls this on every message,
  // and rebuilding the whole transcript each time reset the scroll position
  // and re-read the history aloud. Awaited by send(), so the optimistic
  // bubbles it appends next cannot be wiped by this rebuild.
  if (name === "assistant" && changed) {
    await loadMessages().catch(() => {});
  }
}

/* ---------- events ---------- */

document.addEventListener("click", (event) => {
  const target = event.target;

  const scenarioBtn = target.closest("[data-scenario]");
  if (scenarioBtn) {
    loadScenario(scenarioBtn.dataset.scenario);
    return;
  }

  const answerBtn = target.closest("[data-answer]");
  if (answerBtn) {
    answer(answerBtn.dataset.answer === "yes", answerBtn.dataset.id);
    return;
  }

  const roomBtn = target.closest("[data-room]");
  if (roomBtn) {
    const name = roomBtn.dataset.room;
    activeRoom = activeRoom === name ? "All" : name;
    renderRooms();
    renderDevices();
    return;
  }

  const powerBtn = target.closest("[data-power]");
  if (powerBtn) {
    const on = powerBtn.dataset.on === "1";
    control(powerBtn.dataset.power, [{ action: "power", value: !on }]);
    return;
  }

  const stepBtn = target.closest("[data-step]");
  if (stepBtn && sheetId) {
    const device = findDevice(sheetId);
    const current = (device.state || {}).target_temp;
    const next = (current == null ? 26 : current) + Number(stepBtn.dataset.step);
    control(sheetId, [{ action: "temperature", value: Math.max(16, Math.min(30, next)) }]);
    return;
  }

  const modeBtn = target.closest("[data-mode]");
  if (modeBtn && sheetId) {
    control(sheetId, [{ action: "mode", value: modeBtn.dataset.mode }]);
    return;
  }

  const sheetPower = target.closest("[data-sheet-power]");
  if (sheetPower && sheetId) {
    control(sheetId, [{ action: "power", value: sheetPower.dataset.sheetPower === "1" }]);
    return;
  }

  const agentModeBtn = target.closest("[data-agent-mode]");
  if (agentModeBtn) {
    setMode(agentModeBtn.dataset.agentMode);
    return;
  }

  const sceneBtn = target.closest("[data-scene]");
  if (sceneBtn) {
    const scene = SCENES.find((s) => s.key === sceneBtn.dataset.scene);
    if (scene) runScene(scene);
    return;
  }

  const card = target.closest("[data-open]");
  if (card) {
    openSheet(card.dataset.open);
    return;
  }

  const tab = target.closest("[data-tab]");
  if (tab) {
    switchTab(tab.dataset.tab);
    return;
  }

  const del = target.closest("[data-del]");
  if (del) {
    api(`/api/jobs/${del.dataset.del}`, { method: "DELETE" })
      .then((data) => {
        if (data.snapshot) {
          snapshot = data.snapshot;
          renderAll();
        }
      })
      .catch(() => {});
    return;
  }

  const say = target.closest("[data-say]");
  if (say) {
    send(say.dataset.say);
    return;
  }

  if (target.id === "scrim") closeSheet();
});

$("#composer").addEventListener("submit", (event) => {
  event.preventDefault();
  send($("#input").value);
});

$("#mic").addEventListener("click", startVoice);

$("#lang").addEventListener("click", () => {
  voiceLang = VOICE_LANGS[(VOICE_LANGS.indexOf(voiceLang) + 1) % VOICE_LANGS.length];
  renderLangButton();
  toast(voiceLang.startsWith("ko") ? "한국어 음성 인식" : "English voice input");
});

$("#tts").addEventListener("click", () => {
  ttsOn = !ttsOn;
  const btn = $("#tts");
  btn.classList.toggle("active", ttsOn);
  btn.setAttribute("aria-pressed", String(ttsOn));
  if (!ttsOn && "speechSynthesis" in window) window.speechSynthesis.cancel();
  toast(ttsOn ? "Reading replies aloud" : "Reading aloud off");
});

/* ---------- boot ---------- */

refresh();
renderLangButton();
setInterval(refresh, 4000);
setInterval(renderAsk, 1000);   // the clock is the point: it has to move

const startTab = (location.hash || "").replace("#", "");
switchTab(TABS.includes(startTab) ? startTab : "home");

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
