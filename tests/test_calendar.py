import os
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from schulblick.web.calendar import _esc, _fold

FIX = Path(__file__).parent / "fixtures"
ORIGIN = {"origin": "http://testserver"}


@pytest.fixture()
def clients(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHULBLICK_CONFIG", str(FIX / "family_test.toml"))
    monkeypatch.setenv("SCHULBLICK_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SCHULBLICK_COOKIE_SECURE", "false")
    from schulblick import db
    from schulblick.config import load_settings
    from schulblick.fixtures import FixtureClient
    from schulblick.sync.runner import Syncer
    from schulblick.web import auth
    from schulblick.web.app import create_app

    settings = load_settings(os.environ["SCHULBLICK_CONFIG"])
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    auth.sync_users(conn, settings)
    for name in ("papa", "lena", "paul"):
        auth.set_password(conn, name, f"{name}-passwort")
    Syncer(settings, conn, FixtureClient(FIX)).run()
    app = create_app(settings)

    def make(user):
        c = TestClient(app, base_url="http://testserver")
        c.__enter__()
        r = c.post("/login", data={"username": user, "password": f"{user}-passwort"}, headers=ORIGIN,
                   follow_redirects=False)
        assert r.status_code == 303
        return c

    made = {u: make(u) for u in ("papa", "lena", "paul")}
    yield made
    for c in made.values():
        c.__exit__(None, None, None)


DAY = (date.today() + timedelta(days=2)).isoformat()


def create(client, title, audience, **extra):
    data = {"title": title, "date": DAY, "audience": audience, **extra}
    return client.post("/kalender/neu", data=data, headers=ORIGIN, follow_redirects=False)


def event_id(client, title):
    html = client.get(f"/kalender?von={DAY}").text
    ids = re.findall(r'<strong>(?:<span[^>]*>[^<]*</span> )?' + re.escape(title) + r'</strong>.*?href="/kalender/(\d+)"',
                     html, re.S)
    return int(ids[0]) if ids else None


def kid_id(name):
    return {"Max": "1000001", "Paul": "1000002", "Lena": "1000003"}[name]


def test_visibility_rules(clients):
    papa, lena, paul = clients["papa"], clients["lena"], clients["paul"]
    assert create(papa, "Zahnarzt Lena", [kid_id("Lena")], start_time="15:30").status_code == 303
    assert create(papa, "Grillfest", ["*"]).status_code == 303
    assert create(papa, "Elternabend privat", ["parents"]).status_code == 303
    assert create(lena, "Ballett", [kid_id("Lena")], start_time="17:00", end_time="18:15").status_code == 303

    lena_view = lena.get(f"/kalender?von={DAY}").text
    paul_view = paul.get(f"/kalender?von={DAY}").text
    papa_view = papa.get(f"/kalender?von={DAY}").text

    assert "Zahnarzt Lena" in lena_view and "Zahnarzt Lena" not in paul_view
    assert "Grillfest" in lena_view and "Grillfest" in paul_view
    assert "Elternabend privat" not in lena_view and "Elternabend privat" not in paul_view
    assert "Ballett" in lena_view and "Ballett" not in paul_view
    for t in ("Zahnarzt Lena", "Grillfest", "Elternabend privat", "Ballett"):
        assert t in papa_view
    assert "17:00–18:15" in lena_view


def test_edit_rights(clients):
    papa, lena, paul = clients["papa"], clients["lena"], clients["paul"]
    create(papa, "Oma Geburtstag", ["*"])
    create(lena, "Lenas Training", [kid_id("Lena")])
    papa_ev = event_id(papa, "Oma Geburtstag")
    lena_ev = event_id(papa, "Lenas Training")
    assert papa_ev and lena_ev

    # children cannot edit what parents created, but can see it
    assert "Oma Geburtstag" in lena.get(f"/kalender?von={DAY}").text
    assert lena.get(f"/kalender/{papa_ev}").status_code == 404
    r = lena.post(f"/kalender/{papa_ev}", data={"title": "x", "date": DAY, "audience": "*"}, headers=ORIGIN)
    assert r.status_code == 404
    # siblings cannot open a private entry at all
    assert paul.get(f"/kalender/{lena_ev}").status_code == 404
    # the author and parents can edit
    assert lena.get(f"/kalender/{lena_ev}").status_code == 200
    r = papa.post(f"/kalender/{lena_ev}", data={"title": "Lenas Training (verlegt)", "date": DAY,
                                                 "audience": kid_id("Lena")}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert "Lenas Training (verlegt)" in lena.get(f"/kalender?von={DAY}").text


def test_child_cannot_target_siblings(clients):
    lena, paul = clients["lena"], clients["paul"]
    # a crafted form value for a sibling is ignored: the entry stays Lena's own
    assert create(lena, "Geheimtreffen", [kid_id("Paul")]).status_code == 303
    assert "Geheimtreffen" not in paul.get(f"/kalender?von={DAY}").text
    assert "Geheimtreffen" in lena.get(f"/kalender?von={DAY}").text


def test_validation_and_delete(clients):
    papa = clients["papa"]
    r = create(papa, "", ["*"])
    assert r.status_code == 400 and "Bitte einen Titel eingeben." in r.text
    r = create(papa, "Kaputt", ["*"], start_time="18:00", end_time="17:00")
    assert r.status_code == 400 and "Ende liegt vor dem Beginn" in r.text
    r = create(papa, "Ohne Zielgruppe", [])
    assert r.status_code == 400
    create(papa, "Weg damit", ["*"])
    ev = event_id(papa, "Weg damit")
    # without confirmation nothing happens
    papa.post(f"/kalender/{ev}/loeschen", data={}, headers=ORIGIN)
    assert "Weg damit" in papa.get(f"/kalender?von={DAY}").text
    papa.post(f"/kalender/{ev}/loeschen", data={"confirm": "ja"}, headers=ORIGIN)
    assert "Weg damit" not in papa.get(f"/kalender?von={DAY}").text


def test_overview_shows_family_events(clients):
    papa, lena = clients["papa"], clients["lena"]
    create(papa, "Kino mit allen", ["*"], start_time="19:00")
    html = lena.get("/").text
    assert "Kino mit allen" in html and 'href="/kalender"' in html


def test_ics_feed_and_rotation(clients):
    papa, lena = clients["papa"], clients["lena"]
    create(papa, "Zahnarzt, Kontrolle; jährlich", [kid_id("Lena")], start_time="15:30", location="Praxis Dr. X")
    create(papa, "Nur für Paul", [kid_id("Paul")])
    page = lena.get("/kalender/abo").text
    url = re.search(r'value="(https://testserver/ics/[^"]+\.ics)"', page).group(1)
    path = url.replace("https://testserver", "")
    r = lena.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.rstrip().endswith("END:VCALENDAR")
    assert "SUMMARY:Zahnarzt\\, Kontrolle\\; jährlich" in body
    assert "LOCATION:Praxis Dr. X" in body
    assert "Nur für Paul" not in body
    assert re.search(r"DTSTART:\d{8}T\d{6}Z", body)
    # rotating invalidates the old link
    lena.post("/kalender/abo/neu", headers=ORIGIN)
    assert lena.get(path).status_code == 404
    assert TestClient(lena.app).get("/ics/kurz.ics").status_code == 404


def test_ics_text_helpers():
    assert _esc("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"
    long = "SUMMARY:" + "ä" * 60
    folded = _fold(long)
    assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
    assert folded.replace("\r\n ", "") == long
