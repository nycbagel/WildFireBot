"""Rule-based place and date extraction from a mention and the post it replies to."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

MONTHS = {
    name: index
    for index, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}
WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}  # fmt: skip
NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple": 2, "few": 3,
}  # fmt: skip

_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_DAY = "|".join(sorted((d for d in WEEKDAYS), key=len, reverse=True))
_NUM = r"\d{1,2}|" + "|".join(NUMBER_WORDS)
_ORD = r"(?:st|nd|rd|th)?"

PATTERNS: dict[str, re.Pattern[str]] = {
    "relative": re.compile(rf"\bin (?:a )?({_NUM})(?: or \w+)? (day|days|week|weeks)\b", re.I),
    "keyword": re.compile(r"\b(today|tonight|tomorrow)\b", re.I),
    "weekend": re.compile(r"\b(this|next|the) weekend\b|\bweekend\b", re.I),
    "weekday": re.compile(rf"\b(?:(this|next|on)\s+)?({_DAY})\b\.?", re.I),
    "month_day": re.compile(
        rf"\b({_MONTH})\.?\s+(\d{{1,2}}){_ORD}(?:\s*(?:-|–|to|through)\s*(\d{{1,2}}){_ORD})?\b", re.I
    ),
    "day_month": re.compile(
        rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH})\b\.?(?:\s*(?:-|–|to)\s*(\d{{1,2}}){_ORD})?", re.I
    ),
    "numeric": re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b"),
}  # fmt: skip


@dataclass(frozen=True)
class When:
    start: datetime
    end: datetime
    label: str
    span: tuple[int, int]


def _day_start(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _explicit(
    now: datetime, month: int, day: int, last_day: int | None
) -> tuple[datetime, datetime]:
    year = now.year
    for _ in range(2):
        try:
            start = datetime(year, month, day, tzinfo=UTC)
            end = datetime(year, month, last_day or day, tzinfo=UTC) + timedelta(days=1)
        except ValueError:
            raise
        if end >= now:
            break
        year += 1
    return start, end


def find_when(text: str, now: datetime) -> When | None:
    """The earliest date phrase in `text` as a UTC window, or None. Windows are UTC."""
    best: When | None = None
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            try:
                found = _build(kind, match, now)
            except ValueError:
                continue
            if found is not None and (best is None or found.span[0] < best.span[0]):
                best = found
    return best


def _build(kind: str, match: re.Match[str], now: datetime) -> When | None:
    span = match.span()
    today = _day_start(now)
    if kind == "relative":
        raw = match.group(1).lower()
        count = int(raw) if raw.isdigit() else NUMBER_WORDS[raw]
        count *= 7 if match.group(2).lower().startswith("week") else 1
        center = now + timedelta(days=count)
        label = f"in {count} day{'s' if count != 1 else ''}"
        return When(
            max(now, center - timedelta(hours=12)), center + timedelta(hours=12), label, span
        )
    if kind == "keyword":
        word = match.group(1).lower()
        if word == "today":
            return When(now, today + timedelta(days=1), "today", span)
        if word == "tonight":
            return When(now, now + timedelta(hours=12), "tonight", span)
        center = now + timedelta(days=1)
        return When(
            max(now, center - timedelta(hours=12)), center + timedelta(hours=12), "tomorrow", span
        )
    if kind == "weekend":
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        if today.weekday() >= 5:
            saturday = today - timedelta(days=today.weekday() - 5)
        word = (match.group(1) or "").lower()
        label = "this weekend"
        if word == "next":
            saturday += timedelta(days=7)
            label = "next weekend"
        return When(max(now, saturday), saturday + timedelta(days=2), label, span)
    if kind == "weekday":
        index = WEEKDAYS[match.group(2).lower()]
        if match.group(2).lower() in ("sat", "sun", "mon", "wed", "may") and not match.group(1):
            return None  # too ambiguous without "on", "this" or "next"
        ahead = (index - today.weekday()) % 7
        if (match.group(1) or "").lower() == "next":
            ahead += 7 if ahead < 7 else 0
        start = today + timedelta(days=ahead)
        return When(max(now, start), start + timedelta(days=1), match.group(2).capitalize(), span)
    if kind == "month_day":
        month, day = MONTHS[match.group(1).lower()], int(match.group(2))
        last = int(match.group(3)) if match.group(3) else None
    elif kind == "day_month":
        day, month = int(match.group(1)), MONTHS[match.group(2).lower()]
        last = int(match.group(3)) if match.group(3) else None
    else:
        month, day, last = int(match.group(1)), int(match.group(2)), None
        if not 1 <= month <= 12:
            return None
    if last is not None and last < day:
        last = None
    start, end = _explicit(now, month, day, last)
    label = f"{start:%b} {day}" + (f"-{last}" if last else "")
    return When(max(now, start), end, label, span)


def strip_when(text: str) -> str:
    out = text
    for pattern in PATTERNS.values():
        out = pattern.sub(" | ", out)
    return out


# ---------------------------------------------------------------- places

STOP = frozenset(
    """a an the this that these those next last on in at for from with and but or so because
    since when while then to is are was be been being will would can could should may might
    i im i'm ive we our my your you it its he she they them us me soon please hope hoping
    excited thanks thank also just really very too again before after during until by about
    through over under around near toward towards if not no yes lol omg rn tho though
    see go get have do make take visit hike camp drive fly come stay run ride bike swim ski
    watch meet there here where how what who why which morning evening afternoon night
    day days week weeks weekend month year hello hi hey hiya help ok okay thoughts thought any anyone safe
    smoke fire fires wildfire wildfires aqi air quality status update advice""".split()
)
STRONG = re.compile(
    r"\b(?:trip|travel(?:l?ing)?|going|go|goes|heading|headed|head|driving|drive|flying|fly|"
    r"visiting|visit|camping|camp|hiking|hike|vacation(?:ing)?|getaway|roadtrip|road trip|"
    r"skiing|ski|surfing|biking|backpacking|trek(?:king)?|moving|moves|relocating|staying|"
    r"stay|arriving|landing|race|wedding|festival|concert|weekend)\s+(?:out\s+)?"
    r"(?:to|in|at|near|around|into|for)\s+(?:the\s+)?",
    re.I,
)
WEAK = re.compile(r"\b(?:to|in|at|near|around|visiting|visit|into)\s+(?:the\s+)?", re.I)
WORD = re.compile(r"[A-Za-z][A-Za-z0-9'’.\-]*")
SUFFIXES = (
    " national park", " national forest", " state park", " national monument", " park",
    " national recreation area", " wilderness",
)  # fmt: skip


def clean(text: str) -> str:
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"(?<!\w)@\w+", " ", text)
    text = re.sub(r"#(\w+)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _take(rest: str, require_capital: bool) -> str | None:
    words: list[str] = []
    for token in rest.split(" "):
        match = WORD.fullmatch(token.rstrip(",.;:!?)\"'"))
        if match is None or token.startswith("|") or token == "|":
            break
        word = match.group(0).rstrip(".")
        if word.lower() in STOP and not (words and word.lower() in {"of", "de", "la", "del"}):
            break
        words.append(word)
        if len(words) == 4 or token[-1] in ",.;:!?)":
            break
    if not words:
        return None
    if require_capital and not words[0][0].isupper():
        return None
    return " ".join(words)


def place_candidates(*texts: str) -> list[str]:
    """Place-name guesses, best first, from the mention then the parent post."""
    found: list[str] = []

    def add(value: str | None) -> None:
        if value and value.lower() not in {c.lower() for c in found}:
            found.append(value)

    for index, raw in enumerate(texts):
        text = strip_when(clean(raw))
        for pattern, capital in ((STRONG, False), (WEAK, True)):
            for match in pattern.finditer(text):
                add(_take(text[match.end() :], capital))
        # a bare "Yosemite this weekend?" or "Yosemite?" after the @handle
        leading = re.split(r"[|?!.,;:]", text)[0].strip()
        if index == 0 and leading and len(leading.split()) <= 4 and not found:
            add(_take(leading, True))
    return found


def search_names(candidate: str) -> list[str]:
    """Names to try on the geocoder: the candidate, then without 'National Park' and the like."""
    names = [candidate]
    lowered = candidate.lower()
    for suffix in SUFFIXES:
        if lowered.endswith(suffix) and len(candidate) > len(suffix):
            names.append(candidate[: -len(suffix)])
            break
    return names


@dataclass(frozen=True)
class Parsed:
    candidates: list[str]
    when: When | None


def parse_mention(mention: str, parent: str | None, now: datetime) -> Parsed:
    texts = [mention] + ([parent] if parent else [])
    when = None
    for text in texts:
        when = find_when(clean(text), now)
        if when is not None:
            break
    return Parsed(place_candidates(*texts), when)
