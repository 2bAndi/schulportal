"""AI layer with a fake Anthropic client: summaries, to-dos, fallback assignment, morning summary, failures."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from schulblick import ai, db
from schulblick.config import load_settings
from schulblick.fixtures import FixtureClient
from schulblick.sync.runner import Syncer
from schulblick.web import auth
from schulblick.web.views import OverviewBuilder

FIX = Path(__file__).parent / "fixtures"
MAX, PAUL, LENA = "1000001", "1000002", "1000003"


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeSDK:
    """Stands in for anthropic.Anthropic(): records requests, answers via a handler."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return self.handler(kw)


def tool_answer(data):
    return Obj(content=[Obj(type="tool_use", name="record_message", input=data)],
               usage=Obj(input_tokens=400, output_tokens=120))


def text_answer(text):
    return Obj(content=[Obj(type="text", text=text)], usage=Obj(input_tokens=600, output_tokens=90))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHULBLICK_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SCHULBLICK_ANTHROPIC_SECRET", str(tmp_path / "no-key"))
    monkeypatch.setenv("SCHULBLICK_COOKIE_SECURE", "false")
    settings = load_settings(FIX / "family_test.toml")
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    auth.sync_users(conn, settings)
    Syncer(settings, conn, FixtureClient(FIX)).run()
    now = datetime.now(settings.tz)
    for mid, subject, groups, child_ids, kid_ids, body in (
        ("x-100", "Robotik-AG: Ausflug", "Lernende der Robotik-AG", [], [],
         "Liebe AG, am Freitag fahren wir ins Museum. Bitte 5 Euro mitbringen."),
        ("x-101", "Unterschrift Deutscharbeit", "Eltern der Lerngruppe Deutsch g5c (051D06-GYM)", [LENA], [],
         "Bitte die Arbeit bis Montag unterschreiben lassen."),
    ):
        conn.execute(
            "INSERT INTO messages(id, subject, sender, sent_at, recipients, groups, body, child_ids, kid_ids, "
            "first_seen, changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (mid, subject, "Lehrkraft, Test (TEST)", (now - timedelta(hours=2)).isoformat(), "", groups, body,
             db.dumps(child_ids), db.dumps(kid_ids), db.utcnow(), db.utcnow()))
    return settings, conn, now


def user(conn, name):
    return auth._row_to_user(conn.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone())


def message_handler(today):
    due = (today + timedelta(days=3)).isoformat()

    def handle(kw):
        prompt = kw["messages"][0]["content"]
        assert kw["tool_choice"] == {"type": "tool", "name": "record_message"}
        if "Robotik" in prompt:
            return tool_answer({"summary": "Die Robotik-AG fährt am Freitag ins Museum.",
                                "todos": [{"text": "5 Euro mitgeben", "due": due, "kind": "geld",
                                           "children": ["Paul"]}],
                                "children": ["Paul"]})
        if "Deutscharbeit" in prompt:
            return tool_answer({"summary": "Lenas Deutscharbeit muss unterschrieben werden.",
                                "todos": [{"text": "Deutscharbeit unterschreiben", "due": due,
                                           "kind": "unterschrift", "children": ["Lenchen"]},
                                          {"text": "", "due": None, "kind": "x", "children": []}],
                                "children": []})
        return tool_answer({"summary": "Kurz gesagt: nichts zu tun.", "todos": [], "children": []})
    return handle


def test_summaries_todos_and_fallback(env):
    settings, conn, now = env
    sdk = FakeSDK(message_handler(now.date()))
    client = ai.AIClient(settings, conn, client=sdk)
    result = ai.process_messages(conn, settings, client)
    assert "fehlgeschlagen" not in result
    n = conn.execute("SELECT COUNT(*) FROM messages WHERE body IS NOT NULL AND body != ''").fetchone()[0]
    assert len(sdk.calls) == n
    # message text is wrapped as data; attendance is never part of a message prompt
    assert all("<nachricht>" in c["messages"][0]["content"] for c in sdk.calls)

    # fallback: rules found nobody for the AG message, AI says Paul; sent to "Lernende" so Paul sees it too
    row = conn.execute("SELECT child_ids, kid_ids FROM messages WHERE id='x-100'").fetchone()
    assert db.loads(row["child_ids"]) == [PAUL] and db.loads(row["kid_ids"]) == [PAUL]
    # rules had an answer for the Deutsch message: unchanged, still parents only
    row = conn.execute("SELECT child_ids, kid_ids FROM messages WHERE id='x-101'").fetchone()
    assert db.loads(row["child_ids"]) == [LENA] and db.loads(row["kid_ids"]) == []
    # empty to-do dropped, display name mapped back to the child id
    todos = db.loads(conn.execute("SELECT todos FROM message_ai WHERE message_id='x-101'").fetchone()[0])
    assert todos == [{"text": "Deutscharbeit unterschreiben", "due": (now.date() + timedelta(days=3)).isoformat(),
                      "kind": "unterschrift", "children": [LENA]}]

    # second run: nothing new, no further calls
    ai.process_messages(conn, settings, client)
    assert len(sdk.calls) == n

    papa = OverviewBuilder(conn, settings, user(conn, "papa"), since=None).build()
    msg = next(m for m in papa.messages if m["id"] == "x-101")
    assert msg["summary"].startswith("Lenas Deutscharbeit") and msg["todos"][0]["text"] == "Deutscharbeit unterschreiben"
    todo_items = [i for d in papa.days for i in d["items"] if i["type"] == "todo"]
    assert {i["title"] for i in todo_items} == {"5 Euro mitgeben", "Deutscharbeit unterschreiben"}

    # Lena does not see the parents-only message, nor its to-do; Paul sees his AG to-do
    lena = OverviewBuilder(conn, settings, user(conn, "lena"), since=None).build()
    assert "x-101" not in [m["id"] for m in lena.messages]
    assert not [i for d in lena.days for i in d["items"] if i["type"] == "todo"]
    paul = OverviewBuilder(conn, settings, user(conn, "paul"), since=None).build()
    assert [t["text"] for t in paul.todos] == ["5 Euro mitgeben"]

    # the fallback survives the next portal sync (rules still find nothing)
    assert ai.stored_fallback(conn, "x-100", "Lernende der Robotik-AG") == ([PAUL], [PAUL])
    assert ai.stored_fallback(conn, "x-100", "Eltern der Robotik-AG") == ([PAUL], [])


def test_failures_fall_back_to_original_text(env):
    settings, conn, _now = env

    def broken(_kw):
        raise RuntimeError("credit balance is too low")
    client = ai.AIClient(settings, conn, client=FakeSDK(broken))
    assert "fehlgeschlagen" in ai.process_messages(conn, settings, client)
    row = conn.execute("SELECT summary, attempts, error FROM message_ai WHERE message_id='x-100'").fetchone()
    assert row["summary"] is None and row["attempts"] == 1 and "credit" in row["error"]
    for _ in range(4):
        ai.process_messages(conn, settings, client)
    assert conn.execute("SELECT attempts FROM message_ai WHERE message_id='x-100'").fetchone()[0] == ai.MAX_ATTEMPTS
    papa = OverviewBuilder(conn, settings, user(conn, "papa"), since=None).build()
    msg = next(m for m in papa.messages if m["id"] == "x-100")
    assert msg["summary"] == "" and "Museum" in msg["body"]
    assert papa.ai_status["error"] and papa.ai_status["calls"] > 0


def test_daily_limit_and_missing_key(env):
    settings, conn, _now = env
    assert not ai.AIClient(settings, conn).available  # no key file, no client: AI off
    assert ai.process_messages(conn, settings, ai.AIClient(settings, conn)) == "KI nicht eingerichtet"
    for _ in range(settings.ai.max_calls_per_day):
        conn.execute("INSERT INTO ai_usage(at, purpose, model, ok) VALUES(?,?,?,1)", (db.utcnow(), "message", "m"))
    sdk = FakeSDK(lambda kw: pytest.fail("must not call the API over the limit"))
    ai.process_messages(conn, settings, ai.AIClient(settings, conn, client=sdk))
    assert sdk.calls == []


def test_morning_summary_without_attendance(env):
    settings, conn, now = env
    sdk = FakeSDK(lambda kw: text_answer("Paul: heute nichts Besonderes."))
    client = ai.AIClient(settings, conn, client=sdk)
    result = ai.build_digests(conn, settings, client, today=now.date())
    assert result.startswith("3 ")
    # papa + two children, but never attendance data
    prompts = [c["messages"][0]["content"] for c in sdk.calls]
    assert len(prompts) == 3 and not any("Anwesenheit" in p or "fehlend" in p for p in prompts)
    kid_prompt = next(p for p in prompts if "Kind Paul" in p)
    assert "## Paul" in kid_prompt and "## Max" not in kid_prompt and "## Lenchen" not in kid_prompt
    # once per day
    ai.build_digests(conn, settings, client, today=now.date())
    assert len(sdk.calls) == 3
    ov = OverviewBuilder(conn, settings, user(conn, "paul"), since=None, now=now).build()
    assert ov.digest == "Paul: heute nichts Besonderes."


def test_overview_page_shows_ai_parts(env):
    from fastapi.testclient import TestClient

    from schulblick.web.app import create_app

    settings, conn, now = env
    ai.process_messages(conn, settings, ai.AIClient(settings, conn, client=FakeSDK(message_handler(now.date()))))
    ai.build_digests(conn, settings, ai.AIClient(settings, conn, client=FakeSDK(lambda kw: text_answer("Guten Morgen."))))
    auth.set_password(conn, "papa", "papa-passwort")
    c = TestClient(create_app(settings), base_url="http://testserver")
    c.post("/login", data={"username": "papa", "password": "papa-passwort"}, headers={"origin": "http://testserver"})
    html = c.get("/").text
    assert "Guten Morgen." in html and "Die Robotik-AG fährt am Freitag ins Museum." in html
    assert "Originaltext anzeigen" in html and "KI-Zusammenfassung" in html and 'class="ev todo' in html
