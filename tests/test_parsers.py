import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from schulblick.sph import parsers as P
from schulblick.sph.client import parse_portal_datetime

FIX = Path(__file__).parent / "fixtures"


def read(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_lessons_three_children():
    kids = P.parse_lessons(read("meinunterricht_parent.html"))
    assert [k.pupil_id for k in kids] == ["1000001", "1000002", "1000003"]
    assert [k.first_name for k in kids] == ["Max", "Paul", "Lena"]
    max_ = kids[0]
    frz = next(e for e in max_.entries if e.course == "Französisch g8a")
    assert frz.teacher_code == "SCHL" and frz.teacher_name == "Schlaud, Catherine"
    assert frz.lesson_date == date(2026, 10, 20)
    assert frz.homework.startswith("Arbeitsaufträge") and frz.homework_done is False
    chem = max_.entries[0]
    assert chem.homework is None and chem.open_excuses == 2
    assert "religion - katholisch 8" in {P.normalize_course(c.name) for c in max_.courses}
    deutsch = next(e for e in kids[1].entries if e.course == "Deutsch g5b")
    assert deutsch.homework_done is True


def test_attendance_totals_ignore_hidden_hashes():
    max_ = P.parse_lessons(read("meinunterricht_parent.html"))[0]
    a = max_.attendance
    assert a.totals["anwesend"] == 24
    assert a.totals["fehlend"] == 8
    assert a.occurrences["verspätet"] == 2
    assert a.minutes_late == 21 + 70


def test_substitutions():
    classes, days = P.parse_substitutions(read("vertretungsplan.html"))
    assert classes == ["G8a", "G5b", "G5c"]
    assert [d.day for d in days] == [date(2026, 10, 22), date(2026, 10, 23)]
    assert days[0].week == "A"
    assert days[0].infos[0]["title"] == "Von Entfall betroffene Klassen"
    row = days[1].rows[0]
    assert row["hours"] == "1 - 2" and row["classes"] == "G5c" and row["subject"] == "Mu" and row["kind"] == "Vertretung"


def test_timetable_rowspan_and_options():
    links = P.parse_timetable_links(read("stundenplan_overview.html"))
    assert [(l.pupil_id, l.class_name) for l in links] == [("1000001", "G8a"), ("1000002", "G5b"), ("1000003", "G5c")]
    t = P.parse_timetable(read("stundenplan_klasse.html"))
    assert t.week == "A" and t.days[0] == "Montag"
    assert [h["no"] for h in t.hours] == [1, 2, 7]
    assert t.grid[1][0][0].code == "Sp"  # carried over by rowspan=2
    assert [l.code for l in t.grid[0][3]] == ["RE1", "RK1", "Eth1"]
    assert t.grid[0][1][0].room == "Bad1" and t.grid[0][1][0].teacher == "Frit"


def test_calendar_and_messages():
    ev = P.parse_calendar(json.loads(read("kalender_events.json")))
    assert ev[0].group == "Französisch g8a (081F01-GYM)" and ev[0].tool == "Klausurplaner"
    msgs = P.parse_message_headers(json.loads(read("nachrichten_headers.json")))
    assert msgs[0].groups == ["Eltern der Lerngruppe Biologie g5c (051BIO06-GYM)"]
    assert msgs[0].sender == "Heiland, Jana (HEIL)"
    assert msgs[2].recipients == ["Muster, Anna", "Muster, Thomas"]


def test_portal_dates():
    now = datetime(2026, 10, 22, 21, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert parse_portal_datetime("heute 09:00", now).hour == 9
    assert parse_portal_datetime("gestern 07:01", now).day == 21
    assert parse_portal_datetime("19.10.2026 11:22", now) == datetime(2026, 10, 19, 11, 22, tzinfo=now.tzinfo)
