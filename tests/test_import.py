import os
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from schulblick import ics_import

FIX = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 10, 23)
ORIGIN = {"origin": "http://testserver"}


def outlook() -> bytes:
    return (FIX / "outlook_export.ics").read_bytes()


def test_parse_file_keeps_simple_series_and_converts_timezones():
    items = {i.title: i for i in ics_import.parse_file(outlook(), TZ, TODAY)}
    tanz = items["Tanzkurs"]
    assert tanz.repeat == "weekly" and tanz.exdates == ["2026-10-20"]
    assert tanz.start == datetime(2026, 10, 6, 19, 30) and tanz.end == datetime(2026, 10, 6, 21, 0)
    urlaub = items["Urlaub Ostsee"]
    assert urlaub.all_day and urlaub.start == date(2026, 12, 26) and urlaub.end == date(2027, 1, 1)
    # UTC time converted to local time; private entry anonymised
    private = items[ics_import.PRIVATE_TITLE]
    assert private.start == datetime(2026, 10, 28, 9, 0) and private.location == ""
    assert "Abgesagtes Meeting" not in items
    # "every second Thursday" is no simple series: expanded into three single entries
    sprech = [i for i in ics_import.parse_file(outlook(), TZ, TODAY) if i.title == "Elternsprechtag"]
    assert [i.start.date() for i in sprech] == [date(2026, 11, 12), date(2026, 12, 10), date(2027, 1, 14)]
    assert all(i.repeat == "none" for i in sprech)


def test_private_modes():
    titles = [i.title for i in ics_import.parse_file(outlook(), TZ, TODAY, private_mode="show")]
    assert "Arzttermin Papa" in titles
    titles = [i.title for i in ics_import.parse_file(outlook(), TZ, TODAY, private_mode="skip")]
    assert "Arzttermin Papa" not in titles and ics_import.PRIVATE_TITLE not in titles


def test_parse_feed_expands_with_exdate():
    tanz = [i for i in ics_import.parse_feed(outlook(), TZ, TODAY) if i.title == "Tanzkurs"]
    days = [i.start.date() for i in tanz]
    assert date(2026, 10, 13) in days and date(2026, 10, 20) not in days and date(2026, 10, 27) in days
    assert tanz[0].start.hour == 19  # still 19:30 local after the DST change
    assert all(d.weekday() == 1 for d in days)


def test_rejects_garbage_and_bad_urls():
    with pytest.raises(ics_import.ImportError_):
        ics_import.parse_file(b"hello world", TZ, TODAY)
    with pytest.raises(ics_import.ImportError_):
        ics_import.normalise_url("http://example.org/cal.ics")
    assert ics_import.normalise_url("webcal://example.org/a.ics") == "https://example.org/a.ics"
    with pytest.raises(ics_import.ImportError_):
        ics_import._check_host("localhost")


def test_fetch_follows_redirect_and_limits_size():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://cal.example.org/real.ics"})
        return httpx.Response(200, content=outlook())
    data = ics_import.fetch("https://cal.example.org/start", transport=httpx.MockTransport(handler), check_host=False)
    assert data.startswith(b"BEGIN:VCALENDAR")


# --------------------------------------------------------------------------- web


@pytest.fixture()
def app_parts(tmp_path, monkeypatch):
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
    for name in ("papa", "lena"):
        auth.set_password(conn, name, f"{name}-passwort")
    Syncer(settings, conn, FixtureClient(FIX)).run()
    app = create_app(settings)

    def login(user):
        c = TestClient(app, base_url="http://testserver")
        c.__enter__()
        c.post("/login", data={"username": user, "password": f"{user}-passwort"}, headers=ORIGIN)
        return c
    yield conn, settings, login


def test_file_import_is_idempotent_and_respects_audience(app_parts):
    conn, _settings, login = app_parts
    papa, lena = login("papa"), login("lena")
    assert lena.get("/kalender/import", follow_redirects=False).status_code == 303  # parents only
    files = {"file": ("kalender.ics", outlook(), "text/calendar")}
    r = papa.post("/kalender/import/datei", files=files, data={"private_mode": "hide"}, headers=ORIGIN,
                  follow_redirects=False)
    assert r.status_code == 303 and "%C3%BCbernommen" in r.headers["location"]
    n1 = conn.execute("SELECT COUNT(*) FROM family_events WHERE source='file'").fetchone()[0]
    papa.post("/kalender/import/datei", files=files, data={}, headers=ORIGIN)
    n2 = conn.execute("SELECT COUNT(*) FROM family_events WHERE source='file'").fetchone()[0]
    assert n1 == n2 == 6  # Tanzkurs series, Urlaub, private, 3x Elternsprechtag
    # imported without ticks -> parents only, so Lena does not see them
    html = lena.get("/kalender?von=2026-10-26").text
    assert "Tanzkurs" not in html
    assert "Tanzkurs" in papa.get("/kalender?von=2026-10-26").text
    # imported entries are normal entries: parents can edit them
    ev = conn.execute("SELECT id FROM family_events WHERE title='Tanzkurs'").fetchone()[0]
    assert papa.get(f"/kalender/{ev}").status_code == 200
    r = papa.post("/kalender/import/datei", files={"file": ("x.ics", b"kaputt", "text/calendar")}, headers=ORIGIN)
    assert r.status_code == 400 and "keine gültige Kalenderdatei" in r.text


def test_feed_is_read_only_and_replaced_on_sync(app_parts, monkeypatch):
    conn, settings, login = app_parts
    papa, lena = login("papa"), login("lena")
    monkeypatch.setattr(ics_import, "fetch", lambda url, **kw: outlook())
    r = papa.post("/kalender/import/abo", data={"name": "Outlook Papa", "url": "webcal://cal.example.org/p.ics",
                                                 "audience": "*"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "verbunden" in r.headers["location"]
    feed = conn.execute("SELECT * FROM calendar_feeds").fetchone()
    assert feed["url"] == "https://cal.example.org/p.ics" and feed["last_ok"] == 1
    html = lena.get("/kalender?von=2026-10-26").text
    assert "Tanzkurs" in html and "von Outlook Papa" in html
    ev = conn.execute("SELECT id FROM family_events WHERE source=? LIMIT 1", (f"feed:{feed['id']}",)).fetchone()[0]
    assert papa.get(f"/kalender/{ev}").status_code == 404  # edit in Outlook, not here
    before = conn.execute("SELECT COUNT(*) FROM family_events").fetchone()[0]
    papa.post(f"/kalender/import/abo/{feed['id']}/abrufen", headers=ORIGIN)
    assert conn.execute("SELECT COUNT(*) FROM family_events").fetchone()[0] == before
    papa.post(f"/kalender/import/abo/{feed['id']}/loeschen", data={"confirm": "ja"}, headers=ORIGIN)
    assert conn.execute("SELECT COUNT(*) FROM family_events WHERE source LIKE 'feed:%'").fetchone()[0] == 0
    assert "Tanzkurs" not in lena.get("/kalender?von=2026-10-26").text


def test_import_page_renders(app_parts):
    _conn, _s, login = app_parts
    html = login("papa").get("/kalender/import").text
    assert "Outlook-Kalender verbinden" in html and 'enctype="multipart/form-data"' in html
    assert re.search(r'name="audience" value="1000003"', html)
