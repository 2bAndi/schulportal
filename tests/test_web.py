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
    for text in ("Max", "Paul", "Lenchen", "Rechtschreibleistung", "Ski-Infoelternabend", "Abrufstatus"):
        assert text in html


def test_child_sees_only_own(app_client):
    assert login(app_client, "lena", "lena-passwort").status_code == 303
    html = app_client.get("/").text
    assert "Lenchen" in html and "Ethik" in html
    for text in ("Max", "Paul", "Rechtschreibleistung", "Unterrichtsentfall", "Ski-Infoelternabend", "Abrufstatus",
                 "Religion - katholisch 5"):
        assert text not in html, text
    assert "Informationen zu Hilfs- und Beratungsangeboten" in html  # addressed to all students


def test_security_headers(app_client):
    r = app_client.get("/login")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-robots-tag"].startswith("noindex")
