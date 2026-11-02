import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
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
    # the fixture messages are from October 2026; keep them inside the window whatever today is
    from dataclasses import replace
    settings = replace(settings, message_window_days=3650)
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    auth.sync_users(conn, settings)
    auth.set_password(conn, "papa", "papa-passwort")
    auth.set_password(conn, "lena", "lena-passwort")
    Syncer(settings, conn, FixtureClient(FIX)).run()
    with TestClient(create_app(settings), base_url="http://testserver") as client:
        yield client


def login(client, user, pw):
    return client.post("/login", data={"username": user, "password": pw}, headers={"origin": "http://testserver"},
                       follow_redirects=False)


def test_requires_login(app_client):
    r = app_client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_wrong_password_and_throttle(app_client):
    for _ in range(5):
        assert login(app_client, "papa", "falsch-falsch").status_code == 401
    assert login(app_client, "papa", "papa-passwort").status_code == 429


def test_post_without_origin_rejected(app_client):
    assert app_client.post("/login", data={"username": "papa", "password": "x"}).status_code == 403


def test_parent_sees_everything(app_client):
    assert login(app_client, "papa", "papa-passwort").status_code == 303
    html = app_client.get("/").text
    for text in ("Max", "Paul", "Lenchen", "Rechtschreibleistung", "Abrufstatus"):
        assert text in html
    # parents-only school event; the calendar page has a fixed start, so this does not depend on today
    assert "Ski-Infoelternabend" in app_client.get("/kalender?von=2026-10-26").text


def test_child_sees_only_own(app_client):
    assert login(app_client, "lena", "lena-passwort").status_code == 303
    html = app_client.get("/").text
    assert "Lenchen" in html and "Ethik" in html
    for text in ("Max", "Paul", "Rechtschreibleistung", "Unterrichtsentfall", "Ski-Infoelternabend", "Abrufstatus",
                 "Religion - katholisch 5"):
        assert text not in html, text
    assert "Informationen zu Hilfs- und Beratungsangeboten" in html  # addressed to all students
    assert "Ski-Infoelternabend" not in app_client.get("/kalender?von=2026-10-26").text


def test_security_headers(app_client):
    r = app_client.get("/login")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-robots-tag"].startswith("noindex")


def test_layout_order_and_collapse(app_client):
    import re
    assert login(app_client, "papa", "papa-passwort").status_code == 303
    H = {"origin": "http://testserver", "x-requested-with": "fetch"}

    def order(html):
        return re.findall(r'<section id="([a-z]+)" class="block"', html)

    html = app_client.get("/").text
    default = order(html)
    assert default[:3] == ["neu", "tage", "hausaufgaben"] or default[:2] == ["tage", "hausaufgaben"]
    assert html.count("<details class=\"blk\" open>") == len(default)

    # move the timetable to the top; blocks that are not shown keep their slots
    visible = [k for k in default if k != "stundenplan"]
    r = app_client.post("/ansicht", data={"order": ",".join(["stundenplan"] + visible)}, headers=H)
    assert r.status_code == 204
    html = app_client.get("/").text
    assert order(html)[0] == "stundenplan"
    jump = re.findall(r'<a href="#([a-z]+)" data-key=', html)
    assert jump == order(html)

    # collapse one block, then reopen it
    app_client.post("/ansicht", data={"block": "arbeiten", "open": "0"}, headers=H)
    html = app_client.get("/").text
    assert re.search(r'id="arbeiten" class="block" data-key="arbeiten">\s*<details class="blk">', html)
    app_client.post("/ansicht", data={"block": "arbeiten", "open": "1"}, headers=H)
    assert re.search(r'id="arbeiten" class="block" data-key="arbeiten">\s*<details class="blk" open>',
                     app_client.get("/").text)

    # unknown keys are ignored; reset restores the default order
    app_client.post("/ansicht", data={"order": "evil,stundenplan"}, headers=H)
    app_client.post("/ansicht", data={"order": ""}, headers=H)
    assert order(app_client.get("/").text) == default


def test_layout_requires_login(app_client):
    r = app_client.post("/ansicht", data={"order": "tage"}, headers={"origin": "http://testserver"})
    assert r.status_code == 401


def test_reorder_keeps_hidden_slots():
    from schulblick.web.views import BLOCK_KEYS, full_order, reorder
    assert full_order([]) == BLOCK_KEYS
    # "neu" and "status" are not on the page; they stay where they were
    new = reorder([], ["stundenplan", "tage", "hausaufgaben", "arbeiten", "vertretungsplan", "nachrichten",
                       "anwesenheit"])
    assert new[0] == "neu" and new[1] == "stundenplan" and new[-1] == "status"
    assert sorted(new) == sorted(BLOCK_KEYS)


def test_attendance_shows_open_excuses_per_course(app_client):
    import re
    assert login(app_client, "papa", "papa-passwort").status_code == 303
    html = app_client.get("/").text
    assert "<th>offene Entschuldigungen</th>" in html
    # Chemie g8a carries the badge "2" in the fixture
    assert re.search(r"<td>Chemie g8a</td>(?:<td>[^<]*</td>)*<td class=\"openexc\">2</td>", html)
