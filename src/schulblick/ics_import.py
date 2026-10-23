"""Import calendars from Outlook (or any iCalendar source).

Two ways in:
* a one-time file upload (.ics exported from Outlook), copied into editable family entries;
  simple series become Schulblick series, everything else is expanded into single entries;
* a published calendar link (Outlook on the web: "Kalender veröffentlichen"), fetched regularly and shown
  read-only; every sync replaces the previous copy.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

from . import db

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
MAX_EVENTS = 5000
FEED_PAST_DAYS = 30
FEED_FUTURE_DAYS = 365
FILE_PAST_DAYS = 30
FILE_FUTURE_DAYS = 730
PRIVATE_TITLE = "Privater Termin"

_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


class ImportError_(Exception):
    """Readable problem with a calendar file or link (shown to the user)."""


@dataclass
class Item:
    uid: str
    title: str
    start: date | datetime          # naive local datetime or date
    end: date | datetime | None     # all-day: inclusive last day; timed: naive local end
    all_day: bool
    location: str = ""
    note: str = ""
    repeat: str = "none"
    repeat_until: date | None = None
    exdates: list[str] = field(default_factory=list)

    def db_values(self) -> dict:
        def fmt(v):
            if v is None:
                return None
            return v.isoformat() if not isinstance(v, datetime) else v.isoformat(timespec="minutes")
        end = self.end
        if end is not None and end == self.start:
            end = None
        return {"title": self.title[:120], "start": fmt(self.start), "end": fmt(end), "all_day": self.all_day,
                "location": self.location[:200], "note": self.note[:2000], "repeat": self.repeat,
                "repeat_until": self.repeat_until.isoformat() if self.repeat_until else None,
                "exdates": self.exdates}


# --------------------------------------------------------------------------- parsing


def _load(data: bytes) -> icalendar.Calendar:
    if len(data) > MAX_BYTES:
        raise ImportError_("Die Datei ist größer als 5 MB.")
    try:
        cal = icalendar.Calendar.from_ical(data)
    except Exception as exc:  # noqa: BLE001 - library raises various errors
        raise ImportError_("Das ist keine gültige Kalenderdatei (.ics).") from exc
    if not isinstance(cal, icalendar.Calendar) or cal.name != "VCALENDAR":
        raise ImportError_("Das ist keine gültige Kalenderdatei (.ics).")
    return cal


def _local(value, tz: ZoneInfo) -> date | datetime:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(tz)
        return value.replace(tzinfo=None, second=0, microsecond=0)
    return value


def _text(component, key: str) -> str:
    value = component.get(key)
    return str(value).strip() if value is not None else ""


def _times(component, tz: ZoneInfo) -> tuple[date | datetime, date | datetime | None, bool]:
    start = component.decoded("DTSTART")
    if "DTEND" in component:
        end = component.decoded("DTEND")
    elif "DURATION" in component:
        end = start + component.decoded("DURATION")
    else:
        end = None
    all_day = not isinstance(start, datetime)
    start_l = _local(start, tz)
    if all_day:
        if isinstance(end, date) and not isinstance(end, datetime) and end > start:
            end_l = end - timedelta(days=1)  # iCalendar all-day ends are exclusive
        else:
            end_l = None
    else:
        end_l = _local(end, tz) if end is not None else None
    return start_l, end_l, all_day


def _item(component, uid: str, tz: ZoneInfo, private_mode: str) -> Item | None:
    if _text(component, "STATUS").upper() == "CANCELLED":
        return None
    start, end, all_day = _times(component, tz)
    title = _text(component, "SUMMARY") or "(ohne Titel)"
    location, note = _text(component, "LOCATION"), _text(component, "DESCRIPTION")
    if _text(component, "CLASS").upper() in ("PRIVATE", "CONFIDENTIAL"):
        if private_mode == "skip":
            return None
        if private_mode == "hide":
            title, location, note = PRIVATE_TITLE, "", ""
    return Item(uid=uid, title=title, start=start, end=end, all_day=all_day, location=location, note=note)


def _simple_series(master, tz: ZoneInfo) -> tuple[str, date | None, list[str]] | None:
    """Map an RRULE to a Schulblick series, or None when it needs expansion."""
    rrule = master.get("RRULE")
    if rrule is None:
        return None
    rule = {k.upper(): v for k, v in rrule.items()}
    allowed = {"FREQ", "INTERVAL", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "WKST", "COUNT"}
    if set(rule) - allowed:
        return None
    freq = rule["FREQ"][0].upper()
    interval = int(rule.get("INTERVAL", [1])[0])
    start = _local(master.decoded("DTSTART"), tz)
    first_day = start.date() if isinstance(start, datetime) else start
    byday = [str(d).upper() for d in rule.get("BYDAY", [])]
    if freq == "DAILY" and interval == 1 and not byday:
        repeat = "daily"
    elif freq == "WEEKLY" and interval in (1, 2) and byday in ([], [_WEEKDAYS[first_day.weekday()]]):
        repeat = "weekly" if interval == 1 else "biweekly"
    elif freq == "MONTHLY" and interval == 1 and not byday and \
            [int(x) for x in rule.get("BYMONTHDAY", [first_day.day])] == [first_day.day]:
        repeat = "monthly"
    elif freq == "YEARLY" and interval == 1 and not byday and "BYMONTHDAY" not in rule:
        repeat = "yearly"
    else:
        return None
    until = None
    if "UNTIL" in rule:
        u = rule["UNTIL"][0]
        u = _local(u, tz)
        until = u.date() if isinstance(u, datetime) else u
    if "COUNT" in rule:
        count = int(rule["COUNT"][0])
        if repeat in ("daily", "weekly", "biweekly"):
            step = {"daily": 1, "weekly": 7, "biweekly": 14}[repeat]
            until = first_day + timedelta(days=step * (count - 1))
        else:
            return None
    exdates: list[str] = []
    ex = master.get("EXDATE")
    for group in (ex if isinstance(ex, list) else [ex] if ex is not None else []):
        for d in group.dts:
            v = _local(d.dt, tz)
            exdates.append((v.date() if isinstance(v, datetime) else v).isoformat())
    return repeat, until, sorted(set(exdates))


def parse_file(data: bytes, tz: ZoneInfo, today: date, private_mode: str = "hide") -> list[Item]:
    """Items for a one-time import: simple series stay series, the rest is expanded."""
    cal = _load(data)
    events = list(cal.walk("VEVENT"))
    overridden = {str(e.get("UID")) for e in events if e.get("RECURRENCE-ID") is not None}
    items: list[Item] = []
    expand_uids: set[str] = set()
    for e in events:
        uid = str(e.get("UID") or "")
        if not uid or e.get("RECURRENCE-ID") is not None:
            continue
        if e.get("RRULE") is None:
            it = _item(e, uid, tz, private_mode)
            if it:
                items.append(it)
            continue
        series = None if uid in overridden else _simple_series(e, tz)
        if series is None:
            expand_uids.add(uid)
            continue
        it = _item(e, uid, tz, private_mode)
        if it:
            it.repeat, it.repeat_until, it.exdates = series
            items.append(it)
    if expand_uids:
        start, end = today - timedelta(days=FILE_PAST_DAYS), today + timedelta(days=FILE_FUTURE_DAYS)
        for occ in recurring_ical_events.of(cal).between(start, end):
            uid = str(occ.get("UID") or "")
            if uid not in expand_uids:
                continue
            day = _local(occ.decoded("DTSTART"), tz)
            day = day.date() if isinstance(day, datetime) else day
            it = _item(occ, f"{uid}#{day.isoformat()}", tz, private_mode)
            if it:
                items.append(it)
            if len(items) > MAX_EVENTS:
                break
    if len(items) > MAX_EVENTS:
        raise ImportError_(f"Die Datei enthält mehr als {MAX_EVENTS} Termine.")
    return items


def parse_feed(data: bytes, tz: ZoneInfo, today: date, private_mode: str = "hide") -> list[Item]:
    """Expanded occurrences of a subscribed calendar for the sync window."""
    cal = _load(data)
    start, end = today - timedelta(days=FEED_PAST_DAYS), today + timedelta(days=FEED_FUTURE_DAYS)
    items: list[Item] = []
    for occ in recurring_ical_events.of(cal).between(start, end):
        uid = str(occ.get("UID") or "")
        day = _local(occ.decoded("DTSTART"), tz)
        day = day.date() if isinstance(day, datetime) else day
        it = _item(occ, f"{uid}#{day.isoformat()}", tz, private_mode)
        if it:
            items.append(it)
        if len(items) > MAX_EVENTS:
            raise ImportError_(f"Der Kalender enthält mehr als {MAX_EVENTS} Termine im nächsten Jahr.")
    return items


# --------------------------------------------------------------------------- fetching


def normalise_url(url: str) -> str:
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ImportError_("Bitte einen https- oder webcal-Link eintragen.")
    return url


def _check_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ImportError_(f"Der Server {host} ist nicht erreichbar.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ImportError_("Links ins Heimnetz oder auf den Server selbst sind nicht erlaubt.")


def fetch(url: str, transport: httpx.BaseTransport | None = None, check_host: bool = True) -> bytes:
    url = normalise_url(url)
    with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False, transport=transport,
                      headers={"User-Agent": "Schulblick/0.1 (calendar import)"}) as client:
        for _ in range(4):
            host = urlparse(url).hostname or ""
            if check_host:
                _check_host(host)
            with client.stream("GET", url) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    url = normalise_url(str(resp.url.join(resp.headers["location"])))
                    continue
                if resp.status_code != 200:
                    raise ImportError_(f"Der Link antwortet mit Fehler {resp.status_code}.")
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ImportError_("Der Kalender ist größer als 5 MB.")
                    chunks.append(chunk)
                return b"".join(chunks)
    raise ImportError_("Zu viele Weiterleitungen.")


# --------------------------------------------------------------------------- storing


def import_items(conn: sqlite3.Connection, items: list[Item], audience: list[str], user_id: int, user_name: str,
                 source: str = "file") -> tuple[int, int]:
    """Upsert editable entries by UID. Returns (new, updated)."""
    now = db.utcnow()
    new = updated = 0
    with db.transaction(conn):
        for it in items:
            v = it.db_values()
            row = conn.execute("SELECT id FROM family_events WHERE source=? AND ext_uid=?", (source, it.uid)).fetchone()
            if row:
                conn.execute(
                    "UPDATE family_events SET title=?, start=?, end=?, all_day=?, location=?, note=?, repeat=?, "
                    "repeat_until=?, exdates=?, updated_at=? WHERE id=?",
                    (v["title"], v["start"], v["end"], int(v["all_day"]), v["location"], v["note"], v["repeat"],
                     v["repeat_until"], db.dumps(v["exdates"]), now, row["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO family_events(title, start, end, all_day, location, note, audience, repeat, "
                    "repeat_until, exdates, skip_holidays, created_by, created_by_name, created_at, updated_at, "
                    "source, ext_uid, readonly) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,0)",
                    (v["title"], v["start"], v["end"], int(v["all_day"]), v["location"], v["note"],
                     db.dumps(audience), v["repeat"], v["repeat_until"], db.dumps(v["exdates"]), user_id, user_name,
                     now, now, source, it.uid),
                )
                new += 1
    return new, updated


def replace_feed_items(conn: sqlite3.Connection, feed: sqlite3.Row, items: list[Item]) -> int:
    """Replace all entries of a subscribed calendar with a fresh copy (read-only in the app)."""
    source = f"feed:{feed['id']}"
    now = db.utcnow()
    audience = db.loads(feed["audience"], [])
    old = {r["ext_uid"]: r for r in conn.execute(
        "SELECT ext_uid, created_at, title, start, end, location FROM family_events WHERE source=?", (source,))}
    with db.transaction(conn):
        conn.execute("DELETE FROM family_events WHERE source=?", (source,))
        for it in items:
            v = it.db_values()
            prev = old.get(it.uid)
            unchanged = prev is not None and (prev["title"], prev["start"], prev["end"], prev["location"] or "") == \
                (v["title"], v["start"], v["end"], v["location"])
            created = prev["created_at"] if prev is not None else now
            conn.execute(
                "INSERT INTO family_events(title, start, end, all_day, location, note, audience, repeat, "
                "repeat_until, exdates, skip_holidays, created_by, created_by_name, created_at, updated_at, source, "
                "ext_uid, readonly) VALUES(?,?,?,?,?,?,?,'none',NULL,'[]',0,?,?,?,?,?,?,1)",
                (v["title"], v["start"], v["end"], int(v["all_day"]), v["location"], v["note"], db.dumps(audience),
                 feed["created_by"], feed["name"], created, created if unchanged else now, source, it.uid),
            )
    return len(items)


def sync_feed(conn: sqlite3.Connection, feed_id: int, tz: ZoneInfo, today: date | None = None,
              fetcher=None) -> tuple[bool, str]:
    feed = conn.execute("SELECT * FROM calendar_feeds WHERE id=?", (feed_id,)).fetchone()
    if feed is None:
        return False, "unbekannter Kalender"
    today = today or datetime.now(tz).date()
    try:
        items = parse_feed((fetcher or fetch)(feed["url"]), tz, today, feed["private_mode"])
        count = replace_feed_items(conn, feed, items)
        msg, ok = f"{count} Termine", True
    except ImportError_ as exc:
        msg, ok = str(exc), False
    except httpx.HTTPError as exc:
        msg, ok = f"Abruf fehlgeschlagen ({type(exc).__name__})", False
    conn.execute("UPDATE calendar_feeds SET last_sync=?, last_ok=?, last_message=? WHERE id=?",
                 (db.utcnow(), int(ok), msg[:300], feed_id))
    if not ok:
        log.warning("calendar feed %s: %s", feed_id, msg)
    return ok, msg


def sync_all_feeds(conn: sqlite3.Connection, tz: ZoneInfo) -> None:
    for row in conn.execute("SELECT id FROM calendar_feeds ORDER BY id").fetchall():
        sync_feed(conn, row["id"], tz)
