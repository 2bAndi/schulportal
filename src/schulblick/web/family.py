"""Family calendar: storage, recurrence, visibility, edit rights and form validation."""

from __future__ import annotations

import calendar as _cal
import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta

from .. import db
from .auth import User

EVERYONE = "*"
PARENTS = "parents"
MAX_TITLE = 120
MAX_TEXT = 2000

REPEATS = {
    "none": "nicht wiederholen",
    "daily": "täglich",
    "weekly": "wöchentlich",
    "biweekly": "alle 2 Wochen",
    "monthly": "monatlich",
    "yearly": "jährlich",
}
MAX_OCCURRENCES = 2000  # safety net per expansion


def child_id_for(conn: sqlite3.Connection, user: User) -> str | None:
    """Portal pupil id of a child account, None for parents or unknown children."""
    if user.is_parent or not user.child_first_name:
        return None
    row = conn.execute("SELECT id FROM children WHERE lower(first_name)=lower(?)", (user.child_first_name,)).fetchone()
    return row["id"] if row else None


# --------------------------------------------------------------------------- model


@dataclass
class FamilyEvent:
    id: int
    title: str
    start: date | datetime
    end: date | datetime | None
    all_day: bool
    location: str
    note: str
    audience: list[str]
    created_by: int | None
    created_by_name: str
    created_at: str
    updated_at: str
    repeat: str = "none"
    repeat_until: date | None = None
    exdates: list[str] = field(default_factory=list)
    skip_holidays: bool = False
    can_edit: bool = False
    occurrence: date | None = None  # set on expanded occurrences of a series

    @property
    def first_day(self) -> date:
        return self.start.date() if isinstance(self.start, datetime) else self.start

    @property
    def last_day(self) -> date:
        if self.end is None:
            return self.first_day
        return self.end.date() if isinstance(self.end, datetime) else self.end

    @property
    def time_label(self) -> str:
        if self.all_day or not isinstance(self.start, datetime):
            return ""
        if isinstance(self.end, datetime) and self.end.date() == self.start.date():
            return f"{self.start:%H:%M}–{self.end:%H:%M}"
        return f"{self.start:%H:%M}"

    @property
    def everyone(self) -> bool:
        return EVERYONE in self.audience

    @property
    def parents_only(self) -> bool:
        return self.audience == [PARENTS]

    @property
    def child_ids(self) -> list[str]:
        return [a for a in self.audience if a not in (EVERYONE, PARENTS)]

    @property
    def is_series(self) -> bool:
        return self.repeat != "none"

    @property
    def repeat_label(self) -> str:
        if not self.is_series:
            return ""
        label = REPEATS.get(self.repeat, self.repeat)
        if self.repeat_until:
            label += f" bis {self.repeat_until:%d.%m.%Y}"
        if self.skip_holidays:
            label += ", nicht in den Ferien"
        return label

    @property
    def edit_url(self) -> str:
        return f"/kalender/{self.id}" + (f"?am={self.occurrence.isoformat()}" if self.occurrence else "")


def _parse(value: str | None) -> date | datetime | None:
    if not value:
        return None
    return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)


def _row_to_event(row: sqlite3.Row, user: User) -> FamilyEvent:
    keys = row.keys()
    return FamilyEvent(
        id=row["id"], title=row["title"], start=_parse(row["start"]), end=_parse(row["end"]),
        all_day=bool(row["all_day"]), location=row["location"] or "", note=row["note"] or "",
        audience=db.loads(row["audience"], []), created_by=row["created_by"],
        created_by_name=row["created_by_name"] or "", created_at=row["created_at"], updated_at=row["updated_at"],
        repeat=(row["repeat"] if "repeat" in keys else None) or "none",
        repeat_until=date.fromisoformat(row["repeat_until"]) if "repeat_until" in keys and row["repeat_until"] else None,
        exdates=db.loads(row["exdates"], []) if "exdates" in keys else [],
        skip_holidays=bool(row["skip_holidays"]) if "skip_holidays" in keys else False,
        can_edit=may_edit(user, row["created_by"]),
    )


# --------------------------------------------------------------------------- rights


def may_see(user: User, own_child_id: str | None, audience: list[str], created_by: int | None) -> bool:
    if user.is_parent:
        return True
    if created_by is not None and created_by == user.id:
        return True
    if EVERYONE in audience:
        return True
    return own_child_id is not None and own_child_id in audience


def may_edit(user: User, created_by: int | None) -> bool:
    return user.is_parent or (created_by is not None and created_by == user.id)


# --------------------------------------------------------------------------- recurrence


def _add_months(d: date, months: int) -> date | None:
    """Same day of month `months` later; None if that day does not exist (e.g. 31 April)."""
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    if d.day > _cal.monthrange(y, m)[1]:
        return None
    return date(y, m, d.day)


def occurrence_dates(ev: FamilyEvent, start: date, end: date, no_school: set[date] | None = None) -> list[date]:
    """First days of all occurrences of `ev` that overlap [start, end]."""
    first = ev.first_day
    span = (ev.last_day - first).days
    if not ev.is_series:
        return [first] if first <= end and first + timedelta(days=span) >= start else []
    until = ev.repeat_until or date.max
    skipped = set(ev.exdates)
    out: list[date] = []

    if ev.repeat in ("daily", "weekly", "biweekly"):
        step = {"daily": 1, "weekly": 7, "biweekly": 14}[ev.repeat]
        # jump close to the window instead of iterating from the first occurrence
        k = max(0, ((start - first).days - span) // step)
        candidates = (first + timedelta(days=step * (k + i)) for i in range(MAX_OCCURRENCES))
    else:
        months = 1 if ev.repeat == "monthly" else 12
        k = max(0, ((start.year - first.year) * 12 + start.month - first.month - 1) // months)
        candidates = (_add_months(first, months * (k + i)) for i in range(MAX_OCCURRENCES))

    for d in candidates:
        if d is None:
            continue
        if d > end or d > until:
            break
        if d + timedelta(days=span) < start or d.isoformat() in skipped:
            continue
        if ev.skip_holidays and no_school and d in no_school:
            continue
        out.append(d)
    return out


def _shift(ev: FamilyEvent, day: date) -> FamilyEvent:
    delta = day - ev.first_day
    return replace(ev, start=ev.start + delta, end=(ev.end + delta) if ev.end is not None else None,
                   occurrence=day if ev.is_series else None)


def no_school_days(conn: sqlite3.Connection, start: date, end: date) -> set[date]:
    """Days covered by school holidays and other days without lessons (from the school calendar)."""
    days: set[date] = set()
    rows = conn.execute(
        "SELECT start, end, all_day FROM events WHERE active=1 AND kind='holiday' "
        "AND substr(start,1,10) <= ? AND substr(COALESCE(end, start),1,10) >= ?",
        (end.isoformat(), start.isoformat()),
    )
    for r in rows:
        s = date.fromisoformat(r["start"][:10])
        e = date.fromisoformat((r["end"] or r["start"])[:10])
        if not r["all_day"] or e == s:
            e = e + timedelta(days=1)  # treat as covering its own day
        d = max(s, start)
        while d < e and d <= end:
            days.add(d)
            d += timedelta(days=1)
    return days


# --------------------------------------------------------------------------- queries


def list_events(conn: sqlite3.Connection, user: User, start: date, end: date) -> list[FamilyEvent]:
    """Occurrences visible to `user` that overlap [start, end] (inclusive days), sorted by start."""
    own = child_id_for(conn, user)
    rows = conn.execute(
        "SELECT * FROM family_events WHERE substr(start,1,10) <= ? AND ("
        "  (repeat = 'none' AND substr(COALESCE(end, start),1,10) >= ?)"
        "  OR (repeat != 'none' AND (repeat_until IS NULL OR repeat_until >= ?))"
        ") ORDER BY start, id",
        (end.isoformat(), start.isoformat(), (start - timedelta(days=366)).isoformat()),
    ).fetchall()
    holidays: set[date] | None = None
    out: list[FamilyEvent] = []
    for r in rows:
        if not may_see(user, own, db.loads(r["audience"], []), r["created_by"]):
            continue
        ev = _row_to_event(r, user)
        if ev.skip_holidays and holidays is None:
            holidays = no_school_days(conn, start - timedelta(days=31), end)
        for day in occurrence_dates(ev, start, end, holidays):
            out.append(_shift(ev, day))
    out.sort(key=lambda e: (e.first_day, e.time_label or "", e.title))
    return out


def get_event(conn: sqlite3.Connection, user: User, event_id: int) -> FamilyEvent | None:
    row = conn.execute("SELECT * FROM family_events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        return None
    if not may_see(user, child_id_for(conn, user), db.loads(row["audience"], []), row["created_by"]):
        return None
    return _row_to_event(row, user)


def save_event(conn: sqlite3.Connection, user: User, data: dict, event_id: int | None = None) -> int:
    now = db.utcnow()
    values = (data["title"], data["start"], data["end"], int(data["all_day"]), data["location"], data["note"],
              db.dumps(data["audience"]), data.get("repeat", "none"), data.get("repeat_until"),
              int(bool(data.get("skip_holidays"))))
    if event_id is None:
        cur = conn.execute(
            "INSERT INTO family_events(title, start, end, all_day, location, note, audience, repeat, repeat_until, "
            "skip_holidays, exdates, created_by, created_by_name, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'[]',?,?,?,?)",
            (*values, user.id, user.display_name, now, now),
        )
        return int(cur.lastrowid)
    conn.execute(
        "UPDATE family_events SET title=?, start=?, end=?, all_day=?, location=?, note=?, audience=?, repeat=?, "
        "repeat_until=?, skip_holidays=?, updated_at=? WHERE id=?", (*values, now, event_id),
    )
    return event_id


def delete_event(conn: sqlite3.Connection, event_id: int) -> None:
    conn.execute("DELETE FROM family_events WHERE id=?", (event_id,))


def cancel_occurrence(conn: sqlite3.Connection, event_id: int, day: date) -> None:
    row = conn.execute("SELECT exdates FROM family_events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        return
    dates = set(db.loads(row["exdates"], []))
    dates.add(day.isoformat())
    conn.execute("UPDATE family_events SET exdates=?, updated_at=? WHERE id=?",
                 (db.dumps(sorted(dates)), db.utcnow(), event_id))


# --------------------------------------------------------------------------- form handling


@dataclass
class FormResult:
    values: dict
    errors: list[str] = field(default_factory=list)
    data: dict | None = None  # normalised data ready for save_event


_TIME = re.compile(r"^\d{1,2}:\d{2}$")


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _time(value: str) -> time | None:
    value = value.strip()
    if not _TIME.match(value):
        return None
    h, m = (int(x) for x in value.split(":"))
    if h > 23 or m > 59:
        return None
    return time(h, m)


def form_values(event: FamilyEvent | None, default_day: date | None, own_child_id: str | None,
                is_parent: bool) -> dict:
    """Initial values for the form (new or existing event)."""
    if event is None:
        return {"title": "", "date": (default_day or date.today()).isoformat(), "start_time": "", "end_date": "",
                "end_time": "", "location": "", "note": "", "repeat": "none", "repeat_until": "",
                "skip_holidays": False,
                "audience": [EVERYONE] if is_parent else ([own_child_id] if own_child_id else [EVERYONE])}
    start, end = event.start, event.end
    return {
        "title": event.title,
        "date": event.first_day.isoformat(),
        "start_time": "" if event.all_day else f"{start:%H:%M}",
        "end_date": "" if event.last_day == event.first_day else event.last_day.isoformat(),
        "end_time": f"{end:%H:%M}" if isinstance(end, datetime) and not event.all_day else "",
        "location": event.location,
        "note": event.note,
        "repeat": event.repeat,
        "repeat_until": event.repeat_until.isoformat() if event.repeat_until else "",
        "skip_holidays": event.skip_holidays,
        "audience": event.audience,
    }


def parse_form(form: dict, user: User, own_child_id: str | None, valid_child_ids: set[str]) -> FormResult:
    values = {
        "title": (form.get("title") or "").strip(),
        "date": (form.get("date") or "").strip(),
        "start_time": (form.get("start_time") or "").strip(),
        "end_date": (form.get("end_date") or "").strip(),
        "end_time": (form.get("end_time") or "").strip(),
        "location": (form.get("location") or "").strip(),
        "note": (form.get("note") or "").strip(),
        "repeat": (form.get("repeat") or "none").strip(),
        "repeat_until": (form.get("repeat_until") or "").strip(),
        "skip_holidays": bool(form.get("skip_holidays")),
        "audience": form.get("audience") or [],
    }
    if isinstance(values["audience"], str):
        values["audience"] = [values["audience"]]
    res = FormResult(values=values)
    err = res.errors.append

    if not values["title"]:
        err("Bitte einen Titel eingeben.")
    elif len(values["title"]) > MAX_TITLE:
        err(f"Der Titel darf höchstens {MAX_TITLE} Zeichen haben.")
    if len(values["location"]) > 200 or len(values["note"]) > MAX_TEXT:
        err("Ort oder Notiz sind zu lang.")

    day = _date(values["date"])
    if day is None:
        err("Bitte ein gültiges Datum wählen.")
    end_day = _date(values["end_date"]) if values["end_date"] else day
    if values["end_date"] and end_day is None:
        err("Das Enddatum ist ungültig.")
    start_t = _time(values["start_time"]) if values["start_time"] else None
    end_t = _time(values["end_time"]) if values["end_time"] else None
    if values["start_time"] and start_t is None:
        err("Die Startzeit ist ungültig (Format 14:30).")
    if values["end_time"] and end_t is None:
        err("Die Endzeit ist ungültig (Format 16:00).")
    if end_t and not start_t and not values["start_time"]:
        err("Eine Endzeit geht nur zusammen mit einer Startzeit.")

    repeat = values["repeat"] if values["repeat"] in REPEATS else None
    if repeat is None:
        err("Bitte eine gültige Wiederholung wählen.")
    until = _date(values["repeat_until"]) if values["repeat_until"] else None
    if values["repeat_until"] and until is None:
        err("Das Datum „Wiederholen bis“ ist ungültig.")
    if repeat and repeat != "none" and until and day and until < day:
        err("„Wiederholen bis“ liegt vor dem ersten Termin.")
    if repeat and repeat != "none" and day and end_day:
        max_span = {"daily": 0, "weekly": 6, "biweekly": 13, "monthly": 27, "yearly": 364}[repeat]
        if (end_day - day).days > max_span:
            err("Ein einzelner Termin dauert länger als der Abstand der Wiederholung.")

    # audience
    raw = [a for a in values["audience"] if a]
    if user.is_parent:
        if EVERYONE in raw:
            audience = [EVERYONE]
        elif PARENTS in raw and not [a for a in raw if a != PARENTS]:
            audience = [PARENTS]
        else:
            audience = sorted({a for a in raw if a in valid_child_ids})
        if not audience:
            err("Bitte auswählen, für wen der Termin ist.")
    else:
        audience = [EVERYONE] if EVERYONE in raw else ([own_child_id] if own_child_id else [])
        if not audience:
            err("Dein Konto ist noch keinem Kind im Schulportal zugeordnet.")

    if res.errors or day is None or end_day is None:
        return res

    all_day = start_t is None
    if all_day:
        start_s, end_s = day.isoformat(), (end_day.isoformat() if end_day != day else None)
        if end_day < day:
            err("Das Ende liegt vor dem Beginn.")
    else:
        start_dt = datetime.combine(day, start_t)
        if end_t is not None:
            end_dt = datetime.combine(end_day, end_t)
        elif end_day != day:
            end_dt = datetime.combine(end_day, start_t)
        else:
            end_dt = None
        if end_dt is not None and end_dt <= start_dt:
            err("Das Ende liegt vor dem Beginn.")
        start_s = start_dt.isoformat(timespec="minutes")
        end_s = end_dt.isoformat(timespec="minutes") if end_dt else None
    if res.errors:
        return res
    is_series = repeat != "none"
    res.data = {"title": values["title"], "start": start_s, "end": end_s, "all_day": all_day,
                "location": values["location"], "note": values["note"], "audience": audience,
                "repeat": repeat, "repeat_until": until.isoformat() if is_series and until else None,
                "skip_holidays": is_series and values["skip_holidays"]}
    return res


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())
