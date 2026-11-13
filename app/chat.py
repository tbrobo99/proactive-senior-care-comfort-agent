"""Plain-text conversation for messages that are not home commands.

The assistant has two doors, and only one of them reaches an appliance:

    a home command  -> rule engine / safety approval / audited execution
    anything else   -> here, one short text reply, actions = []

This module is deliberately unable to act. It imports the HTTP chat helper and
nothing else from the house: no adapter, no audit log, no approval queue, no
tool schemas, no scheduler. It returns a **string**, never an intent dict, so
there is no code path from a model's words in this file to a device write. The
model is asked for prose with no tools attached; what comes back is cleaned and
used as text, and a reply that claims an action already happened is thrown away.

Offline is a first-class case, not an error: the demo often runs with the LLM
endpoint pointed at a dead port. `local_reply` answers every conversational
kind in plain English with no network at all, so "hello" still sounds like an
assistant when the model is off, unreachable, slow or nonsense.
"""
from __future__ import annotations

import re

from . import llm

# ---------------------------------------------------------------- which kind
#
# A white list, not a black list. "good morning", "good night" and "I'm
# leaving" are *scenes* that switch real appliances on and off; they must stay
# commands. "make it nicer in there" and "the whole house is too warm" are
# vague home requests the tool loop is meant to resolve. None of them matches
# anything below, and none is swept up by the generic small-talk rule either.

_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("greeting", re.compile(
        r"^(?:hi|hello|hey|hiya|howdy|yo|greetings|good\s+(?:afternoon|evening))"
        r"(?:\s+there)?\b[\s!.?~]*$", re.I)),
    ("thanks", re.compile(
        r"^(?:thanks|thank\s+you|thx|ty|cheers|many\s+thanks|thanks\s+a\s+lot)"
        r"(?:\s+(?:so\s+)?much)?\b[\s!.?~]*$", re.I)),
    ("farewell", re.compile(
        r"^(?:bye|goodbye|bye\s+bye|see\s+you|see\s+ya|later|take\s+care|ciao)"
        r"(?:\s+(?:soon|later|again|all))?\b[\s!.?~]*$", re.I)),
    ("capabilities", re.compile(
        r"^(?:what\s+can\s+you\s+do|what\s+do\s+you\s+do|who\s+are\s+you|"
        r"what\s+are\s+you|what\s+can\s+you\s+help(?:\s+me)?\s+with|"
        r"what\s+are\s+your\s+(?:features|capabilities|skills|functions)|"
        r"how\s+can\s+you\s+help(?:\s+me)?|help(?:\s+me)?|what\s+is\s+this|"
        r"tell\s+me\s+about\s+yourself|what\s+can\s+i\s+(?:ask|say)|"
        r"show\s+me\s+what\s+you\s+can\s+do)\b[\s?.!]*$", re.I)),
    ("smalltalk", re.compile(
        r"^(?:how\s+are\s+you|how'?s\s+it\s+going|what'?s\s+up|"
        r"how\s+do\s+you\s+do|nice\s+to\s+meet\s+you|pleased\s+to\s+meet\s+you|"
        r"i'?m\s+bored|tell\s+me\s+a\s+joke|make\s+me\s+laugh|"
        r"are\s+you\s+(?:there|ok|okay|real|a\s+robot|a\s+human)|"
        r"do\s+you\s+(?:sleep|dream|eat)|what'?s\s+your\s+name|"
        r"who\s+made\s+you|say\s+something\s+nice)\b[\s?.!]*$", re.I)),
)

# scenes.py reads these as routines that switch real appliances on and off.
# They sound exactly like greetings, which is why they cannot be left to a
# white list of "conversational" phrases: "good morning" turns the house on.
# The rule engine sees them first anyway; this is the second lock.
_SCENE_TALK = re.compile(
    r"\bgood\s+morning\b|\bwake\b[^.?!]{0,24}?\bup\b"
    r"|\bgood\s*night\b|\bbed\s*time\b"
    r"|\b(?:i'?m|i\s+am)\s+(?:leaving|going\s+out|heading\s+out|off\s+out)\b"
    r"|\bleaving\s+now\b|\bgoing\s+out\b|\bheading\s+out\b|\baway\s+mode\b",
    re.I,
)

# Words that make a sentence about the house, even when the parser could not
# read it as a command. A miss here is expensive: sending "hi, living room AC
# 25" or "the whole house is too warm" to a chat model would turn a perfectly
# good command into small talk. So the generic small-talk fallback is narrow.
_HOME_SIGNAL = re.compile(
    r"\b(?:turn|switch|shut|set|start|stop|power|put|open|close|wake|"
    r"activate|deactivate|enable|disable|cancel|clear|list|show|schedule|"
    r"on|off|up|down|cool|cooling|heat|heating|warm|cold|hot|stuffy|humid|"
    r"clammy|muggy|noisy|loud|smell|stink|odou?r|"
    r"air|water|temp|temperature|degrees?|celsius|fahrenheit|fan|mode|speed|"
    r"status|state|running|working|level|"
    r"ac|a/c|acs|aircon|purifier|fridge|refrigerator|washer|dryer|oven|"
    r"cooktop|hood|dehumidifier|dishwasher|heater|styler|vacuum|robot|"
    r"room|rooms|house|home|flat|place|kitchen|bedroom|balcony|living|"
    r"device|devices|appliance|appliances|everything|all|both|whole|entire|"
    r"timer|timers|task|tasks|job|jobs|reminder|reminders|routine|scene|"
    r"it|there|here|this|that|them|"
    r"weather|forecast|rain|snow|news|stock|music|song|movie|recipe|traffic)\b",
    re.I,
)


def classify(text: str) -> str | None:
    """A conversational kind, or None when this is not a chat message."""
    t = (text or "").strip()
    if not t:
        return None
    for kind, rx in _PATTERNS:
        if rx.match(t):
            return kind
    return None


def is_conversation(text: str) -> bool:
    """Should this message get a text-only reply instead of the command path?

    Only ever called after the rule engine has already said `unknown`. Even
    then, a sentence carrying any home/control word stays on the command path
    so the existing tier 2 / tool loop can still try to understand it. What is
    left is a greeting, a thank-you, a short bit of small talk, or a bare
    unrecognisable phrase — the four things the user asked the assistant to
    answer like an assistant.
    """
    t = (text or "").strip()
    if not t:
        return False
    if _SCENE_TALK.search(t):
        return False
    if classify(t):
        return True
    if _HOME_SIGNAL.search(t):
        return False
    words = re.findall(r"[A-Za-z']+", t)
    if not words or len(words) > 6:
        return False
    return bool(re.fullmatch(r"[A-Za-z'\s,.!?~:-]+", t))


# ---------------------------------------------------------------- local text
#
# Short, plain English, no invented readings or device states. The capability
# list names exactly what this build implements: device control, status reads,
# schedules, and the risk/approval safeguards from Phase 2.

LOCAL_REPLIES: dict[str, str] = {
    "greeting": (
        "Hi! I'm your home assistant. I can switch appliances on and off, set "
        "the temperature, check their status, and set schedules. What would you "
        "like to do?"
    ),
    "capabilities": (
        "I can do four things: control the appliances (on/off, temperature, "
        "mode, fan speed), read what a device is doing, set schedules and "
        "timers, and watch for safety risks — a risky action waits for your "
        "approval before anything is changed. Try \"set the living room AC to "
        "24 degrees\"."
    ),
    "thanks": (
        "You're welcome! Is there anything else you'd like me to do with the "
        "house?"
    ),
    "farewell": (
        "Goodbye! I'll keep watching the home. Just say hello when you're back."
    ),
    "smalltalk": (
        "I'm here and ready. I mostly help with the home — appliances, their "
        "status, and schedules. What can I do for you?"
    ),
    "unknown": (
        "I'm your home assistant. I didn't catch a device or an action there — "
        "tell me what you'd like to change, or ask what I can do."
    ),
}


def local_reply(kind: str) -> str:
    return LOCAL_REPLIES.get(kind) or LOCAL_REPLIES["unknown"]


# ---------------------------------------------------------------- model text

SYSTEM_PROMPT = """You are the conversation layer of a smart-home assistant.
Reply with plain text only, in simple English.

HARD RULES:
1. You CANNOT control devices. Never say you turned something on or off,
   changed a temperature, started a cycle, or set a schedule. Nothing you
   write is executed, and claiming otherwise would be a lie to the user.
2. Never invent device states, sensor readings, weather, news, or anything
   about the outside world. You only know what this assistant can help with.
3. One or two short sentences. For small talk, answer briefly, then gently
   steer back to the home. No JSON, no code, no bullet lists, no markdown,
   no tool calls.

What this assistant can do: switch appliances on and off, set temperature,
mode and fan speed, read device status, set schedules and timers, and watch
for safety risks (a risky action waits for the user's approval). It works
only with the appliances in this house."""

# A model that ignores rule 1 must not be believed. Drop the whole reply and
# use the local one rather than trying to edit a sentence in place.
_ACTION_CLAIM = re.compile(
    r"\b(?:i\s+(?:have\s+|'ve\s+|just\s+)?(?:turned|switched|set|started|"
    r"stopped|scheduled|cancelled|cleared|opened|closed|changed|adjusted|"
    r"activated|deactivated)\b"
    r"|\bdone\s*[—-]"
    r"|\bis\s+now\s+(?:on|off|set|cooling|heating|running)\b"
    r"|\b(?:power|temperature|mode|fan)\s*[:=]\s*)",
    re.I,
)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _clean(raw: str) -> str:
    """Keep prose, throw away anything that is not prose."""
    s = (raw or "").strip()
    if not s or s[0] in "{[":
        return ""
    s = _FENCE.sub("", s).strip()
    s = re.sub(r"\s+", " ", s)
    while len(s) >= 2 and s[0] in "\"'\u201c" and s[-1] in "\"'\u201d":
        s = s[1:-1].strip()
    if _ACTION_CLAIM.search(s):
        return ""
    if len(s) > 320:
        head = s[:320]
        stop = max(head.rfind(". "), head.rfind("? "), head.rfind("! "))
        s = (head[:stop + 1] if stop > 80 else head.rstrip() + "...")
    return s.strip()


def reply(cfg: dict, text: str, kind: str | None = None) -> str:
    """One short text answer. Never an intent, never an action, never a throw.

    Any failure — model disabled, offline, timeout, bad payload — returns the
    local wording instead, so the assistant always answers a greeting.
    """
    kind = kind or classify(text) or "unknown"
    local = local_reply(kind)
    opts = cfg.get("chat") or {}
    if opts.get("enabled", True) is False or not llm.available(cfg):
        return local
    try:
        message = llm.chat(
            cfg,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            json_mode=False,
            temperature=float(opts.get("temperature", 0.6)),
            max_tokens=int(opts.get("max_tokens", 200)),
            timeout=float(opts.get("timeout", 8)),
        )
        cleaned = _clean(message.get("content") or "")
        return cleaned or local
    except Exception:
        return local
