"""Natural-language understanding (rule engine).

Why rules before an LLM:

1. Zero dependencies, zero cost, works offline — the demo runs on any laptop.
2. Time parsing has to be exactly right. Misreading "half past ten" turns the
   AC on at the wrong hour, and that is not a failure mode you want a
   probabilistic model to own.
3. It is deterministic and testable, so the LLM only has to handle the long
   tail ("it feels stuffy in here") instead of the common cases.

Everything returns the same intent dict, which agent.py executes.
"""
from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta

from . import food, scenes

# ---------------------------------------------------------------- numbers

WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "twenty": 20,
}
NUMW = r"(?:\d{1,2}|" + "|".join(WORD_NUM) + r")"


def to_num(s: str) -> int | None:
    s = (s or "").strip().lower()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in ("a", "an"):
        return 1
    return WORD_NUM.get(s)


# ---------------------------------------------------------------- time

COLON_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?", re.I)
AT_HOUR_RE = re.compile(
    r"\bat\s+(" + NUMW + r")(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|o'?clock)?", re.I
)
MERIDIEM_RE = re.compile(r"\b(" + NUMW + r")(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)", re.I)
HALF_PAST_RE = re.compile(r"\bhalf\s+past\s+(" + NUMW + r")", re.I)
QUARTER_PAST_RE = re.compile(r"\b(?:a\s+)?quarter\s+past\s+(" + NUMW + r")", re.I)
QUARTER_TO_RE = re.compile(r"\b(?:a\s+)?quarter\s+to\s+(" + NUMW + r")", re.I)
IN_HALF_RE = re.compile(r"\bin\s+(?:half\s+an?|a\s+half)\s*(?:hour|hr)\b", re.I)
IN_N_RE = re.compile(
    r"\bin\s+(" + NUMW + r"|an?)\s*(hours?|hrs?|minutes?|mins?)", re.I
)

DAY_WORDS = [
    ("monday", 0), ("mon", 0), ("tuesday", 1), ("tues", 1), ("tue", 1),
    ("wednesday", 2), ("weds", 2), ("wed", 2), ("thursday", 3), ("thurs", 3),
    ("thur", 3), ("thu", 3), ("friday", 4), ("fri", 4), ("saturday", 5),
    ("sat", 5), ("sunday", 6), ("sun", 6),
]
DAY_MAP = dict(DAY_WORDS)
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_RE = re.compile(r"\b(" + "|".join(w for w, _ in DAY_WORDS) + r")\b", re.I)


def find_days(text: str) -> list[int]:
    days: list[int] = []
    for m in DAY_RE.finditer(text):
        d = DAY_MAP.get(m.group(1).lower())
        if d is not None and d not in days:
            days.append(d)
    return sorted(days)


def _period_word(text: str) -> str | None:
    t = text.lower()
    if re.search(r"\b(morning|sunrise)\b", t):
        return "am"
    if re.search(r"\b(afternoon|evening|tonight|tonite|night)\b", t):
        return "pm"
    if re.search(r"\bnoon\b", t):
        return "noon"
    if re.search(r"\bmidnight\b", t):
        return "midnight"
    return None


def _apply_meridiem(hour: int, meridiem: str | None, now: datetime) -> int:
    if meridiem == "noon":
        return 12
    if meridiem == "midnight":
        return 0
    if meridiem in ("pm", "p.m."):
        return (hour % 12) + 12 if hour < 12 else hour
    if meridiem in ("am", "a.m."):
        return 0 if hour == 12 else hour
    if hour == 12:
        return 12
    # No explicit am/pm: if it is already afternoon, assume the coming pm hour.
    # "at 10" said at 18:42 means 22:00, not 10:00 tomorrow.
    if now.hour >= 12 and hour <= 11:
        return hour + 12
    return hour


def extract_time(text: str, now: datetime) -> dict | None:
    """Return {'kind':'relative','seconds':n} or {'kind':'clock','hour','minute'}."""
    if IN_HALF_RE.search(text):
        return {"kind": "relative", "seconds": 1800}

    m = IN_N_RE.search(text)
    if m:
        n = to_num(m.group(1))
        if n:
            unit = m.group(2).lower()
            return {"kind": "relative",
                    "seconds": n * (3600 if unit.startswith(("hour", "hr")) else 60)}

    m = HALF_PAST_RE.search(text)
    if m:
        h = to_num(m.group(1))
        if h is not None:
            return {"kind": "clock", "hour": h, "minute": 30, "explicit": None}

    m = QUARTER_PAST_RE.search(text)
    if m:
        h = to_num(m.group(1))
        if h is not None:
            return {"kind": "clock", "hour": h, "minute": 15, "explicit": None}

    m = QUARTER_TO_RE.search(text)
    if m:
        h = to_num(m.group(1))
        if h is not None:
            return {"kind": "clock", "hour": (h - 1) % 24, "minute": 45, "explicit": None}

    for rx in (COLON_RE, AT_HOUR_RE, MERIDIEM_RE):
        m = rx.search(text)
        if m:
            h = int(m.group(1)) if m.group(1).isdigit() else to_num(m.group(1))
            if h is None:
                continue
            minute = int(m.group(2)) if m.group(2) else 0
            return {"kind": "clock", "hour": h % 24, "minute": minute,
                    "explicit": m.group(3)}
    return None


def detect_repeat(text: str) -> dict | None:
    """Detect recurrence."""
    m = re.search(r"\bevery\s+(" + NUMW + r"|few)\s*(hours?|hrs?|minutes?|mins?)",
                  text, re.I)
    if m:
        n = to_num(m.group(1)) or 1
        unit = m.group(2).lower()
        return {"type": "interval",
                "interval_seconds": n * (3600 if unit.startswith(("hour", "hr")) else 60)}

    if re.search(r"\bevery\s+hour\b|\bhourly\b", text, re.I):
        return {"type": "interval", "interval_seconds": 3600}

    if re.search(r"\b(every\s+day|daily|each\s+day|every\s+night)\b", text, re.I):
        return {"type": "daily"}

    if re.search(r"\b(every\s+)?weekdays?\b|\bworkdays?\b", text, re.I):
        return {"type": "weekday"}

    if re.search(r"\bevery\s+weekend\b|\bon\s+weekends?\b", text, re.I):
        return {"type": "weekly", "weekdays": [5, 6]}

    days = find_days(text)
    if days and re.search(r"\b(every|each)\b", text, re.I):
        return {"type": "weekly", "weekdays": days}

    return None


def compute_next(rule: dict, now: datetime) -> datetime | None:
    """Next execution time for a rule, or None if the rule is unusable."""
    t = rule.get("type")
    if t == "interval":
        return now + timedelta(seconds=int(rule["interval_seconds"]))
    if t == "once":
        try:
            return datetime.fromisoformat(rule["run_at"])
        except Exception:
            return None

    at = rule.get("at") or "00:00"
    hh, mm = (int(x) for x in at.split(":"))
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)

    if t == "daily":
        return cand
    if t == "weekday":
        days = [0, 1, 2, 3, 4]
    elif t == "weekly":
        days = rule.get("weekdays") or list(range(7))
    else:
        return cand

    for i in range(0, 8):
        d = cand + timedelta(days=i)
        if d.weekday() in days:
            return d
    return cand


def _fmt_once(dt: datetime) -> str:
    return dt.strftime("%b %d, %H:%M")


def build_rule(text: str, now: datetime) -> tuple[dict | None, str]:
    """Combine recurrence and clock time into (rule, human-readable label)."""
    repeat = detect_repeat(text)
    tm = extract_time(text, now)

    if repeat and repeat["type"] == "interval":
        sec = repeat["interval_seconds"]
        label = (f"every {sec // 3600} h" if sec >= 3600
                 else f"every {sec // 60} min")
        return {"type": "interval", "interval_seconds": sec}, label

    if tm and tm["kind"] == "relative":
        run_at = now + timedelta(seconds=tm["seconds"])
        label = (f"in {tm['seconds'] // 60} min" if tm["seconds"] < 3600
                 else f"in {tm['seconds'] // 3600} h")
        return {"type": "once", "run_at": run_at.isoformat(timespec="seconds")}, label

    if not tm:
        return None, ""

    meridiem = tm.get("explicit") or _period_word(text)
    hour = _apply_meridiem(tm["hour"], meridiem, now)
    minute = tm["minute"]
    at = f"{hour:02d}:{minute:02d}"

    if not repeat:
        days = find_days(text)
        base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if re.search(r"\btomorrow\b", text, re.I):
            base += timedelta(days=1)
        elif days:
            # "Monday at 7" means the next Monday, not today
            target = days[0]
            while base.weekday() != target:
                base += timedelta(days=1)
        if base <= now:
            base += timedelta(days=1)
            if days:
                target = days[0]
                while base.weekday() != target:
                    base += timedelta(days=1)
        return {"type": "once", "run_at": base.isoformat(timespec="seconds")}, _fmt_once(base)

    rule: dict = {"type": repeat["type"], "at": at}
    if repeat["type"] == "weekly":
        rule["weekdays"] = repeat["weekdays"]
        label = f"every {', '.join(DAY_NAMES[d] for d in repeat['weekdays'])} at {at}"
    elif repeat["type"] == "weekday":
        label = f"every weekday at {at}"
    else:
        label = f"every day at {at}"
    return rule, label


# ---------------------------------------------------------------- devices

TYPE_ALIASES = {
    "air_conditioner": [r"\bac\b", r"\bacs\b", r"\ba/c\b",
                        r"air[\s-]?con(?:ditioner|ditioning)?s?",
                        r"\baircon\b"],
    "air_purifier": [r"\bpurifiers?\b", r"air purifier"],
    "washer": [r"\bwashers?\b", r"washing machine"],
    "dryer": [r"\bdryers?\b", r"tumble dryer"],
    "refrigerator": [r"\bfridges?\b", r"\brefrigerators?\b"],
    "dish_washer": [r"\bdishwashers?\b"],
    "water_heater": [r"water heater"],
    "dehumidifier": [r"\bdehumidifiers?\b"],
    "styler": [r"\bstyler\b", r"clothes care"],
    "robot_cleaner": [r"robot vacuum", r"\broombra\b", r"vacuum"],
}

_ALIAS_CACHE: dict[str, list[re.Pattern]] = {}


def _alias_patterns(dtype: str) -> list[re.Pattern]:
    if dtype not in _ALIAS_CACHE:
        _ALIAS_CACHE[dtype] = [re.compile(p, re.I)
                               for p in TYPE_ALIASES.get(dtype, [])]
    return _ALIAS_CACHE[dtype]


def match_device(text: str, devices: list[dict], default_room: str = "",
                 fallback: dict | None = None) -> tuple[dict | None, list[dict]]:
    """Return (best match, tied candidates)."""
    text_l = text.lower()
    scored: list[tuple[int, dict]] = []

    for d in devices:
        score = 0
        name = (d.get("name") or "").lower()
        room = (d.get("room") or "").lower()
        if name and name in text_l:
            score = 100
        else:
            type_hit = any(p.search(text_l) for p in _alias_patterns(d.get("type", "")))
            room_hit = bool(room) and room in text_l
            if type_hit and room_hit:
                score = 80
            elif type_hit:
                score = 50
            elif room_hit:
                score = 25
        if score and room and room not in text_l:
            score -= 3
        if score > 0:
            scored.append((score, d))

    if not scored:
        if fallback:
            return fallback, []
        return None, []

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[0][0]
    ties = [d for s, d in scored if s == top]

    if len(ties) > 1 and default_room:
        for d in ties:
            if (d.get("room") or "").lower() == default_room.lower():
                return d, ties
    if len(ties) > 1:
        online = [d for d in ties if d.get("online", True)]
        if len(online) == 1:
            return online[0], ties
        return ties[0], ties
    return ties[0], []


# A bare "the <noun>" that names nothing in this house is the user asking for
# an appliance we do not have. It is NOT a reference to the device from the
# previous turn. Measured live: after "turn on the living room AC", both
# "set the TV to 24 degrees" and "turn on the banana" silently wrote to that
# AC, because match_device() returned the fallback when nothing scored.
_DEVICE_NOUN_RE = re.compile(
    r"\b(?:the|my|a|an|this|that)\s+([A-Za-z][A-Za-z'-]{1,})\b")

# Words that may follow a determiner in a home command without naming an
# appliance: attributes, rooms, times, and the general nouns the rules use.
# A closed list on purpose — anything else is an unknown device name.
_NOT_A_DEVICE = frozenset("""
temperature temp status state mode fan speed level power setting settings
schedule timer task job reminder routine scene room rooms house home flat
place area air water weather morning afternoon evening night day time clock
light lights volume humidity outside forecast news music song movie recipe
whole everything rest same best current target setpoint reading
""".split())


def _known_nouns(devices: list[dict]) -> set[str]:
    """Words that can appear after a determiner without naming a new device.

    Deliberately NOT `_vocab()`: that one also holds the food table, and
    "banana" is a perfectly good food word but a terrible reason to treat
    "turn on the banana" as a reference to the last appliance.
    """
    words = set(DOMAIN_WORDS) | _NOT_A_DEVICE
    for d in devices:
        for field in ("name", "room", "type", "type_label"):
            words.update(re.findall(r"[a-z]+", (d.get(field) or "").lower()))
    return words


def names_unknown_device(text: str, devices: list[dict]) -> str | None:
    """The first determiner+noun that names no appliance here, or None.

    Used to stop the previous-turn device from standing in for something the
    user named. The word itself is returned so the reply can say honestly
    "there is no TV in this house" instead of just "which device?".
    """
    known = _known_nouns(devices)
    for m in _DEVICE_NOUN_RE.finditer(text or ""):
        word = m.group(1).lower()
        if word in known:
            continue
        if any(p.search(word) for d in devices
               for p in _alias_patterns(d.get("type", ""))):
            continue
        return m.group(1)
    return None


def device_clarify(word: str | None, devices: list[dict]) -> str:
    """An honest question when the target cannot be determined."""
    options = ", ".join(d["name"] for d in devices[:6])
    if not word:
        return f"Which device? I can control: {options}"
    return (f'There is no "{word}" in this house. Which device did you mean? '
            f'I can control: {options}')


# ---------------------------------------------------------------- actions

OFF_RE = re.compile(
    r"\b(turn|switch|power|shut)\b[^.?!]{0,24}?\boff\b"
    r"|\bshut\s+(?:it\s+|them\s+)?(?:down|off)\b"
    r"|\b(stop|kill|deactivate|disable)\b", re.I)
ON_RE = re.compile(
    r"\b(turn|switch|power)\b[^.?!]{0,24}?\bon\b"
    r"|\b(start|activate|enable|resume)\b"
    r"|\bwake\s+up\b", re.I)

TEMP_DEG_RE = re.compile(
    r"\b(\d{1,2})\s*(?:\u00b0\s*c|\u00b0|degrees?(?:\s+celsius)?|celsius)\b", re.I)
TEMP_SET_RE = re.compile(r"\b(?:set|change|adjust|put|turn)\b[^.?!]{0,24}?\bto\s+(\d{1,2})\b", re.I)
TEMP_MAKE_RE = re.compile(r"\bmake\s+it\s+(\d{1,2})\b", re.I)

MODE_PATTERNS = [
    (re.compile(r"\b(cool|cooling|air\s*condition(?:ing)?|chill)\b", re.I), "cool"),
    (re.compile(r"\b(heat|heating|warm\s+up)\b", re.I), "heat"),
    (re.compile(r"\b(dry|dehumidif\w*)\b", re.I), "dry"),
    (re.compile(r"\bfan\s*(?:only|mode)\b", re.I), "fan"),
    (re.compile(r"\bauto\s*mode\b", re.I), "auto"),
]
FAN_PATTERNS = [
    (re.compile(r"\bfan\b[^.?!]{0,14}?\b(?:high|max|strong)\b|\bhigh\s+fan\b", re.I), "high"),
    (re.compile(r"\bfan\b[^.?!]{0,14}?\b(?:mid|medium|normal)\b", re.I), "mid"),
    (re.compile(r"\bfan\b[^.?!]{0,14}?\b(?:low|min|quiet)\b|\blow\s+fan\b", re.I), "low"),
    (re.compile(r"\bfan\b[^.?!]{0,14}?\bauto\b|\bauto\s+fan\b", re.I), "auto"),
]


def extract_actions(text: str) -> list[dict]:
    acts: list[dict] = []

    off = bool(OFF_RE.search(text))
    on = bool(ON_RE.search(text))
    if off:
        acts.append({"action": "power", "value": False})
    elif on:
        acts.append({"action": "power", "value": True})

    temp = None
    m = TEMP_DEG_RE.search(text)
    if m:
        temp = int(m.group(1))
    if temp is None:
        m = TEMP_SET_RE.search(text) or TEMP_MAKE_RE.search(text)
        if m:
            temp = int(m.group(1))
    if temp is not None and 16 <= temp <= 30:
        acts.append({"action": "temperature", "value": temp})

    for rx, mode in MODE_PATTERNS:
        if rx.search(text):
            acts.append({"action": "mode", "value": mode})
            break

    for rx, fan in FAN_PATTERNS:
        if rx.search(text):
            acts.append({"action": "fan", "value": fan})
            break

    return acts


# ---------------------------------------------------------------- entry point

QUERY_RE = re.compile(
    r"\bis\s+(?:it|the\s+[\w\s]{0,18}?)\s+(?:on|off|running|working)\b"
    r"|\bwhat(?:'s|\s+is)\s+the\s+(?:temperature|temp|status|state)\b"
    r"|\bhow\s+(?:hot|cold|warm)\b"
    r"|\bwhat(?:'s|\s+is)\s+(?:it|the\s+[\w\s]{0,18}?)\s+set\s+to\b"
    r"|\bcurrent\s+(?:temperature|temp|status|state)\b"
    r"|\b(?:status|state)\s+(?:of|report)\b"
    r"|\bis\s+everything\s+(?:off|on)\b"
    r"|\bwhat(?:'s|\s+is)\s+(?:it|the\s+\w+)\s+doing\b", re.I)

# A question about temperature has to be answered by a device that reports a
# room temperature, not by the first appliance that happens to sit in the
# room. In the kitchen, five devices tie on the room name and the first in
# the list is the range hood, which has no thermometer at all.
TEMP_QUERY_RE = re.compile(
    r"\b(?:temperature|temp|degrees?|set\s+to)\b"
    r"|\bhow\s+(?:hot|cold|warm)\b", re.I)

CANCEL_RE = re.compile(
    r"\b(cancel|delete|remove|clear|drop)\b.*\b(schedule[sd]?|timer[s]?|task[s]?|job[s]?|reminder[s]?)\b"
    r"|\b(schedule[sd]?|timer[s]?|task[s]?|job[s]?)\b.*\b(cancel|delete|remove|clear)\b", re.I)

LIST_RE = re.compile(
    r"\b(what|which|list|show|any)\b.*\b(schedule[sd]?|timer[s]?|task[s]?|job[s]?|reminder[s]?)\b"
    r"|\b(schedule[sd]?|timer[s]?|task[s]?|job[s]?)\b.*\b(list|do i have|are there)\b", re.I)


GROUP_RE = re.compile(r"\b(everything|all|both)\b|\bevery\s+device\b", re.I)

COMFORT_PATTERNS = [
    # Damp before hot: "clammy" is a humidity complaint, and cooling it does not
    # dry it. Measured — the loop answered "it feels clammy in the bedroom" with
    # mode=auto, which is not wrong so much as not the point.
    (re.compile(r"\b(clammy|damp|moist|dank)\b", re.I),
     [{"action": "power", "value": True},
      {"action": "mode", "value": "dry"}]),
    (re.compile(r"\b(stuffy|muggy|humid|hot|sweaty|boiling|stifling)\b", re.I),
     [{"action": "power", "value": True},
      {"action": "mode", "value": "cool"},
      {"action": "temperature", "value": 24}]),
    # A room named as a place that is too hot. These are not feelings — they are
    # comparisons, and the comparison is the whole sentence: "the bedroom is a
    # sauna" is the example this project's own pitch uses, and it fell through
    # every tier to the generic help text, which reads as "I did not understand".
    #
    # The "is/like a" prefix is what keeps cooking out. An oven you cook in is
    # "in the oven", never "is a oven", and "put the chicken in the oven at 7"
    # is a schedule that has to stay a schedule.
    (re.compile(r"\b(?:is|are|feels?|feel|like|as)\s+(?:a|an)\s+"
                r"(?:sauna|oven|furnace|kiln|greenhouse|desert)\b"
                r"|\b(?:sweltering|scorching|baking)\b", re.I),
     [{"action": "power", "value": True},
      {"action": "mode", "value": "cool"},
      {"action": "temperature", "value": 24}]),
    (re.compile(r"\b(cold|freezing|chilly|shivering)\b", re.I),
     [{"action": "power", "value": True},
      {"action": "mode", "value": "heat"},
      {"action": "temperature", "value": 24}]),
    (re.compile(r"\b(too\s+loud|noisy|too\s+noisy)\b", re.I),
     [{"action": "fan", "value": "low"}]),
]


def extract_comfort(text: str) -> list[dict]:
    """Map a feeling ("it's stuffy") onto concrete device actions."""
    for rx, acts in COMFORT_PATTERNS:
        if rx.search(text):
            return [dict(a) for a in acts]
    return []


# A smell is not a temperature, and it is not about the appliance in the room
# the user happened to name. It is an air-quality complaint, and the device that
# answers it is the purifier.
#
# Measured, and the reason this exists: "the kitchen smells" resolved to the
# fridge — the only device in the kitchen — and the tool loop, with nothing
# better to do, switched the fridge off. Two runs out of three.
AIR_RE = re.compile(
    r"\b(?:smell|smells|smelly|smelt|stink|stinks|stank|stinking|reek|reeks)\b"
    r"|\b(?:odou?r|disgusting|gross|foul|stale)\b", re.I)

AIR_ACTIONS = [{"action": "power", "value": True},
               {"action": "fan", "value": "high"}]


# Topics this house cannot answer, whatever the phrasing. The generic fallback
# ("I can turn devices on and off...") is the wrong reply for them: it reads as
# "I did not understand", when the sentence was understood perfectly and simply
# needs data that does not exist here. Saying which data is missing is the
# honest answer, and it is also the more useful one -- the user learns what to
# tell the agent instead of what to stop asking.
#
# Kept as a table, checked only after every control, schedule and query path has
# failed, so "turn on the dehumidifier, it's raining" is still a command.
OUT_OF_SCOPE: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:weather|forecast|rain(?:s|ing|y)?|snow(?:s|ing|y)?|"
                r"sunny|cloudy|windy|umbrella|humid outside|"
                r"(?:hot|cold|warm) (?:outside|out there)|"
                r"how (?:hot|cold|warm) is it (?:outside|out there)|"
                r"(?:good|nice|bad|fine|ok|okay|wet|dry)\s+(?:day|weather)\s+"
                r"(?:to|for))\b", re.I),
     "I cannot see the weather — the only things I can read are the appliances "
     "in this house. Tell me what it is doing outside and I can act on it: a "
     "damp day is a good reason to run the dehumidifier."),
    (re.compile(r"\b(?:music|song|songs|playlist|podcast|radio|spotify|"
                r"netflix|youtube|movie)\b", re.I),
     "I cannot play music or video — I only control the appliances."),
    (re.compile(r"\b(?:news|headlines?|stock|share price|email|calendar|"
                r"traffic|flight|order (?:a|some|me)|book (?:a|me))\b", re.I),
     "That is outside this house — I can only work the appliances. I can start "
     "the washer, set a timer, or read what a device is doing."),
    # Cooking is the line this project draws on purpose. A recipe is a sentence
    # any model can write, nothing in it comes from this house, and no one here
    # can check it. Storage advice is the opposite: it pairs a rule from outside
    # with a temperature read from the appliance, which is why it is answered
    # and this is not.
    (re.compile(r"\b(?:recipe|recipes|cook|cooking|make (?:me |us )?"
                r"(?:a|some|dinner|lunch|breakfast)|ingredient|ingredients|"
                r"what (?:can|should) i (?:make|cook)|seasoning|marinade)\b",
                re.I),
     "No recipes — I would be inventing them. Ask me how long something keeps "
     "and what the fridge is running at: \"how long can I keep the chicken\"."),
]


def extract_air(text: str) -> list[dict]:
    return [dict(a) for a in AIR_ACTIONS] if AIR_RE.search(text) else []


def names_a_device(text: str, devices: list[dict]) -> bool:
    """Did the user name an appliance, rather than a room or a feeling?

    "the kitchen smells" names a room; "the fridge smells" names an appliance
    and is therefore about that appliance — nothing here can clean a fridge.
    """
    t = (text or "").lower()
    return any(p.search(t) for d in devices
               for p in _alias_patterns(d.get("type", "")))


def _names_other_device(text: str, devices: list[dict]) -> bool:
    """The same question with the refrigerator taken out.

    "put the beer in the fridge" is a question about the fridge; "keep the washer
    on" is a command about the washer. Only the first is food storage, and the
    fridge is the appliance a storage question is allowed to name.
    """
    t = (text or "").lower()
    return any(p.search(t) for d in devices
               if d.get("type") != "refrigerator"
               for p in _alias_patterns(d.get("type", "")))


# ---------------------------------------------------------------- typo repair
#
# Typing on a phone produces "bedrom", "purifire", "evry nite". The rules are
# regexes, so one wrong letter means no match at all -- and for a device name
# that is worse than no answer: the parser falls back to the default room and
# turns the wrong appliance off, in 1 ms, with no sign that anything went wrong.
#
# So a second attempt is made on a repaired sentence, and it is only accepted
# when it is strictly better (see Agent._prefer_repaired). Repair is a nearest
# word over a closed vocabulary -- the words this house actually uses -- so it
# cannot invent a device, and it never touches a word that is already valid.

# The typo people make with one hand: "turn of the AC".
_VERB_OF = re.compile(r"\b(turn|switch|shut|power)\s+of\b", re.I)

# Words the domain itself uses. Deliberately closed: anything outside this list
# and the device vocabulary is left exactly as the user typed it.
DOMAIN_WORDS = frozenset("""
a an the and or but if then please thanks thank
turn switch shut set start stop open close power put
on off up down in out to at for with from it its them
now later today tonight tomorrow morning afternoon evening night
every each day daily weekday weekend monday tuesday wednesday thursday
friday saturday sunday once twice
minute minutes hour hours half quarter past to o clock
degree degrees celsius fahrenheit
cool cooling heat heating dry fan auto low mid medium high
all everything whole house home flat place room rooms
wake sleep sleeping leave leaving away going come back
it's that this is are be do does make makes
""".split())


def _vocab(devices: list[dict]) -> set[str]:
    words = set(DOMAIN_WORDS)
    # Ingredients are vocabulary of this house now: "chiken" is as much a
    # near-miss as "bedrom" is, and getting it wrong sends the sentence to the
    # refusal path instead of to the storage answer.
    words.update(k for entry in food.FOOD_TABLE for k in entry["keys"])
    for d in devices:
        for field in ("name", "room", "type"):
            words.update(re.findall(r"[a-z]+", (d.get(field) or "").lower()))
    return words


def repair(text: str, devices: list[dict], cutoff: float = 0.85) -> str:
    """Correct near-miss words against this house's vocabulary.

    Returns the text unchanged when nothing is close enough, which is the
    common case: correct sentences have every word already in the vocabulary.
    """
    if not text:
        return text
    text = _VERB_OF.sub(r"\1 off", text)
    vocab = _vocab(devices)
    cache: dict[str, str] = {}

    def fix(m: re.Match) -> str:
        word = m.group(0)
        low = word.lower()
        if len(low) < 3 or low in vocab:
            return word
        if low not in cache:
            hit = difflib.get_close_matches(low, vocab, n=1, cutoff=cutoff)
            cache[low] = hit[0] if hit else word
        return cache[low]

    # Twice: once before the word repair ("turn of"), once after it ("swich of"
    # becomes "switch of" only in the second pass).
    out = re.sub(r"[A-Za-z]+", fix, _VERB_OF.sub(r"\1 off", text))
    return _VERB_OF.sub(r"\1 off", out)


def resolve_targets(text: str, devices: list[dict], default_room: str = "",
                    fallback: dict | None = None) -> tuple[list[dict], str]:
    """Resolve one utterance to one or more devices.

    Handles "everything", "all the ACs", "both", a bare room name ("turn off the
    living room"), and the ordinary single-device case.
    """
    t = text.lower()
    type_hits = [d for d in devices
                 if any(p.search(t) for p in _alias_patterns(d.get("type", "")))]
    room_hits = [d for d in devices
                 if (d.get("room") or "").lower() and (d.get("room") or "").lower() in t]
    if not room_hits:
        # "the bed room is too hot" is how a phone keyboard writes "bedroom",
        # and the room was named — the only thing between the parser and the
        # answer was one space. Seen live: it asked "Which room?" for a sentence
        # that had already said which room.
        #
        # Compared without spaces, a room name cannot bridge two unrelated
        # words: "put the bed in the room" squeezes to "putthebedintheroom",
        # which does not contain "bedroom".
        squeezed = t.replace(" ", "")
        room_hits = [d for d in devices
                     if (d.get("room") or "").lower()
                     and (d.get("room") or "").lower().replace(" ", "") in squeezed]

    if GROUP_RE.search(t):
        if type_hits and room_hits:
            pool = [d for d in type_hits if d in room_hits] or type_hits
        elif type_hits:
            pool = type_hits
        elif room_hits:
            pool = room_hits
        else:
            pool = list(devices)
        label = ("everything" if len(pool) == len(devices)
                 else " and ".join(d["name"] for d in pool))
        return pool, label

    if room_hits and not type_hits:
        room = room_hits[0].get("room", "")
        return room_hits, f"everything in the {room}"

    single, _ties = match_device(text, devices, default_room, fallback)
    return ([single] if single else []), (single["name"] if single else "")


def set_targets(res: dict, targets: list[dict], label: str = "") -> dict:
    """Write a target list onto an intent, keeping single-device fields for compat."""
    res["targets"] = [{"id": d["id"], "name": d["name"]} for d in targets]
    if len(targets) == 1:
        res["device_id"] = targets[0]["id"]
        res["device_name"] = targets[0]["name"]
    else:
        res["device_id"] = None
        res["device_name"] = label or f"{len(targets)} devices"
    return res


def _ask_for(res: dict, slot: str, devices: list[dict], **partial) -> dict:
    """Mark a slot as missing so the agent can ask for it and remember the rest."""
    res["intent"] = "need_" + slot
    res["missing"] = slot
    res.update(partial)
    if slot == "target":
        prompt = "Which room?" if res.get("comfort") else "Which device?"
        res["clarify"] = (prompt + " I can control: "
                          + ", ".join(d["name"] for d in devices[:6]))
    else:
        res["clarify"] = ('What should I do at that time? '
                          'e.g. "turn off the AC at 11 pm".')
    return res


def parse(text: str, devices: list[dict], now: datetime | None = None,
          default_room: str = "Living Room",
          fallback_device: dict | None = None) -> dict:
    now = now or datetime.now()
    t = (text or "").strip()
    res = {
        "intent": "unknown", "device_id": None, "device_name": None, "targets": [],
        "actions": [], "rule": None, "repeat_label": "",
        "confidence": 0.0, "clarify": None, "raw_text": t,
    }
    if not t:
        res["clarify"] = 'Say something like "turn off the living room AC at 11 pm".'
        return res

    # 1. cancel / list scheduled tasks
    if CANCEL_RE.search(t):
        res["intent"] = "cancel"
        res["confidence"] = 0.9
        res["cancel_all"] = bool(re.search(r"\ball\b|\beverything\b|\bclear\b", t, re.I))
        return res

    if LIST_RE.search(t):
        res["intent"] = "list"
        res["confidence"] = 0.9
        return res

    # 2. a named routine beats the action words inside it. "everything off, I'm
    # leaving" contains a perfectly good control command, and following it would
    # switch the fridge off — the routine knows better than the words do.
    # No default room here on purpose: "wake the house up" names no room, so it
    # must mean every room rather than the one the user happens to be standing in.
    scene = scenes.detect(t)
    if scene:
        scoped, target_label = resolve_targets(t, devices, "", None)
        if not scoped:
            scoped, target_label = list(devices), "everything"
        res.update(intent="scene", scene=scene, scene_label=scenes.label(scene),
                   confidence=0.93)
        set_targets(res, scoped, target_label)
        rule, label = build_rule(t, now)
        if rule:
            res.update(rule=rule, repeat_label=label)
        return res

    # 3. storing food is not operating a device. It has to be decided before the
    # steps below, because the sentence names a device and reads like a feeling:
    # "is the fridge cold enough for vegetables" would otherwise reach the
    # comfort rules, and "cold" there maps to heating the fridge to 24 °C.
    # Measured twice, with and without a previous turn, same wrong result.
    if food.is_food_request(t):
        fridge = next((d for d in devices if d.get("type") == "refrigerator"
                       and d.get("online", True)), None)
        res.update(intent="food", confidence=0.9, food=True)
        set_targets(res, [fridge] if fridge else [])
        return res

    # 3b. A storage question about something the table has never heard of —
    # "where do I keep the mango". It used to reach the generic help text, which
    # answers the wrong question: the house can give the reading, and the rest is
    # a boundary of the table rather than a failure to understand. Guarded on the
    # other appliances so "keep the washer on" stays a control command.
    if food.is_unknown_storage(t) and not _names_other_device(t, devices):
        fridge = next((d for d in devices if d.get("type") == "refrigerator"
                       and d.get("online", True)), None)
        res.update(intent="food", confidence=0.75, food=True)
        set_targets(res, [fridge] if fridge else [])
        return res

    # 4. an air-quality complaint goes to the purifier, not to whatever sits in
    # the room that was mentioned.
    if not names_a_device(t, devices):
        air = extract_air(t)
        purifiers = [d for d in devices
                     if d.get("type") == "air_purifier" and d.get("online", True)]
        if air and purifiers:
            res.update(intent="control", actions=air, confidence=0.9, comfort=True)
            set_targets(res, purifiers, "the purifier")
            return res

    # 4. actions + time decide control vs schedule
    actions = extract_actions(t)
    rule, label = build_rule(t, now)

    comfort: list[dict] = []
    if not actions and not QUERY_RE.search(t):
        comfort = extract_comfort(t)
        if comfort:
            actions = comfort
            res["comfort"] = True

    # A feeling ("it's stuffy") is about the room, not about the last device
    # we happened to touch. A named appliance that does not exist here is not
    # that device either: neither may silently borrow the fallback. Without
    # the second condition, "set the TV to 24 degrees" wrote 24 C to the
    # living room AC the user had just turned on.
    unknown_device = names_unknown_device(t, devices)
    fb = None if (comfort or unknown_device) else fallback_device
    targets, target_label = resolve_targets(t, devices, default_room, fb)

    if comfort:
        # A complaint about the air is a request to move the air, and only a
        # device that can move air can answer it. Resolved by room alone,
        # "it's stuffy in the kitchen" landed on the Kitchen Fridge — the only
        # appliance in that room — and the command layer refused `mode`, which
        # reads as a broken assistant and is the last guardrail before a
        # refrigerator is asked to hold 24 °C. Naming the room's appliances is
        # both the honest answer and the useful one: the ACs are one room over.
        wanted = {a["action"] for a in comfort}
        # A device that declares no capability list cannot be judged, and the
        # command layer stays the last word on what it accepts. Only a device
        # that says what it can do and plainly cannot do this is dropped.
        able = [d for d in targets
                if not d.get("capabilities") or wanted <= set(d["capabilities"])]
        if targets and not able:
            room = (targets[0].get("room") or default_room) if targets else default_room
            here = ", ".join(d["name"] for d in targets) or "nothing"
            elsewhere = ", ".join(sorted({d["room"] for d in devices
                                          if d["type"] == "air_conditioner"}))
            res.update(intent="control", actions=[], confidence=0.7, comfort=True,
                       clarify=(f"There is no air conditioner in the {room} — "
                                f"what is there is {here}, and none of it can "
                                f"cool a room. The air conditioners are in the "
                                f"{elsewhere}."))
            return res
        targets = able

    if rule:
        if not actions:
            return _ask_for(res, "action", devices, rule=rule,
                            targets=[{"id": d["id"], "name": d["name"]} for d in targets])
        if not targets:
            out = _ask_for(res, "target", devices, actions=actions, rule=rule,
                           repeat_label=label)
            if unknown_device:
                out["clarify"] = device_clarify(unknown_device, devices)
            return out
        res.update(intent="schedule", actions=actions, rule=rule,
                   repeat_label=label, confidence=0.92)
        set_targets(res, targets, target_label)
        res["group"] = bool(GROUP_RE.search(t))
        return res

    if actions:
        if not targets:
            out = _ask_for(res, "target", devices, actions=actions)
            if unknown_device:
                out["clarify"] = device_clarify(unknown_device, devices)
            return out
        res.update(intent="control", actions=actions, confidence=0.88)
        set_targets(res, targets, target_label)
        # "everything off" is a figure of speech, and the agent needs to know it
        # was one: a flat list cannot express "except the fridge".
        res["group"] = bool(GROUP_RE.search(t))
        return res

    # 5. status questions
    if QUERY_RE.search(t):
        device, ties = match_device(t, devices, default_room, fb)
        res.update(intent="query", confidence=0.85)
        if not device and unknown_device:
            # "is the TV on?" must not be answered with the living room AC.
            res["clarify"] = device_clarify(unknown_device, devices)
            return res
        if not device:
            pool = ([d for d in devices
                     if (d.get("room") or "").lower() == default_room.lower()]
                    or [d for d in devices if d.get("type") == "air_conditioner"]
                    or devices)
            device = pool[0] if pool else None
            ties = []
        if TEMP_QUERY_RE.search(t) and len(ties) > 1:
            # "what's the temperature in the kitchen" ties five appliances on
            # the room name and ties[0] is the range hood, which has no
            # thermometer. That is not a rough answer, it is a different
            # question. The AC is what reports a room temperature here, so
            # take it when there is exactly one; otherwise ask rather than
            # read a random appliance's power state.
            sensors = [d for d in ties if d.get("type") == "air_conditioner"]
            if len(sensors) == 1:
                device, ties = sensors[0], []
            else:
                names = ", ".join(d["name"] for d in ties[:6])
                res["clarify"] = (f"Which device did you mean? I found {names}. "
                                  f"Name one and I'll read it.")
                return res
        if device:
            set_targets(res, [device])
            if len(ties) > 1:
                res["clarify"] = (f'Multiple devices matched, so I answered for '
                                  f'"{device["name"]}".')
        else:
            res["clarify"] = "I could not tell which device you mean."
        return res

    # 6. topics this house has no data for
    for pattern, reply in OUT_OF_SCOPE:
        if pattern.search(t):
            res["clarify"] = reply
            res["out_of_scope"] = True
            return res

    # 7. fallback
    res["clarify"] = ('I can turn devices on and off, set the temperature, and '
                      'schedule things. Try "set the living room AC to 26 degrees" '
                      'or "turn off the living room AC every night at 11 pm".')
    return res
