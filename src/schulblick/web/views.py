"""Build the overview for one user (parent: all children, child: only themselves)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .. import db
from ..config import Settings
from ..sph.parsers import normalize_course
from . import family as fam
from .auth import User

WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
WEEKDAYS_LONG = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember"]

SUBJECTS = {
    "D": "Deutsch", "E": "Englisch", "M": "Mathe", "Frz": "Französisch", "F": "Französisch", "L": "Latein",
    "Bio": "Biologie", "Che": "Chemie", "Ch": "Chemie", "Phy": "Physik", "Ph": "Physik", "Geo": "Erdkunde",
    "Ek": "Erdkunde", "G": "Geschichte", "Ge": "Geschichte", "PoWi": "PoWi", "Ku": "Kunst", "Mu": "Musik",
    "Sp": "Sport", "Schw": "Schwimmen", "SoLe": "Soziales Lernen", "RK": "Religion kath.", "RE": "Religion ev.",
    "Eth": "Ethik", "Inf": "Informatik", "NaWi": "NaWi", "Na": "NaWi", "LRS": "LRS-Kurs", "Chor": "Chor",
    "LZ": "Lernzeit", "LZR": "Lernzeit", "DW": "Digitale Welt",
}
# timetable code prefix -> course name prefix, used to pick the child's own option in a slot
CHOICE_HINTS = {
    "RK": "religion - katholisch", "RE": "religion - evangelisch", "Eth": "ethik", "Frz": "französisch",
    "L": "latein", "Spa": "spanisch",
}


def _parse_dt(value: str | None) -> datetime | date | None:
    if not value:
        return None
    if len(value) == 10:
        return date.fromisoformat(value)
    return datetime.fromisoformat(value)


def _day_of(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def fmt_day(d: date, today: date) -> str:
    label = f"{WEEKDAYS[d.weekday()]} {d:%d.%m.}"
    if d == today:
        return f"Heute · {label}"
    if d == today + timedelta(days=1):
        return f"Morgen · {label}"
    return label


def fmt_short(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m.}"


def fmt_time(value: datetime | date | None) -> str:
    if isinstance(value, datetime):
        return f"{value:%H:%M}"
    return ""


def _ids(text: str | None) -> list[str]:
    return db.loads(text, []) or []


@dataclass
class Kid:
    id: str
    name: str
    slot: int
    class_name: str | None
    first_name: str


@dataclass
class Overview:
    user: User
    kids: list[Kid]
    kid_by_id: dict[str, Kid]
    today: date
    week: str | None
    new_items: list[dict] = field(default_factory=list)
    days: list[dict] = field(default_factory=list)
    homework: list[dict] = field(default_factory=list)
    exams: list[dict] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    family: list = field(default_factory=list)
    attendance: list[dict] = field(default_factory=list)
    timetables: list[dict] = field(default_factory=list)
    status: list[dict] = field(default_factory=list)
    portal_block: str | None = None
    last_sync: str | None = None
    show_attendance: bool = True
    first_visit: bool = False
    message_days: int = 5
    stale_weeks: int = 3


class OverviewBuilder:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, user: User, since: str | None,
                 now: datetime | None = None) -> None:
        self.conn = conn
        self.s = settings
        self.user = user
        self.since = since
        self.now = now or datetime.now(settings.tz)
        self.today = self.now.date()

    # -- scope -------------------------------------------------------------------------

    def kids(self) -> list[Kid]:
        out = []
        rows = self.conn.execute("SELECT * FROM children ORDER BY class_name DESC, id").fetchall()
        for idx, r in enumerate(rows):
            cfg = self.s.child_by_first_name(r["first_name"])
            out.append(Kid(id=r["id"], name=cfg.display_name if cfg else r["first_name"],
                           slot=cfg.slot if cfg else idx + 1, class_name=r["class_name"],
                           first_name=r["first_name"]))
        out.sort(key=lambda k: k.slot)
        if not self.user.is_parent:
            out = [k for k in out if k.first_name.lower() == (self.user.child_first_name or "").lower()]
        return out

    def _visible(self, ids: list[str], scope: set[str]) -> list[str]:
        if "*" in ids:
            return sorted(scope)
        return [i for i in ids if i in scope]

    def _is_new(self, *timestamps: str | None) -> bool:
        if not self.since:
            return False
        return any(t and t > self.since for t in timestamps)

    # -- build -------------------------------------------------------------------------

    def build(self) -> Overview:
        kids = self.kids()
        scope = {k.id for k in kids}
        ov = Overview(user=self.user, kids=kids, kid_by_id={k.id: k for k in kids}, today=self.today,
                      week=self._week(self.today), first_visit=self.since is None,
                      show_attendance=self.user.is_parent or self.s.kids_see_attendance,
                      message_days=self.s.message_window_days, stale_weeks=max(1, self.s.homework_stale_days // 7))
        if not kids:
            ov.status = self._status()
            ov.family = fam.list_events(self.conn, self.user, self.today,
                                        self.today + timedelta(days=self.s.upcoming_days))
            ov.days = self._upcoming(ov)
            return ov
        ov.homework = self._homework(scope)
        exams_all = self._events(scope, kinds=("exam", "test"), horizon=self.s.exams_horizon_days)
        ov.exams = exams_all
        ov.events = self._events(scope, kinds=("general", "holiday"), horizon=self.s.events_horizon_days)
        ov.plan = self._plan(scope)
        ov.family = fam.list_events(self.conn, self.user, self.today,
                                    self.today + timedelta(days=self.s.upcoming_days))
        ov.messages = self._messages(scope)
        if ov.show_attendance:
            ov.attendance = self._attendance(scope)
        ov.timetables = self._timetables(kids)
        ov.days = self._upcoming(ov)
        ov.new_items = self._new_items(ov)
        if self.user.is_parent:
            ov.status = self._status()
            row = self.conn.execute("SELECT value FROM meta WHERE key='portal_block'").fetchone()
            ov.portal_block = row["value"] if row else None
        row = self.conn.execute("SELECT MAX(finished_at) FROM sync_runs WHERE ok=1").fetchone()
        if row[0]:
            local = datetime.fromisoformat(row[0]).astimezone(self.s.tz)
            ov.last_sync = f"{local:%H:%M}" if local.date() == self.today else f"{local:%d.%m. %H:%M}"
        return ov

    def _week(self, day: date) -> str | None:
        row = self.conn.execute("SELECT week FROM plan_days WHERE day=?", (day.isoformat(),)).fetchone()
        if row and row["week"]:
            return row["week"]
        for r in self.conn.execute("SELECT title, start, end FROM events WHERE kind='week' AND active=1"):
            start, end = date.fromisoformat(r["start"][:10]), date.fromisoformat((r["end"] or r["start"])[:10])
            if start <= day < end:
                m = re.match(r"([AB])-Woche", r["title"])
                return m.group(1) if m else None
        return None

    def _homework(self, scope: set[str]) -> list[dict]:
        out = []
        stale = self.today - timedelta(days=self.s.homework_stale_days)
        rows = self.conn.execute(
            "SELECT * FROM entries WHERE active=1 AND homework IS NOT NULL AND homework != '' "
            "ORDER BY lesson_date DESC, course"
        ).fetchall()
        for r in rows:
            if r["child_id"] not in scope:
                continue
            d = date.fromisoformat(r["lesson_date"]) if r["lesson_date"] else None
            done = r["homework_done"] == 1
            if done and d and d < self.today - timedelta(days=3):
                continue
            out.append({
                "child_id": r["child_id"], "course": r["course"], "topic": r["topic"], "text": r["homework"],
                "date": d, "date_label": fmt_short(d) if d else "", "done": done,
                "stale": bool(d and d < stale and not done), "new": self._is_new(r["first_seen"], r["changed_at"]),
            })
        return out

    def _events(self, scope: set[str], kinds: tuple[str, ...], horizon: int) -> list[dict]:
        out = []
        start = self.today.isoformat()
        end = (self.today + timedelta(days=horizon)).isoformat()
        q = ",".join("?" * len(kinds))
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE active=1 AND kind IN ({q}) AND substr(COALESCE(end, start),1,10) >= ? "
            f"AND substr(start,1,10) <= ? ORDER BY start", (*kinds, start, end),
        ).fetchall()
        for r in rows:
            ids = self._visible(_ids(r["child_ids"]), scope)
            if not ids:
                continue
            if r["parents_only"] and not self.user.is_parent:
                continue
            st, en = _parse_dt(r["start"]), _parse_dt(r["end"])
            first_day = _day_of(st)
            last_day = _day_of(en) if en else first_day
            if r["all_day"] and en and isinstance(en, date) and not isinstance(en, datetime):
                last_day = last_day - timedelta(days=1)  # all-day end dates are exclusive
            if last_day < self.today:
                continue
            everyone = "*" in _ids(r["child_ids"])
            out.append({
                "id": r["id"], "title": r["title"], "kind": r["kind"], "child_ids": [] if everyone else ids,
                "everyone": everyone, "start": st, "end": en, "all_day": bool(r["all_day"]),
                "first_day": first_day, "last_day": last_day,
                "time": "" if r["all_day"] else fmt_time(st),
                "range": (f"{fmt_short(first_day)} – {fmt_short(last_day)}" if last_day != first_day else ""),
                "location": r["location"], "description": r["description"], "group": r["group_name"],
                "course": re.sub(r"\s*\([^)]*\)\s*$", "", r["group_name"] or "").strip(),
                "parents_only": bool(r["parents_only"]),
                "new": self._is_new(r["first_seen"], r["changed_at"]),
            })
        return out

    def _plan(self, scope: set[str]) -> list[dict]:
        days = []
        for d in self.conn.execute("SELECT * FROM plan_days WHERE day >= ? ORDER BY day",
                                   (self.today.isoformat(),)).fetchall():
            rows = []
            for r in self.conn.execute("SELECT * FROM substitutions WHERE day=? AND active=1 ORDER BY hours",
                                       (d["day"],)):
                certainty = db.loads(r["certainty"], {})
                ids = [i for i in certainty if i in scope]
                if not ids:
                    continue
                rows.append({
                    "hours": r["hours"], "classes": r["classes"], "kind": r["kind"], "subject": r["subject"],
                    "subject_long": SUBJECTS.get(r["subject"], ""), "room": r["room"], "teacher": r["teacher"],
                    "substitute": r["substitute"], "note": r["note"], "child_ids": ids,
                    "maybe": all(certainty[i] == "maybe" for i in ids),
                    "new": self._is_new(r["first_seen"]),
                })
            day = date.fromisoformat(d["day"])
            days.append({"day": day, "label": fmt_day(day, self.today), "week": d["week"], "rows": rows,
                         "infos": db.loads(d["infos"], [])})
        return days

    def _messages(self, scope: set[str]) -> list[dict]:
        since = (self.now - timedelta(days=self.s.message_window_days)).isoformat()
        out = []
        for r in self.conn.execute("SELECT * FROM messages WHERE sent_at >= ? ORDER BY sent_at DESC", (since,)):
            concerns = _ids(r["child_ids"])
            if self.user.is_parent:
                ids = self._visible(concerns, scope)
            else:
                ids = self._visible(_ids(r["kid_ids"]), scope)
                if not ids:
                    continue
            sent = datetime.fromisoformat(r["sent_at"])
            out.append({
                "id": r["id"], "subject": r["subject"], "sender": r["sender"], "sent": sent,
                "when": f"{fmt_short(sent.date())} {sent:%H:%M}", "groups": r["groups"],
                "recipients": r["recipients"] if self.user.is_parent else "",
                "body": r["body"], "child_ids": [] if "*" in concerns else ids, "everyone": "*" in concerns,
                "new": self._is_new(r["first_seen"], r["changed_at"]),
            })
        return out

    def _attendance(self, scope: set[str]) -> list[dict]:
        out = []
        for r in self.conn.execute("SELECT * FROM attendance ORDER BY child_id"):
            if r["child_id"] not in scope:
                continue
            data = db.loads(r["data"], {})
            totals = data.get("totals", {})
            occ = data.get("occurrences", {})
            late_key = next((k for k in totals if k.lower().startswith("verspätet")), None)
            out.append({
                "child_id": r["child_id"],
                "excused": totals.get("entschuldigt", 0),
                "missing": totals.get("fehlend", 0),
                "late_times": occ.get(late_key, 0) if late_key else 0,
                "late_minutes": data.get("minutes_late", 0),
                "open_excuses": data.get("open_excuses", 0),
                "rows": data.get("rows", []),
                "columns": data.get("columns", []),
                "new": self._is_new(r["changed_at"]),
            })
        return out

    def _timetables(self, kids: list[Kid]) -> list[dict]:
        out = []
        for kid in kids:
            row = self.conn.execute("SELECT * FROM timetables WHERE child_id=?", (kid.id,)).fetchone()
            if not row:
                continue
            data = db.loads(row["data"], {})
            norms = {r["norm"] for r in self.conn.execute("SELECT norm FROM courses WHERE child_id=?", (kid.id,))}
            rows = []
            for hour, cells in zip(data.get("hours", []), data.get("grid", [])):
                rendered = [self._cell(cell, norms) for cell in cells]
                rows.append({"hour": hour, "cells": rendered})
            # merge consecutive identical hours into double lessons for display
            merged: list[dict] = []
            for r in rows:
                if merged and [c["key"] for c in merged[-1]["cells"]] == [c["key"] for c in r["cells"]] \
                        and merged[-1]["hours"][-1]["no"] + 1 == r["hour"]["no"]:
                    merged[-1]["hours"].append(r["hour"])
                else:
                    merged.append({"hours": [r["hour"]], "cells": r["cells"]})
            out.append({"kid": kid, "days": [d[:2] for d in data.get("days", [])], "rows": merged,
                        "week": row["week"]})
        return out

    @staticmethod
    def _cell(cell: list[dict], norms: set[str]) -> dict:
        lessons = []
        for lesson in cell:
            code = lesson.get("code", "")
            prefix = re.match(r"[A-Za-zÄÖÜäöü]+", code)
            prefix_s = prefix.group(0) if prefix else code
            hint = CHOICE_HINTS.get(prefix_s)
            mine = bool(hint and any(n.startswith(hint) for n in norms))
            lessons.append({"code": code, "name": SUBJECTS.get(prefix_s, code), "room": lesson.get("room", ""),
                            "teacher": lesson.get("teacher", ""), "mine": mine})
        if len(lessons) > 1 and any(l["mine"] for l in lessons):
            lessons = [l for l in lessons if l["mine"]]
            options = False
        else:
            options = len(lessons) > 1
        key = "|".join(f"{l['code']}/{l['room']}" for l in lessons)
        return {"lessons": lessons, "options": options, "key": key}

    def _upcoming(self, ov: Overview) -> list[dict]:
        horizon = self.today + timedelta(days=self.s.upcoming_days)
        buckets: dict[date, list[dict]] = {}

        def add(day: date, item: dict) -> None:
            if self.today <= day <= horizon:
                buckets.setdefault(day, []).append(item)

        for e in ov.exams:
            add(e["first_day"], {"type": e["kind"], "time": e["time"], "title": _exam_title(e["title"]),
                                 "child_ids": e["child_ids"], "note": "", "new": e["new"]})
        for e in ov.events:
            day = max(e["first_day"], self.today)
            if e["kind"] == "holiday" and e["first_day"] < self.today:
                day = self.today
            add(day, {"type": e["kind"], "time": e["time"], "title": e["title"], "child_ids": e["child_ids"],
                      "note": e["range"] or e["location"] or "", "new": e["new"]})
        for ev in ov.family:
            rng = f"bis {fmt_short(ev.last_day)}" if ev.last_day != ev.first_day else ""
            add(max(ev.first_day, self.today), {
                "type": "family", "time": ev.time_label, "title": ev.title,
                "child_ids": [] if ev.everyone or ev.parents_only else ev.child_ids,
                "parents_only": ev.parents_only,
                "note": " · ".join(x for x in (rng, ev.location) if x),
                "new": self._is_new(ev.created_at, ev.updated_at) and ev.created_by != self.user.id})
        for d in ov.plan:
            for r in d["rows"]:
                title = f"{r['subject_long'] or r['subject']}: {r['kind']}"
                note = f"{r['hours']}. Std." + (f" · {r['room']}" if r["room"] else "")
                if r["maybe"]:
                    note += " · falls belegt"
                add(d["day"], {"type": "plan", "time": "", "title": title, "child_ids": r["child_ids"],
                               "note": note, "new": r["new"]})
        days = []
        for day in sorted(buckets):
            items = sorted(buckets[day], key=lambda i: (i["time"] or "99", i["type"]))
            days.append({"day": day, "label": fmt_day(day, self.today), "week": self._week(day), "items": items})
        return days

    def _new_items(self, ov: Overview) -> list[dict]:
        if not self.since:
            return []
        items = []
        for m in ov.messages:
            if m["new"]:
                items.append({"type": "message", "title": m["subject"], "child_ids": m["child_ids"],
                              "note": m["sender"], "anchor": "nachrichten"})
        for h in ov.homework:
            if h["new"] and not h["done"]:
                items.append({"type": "homework", "title": f"{h['course']}: {h['text'][:80]}",
                              "child_ids": [h["child_id"]], "note": "Hausaufgabe", "anchor": "hausaufgaben"})
        for e in ov.exams:
            if e["new"]:
                items.append({"type": e["kind"], "title": _exam_title(e["title"]), "child_ids": e["child_ids"],
                              "note": fmt_short(e["first_day"]), "anchor": "arbeiten"})
        for d in ov.plan:
            for r in d["rows"]:
                if r["new"]:
                    items.append({"type": "plan", "title": f"{r['subject_long'] or r['subject']}: {r['kind']}",
                                  "child_ids": r["child_ids"], "note": d["label"], "anchor": "vertretungsplan"})
        seen_series: set[int] = set()
        for ev in ov.family:
            if ev.id in seen_series:
                continue
            seen_series.add(ev.id)
            if ev.created_by != self.user.id and self._is_new(ev.created_at, ev.updated_at):
                items.append({"type": "family", "title": ev.title,
                              "child_ids": [] if ev.everyone or ev.parents_only else ev.child_ids,
                              "note": f"{fmt_short(ev.first_day)} · von {ev.created_by_name}", "anchor": "tage"})
        for a in ov.attendance:
            if a["new"]:
                items.append({"type": "attendance", "title": "Anwesenheit aktualisiert", "child_ids": [a["child_id"]],
                              "note": "", "anchor": "anwesenheit"})
        return items[:15]

    def _status(self) -> list[dict]:
        out = []
        for area, label in (("lessons", "Mein Unterricht"), ("substitutions", "Vertretungsplan"),
                            ("calendar", "Kalender"), ("messages", "Nachrichten"), ("timetables", "Stundenpläne")):
            row = self.conn.execute(
                "SELECT * FROM sync_runs WHERE area=? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (area,)
            ).fetchone()
            last_ok = self.conn.execute(
                "SELECT finished_at FROM sync_runs WHERE area=? AND ok=1 ORDER BY id DESC LIMIT 1", (area,)
            ).fetchone()
            out.append({
                "area": label,
                "ok": bool(row and row["ok"]),
                "message": row["message"] if row else "noch nie abgerufen",
                "last_ok": _ago(last_ok[0], self.now) if last_ok else "nie",
            })
        return out


def _exam_title(title: str) -> str:
    m = re.match(r"^(.*?)\s+(Arbeit|Lernkontrolle|Klausur|Test)$", title)
    if not m:
        return title
    course = re.sub(r"\s*\([^)]*\)", "", m.group(1))
    course = re.sub(r"\s+[gGrR]\d{1,2}[a-d]?$", "", course).strip()
    return f"{m.group(2)} {course}"


def _ago(iso: str | None, now: datetime) -> str:
    if not iso:
        return "nie"
    then = datetime.fromisoformat(iso)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 1:
        return "gerade eben"
    if minutes < 60:
        return f"vor {minutes} Min."
    hours = minutes // 60
    if hours < 48:
        return f"vor {hours} Std."
    return f"vor {hours // 24} Tagen"


def course_short(name: str) -> str:
    return re.sub(r"\s+[gGrR]\d{1,2}[a-d]?\b.*$", "", normalize_course(name)).strip().title()
