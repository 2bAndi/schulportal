"""Family calendar: storage, visibility, edit rights and form validation."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .. import db
from .auth import User

EVERYONE = "*"
PARENTS = "parents"
MAX_TITLE = 120
MAX_TEXT = 2000


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
    can_edit: bool = False

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


def _parse(value: str | None) -> date | datetime | None:
    if not value:
        return None
    return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)


def _row_to_event(row: sqlite3.Row, user: User) -> FamilyEvent:
    return FamilyEvent(
        id=row["id"], title=row["title"], start=_parse(row["start"]), end=_parse(row["end"]),
        all_day=bool(row["all_day"]), location=row["location"] or "", note=row["note"] or "",
        audience=db.loads(row["audience"], []), created_by=row["created_by"],
        created_by_name=row["created_by_name"] or "", created_at=row["created_at"], updated_at=row["updated_at"],
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


# --------------------------------------------------------------------------- queries


def list_events(conn: sqlite3.Connection, user: User, start: date, end: date) -> list[FamilyEvent]:
    """Events visible to `user` that overlap [start, end] (inclusive days)."""
    own = child_id_for(conn, user)
    rows = conn.execute(
        "SELECT * FROM family_events WHERE substr(start,1,10) <= ? AND substr(COALESCE(end, start),1,10) >= ? "
        "ORDER BY start, id", (end.isoformat(), start.isoformat()),
    ).fetchall()
    out = []
    for r in rows:
        if may_see(user, own, db.loads(r["audience"], []), r["created_by"]):
            out.append(_row_to_event(r, user))
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
              db.dumps(data["audience"]))
    if event_id is None:
        cur = conn.execute(
            "INSERT INTO family_events(title, start, end, all_day, location, note, audience, created_by, "
            "created_by_name, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (*values, user.id, user.display_name, now, now),
        )
        return int(cur.lastrowid)
    conn.execute(
        "UPDATE family_events SET title=?, start=?, end=?, all_day=?, location=?, note=?, audience=?, updated_at=? "
        "WHERE id=?", (*values, now, event_id),
    )
    return event_id


def delete_event(conn: sqlite3.Connection, event_id: int) -> None:
    conn.execute("DELETE FROM family_events WHERE id=?", (event_id,))


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
                "end_time": "", "location": "", "note": "",
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
    res.data = {"title": values["title"], "start": start_s, "end": end_s, "all_day": all_day,
                "location": values["location"], "note": values["note"], "audience": audience}
    return res


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())
