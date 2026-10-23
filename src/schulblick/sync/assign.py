"""Decide which child (or children) an item belongs to, and who may see it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..sph.parsers import CalendarEvent, MessageHeader, normalize_course

ALL = "*"


@dataclass
class ChildProfile:
    id: str
    first_name: str
    portal_name: str
    class_name: str | None
    course_norms: set[str] = field(default_factory=set)
    # timetable lesson codes per (weekday 0..4, hour number)
    lessons: dict[tuple[int, int], set[str]] = field(default_factory=dict)

    @property
    def branch(self) -> str | None:
        m = re.match(r"([GRH])", (self.class_name or "").upper())
        return m.group(1) if m else None

    @property
    def grade(self) -> int | None:
        m = re.search(r"(\d{1,2})", self.class_name or "")
        return int(m.group(1)) if m else None

    @property
    def letter(self) -> str | None:
        m = re.search(r"\d([a-z])$", (self.class_name or "").lower())
        return m.group(1) if m else None


# --------------------------------------------------------------------------- audience parsing


@dataclass(frozen=True)
class Audience:
    branch: str | None
    lo: int
    hi: int
    letter: str | None = None

    def matches(self, child: ChildProfile) -> bool:
        if child.grade is None:
            return False
        if not (self.lo <= child.grade <= self.hi):
            return False
        if self.branch and child.branch and self.branch != child.branch:
            return False
        if self.letter and child.letter and self.letter != child.letter:
            return False
        return True


_CLASS_TOKEN = re.compile(r"(?<![A-Za-z0-9])([GRH])\s?(\d{1,2})([a-d])?(?![0-9])")
_GRADE_WORDS = re.compile(
    r"(?:Jg\.?|JG|Jhg\.?|Jahrgang(?:sstufe)?|Jahrgänge|Klassenstufe|Klassen?)\s*(\d{1,2})"
    r"(?:\s*(?:[-–]|bis|und|\+|/)\s*(\d{1,2}))?",
    re.I,
)
_ORDINAL_CLASSES = re.compile(r"(\d{1,2})\.\s*(?:(?:und|/|-)\s*(\d{1,2})\.\s*)?(?:Klassen|Klasse|Jahrgang)", re.I)
_STAGES = {
    "unterstufe": (5, 7),
    "mittelstufe": (8, 10),
    "oberstufe": (11, 13),
    "abitur": (12, 13),
    "abiturient": (13, 13),
}
_PHASE = re.compile(r"(?<![A-Za-z0-9])([QE])(\d)(\d)?(?![0-9])")


def parse_audience(text: str) -> list[Audience]:
    """Extract grade/class audiences from free text. Empty list = whole school."""
    found: list[Audience] = []
    for branch, grade, letter in _CLASS_TOKEN.findall(text):
        g = int(grade)
        if 5 <= g <= 13:
            found.append(Audience(branch.upper(), g, g, letter.lower() or None))
    for lo, hi in _GRADE_WORDS.findall(text):
        a, b = int(lo), int(hi or lo)
        if 5 <= a <= 13:
            found.append(Audience(None, min(a, b), max(a, b)))
    for lo, hi in _ORDINAL_CLASSES.findall(text):
        a, b = int(lo), int(hi or lo)
        if 5 <= a <= 13:
            found.append(Audience(None, min(a, b), max(a, b)))
    lower = text.lower()
    for word, (lo, hi) in _STAGES.items():
        if word in lower:
            found.append(Audience(None, lo, hi))
    for phase, _a, _b in _PHASE.findall(text):
        found.append(Audience(None, 11, 11) if phase == "E" else Audience(None, 12, 13))
    return found


# --------------------------------------------------------------------------- calendar


def event_kind(ev: CalendarEvent) -> str:
    title = ev.title
    low = title.lower()
    desc = ev.description.lower()
    if ev.category == "12" or re.match(r"^[AB]-Woche", title):
        return "week"
    if ev.tool == "Klausurplaner" or ev.group:
        if re.search(r"lernkontrolle|test|vokabel", low):
            return "test"
        return "exam"
    if re.search(r"\barbeit$|klassenarbeit|klausur", low):
        return "exam"
    if re.search(r"lernstandserhebung|lernkontrolle", low):
        return "test"
    if ev.category == "6" or re.search(r"ferien|feiertag|ferientag|weihnachtsfeiertag|neujahr|ostermontag|pfingst", low):
        return "holiday"
    if "kein unterricht" in desc or "unterrichtsfrei" in desc:
        return "holiday"
    return "general"


def assign_event(ev: CalendarEvent, children: list[ChildProfile], hidden_categories: tuple[str, ...],
                 parent_only_keywords: tuple[str, ...]) -> tuple[str, list[str], bool]:
    """Return (kind, child_ids, parents_only)."""
    kind = event_kind(ev)
    if kind == "week":
        return kind, [ALL], False
    if kind == "holiday":
        return kind, [ALL], False

    if ev.group:
        norm = normalize_course(ev.group)
        ids = [c.id for c in children if norm in c.course_norms]
        if not ids:
            # fallback: the group names the class explicitly ("Englisch g5b (051E05-GYM)")
            ids = [c.id for c in children if c.class_name and
                   re.search(rf"(?<![a-z0-9]){re.escape(c.class_name.lower())}(?![a-z0-9])", ev.group.lower())]
        return kind, ids, False

    if ev.category in hidden_categories:
        return kind, [], False

    text = f"{ev.title}\n{ev.description}"
    parents_only = any(k.lower() in text.lower() for k in parent_only_keywords)
    audiences = parse_audience(text)
    if not audiences:
        return kind, [ALL], parents_only
    ids = [c.id for c in children if any(a.matches(c) for a in audiences)]
    return kind, ids, parents_only


# --------------------------------------------------------------------------- substitutions


def assign_substitution(row: dict, weekday: int, children: list[ChildProfile]) -> dict[str, str]:
    """Return {child_id: 'sure'|'maybe'} for the children affected by a plan row."""
    classes = {c.strip().upper() for c in re.split(r"[,;/]", row.get("classes", "")) if c.strip()}
    hours = _hour_range(row.get("hours", ""))
    subject = (row.get("subject") or "").strip()
    result: dict[str, str] = {}
    for child in children:
        if not child.class_name or child.class_name.upper() not in classes:
            continue
        codes: set[str] = set()
        for h in hours:
            codes |= child.lessons.get((weekday, h), set())
        if not child.lessons:
            result[child.id] = "maybe"
        elif subject and _code_match(subject, codes):
            # several parallel options in that slot (Religion/Ethik) -> still "sure" for a direct match
            result[child.id] = "sure"
        elif not subject and codes:
            result[child.id] = "sure"
        else:
            result[child.id] = "maybe"
    return result


def _code_match(subject: str, codes: set[str]) -> bool:
    s = subject.replace(" ", "").lower()
    return any(c.replace(" ", "").lower() == s for c in codes)


def _hour_range(text: str) -> list[int]:
    nums = [int(n) for n in re.findall(r"\d+", text or "")]
    if not nums:
        return []
    if len(nums) >= 2 and "-" in text:
        return list(range(nums[0], nums[-1] + 1))
    return nums


# --------------------------------------------------------------------------- messages

_STUDENT_WORDS = re.compile(r"lernende|sus\b|schüler|teilnehmer", re.I)
_SCHOOLWIDE = re.compile(r"^alle\b", re.I)
_LERNGRUPPE = re.compile(r"lerngruppe\s+(.+)$", re.I)


def assign_message(msg: MessageHeader, children: list[ChildProfile]) -> tuple[list[str], list[str]]:
    """Return (child_ids it concerns, child_ids allowed to see it). ['*'] = all children."""
    concerns: set[str] = set()
    kids: set[str] = set()

    for group in msg.groups:
        g = group.strip()
        low = g.lower()
        if "lehrkräfte" in low and not _STUDENT_WORDS.search(low) and "eltern" not in low:
            continue
        matched: set[str] = set()
        if _SCHOOLWIDE.match(g) and not re.search(r"klasse|lerngruppe|jahrgang|stufe", low):
            matched = {ALL}
        else:
            lg = _LERNGRUPPE.search(g)
            if lg:
                norm = normalize_course(lg.group(1))
                matched = {c.id for c in children if norm in c.course_norms}
            if not matched:
                matched = {c.id for c in children if c.class_name and
                           re.search(rf"(?<![a-z0-9]){re.escape(c.class_name.lower())}(?![a-z0-9])", low)}
            if not matched:
                auds = parse_audience(g)
                matched = {c.id for c in children if any(a.matches(c) for a in auds)}
        concerns |= matched
        if _STUDENT_WORDS.search(low):
            kids |= matched

    for name in msg.recipients:
        for c in children:
            if c.portal_name and name.strip().lower().startswith(c.portal_name.lower()):
                concerns.add(c.id)
                kids.add(c.id)

    def norm(ids: set[str]) -> list[str]:
        return [ALL] if ALL in ids else sorted(ids)

    return norm(concerns), norm(kids)
