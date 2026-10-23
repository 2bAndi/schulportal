"""One synchronisation pass per area: fetch, parse, assign, upsert."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Callable

from .. import db
from ..config import Settings
from ..sph import parsers as P
from ..sph.client import LoginLocked, PortalDown, SPHClient, WrongCredentials, parse_portal_datetime
from .assign import ChildProfile, assign_event, assign_message, assign_substitution

log = logging.getLogger(__name__)


def _hash(*parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def load_profiles(conn: sqlite3.Connection) -> list[ChildProfile]:
    profiles: dict[str, ChildProfile] = {}
    for r in conn.execute("SELECT * FROM children ORDER BY id"):
        profiles[r["id"]] = ChildProfile(id=r["id"], first_name=r["first_name"], portal_name=r["portal_name"],
                                         class_name=r["class_name"])
    for r in conn.execute("SELECT child_id, norm FROM courses"):
        if r["child_id"] in profiles:
            profiles[r["child_id"]].course_norms.add(r["norm"])
    for r in conn.execute("SELECT child_id, data FROM timetables"):
        p = profiles.get(r["child_id"])
        if not p:
            continue
        data = db.loads(r["data"], {})
        for h_idx, hour in enumerate(data.get("hours", [])):
            for d_idx, cell in enumerate(data.get("grid", [])[h_idx] if h_idx < len(data.get("grid", [])) else []):
                codes = {lesson["code"] for lesson in cell if lesson.get("code")}
                if codes:
                    p.lessons[(d_idx, hour["no"])] = codes
    return list(profiles.values())


class Syncer:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, client: SPHClient) -> None:
        self.s = settings
        self.conn = conn
        self.client = client

    # -- bookkeeping -------------------------------------------------------------

    def run_area(self, area: str, fn: Callable[[], str]) -> bool:
        cur = self.conn.execute("INSERT INTO sync_runs(area, started_at) VALUES(?, ?)", (area, db.utcnow()))
        run_id = cur.lastrowid
        try:
            message = fn() or "ok"
            ok = True
        except Exception as exc:  # noqa: BLE001 - recorded and re-raised for auth problems
            log.exception("sync of %s failed", area)
            message, ok = f"{type(exc).__name__}: {exc}", False
            self.conn.execute("UPDATE sync_runs SET finished_at=?, ok=0, message=? WHERE id=?",
                              (db.utcnow(), message[:500], run_id))
            raise
        self.conn.execute("UPDATE sync_runs SET finished_at=?, ok=?, message=? WHERE id=?",
                          (db.utcnow(), int(ok), message[:500], run_id))
        return ok

    def now_local(self) -> datetime:
        return datetime.now(self.s.tz)

    # -- Mein Unterricht -------------------------------------------------------------

    def sync_lessons(self) -> str:
        children = P.parse_lessons(self.client.lessons_html())
        if not children:
            raise ValueError("no children found on meinunterricht.php – layout changed?")
        now = db.utcnow()
        n_entries = 0
        with db.transaction(self.conn):
            for child in children:
                self.conn.execute(
                    "INSERT INTO children(id, portal_name, first_name, updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET portal_name=excluded.portal_name, first_name=excluded.first_name, "
                    "updated_at=excluded.updated_at",
                    (child.pupil_id, child.portal_name, child.first_name, now),
                )
                for course in child.courses:
                    excuses = sum(e.open_excuses for e in child.entries if e.course == course.name)
                    self.conn.execute(
                        "INSERT INTO courses(child_id, book_id, name, norm, teacher_code, teacher_name, open_excuses, "
                        "updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(child_id, book_id) DO UPDATE SET "
                        "name=excluded.name, norm=excluded.norm, teacher_code=excluded.teacher_code, "
                        "teacher_name=excluded.teacher_name, open_excuses=excluded.open_excuses, "
                        "updated_at=excluded.updated_at",
                        (child.pupil_id, course.book_id or f"name:{P.normalize_course(course.name)}", course.name,
                         P.normalize_course(course.name), course.teacher_code, course.teacher_name, excuses, now),
                    )
                seen_ids = []
                for e in child.entries:
                    entry_id = f"{child.pupil_id}:{e.book_id}:{e.entry_id}"
                    seen_ids.append(entry_id)
                    h = _hash(e.topic, e.lesson_date, e.content, e.homework, e.homework_done)
                    row = self.conn.execute("SELECT hash FROM entries WHERE id=?", (entry_id,)).fetchone()
                    if row is None:
                        self.conn.execute(
                            "INSERT INTO entries(id, child_id, course, teacher_code, topic, lesson_date, content, "
                            "homework, homework_done, hash, active, first_seen, changed_at, last_seen) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                            (entry_id, child.pupil_id, e.course, e.teacher_code, e.topic, P.iso(e.lesson_date),
                             e.content, e.homework, None if e.homework_done is None else int(e.homework_done),
                             h, now, now, now),
                        )
                    else:
                        changed = row["hash"] != h
                        self.conn.execute(
                            "UPDATE entries SET course=?, teacher_code=?, topic=?, lesson_date=?, content=?, homework=?, "
                            "homework_done=?, hash=?, active=1, last_seen=?, changed_at=CASE WHEN ? THEN ? "
                            "ELSE changed_at END WHERE id=?",
                            (e.course, e.teacher_code, e.topic, P.iso(e.lesson_date), e.content, e.homework,
                             None if e.homework_done is None else int(e.homework_done), h, now, int(changed), now,
                             entry_id),
                        )
                    n_entries += 1
                placeholders = ",".join("?" * len(seen_ids)) or "''"
                self.conn.execute(
                    f"UPDATE entries SET active=0 WHERE child_id=? AND id NOT IN ({placeholders})",
                    (child.pupil_id, *seen_ids),
                )
                if child.attendance is not None:
                    data = {
                        "columns": child.attendance.columns,
                        "rows": child.attendance.rows,
                        "totals": child.attendance.totals,
                        "occurrences": child.attendance.occurrences,
                        "minutes_late": child.attendance.minutes_late,
                        "open_excuses": sum(e.open_excuses for e in child.entries),
                    }
                    h = _hash(db.dumps(data["totals"]))
                    row = self.conn.execute("SELECT hash FROM attendance WHERE child_id=?", (child.pupil_id,)).fetchone()
                    changed_at = now if row is None or row["hash"] != h else None
                    self.conn.execute(
                        "INSERT INTO attendance(child_id, data, hash, changed_at, updated_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(child_id) DO UPDATE SET data=excluded.data, hash=excluded.hash, "
                        "updated_at=excluded.updated_at, changed_at=COALESCE(?, attendance.changed_at)",
                        (child.pupil_id, db.dumps(data), h, changed_at or now, now, changed_at),
                    )
            # forget homework entries that disappeared long ago
            cutoff = (datetime.now() - timedelta(days=60)).isoformat()
            self.conn.execute("DELETE FROM entries WHERE active=0 AND last_seen < ?", (cutoff,))
        return f"{len(children)} children, {n_entries} entries"

    # -- Stundenplan -----------------------------------------------------------------

    def sync_timetables(self) -> str:
        links = P.parse_timetable_links(self.client.timetable_overview_html())
        now = db.utcnow()
        count = 0
        for link in links:
            table = P.parse_timetable(self.client.timetable_class_html(link.href))
            with db.transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO children(id, portal_name, first_name, class_name, updated_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET class_name=excluded.class_name, updated_at=excluded.updated_at",
                    (link.pupil_id, link.portal_name, P.first_name_from_portal(link.portal_name), link.class_name, now),
                )
                if table is not None:
                    data = {
                        "days": table.days,
                        "hours": table.hours,
                        "grid": [[[vars(lesson) for lesson in cell] for cell in row] for row in table.grid],
                    }
                    self.conn.execute(
                        "INSERT INTO timetables(child_id, week, data, updated_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(child_id) DO UPDATE SET week=excluded.week, data=excluded.data, "
                        "updated_at=excluded.updated_at",
                        (link.pupil_id, table.week, db.dumps(data), now),
                    )
                    count += 1
        return f"{count} timetables"

    # -- Vertretungsplan -------------------------------------------------------------

    def sync_substitutions(self) -> str:
        _classes, days = P.parse_substitutions(self.client.substitutions_html())
        profiles = load_profiles(self.conn)
        now = db.utcnow()
        n = 0
        with db.transaction(self.conn):
            for day in days:
                self.conn.execute(
                    "INSERT INTO plan_days(day, week, infos, updated_at) VALUES(?,?,?,?) ON CONFLICT(day) DO UPDATE "
                    "SET week=excluded.week, infos=excluded.infos, updated_at=excluded.updated_at",
                    (day.day.isoformat(), day.week, db.dumps(day.infos), now),
                )
                ids = []
                for row in day.rows:
                    certainty = assign_substitution(row, day.day.weekday(), profiles)
                    row_id = _hash(day.day, row.get("hours"), row.get("classes"), row.get("subject"),
                                   row.get("kind"), row.get("teacher"), row.get("substitute"), row.get("room"))
                    ids.append(row_id)
                    self.conn.execute(
                        "INSERT INTO substitutions(id, day, hours, classes, substitute, teacher, kind, subject, room, "
                        "note, child_ids, certainty, active, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET note=excluded.note, child_ids=excluded.child_ids, "
                        "certainty=excluded.certainty, active=1, last_seen=excluded.last_seen",
                        (row_id, day.day.isoformat(), row.get("hours"), row.get("classes"), row.get("substitute"),
                         row.get("teacher"), row.get("kind"), row.get("subject"), row.get("room"),
                         " ".join(x for x in (row.get("note"), row.get("note2")) if x) or None,
                         db.dumps(sorted(certainty)), db.dumps(certainty), now, now),
                    )
                    n += 1
                placeholders = ",".join("?" * len(ids)) or "''"
                self.conn.execute(f"UPDATE substitutions SET active=0 WHERE day=? AND id NOT IN ({placeholders})",
                                  (day.day.isoformat(), *ids))
            old = (date.today() - timedelta(days=7)).isoformat()
            self.conn.execute("DELETE FROM substitutions WHERE day < ?", (old,))
            self.conn.execute("DELETE FROM plan_days WHERE day < ?", (old,))
        return f"{len(days)} days, {n} rows"

    # -- Kalender ----------------------------------------------------------------------

    def sync_calendar(self) -> str:
        start = date.today() - timedelta(days=7)
        end = date.today() + timedelta(days=330)
        events = P.parse_calendar(self.client.calendar_events(start, end))
        profiles = load_profiles(self.conn)
        now = db.utcnow()
        ids = []
        with db.transaction(self.conn):
            for ev in events:
                kind, child_ids, parents_only = assign_event(ev, profiles, self.s.hidden_event_categories,
                                                             self.s.parent_only_keywords)
                h = _hash(ev.title, ev.start, ev.end, ev.location, ev.description, ev.group)
                ids.append(ev.id)
                row = self.conn.execute("SELECT hash FROM events WHERE id=?", (ev.id,)).fetchone()
                changed = row is None or row["hash"] != h
                self.conn.execute(
                    "INSERT INTO events(id, title, start, end, all_day, category, kind, location, description, "
                    "group_name, child_ids, parents_only, hash, active, first_seen, changed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, "
                    "start=excluded.start, end=excluded.end, all_day=excluded.all_day, category=excluded.category, "
                    "kind=excluded.kind, location=excluded.location, description=excluded.description, "
                    "group_name=excluded.group_name, child_ids=excluded.child_ids, parents_only=excluded.parents_only, "
                    "hash=excluded.hash, active=1, changed_at=CASE WHEN ? THEN excluded.changed_at "
                    "ELSE events.changed_at END",
                    (ev.id, ev.title, ev.start, ev.end, int(ev.all_day), ev.category, kind, ev.location,
                     ev.description, ev.group, db.dumps(child_ids), int(parents_only), h, now, now, int(changed)),
                )
            placeholders = ",".join("?" * len(ids)) or "''"
            self.conn.execute(
                f"UPDATE events SET active=0 WHERE substr(start,1,10) >= ? AND substr(start,1,10) <= ? "
                f"AND id NOT IN ({placeholders})",
                (start.isoformat(), end.isoformat(), *ids),
            )
            self.conn.execute("DELETE FROM events WHERE substr(COALESCE(end, start),1,10) < ?",
                              ((date.today() - timedelta(days=30)).isoformat(),))
        return f"{len(events)} events"

    # -- Nachrichten -------------------------------------------------------------------

    def sync_messages(self) -> str:
        headers = P.parse_message_headers(self.client.message_headers())
        profiles = load_profiles(self.conn)
        now_local = self.now_local()
        window_start = now_local - timedelta(days=self.s.message_window_days)
        now = db.utcnow()
        stored = fetched = 0
        for msg in headers:
            if msg.hidden or not msg.id:
                continue
            try:
                sent = parse_portal_datetime(msg.when, now_local)
            except ValueError:
                log.warning("cannot parse message date %r", msg.when)
                continue
            if sent < window_start:
                continue
            concerns, kids = assign_message(msg, profiles)
            row = self.conn.execute("SELECT sent_at, body FROM messages WHERE id=?", (msg.id,)).fetchone()
            body = row["body"] if row else None
            needs_body = self.s.fetch_message_bodies and (row is None or body is None or row["sent_at"] != sent.isoformat())
            if needs_body:
                try:
                    body = P.parse_message_body(self.client.message(msg.id))
                    fetched += 1
                except Exception:  # noqa: BLE001 - keep the header even if the body fails
                    log.exception("could not load message %s", msg.id)
            with db.transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO messages(id, subject, sender, sent_at, recipients, groups, body, child_ids, kid_ids, "
                    "first_seen, changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "subject=excluded.subject, sender=excluded.sender, recipients=excluded.recipients, "
                    "groups=excluded.groups, body=COALESCE(excluded.body, messages.body), "
                    "child_ids=excluded.child_ids, kid_ids=excluded.kid_ids, "
                    "changed_at=CASE WHEN messages.sent_at != excluded.sent_at THEN excluded.changed_at "
                    "ELSE messages.changed_at END, sent_at=excluded.sent_at",
                    (msg.id, msg.subject, msg.sender, sent.isoformat(), ", ".join(msg.recipients),
                     ", ".join(msg.groups), body, db.dumps(concerns), db.dumps(kids), now, now),
                )
            stored += 1
        cutoff = (now_local - timedelta(days=30)).isoformat()
        self.conn.execute("DELETE FROM messages WHERE sent_at < ?", (cutoff,))
        return f"{stored} recent messages, {fetched} bodies loaded"

    # -- orchestration -------------------------------------------------------------------

    AREAS = ("lessons", "timetables", "substitutions", "calendar", "messages")

    def run(self, areas: tuple[str, ...] = AREAS) -> dict[str, bool]:
        results: dict[str, bool] = {}
        functions = {
            "lessons": self.sync_lessons,
            "timetables": self.sync_timetables,
            "substitutions": self.sync_substitutions,
            "calendar": self.sync_calendar,
            "messages": self.sync_messages,
        }
        for area in areas:
            try:
                results[area] = self.run_area(area, functions[area])
            except Exception as exc:  # noqa: BLE001
                results[area] = False
                if isinstance(exc, (WrongCredentials, LoginLocked, PortalDown)):
                    raise
        return results

