"""Calendar page (family entries + read-only school items) and the per-person ICS feed."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

from .. import db
from ..config import Settings
from . import family
from .auth import User, _row_to_user
from .views import WEEKDAYS, WEEKDAYS_LONG, MONTHS, OverviewBuilder, _exam_title, fmt_short

AGENDA_DAYS = 28
ICS_PAST_DAYS = 30
ICS_FUTURE_DAYS = 365


# --------------------------------------------------------------------------- shared collection


class _Collector:
    """Collects family and school items visible to one user for a date range."""

    def __init__(self, conn: sqlite3.Connection, settings: Settings, user: User, start: date, days: int) -> None:
        self.conn, self.s, self.user = conn, settings, user
        self.start, self.end = start, start + timedelta(days=days - 1)
        self.builder = OverviewBuilder(conn, settings, user, since=None,
                                       now=datetime.combine(start, time(0), settings.tz))
        self.kids = self.builder.kids()
        self.scope = {k.id for k in self.kids}
        self.kid_by_id = {k.id: k for k in self.kids}

    def family(self) -> list[family.FamilyEvent]:
        return family.list_events(self.conn, self.user, self.start, self.end)

    def school(self) -> list[dict]:
        if not self.scope:
            return []
        horizon = (self.end - self.start).days
        return self.builder._events(self.scope, ("exam", "test", "holiday", "general"), horizon=horizon)

    def plan(self) -> list[dict]:
        if not self.scope:
            return []
        return [d for d in self.builder._plan(self.scope) if self.start <= d["day"] <= self.end]

    def hour_times(self, child_id: str) -> dict[int, tuple[str, str]]:
        row = self.conn.execute("SELECT data FROM timetables WHERE child_id=?", (child_id,)).fetchone()
        if not row:
            return {}
        return {h["no"]: (h.get("start", ""), h.get("end", "")) for h in db.loads(row["data"], {}).get("hours", [])
                if h.get("start")}


def _hours(text: str) -> list[int]:
    nums = [int(n) for n in re.findall(r"\d+", text or "")]
    if len(nums) >= 2 and "-" in (text or ""):
        return list(range(nums[0], nums[-1] + 1))
    return nums


# --------------------------------------------------------------------------- agenda page


def build_agenda(conn: sqlite3.Connection, settings: Settings, user: User, start: date,
                 today: date | None = None) -> dict:
    today = today or datetime.now(settings.tz).date()
    c = _Collector(conn, settings, user, start, AGENDA_DAYS)
    buckets: dict[date, list[dict]] = {}

    def add(day: date, item: dict) -> None:
        day = max(day, c.start)
        if day <= c.end:
            buckets.setdefault(day, []).append(item)

    for ev in c.family():
        rng = (f"{fmt_short(ev.first_day)} – {fmt_short(ev.last_day)}" if ev.last_day != ev.first_day else "")
        add(ev.first_day, {
            "kind": "family", "time": ev.time_label, "title": ev.title, "child_ids": ev.child_ids,
            "everyone": ev.everyone, "parents_only": ev.parents_only,
            "note": " · ".join(x for x in (rng, ev.location) if x), "detail": ev.note,
            "by": ev.created_by_name, "edit_url": ev.edit_url if ev.can_edit else None,
            "repeat": ev.repeat_label, "sort": ev.time_label or "00:00",
        })
    for e in c.school():
        title = e["title"]
        if e["kind"] in ("exam", "test"):  # the tag already says "Arbeit"/"Lernkontrolle"
            title = re.sub(r"^(Arbeit|Lernkontrolle|Klausur|Test)\s+", "", _exam_title(e["title"]))
        add(e["first_day"], {
            "kind": e["kind"], "time": e["time"], "title": title, "child_ids": e["child_ids"],
            "everyone": e["everyone"], "parents_only": e["parents_only"],
            "note": " · ".join(x for x in (e["range"], e["location"]) if x), "detail": "",
            "by": "Schule", "edit_url": None, "repeat": "", "sort": e["time"] or "00:00",
        })
    for d in c.plan():
        for r in d["rows"]:
            note = f"{r['hours']}. Std." + (f" · {r['room']}" if r["room"] else "")
            if r["maybe"]:
                note += " · falls belegt"
            add(d["day"], {
                "kind": "plan", "time": "", "title": f"{r['subject_long'] or r['subject']}: {r['kind']}",
                "child_ids": r["child_ids"], "everyone": False, "parents_only": False, "note": note, "detail": "",
                "by": "Vertretungsplan", "edit_url": None, "repeat": "", "sort": "00:01",
            })

    days = []
    for i in range(AGENDA_DAYS):
        day = c.start + timedelta(days=i)
        items = sorted(buckets.get(day, []), key=lambda it: (it["sort"], it["kind"] != "holiday", it["title"]))
        days.append({
            "day": day, "weekday": WEEKDAYS[day.weekday()], "weekday_long": WEEKDAYS_LONG[day.weekday()],
            "label": f"{day:%d.%m.}", "is_today": day == today, "is_weekend": day.weekday() >= 5,
            "monday": day.weekday() == 0, "week": c.builder._week(day) if day.weekday() == 0 or i == 0 else None,
            "items": items,
        })
    title = (f"{c.start.day}. {MONTHS[c.start.month - 1]} – {c.end.day}. {MONTHS[c.end.month - 1]} {c.end.year}")
    return {
        "days": days, "kids": c.kids, "kid_by_id": c.kid_by_id, "title": title,
        "prev": (c.start - timedelta(days=AGENDA_DAYS)).isoformat(),
        "next": (c.start + timedelta(days=AGENDA_DAYS)).isoformat(),
        "today": today, "start": c.start,
        "own_child_id": family.child_id_for(conn, user),
    }


# --------------------------------------------------------------------------- ICS tokens


def ics_token(conn: sqlite3.Connection, user: User, rotate: bool = False) -> str:
    row = conn.execute("SELECT ics_token FROM users WHERE id=?", (user.id,)).fetchone()
    token = row["ics_token"] if row else None
    if token and not rotate:
        return token
    token = secrets.token_urlsafe(24)
    conn.execute("UPDATE users SET ics_token=? WHERE id=?", (token, user.id))
    return token


def user_for_ics_token(conn: sqlite3.Connection, token: str) -> User | None:
    if not token or len(token) < 20:
        return None
    row = conn.execute("SELECT * FROM users WHERE ics_token=?", (token,)).fetchone()
    return _row_to_user(row) if row else None


# --------------------------------------------------------------------------- ICS feed


def _esc(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, chunk = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > (75 if not parts else 74):
            parts.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += b
    parts.append(chunk.decode("utf-8"))
    return "\r\n ".join(parts)


def _utc(dt: datetime, tz) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _vevent(uid: str, summary: str, start: date | datetime, end: date | datetime | None, tz, *,
            location: str = "", description: str = "", stamp: str) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}"]
    if isinstance(start, datetime):
        end = end if isinstance(end, datetime) and end > start else start + timedelta(hours=1)
        lines += [f"DTSTART:{_utc(start, tz)}", f"DTEND:{_utc(end, tz)}"]
    else:
        last = end if isinstance(end, date) and not isinstance(end, datetime) and end >= start else start
        lines += [f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{last + timedelta(days=1):%Y%m%d}",
                  "TRANSP:TRANSPARENT"]
    lines.append(f"SUMMARY:{_esc(summary)}")
    if location:
        lines.append(f"LOCATION:{_esc(location)}")
    if description:
        lines.append(f"DESCRIPTION:{_esc(description)}")
    lines.append("END:VEVENT")
    return lines


def build_ics(conn: sqlite3.Connection, settings: Settings, user: User, today: date | None = None) -> str:
    tz = settings.tz
    today = today or datetime.now(tz).date()
    start = today - timedelta(days=ICS_PAST_DAYS)
    c = _Collector(conn, settings, user, start, ICS_PAST_DAYS + ICS_FUTURE_DAYS)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    multi = len(c.kids) > 1

    def prefix(child_ids: list[str], everyone: bool = False, parents_only: bool = False) -> str:
        if not multi or everyone:
            return ""
        if parents_only:
            return "[Eltern] "
        names = [c.kid_by_id[i].name for i in child_ids if i in c.kid_by_id]
        return f"[{', '.join(names)}] " if names else ""

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Schulblick//Familienkalender//DE", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_esc('Schulblick · ' + user.display_name)}",
             "X-WR-TIMEZONE:Europe/Berlin", "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]

    for ev in c.family():
        desc = "\n".join(x for x in (ev.note, f"Eingetragen von {ev.created_by_name}" if ev.created_by_name else "") if x)
        uid = f"family-{ev.id}" + (f"-{ev.occurrence:%Y%m%d}" if ev.occurrence else "") + "@schulblick"
        lines += _vevent(uid, prefix(ev.child_ids, ev.everyone, ev.parents_only) + ev.title,
                         ev.start, ev.end, tz, location=ev.location, description=desc, stamp=stamp)

    for e in c.school():
        title = _exam_title(e["title"]) if e["kind"] in ("exam", "test") else e["title"]
        start_v = e["start"] if not e["all_day"] else e["first_day"]
        end_v = e["end"] if not e["all_day"] else e["last_day"]
        uid = "school-" + hashlib.sha1(str(e["id"]).encode()).hexdigest()[:16] + "@schulblick"
        lines += _vevent(uid, prefix(e["child_ids"], e["everyone"]) + title, start_v, end_v, tz,
                         location=e["location"] or "", description=(e["description"] or "") + "\n(Schulkalender)",
                         stamp=stamp)

    for d in c.plan():
        for r in d["rows"]:
            title = f"{r['subject_long'] or r['subject']}: {r['kind']}"
            times = c.hour_times(r["child_ids"][0]) if r["child_ids"] else {}
            hours = [h for h in _hours(r["hours"]) if h in times]
            if hours:
                s_t = time.fromisoformat(times[hours[0]][0])
                e_t = time.fromisoformat(times[hours[-1]][1]) if times[hours[-1]][1] else None
                start_v = datetime.combine(d["day"], s_t)
                end_v = datetime.combine(d["day"], e_t) if e_t else None
            else:
                start_v, end_v = d["day"], None
            note = f"{r['hours']}. Stunde" + (f", Raum {r['room']}" if r["room"] else "")
            if r["substitute"]:
                note += f", Vertretung: {r['substitute']}"
            if r["maybe"]:
                note += " (falls belegt)"
            uid = "plan-" + hashlib.sha1(f"{d['day']}|{r['hours']}|{r['subject']}|{r['kind']}".encode()).hexdigest()[:16]
            lines += _vevent(uid + "@schulblick", prefix(r["child_ids"]) + title, start_v, end_v, tz,
                             location=r["room"] or "", description=note + "\n(Vertretungsplan)", stamp=stamp)

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
