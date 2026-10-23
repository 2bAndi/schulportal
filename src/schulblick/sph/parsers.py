"""Pure parsers for the parent views of Schulportal Hessen.

All functions take raw HTML/JSON and return plain dataclasses. They never
talk to the network, so they can be tested against fixtures.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

_BR = re.compile(r"<br\s*/?>", re.I)
_BLOCK_END = re.compile(r"</(p|div|li|tr|h[1-6])>", re.I)
_SPACES = re.compile(r"[ \t ]+")


# --------------------------------------------------------------------------- helpers


def html_to_text(fragment: str | None) -> str:
    if not fragment:
        return ""
    fragment = _BR.sub("\n", fragment)
    fragment = _BLOCK_END.sub(lambda m: m.group(0) + "\n", fragment)
    text = HTMLParser(f"<div>{fragment}</div>").body.text(deep=True, separator="")
    text = html_lib.unescape(text)
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def node_text(node: Node | None) -> str:
    if node is None:
        return ""
    return html_to_text(node.html)


def _prepared(html: str) -> HTMLParser:
    return HTMLParser(html)


def parse_de_date(text: str) -> date | None:
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text or "")
    if not m:
        return None
    d, mth, y = (int(x) for x in m.groups())
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def normalize_course(name: str) -> str:
    """'Religion - katholisch 8 (g8a, g8d) (081RKA01-GYM)' -> 'religion - katholisch 8'."""
    name = re.sub(r"\([^)]*\)", " ", name or "")
    return _SPACES.sub(" ", name).strip().lower()


_TEACHER_TITLE = re.compile(r"^(?P<name>.*?)\s*\((?P<code>[^)]+)\)\s*$")


# --------------------------------------------------------------------------- Mein Unterricht


@dataclass
class LessonEntry:
    book_id: str
    entry_id: str
    course: str
    teacher_code: str
    teacher_name: str
    open_excuses: int
    topic: str
    lesson_date: date | None
    content: str
    homework: str | None
    homework_done: bool | None


@dataclass
class Course:
    name: str
    teacher_code: str = ""
    teacher_name: str = ""
    book_id: str = ""


@dataclass
class Attendance:
    columns: list[str]
    rows: list[dict]  # {"course", "teacher", "values": {column: text}}
    totals: dict[str, int]
    occurrences: dict[str, int]
    minutes_late: int


@dataclass
class ChildLessons:
    pupil_id: str
    portal_name: str
    entries: list[LessonEntry] = field(default_factory=list)
    courses: list[Course] = field(default_factory=list)
    attendance: Attendance | None = None

    @property
    def first_name(self) -> str:
        return first_name_from_portal(self.portal_name)


def first_name_from_portal(portal_name: str) -> str:
    """'Muster, Lena Marie' -> 'Lena'."""
    after = portal_name.split(",", 1)[-1].strip()
    return after.split()[0] if after else portal_name.strip()


def _teacher(cell: Node) -> tuple[str, str]:
    btn = cell.css_first("span.teacher button, .teacher [title], span.label[title]")
    if btn is None:
        return "", ""
    title = btn.attributes.get("title") or ""
    m = _TEACHER_TITLE.match(title.strip())
    if m:
        return m.group("code").strip(), m.group("name").strip()
    return node_text(btn).strip(), ""


def parse_lessons(html: str) -> list[ChildLessons]:
    tree = _prepared(html)
    children: list[ChildLessons] = []
    for pane in tree.css("div.tab-pane"):
        pane_id = pane.attributes.get("id") or ""
        m = re.fullmatch(r"p(\d+)", pane_id)
        if not m:
            continue
        pupil_id = m.group(1)
        heading = pane.css_first(".pupilbox h2") or pane.css_first("h2")
        child = ChildLessons(pupil_id=pupil_id, portal_name=node_text(heading))

        courses: dict[str, Course] = {}
        for row in pane.css(f"#aktuell{pupil_id} table tbody tr"):
            cells = row.css("td")
            if len(cells) < 2:
                continue
            course = node_text(cells[0].css_first(".name")) or node_text(cells[0].css_first("h3"))
            code, tname = _teacher(cells[0])
            badge = cells[0].css_first(".badge")
            excuses = int(re.sub(r"\D", "", node_text(badge)) or 0) if badge else 0
            body = cells[1]
            hw_node = body.css_first(".homework")
            homework = None
            done = None
            if hw_node is not None:
                homework = node_text(hw_node.css_first(".realHomework")) or ""
                status = node_text(hw_node.css_first(".done")).lower()
                done = "erledigt" in status
            entry = LessonEntry(
                book_id=row.attributes.get("data-book") or "",
                entry_id=row.attributes.get("data-entry") or "",
                course=course,
                teacher_code=code,
                teacher_name=tname,
                open_excuses=excuses,
                topic=node_text(body.css_first(".thema")),
                lesson_date=parse_de_date(node_text(body.css_first(".datum"))),
                content=node_text(body.css_first(".inhalt .markup")) or node_text(body.css_first(".inhalt")),
                homework=homework,
                homework_done=done,
            )
            child.entries.append(entry)
            courses.setdefault(normalize_course(course), Course(course, code, tname, entry.book_id))

        # Kursmappen: course folders, also those without a recent entry
        for folder in pane.css(f"#mappen{pupil_id} h2"):
            name = node_text(folder)
            if name and normalize_course(name) not in courses:
                courses[normalize_course(name)] = Course(name)

        child.courses = list(courses.values())
        child.attendance = _parse_attendance(pane.css_first(f"#anwesend{pupil_id} table"))
        children.append(child)
    return children


_LEADING_INT = re.compile(r"^\s*(\d+)")
_OCCURRENCES = re.compile(r"\((\d+)x")
_MINUTES = re.compile(r"insg\.\s*(?:(\d+)\s*h\s*)?(\d+)?\s*min")


def _parse_attendance(table: Node | None) -> Attendance | None:
    if table is None:
        return None
    columns = [node_text(th) for th in table.css("thead th")]
    if not columns:
        return None
    value_cols = columns[2:]
    rows = []
    totals = {c: 0 for c in value_cols}
    occurrences = {c: 0 for c in value_cols}
    minutes_late = 0
    for tr in table.css("tbody tr"):
        cells = tr.css("td")
        if len(cells) < 2:
            continue
        for hidden in tr.css(".hidden"):
            hidden.decompose()
        values = {}
        for col, cell in zip(value_cols, cells[2:]):
            text = node_text(cell)
            values[col] = text
            m = _LEADING_INT.match(text)
            if m:
                totals[col] += int(m.group(1))
            o = _OCCURRENCES.search(text)
            if o:
                occurrences[col] += int(o.group(1))
            if col.lower().startswith("verspätet"):
                mm = _MINUTES.search(text)
                if mm:
                    minutes_late += int(mm.group(1) or 0) * 60 + int(mm.group(2) or 0)
        rows.append({"course": node_text(cells[0]), "teacher": node_text(cells[1]), "values": values})
    return Attendance(columns=value_cols, rows=rows, totals=totals, occurrences=occurrences,
                      minutes_late=minutes_late)


# --------------------------------------------------------------------------- Vertretungsplan

_SUB_COLUMNS = {
    "stunde": "hours",
    "klasse": "classes",
    "vertretung": "substitute",
    "lehrkraft": "teacher",
    "lehrer": "teacher",
    "art": "kind",
    "fach": "subject",
    "raum": "room",
    "hinweis": "note",
    "hinweis2": "note2",
    "(lehrer)": "teacher",
    "(fach)": "old_subject",
    "(raum)": "old_room",
}
_DEFAULT_SUB_ORDER = ["", "Stunde", "Klasse", "Vertretung", "Lehrkraft", "Art", "Fach", "Raum", "Hinweis"]


@dataclass
class SubstitutionDay:
    day: date
    week: str | None
    infos: list[dict]
    rows: list[dict]


def parse_substitutions(html: str) -> tuple[list[str], list[SubstitutionDay]]:
    tree = _prepared(html)
    classes: list[str] = []
    h1 = tree.css_first("#content h1") or tree.css_first("h1")
    if h1 is not None:
        m = re.search(r"für\s+(.+)$", node_text(h1))
        if m:
            classes = [c.strip() for c in m.group(1).split(",") if c.strip()]

    days: list[SubstitutionDay] = []
    for panel in tree.css("div[id^=tag]"):
        m = re.fullmatch(r"tag(\d{2})_(\d{2})_(\d{4})", panel.attributes.get("id") or "")
        if not m:
            continue
        d, mth, y = (int(x) for x in m.groups())
        week_node = panel.css_first(".woche")
        week = None
        if week_node is not None:
            wm = re.search(r"([A-Z])-Woche", node_text(week_node))
            week = wm.group(1) if wm else None

        infos: list[dict] = []
        for table in panel.css("table.infos"):
            current = None
            for tr in table.css("tr"):
                text = node_text(tr)
                if not text:
                    continue
                if "subheader" in (tr.attributes.get("class") or ""):
                    current = {"title": text, "text": ""}
                    infos.append(current)
                elif current is not None:
                    current["text"] = (current["text"] + "\n" + text).strip()
                else:
                    infos.append({"title": "", "text": text})

        rows: list[dict] = []
        table = panel.css_first("table[id^=vtable]")
        if table is not None:
            headers = [node_text(th) for th in table.css("thead th")] or _DEFAULT_SUB_ORDER
            keys = [_SUB_COLUMNS.get(h.strip().lower(), h.strip().lower() or "_") for h in headers]
            for tr in table.css("tbody tr"):
                cells = tr.css("td")
                if len(cells) < 3:
                    continue
                row = {k: node_text(c) for k, c in zip(keys, cells) if k != "_"}
                if not any(row.get(k) for k in ("hours", "classes", "subject")):
                    continue
                rows.append(row)
        days.append(SubstitutionDay(day=date(y, mth, d), week=week, infos=infos, rows=rows))
    return classes, days


# --------------------------------------------------------------------------- Stundenplan


@dataclass
class TimetableLink:
    pupil_id: str
    class_name: str
    href: str
    portal_name: str


def parse_timetable_links(html: str) -> list[TimetableLink]:
    tree = _prepared(html)
    links: list[TimetableLink] = []
    seen = set()
    for a in tree.css("a"):
        href = a.attributes.get("href") or ""
        if "detail_klasse" not in href:
            continue
        qs = parse_qs(urlparse(href).query)
        pupil = (qs.get("p") or [""])[0]
        klass = (qs.get("k") or [""])[0]
        if not pupil or pupil in seen:
            continue
        seen.add(pupil)
        name = re.sub(r"\s*\([^)]*\)\s*$", "", node_text(a))
        links.append(TimetableLink(pupil_id=pupil, class_name=klass, href=href, portal_name=name))
    return links


@dataclass
class Lesson:
    code: str
    room: str
    teacher: str
    title: str


@dataclass
class Timetable:
    week: str | None
    days: list[str]
    hours: list[dict]  # {"no": 1, "label": "1. Stunde", "start": "07:45", "end": "08:30"}
    grid: list[list[list[Lesson]]]  # grid[hour_index][day_index] -> lessons


def _lesson(div: Node) -> Lesson:
    code = node_text(div.css_first("b"))
    teacher = node_text(div.css_first("small"))
    full = node_text(div)
    rest = full.replace(code, "", 1)
    if teacher:
        rest = rest.rsplit(teacher, 1)[0]
    room = " ".join(rest.split())
    return Lesson(code=code, room=room, teacher=teacher, title=(div.attributes.get("title") or "").strip())


def parse_timetable(html: str, table_selector: str = "#all table") -> Timetable | None:
    tree = _prepared(html)
    table = tree.css_first(table_selector)
    if table is None:
        return None
    week_node = tree.css_first("#aktuelleWoche")
    week = node_text(week_node) or None
    days = [node_text(th) for th in table.css("thead th")][1:]
    body_rows = [tr for tr in table.css("tbody tr")]
    hours: list[dict] = []
    grid: list[list[list[Lesson]]] = []
    pending: dict[int, int] = {}  # day index -> remaining rowspan rows
    for tr in body_rows:
        cells = tr.css("td")
        if not cells:
            continue
        label_cell = cells[0]
        label = node_text(label_cell)
        if not label:
            continue  # leading empty row
        no_match = re.search(r"(\d+)\.", label)
        time_match = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", label)
        hours.append({
            "no": int(no_match.group(1)) if no_match else len(hours) + 1,
            "start": time_match.group(1) if time_match else "",
            "end": time_match.group(2) if time_match else "",
        })
        row: list[list[Lesson]] = [[] for _ in days]
        cell_iter = iter(cells[1:])
        for day_idx in range(len(days)):
            if pending.get(day_idx, 0) > 0:
                pending[day_idx] -= 1
                row[day_idx] = list(grid[-1][day_idx]) if grid else []
                continue
            cell = next(cell_iter, None)
            if cell is None:
                break
            span = int(cell.attributes.get("rowspan") or 1)
            if span > 1:
                pending[day_idx] = span - 1
            row[day_idx] = [_lesson(div) for div in cell.css("div.stunde")]
        grid.append(row)
    return Timetable(week=week, days=days, hours=hours, grid=grid)


# --------------------------------------------------------------------------- Kalender


@dataclass
class CalendarEvent:
    id: str
    title: str
    start: str
    end: str | None
    all_day: bool
    category: str
    location: str
    description: str
    group: str
    tool: str


def parse_calendar(events: list[dict]) -> list[CalendarEvent]:
    out = []
    for e in events:
        group = e.get("Lerngruppe")
        group_name = group.get("Name", "") if isinstance(group, dict) else ""
        out.append(CalendarEvent(
            id=str(e.get("Id") or e.get("id") or f"{e.get('title')}-{e.get('start')}"),
            title=html_lib.unescape(str(e.get("title") or "")).strip(),
            start=str(e.get("start") or ""),
            end=str(e.get("end")) if e.get("end") else None,
            all_day=bool(e.get("allDay")),
            category=str(e.get("category") or ""),
            location=html_lib.unescape(str(e.get("Ort") or "")).strip(),
            description=html_to_text(str(e.get("description") or "")),
            group=html_lib.unescape(group_name),
            tool=str(e.get("_Tool") or ""),
        ))
    return out


# --------------------------------------------------------------------------- Nachrichten


@dataclass
class MessageHeader:
    id: str
    subject: str
    sender: str
    when: str  # raw portal date
    unread: bool
    groups: list[str]
    recipients: list[str]
    hidden: bool


def _spans_or_text(fragment: str) -> list[str]:
    if not fragment:
        return []
    tree = HTMLParser(f"<div>{fragment}</div>")
    spans = [node_text(s) for s in tree.css("span")]
    spans = [s for s in spans if s]
    if spans:
        return spans
    text = node_text(tree.body)
    return [t.strip() for t in re.split(r"\s{3,}|\n", text) if t.strip()]


def parse_message_headers(rows: list[dict]) -> list[MessageHeader]:
    out = []
    for r in rows:
        sender = html_to_text(str(r.get("SenderName") or "")).strip(" ,")
        recipients: list[str] = []
        for item in r.get("empf") or []:
            recipients.extend(_spans_or_text(str(item)))
        out.append(MessageHeader(
            id=str(r.get("Uniquid") or ""),
            subject=html_lib.unescape(str(r.get("Betreff") or "")).strip(),
            sender=sender,
            when=str(r.get("Datum") or ""),
            unread=str(r.get("unread")) == "1",
            groups=_spans_or_text(str(r.get("WeitereEmpfaenger") or "")),
            recipients=[x.lstrip("+- ").strip() for x in recipients if x.strip()],
            hidden=str(r.get("Papierkorb") or "") == "ja",
        ))
    return out


def parse_message_body(conversation: dict) -> str:
    parts = [html_to_text(str(conversation.get("Inhalt") or ""))]
    for reply in conversation.get("reply") or []:
        author = html_to_text(str(reply.get("username") or "")).strip(" ,")
        when = str(reply.get("Datum") or "")
        parts.append(f"— Antwort {author} ({when}):\n{html_to_text(str(reply.get('Inhalt') or ''))}")
    return "\n\n".join(p for p in parts if p)


def iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None
