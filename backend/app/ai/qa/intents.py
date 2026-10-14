"""Phase 7E intent router and entity extraction.

    route(question, factsheet, previous=None, client=None) -> Route(intent, confidence, entities, method, ...)
    extract_entities(question, factsheet) -> Entities

v1 is a keyword/regex scorer. Each intent sums the weights of its patterns over the normalised
question (lower case, typos corrected against the router's own vocabulary); entities add a boost
(a named corner → corner_detail, a named sector → why_faster_in_sector). Confidence comes from the
top score and its margin over the runner-up. Below LOW_CONFIDENCE a small constrained LLM call
classifies (output limited to the intent enum); without a client, or when that call fails, the
top-scoring intent is kept if it scored at all, otherwise out_of_scope.

Clear out-of-scope questions (other sessions or drivers, future races, personal information,
opinions on people, instructions to break the rules, unrelated requests) are caught by rules first,
so they never reach an LLM.

Entities are matched against the FactSheet only: a corner exists if the FactSheet has its facts.
Anything named that is not in this comparison is reported in `unknown` / `other_drivers`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

import structlog

from ..llm_client import LLMClient, LLMError, Message
from ..schemas import FactSheet

log = structlog.get_logger("raceiq.ai")

LOW_CONFIDENCE = 0.35


class Intent(str, Enum):
    WHERE_LOST_TIME = "where_lost_time"
    WHY_FASTER_IN_SECTOR = "why_faster_in_sector"
    TIRE_STATUS = "tire_status"
    FOCUS_CORNERS = "focus_corners"
    WHEN_TO_PIT = "when_to_pit"
    CAUSE_ATTRIBUTION = "cause_attribution"
    CORNER_DETAIL = "corner_detail"
    OUT_OF_SCOPE = "out_of_scope"


SUPPORTED = tuple(i for i in Intent if i is not Intent.OUT_OF_SCOPE)


# ---------------------------------------------------------------- normalisation and typos
NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
NUMBER_WORDS |= {f"twenty {w}": 20 + i for w, i in list(NUMBER_WORDS.items())[1:10]}
ORDINALS = {w: i + 1 for i, w in enumerate(
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth "
    "fifteenth sixteenth seventeenth eighteenth nineteenth twentieth".split())}
ALIASES = {"loose": "lose", "loosing": "losing", "pitt": "pit", "pitting": "pit", "tyre": "tyre", "tire": "tyre",
           "tires": "tyres", "deg": "degradation", "quali": "qualifying", "whats": "what is", "wheres": "where is",
           "hes": "he is", "trafic": "traffic", "secter": "sector", "sectr": "sector", "crner": "corner"}
# words a typo must never be "corrected" from (real words close to a keyword)
KEEP = {"tired", "times", "timer", "where", "there", "were", "lost", "most", "post", "cost", "best", "test", "fast",
        "last", "past", "stop", "step", "slow", "show", "sets", "less", "loss", "lose", "pace", "place", "turn",
        "tune", "work", "word", "worn", "right", "night", "light", "weight", "sight", "tenths", "corner", "corners", "tyres", "tyre", "about", "after", "faster", "slower"}


def _distance(a: str, b: str) -> int:
    """Damerau–Levenshtein (optimal string alignment) distance."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


# ---------------------------------------------------------------- scoring patterns
W = list[tuple[re.Pattern[str], float]]


def _p(*items: tuple[str, float]) -> W:
    return [(re.compile(rf"\b(?:{pattern})\b"), weight) for pattern, weight in items]


PATTERNS: dict[Intent, W] = {
    Intent.WHERE_LOST_TIME: _p(
        (r"lose|lost|losing|loss|losses|losing out", 1.5), (r"gain|gained|gaining|gains", 1.0), (r"where", 1.0),
        (r"biggest|largest|most", 0.5), (r"delta|deltas|gap|deficit", 1.0), (r"time is going|time going", 2.0),
        (r"cost|costing", 1.0), (r"slower|quicker|faster", 0.5), (r"part of the lap", 1.0)),
    Intent.WHY_FASTER_IN_SECTOR: _p((r"why|how|explain|what made", 0.5)),
    Intent.TIRE_STATUS: _p(
        (r"tyres?|rubber|compound|hards?|mediums?|softs?", 2.0),
        (r"degradation|degredation|wear|worn|life|grip|going off|dropping off|drop off|cliff|stint|competitive|"
         r"holding up", 2.0)),
    Intent.WHEN_TO_PIT: _p((r"pit|pits|box|boxing|stop|stopping|window|undercut|overcut|come in", 3.0),
                           (r"when", 1.0)),
    Intent.FOCUS_CORNERS: _p(
        (r"focus|work on|improve|improvement|improving|prioriti[sz]e|priority|priorities|advice|tips?|coach\w*|"
         r"differently|find time|key areas|work", 3.0),
        (r"corners|turns", 1.5), (r"should", 0.5)),
    Intent.CAUSE_ATTRIBUTION: _p(
        (r"caused?|causes|cause of|traffic|fault|blame|error|mistake|driver or|or the driver|tyres or|"
         r"or the tyres|or traffic|conditions|weather|wind|flag|flags|rain", 5.0),
        (r"(?:was|is|were) (?:it|that|this|they) (?:the |his |her |a )?(?:tyres?|driver|traffic|conditions)", 5.0),
        (r"reason|due to|down to|because|responsible", 2.5), (r"why", 1.0),
        (r"why\b.*\b(?:lose|lost|losing|slow|slower|deficit|gap|behind)", 2.5)),
    Intent.CORNER_DETAIL: _p((r"happened|happen|details?|tell me about|show me|analysis|braking|apex|exit|entry", 1.0)),
}
ENTITY_BOOST = 4.0

OUT_OF_SCOPE_RULES: dict[str, re.Pattern[str]] = {
    "instructions": re.compile(
        r"\b(?:ignore (?:all |your |the |any )?(?:previous |prior |above )?(?:rules|instructions|prompt)|"
        r"pretend|jailbreak|developer mode|system prompt|act as|no rules|make up (?:a|an|some|numbers|data)|made up|"
        r"guess|invent|"
        r"disregard)\b"),
    "other_session": re.compile(
        r"\b(?:last|next|previous|other|another) (?:year|years|season|race|races|weekend|grand prix|gp|session|"
        r"event)\b|\blast year\b|\bnext year\b|\bthis season\b"),
    "future": re.compile(r"\b(?:championship|title|who will|will \w+ win|win the|predict(?:ion)? (?:for|of) the)\b"),
    "personal": re.compile(
        r"\b(?:how old|age of|birthday|salary|paid|earn|net worth|dating|girlfriend|boyfriend|wife|husband|married|"
        r"family|where does \w+ live|lives? in|religion|breakfast|eat|personal)\b"),
    "opinion": re.compile(
        r"\b(?:best|worst|greatest|better|overrated|underrated) driver\b|\bbetter driver\b|\bdo you (?:like|love|hate)\b|"
        r"\bopinion (?:of|on) (?:him|her|them|\w+)\b"),
    "unrelated": re.compile(
        r"\b(?:poem|joke|song|story|recipe|capital of|tomorrow|homework|stock|bitcoin|translate|write me|"
        r"write a)\b"),
}

# drivers 2018–2025 (code, surname): only to RECOGNISE a name; whether it is in the comparison is
# decided by the FactSheet's two codes
DRIVERS = {
    "ALB": "albon", "ALO": "alonso", "BEA": "bearman", "BOT": "bottas", "COL": "colapinto", "DEV": "de vries",
    "GAS": "gasly", "GIO": "giovinazzi", "GRO": "grosjean", "HAM": "hamilton", "HUL": "hulkenberg",
    "KUB": "kubica", "KVY": "kvyat", "LAT": "latifi", "LAW": "lawson", "LEC": "leclerc", "MAG": "magnussen",
    "MAZ": "mazepin", "MSC": "schumacher", "NOR": "norris", "OCO": "ocon", "PER": "perez", "PIA": "piastri",
    "RAI": "raikkonen", "RIC": "ricciardo", "RUS": "russell", "SAI": "sainz", "SAR": "sargeant", "STR": "stroll",
    "TSU": "tsunoda", "VER": "verstappen", "VET": "vettel", "ZHO": "zhou", "ANT": "antonelli", "BOR": "bortoleto",
    "HAD": "hadjar", "DOO": "doohan", "AIT": "aitken", "FIT": "fittipaldi",
}
FIRST_NAMES = {"charles": "LEC", "oscar": "PIA", "max": "VER", "lewis": "HAM", "lando": "NOR", "fernando": "ALO",
               "carlos": "SAI", "george": "RUS", "sergio": "PER", "checo": "PER", "valtteri": "BOT"}
ROLES = {"target": r"\b(?:driver a|the target|target driver|the driver)\b",
         "reference": r"\b(?:driver b|the reference|reference driver|the benchmark|the other car)\b"}
# other events (name → a word of the FactSheet's event name that means "this event")
EVENTS = {"bahrain": "bahrain", "saudi": "saudi", "jeddah": "saudi", "australian": "australian",
          "melbourne": "australian", "japanese": "japanese", "suzuka": "japanese", "chinese": "chinese",
          "shanghai": "chinese", "miami": "miami", "imola": "emilia", "emilia": "emilia", "monaco": "monaco",
          "canadian": "canadian", "montreal": "canadian", "spanish": "spanish", "barcelona": "spanish",
          "austrian": "austrian", "british": "british", "silverstone": "british", "hungarian": "hungarian",
          "belgian": "belgian", "spa": "belgian", "dutch": "dutch", "zandvoort": "dutch", "italian": "italian",
          "monza": "italian", "azerbaijan": "azerbaijan", "baku": "azerbaijan", "singapore": "singapore",
          "united states": "united states", "austin": "united states", "mexico": "mexico", "mexican": "mexico",
          "brazil": "paulo", "sao paulo": "paulo", "interlagos": "paulo", "las vegas": "vegas", "qatar": "qatar",
          "abu dhabi": "abu dhabi", "french": "french", "portuguese": "portuguese", "turkish": "turkish"}


# ---------------------------------------------------------------- entities
@dataclass
class Entities:
    corners: list[str] = field(default_factory=list)       # fact prefixes: ["T7"]
    sectors: list[str] = field(default_factory=list)       # ["SEC2"]
    straights: list[str] = field(default_factory=list)     # ["STR1"]
    drivers: list[str] = field(default_factory=list)       # "target" / "reference", in order of mention
    unknown: list[str] = field(default_factory=list)       # labels named but not in this comparison
    other_drivers: list[str] = field(default_factory=list)  # codes not in this comparison

    @property
    def segments(self) -> list[str]:
        return [*self.corners, *self.straights, *self.sectors]

    @property
    def primary(self) -> str | None:
        segments = self.corners or self.straights or self.sectors
        return segments[0] if segments else None

    def as_dict(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self.__dict__.items() if v}


def _number(token: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    if token in NUMBER_WORDS:
        return NUMBER_WORDS[token]
    m = re.fullmatch(r"(\d+)(?:st|nd|rd|th)", token)
    if m:
        return int(m.group(1))
    return ORDINALS.get(token)


NUM = r"\d{1,2}|" + "|".join(sorted((re.escape(w) for w in NUMBER_WORDS), key=len, reverse=True))
ORD = r"\d{1,2}(?:st|nd|rd|th)|" + "|".join(ORDINALS) + "|last|final"
CORNER_RE = [re.compile(rf"\b(?:turn|turns|corner|t)\s*-?\s*({NUM})\b"),
             re.compile(rf"\b(?:the )?({ORD}) (?:corner|turn)\b")]
SECTOR_RE = [re.compile(rf"\bsectors?\s*-?\s*({NUM})\b"), re.compile(r"\bs([1-9])\b"),
             re.compile(rf"\b(?:the )?({ORD}) sector\b")]
STRAIGHT_RE = [re.compile(rf"\bstraights?\s*-?\s*({NUM})\b"), re.compile(rf"\b(?:the )?({ORD}) straight\b")]


def normalise(question: str) -> str:
    text = question.lower().replace("’", "'")
    text = re.sub(r"'s\b", "", text)
    text = re.sub(r"[^a-z0-9\s-]", " ", text).replace(" - ", " ")
    words: list[str] = []
    vocab = _vocabulary()
    for w in text.split():
        w = ALIASES.get(w, w)
        if len(w) >= 5 and w not in vocab and w not in KEEP and not w.isdigit():
            limit = 2 if len(w) >= 9 else 1
            best = min(vocab, key=lambda v: (_distance(w, v), v))
            if _distance(w, best) <= limit and abs(len(best) - len(w)) <= limit:
                w = best
        words.append(w)
    return " ".join(words)


_VOCAB: set[str] = set()


def _vocabulary() -> set[str]:
    if not _VOCAB:
        words = {"sector", "corner", "straight", "traffic", "happened", "focus", "degradation", "improve", "caused",
                 "competitive", "window", "losing", "slower", "faster", "quicker", "biggest", "largest", "advice",
                 "prioritise", "prioritize", "analysis", "details", "braking", "conditions", "weather", "stint",
                 "compound", "mistake", "error", "differently", "improvement", "championship", "undercut", "overcut",
                 "coaching", "responsible", "because", "reason", "explain", "rubber", "mediums", "softs", "hards"}
        words |= {w for w in NUMBER_WORDS if " " not in w} | set(ORDINALS)
        _VOCAB.update(w for w in words if len(w) >= 5)
    return _VOCAB


def _prefixes(fs: FactSheet) -> set[str]:
    return {f.id.split(".")[0] for f in fs.facts}


def _numbered(kind: str, fs: FactSheet) -> list[int]:
    return sorted(int(p[len(kind):]) for p in _prefixes(fs) if re.fullmatch(rf"{kind}\d+", p))


def extract_entities(question: str, fs: FactSheet) -> Entities:
    text = normalise(question)
    e = Entities()
    for kind, patterns, name, out in (("T", CORNER_RE, "Turn", e.corners), ("SEC", SECTOR_RE, "Sector", e.sectors),
                                      ("STR", STRAIGHT_RE, "Straight", e.straights)):
        have = _numbered(kind, fs)
        found: list[tuple[int, int]] = []
        for pattern in patterns:
            for m in pattern.finditer(text):
                token = m.group(1)
                n = (have[-1] if have else None) if token in ("last", "final") else _number(token)
                if n is not None:
                    found.append((m.start(), n))
        for _, n in sorted(found):
            label = f"{kind}{n}"
            if n in have:
                if label not in out:
                    out.append(label)
            elif f"{name} {n}" not in e.unknown:
                e.unknown.append(f"{name} {n}")
    # drivers: codes (as written, upper case), surnames, first names, roles
    codes = {fs.drivers.target.code: "target", fs.drivers.reference.code: "reference"}
    mentions: list[tuple[int, str]] = []
    for m in re.finditer(r"\b[A-Z]{3}\b", question):
        if m.group(0) in codes:
            mentions.append((m.start(), m.group(0)))
        elif m.group(0) in DRIVERS:
            mentions.append((m.start(), m.group(0)))
    low = question.lower()
    for code, surname in DRIVERS.items():
        for m in re.finditer(rf"\b{re.escape(surname)}\b", low):
            mentions.append((m.start(), code))
    for first, code in FIRST_NAMES.items():
        for m in re.finditer(rf"\b{first}\b", low):
            mentions.append((m.start(), code))
    for role, role_pattern in ROLES.items():
        for m in re.finditer(role_pattern, low):
            mentions.append((m.start(), fs.drivers.target.code if role == "target" else fs.drivers.reference.code))
    for _, code in sorted(mentions):
        if code in codes:
            if codes[code] not in e.drivers:
                e.drivers.append(codes[code])
        elif code not in e.other_drivers:
            e.other_drivers.append(code)
    return e


# ---------------------------------------------------------------- routing
@dataclass
class Route:
    intent: Intent
    confidence: float
    entities: Entities
    method: Literal["rules", "llm", "follow_up", "default"]
    out_of_scope: str | None = None             # the out-of-scope category, when refused by rule
    scores: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PreviousTurn:
    """What a follow-up may inherit: the last turn's intent and entities (validated turns only)."""
    intent: Intent
    entities: Entities


ELLIPTICAL = re.compile(r"^(?:and|what about|how about|same for|and in|and at|also|then|ok and|what of|so|"
                        r"was (?:it|that|this)|is (?:it|that|this))\b|^\w+\??$")


def _scores(text: str, e: Entities) -> dict[Intent, float]:
    scores: dict[Intent, float] = {i: sum(w for p, w in PATTERNS.get(i, []) if p.search(text)) for i in SUPPORTED}
    # a named segment counts even when it is not in this comparison (it then gets a clarification)
    unknown_sector = any(u.startswith("Sector") for u in e.unknown)
    unknown_corner = any(not u.startswith("Sector") for u in e.unknown)
    if e.sectors or unknown_sector:
        scores[Intent.WHY_FASTER_IN_SECTOR] += ENTITY_BOOST
    if e.corners or e.straights or unknown_corner:
        scores[Intent.CORNER_DETAIL] += ENTITY_BOOST
    else:
        scores[Intent.CORNER_DETAIL] = 0.0            # corner words mean nothing without a corner
    if not (e.sectors or unknown_sector):
        scores[Intent.WHY_FASTER_IN_SECTOR] = 0.0
    return scores


def _confidence(top: float, second: float) -> float:
    if top <= 0:
        return 0.0
    return round(min(1.0, top / 4.0) * min(1.0, 0.4 + (top - second) / 2.0), 2)


def _out_of_scope(question: str, text: str, fs: FactSheet, e: Entities) -> str | None:
    for category, pattern in OUT_OF_SCOPE_RULES.items():
        if pattern.search(text):
            return category
    if e.other_drivers:
        return "other_driver"
    years = {int(y) for y in re.findall(r"\b(19[5-9]\d|20\d\d)\b", text)}
    session_year = int(fs.session.key[:4]) if fs.session.key and fs.session.key[:4].isdigit() else None
    if years and years != {session_year}:
        return "other_session"
    event = (fs.session.event or "").lower()
    if any(re.search(rf"\b{re.escape(name)}\b", text) and word not in event for name, word in EVENTS.items()):
        return "other_session"
    return None


def _classify_with_llm(question: str, client: LLMClient) -> Intent | None:
    schema = {"type": "object", "additionalProperties": False, "required": ["intent"],
              "properties": {"intent": {"type": "string", "enum": [i.value for i in Intent]}}}
    system = ("Classify a question about ONE Formula 1 lap comparison (two drivers' laps in one session) into "
              "exactly one intent. Intents: " + "; ".join([
                  "where_lost_time = where on the lap time was lost or gained",
                  "why_faster_in_sector = why a driver was faster or slower in a sector",
                  "tire_status = the current tyre stint, degradation, competitiveness",
                  "focus_corners = which corners to work on, advice",
                  "when_to_pit = pit timing, the pit window",
                  "cause_attribution = whether the driver, the tyres, traffic or conditions caused a loss",
                  "corner_detail = what happened in one corner",
                  "out_of_scope = anything else (other sessions or drivers, the future, people, unrelated)"])
              + ". Return only the JSON object.")
    try:
        res = client.generate(system=system, messages=[Message("user", question)], response_schema=schema,
                              temperature=0.0, max_tokens=50, timeout_s=15.0)
    except LLMError as exc:
        log.warning("ai.qa_classify_failed", error=type(exc).__name__)
        return None
    value: Any = (res.parsed or {}).get("intent")
    try:
        return Intent(value)
    except ValueError:
        log.warning("ai.qa_classify_invalid")
        return None


def route(question: str, fs: FactSheet, previous: PreviousTurn | None = None,
          client: LLMClient | None = None) -> Route:
    text = normalise(question)
    e = extract_entities(question, fs)
    category = _out_of_scope(question, text, fs, e)
    if category is not None:
        return Route(Intent.OUT_OF_SCOPE, 1.0, e, "rules", out_of_scope=category)
    scores = _scores(text, e)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (top_intent, top), (_, second) = ranked[0], ranked[1]
    conf = _confidence(top, second)
    named = {k.value: v for k, v in scores.items() if v}
    # follow-ups: "and in Turn 8?" keeps the previous intent; "why?" keeps the previous entity
    if previous is not None and ELLIPTICAL.search(text.strip()):
        keyword_score = {i: sum(w for p, w in PATTERNS.get(i, []) if p.search(text)) for i in SUPPORTED}
        if not any(keyword_score.values()) and e.segments and _accepts(previous.intent, e):
            return Route(previous.intent, 0.9, e, "follow_up", scores=named)
        if not e.segments and len(previous.entities.segments) == 1:
            intent = top_intent if top > 0 else previous.intent
            inherited = Entities(corners=list(previous.entities.corners), sectors=list(previous.entities.sectors),
                                 straights=list(previous.entities.straights), drivers=e.drivers)
            if intent is Intent.WHY_FASTER_IN_SECTOR and not inherited.sectors:
                intent = Intent.CORNER_DETAIL
            return Route(intent, 0.8, inherited, "follow_up", scores=named)
    if conf >= LOW_CONFIDENCE:
        return Route(top_intent, conf, e, "rules", scores=named)
    if client is not None:
        got = _classify_with_llm(question, client)
        if got is not None:
            return Route(got, conf, e, "llm", scores=named)
    if top > 0:
        return Route(top_intent, conf, e, "rules", scores=named)
    return Route(Intent.OUT_OF_SCOPE, 0.0, e, "default", out_of_scope="unclear", scores=named)


def _accepts(intent: Intent, e: Entities) -> bool:
    if e.sectors and not (e.corners or e.straights):
        return intent in (Intent.WHY_FASTER_IN_SECTOR, Intent.CAUSE_ATTRIBUTION)
    return intent in (Intent.CORNER_DETAIL, Intent.CAUSE_ATTRIBUTION)
