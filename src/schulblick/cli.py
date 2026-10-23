"""Command line tools for setup and maintenance (run inside the container).

    schulblick users                     list accounts and their state
    schulblick set-password <name>       set or reset a password
    schulblick totp-setup <name>         set up the authenticator app (parents)
    schulblick totp-remove <name>        remove the second factor
    schulblick check-login               test the parent credentials against the portal
    schulblick sync [areas...]           run one sync now (lessons timetables substitutions calendar messages)
    schulblick sync --fixtures DIR       load saved pages instead of the portal (preview/testing)
    schulblick ai-status                 is the AI layer set up? calls and errors today
    schulblick ai-run                    summarise new messages now (normally done by every sync)
    schulblick ai-digest [--neu]         write today's morning summaries now (--neu: replace existing ones)
    schulblick worker | web              start the long-running services
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from . import db
from .config import get_settings, read_sph_credentials


def _conn():
    settings = get_settings()
    db.init_db(settings.db_path)
    from .web.auth import sync_users

    conn = db.connect(settings.db_path)
    sync_users(conn, settings)
    return settings, conn


def cmd_users(_args) -> int:
    _settings, conn = _conn()
    print(f"{'Name':<12} {'Rolle':<8} {'Kind':<10} {'Passwort':<9} {'2FA':<5} letzter Besuch")
    for r in conn.execute("SELECT * FROM users ORDER BY role DESC, username"):
        print(f"{r['username']:<12} {r['role']:<8} {r['child_first_name'] or '':<10} "
              f"{'ja' if r['password_hash'] else 'NEIN':<9} {'ja' if r['totp_secret'] else '-':<5} {r['last_visit'] or '-'}")
    return 0


def cmd_set_password(args) -> int:
    from .web.auth import set_password

    _settings, conn = _conn()
    pw1 = getpass.getpass(f"Neues Passwort für {args.username}: ")
    pw2 = getpass.getpass("Wiederholen: ")
    if pw1 != pw2:
        print("Die Passwörter stimmen nicht überein.")
        return 1
    try:
        ok = set_password(conn, args.username, pw1)
    except ValueError as exc:
        print(f"Abgelehnt: {exc}")
        return 1
    print("Gespeichert. Bestehende Anmeldungen dieses Kontos wurden beendet." if ok else "Unbekannter Name.")
    return 0 if ok else 1


def cmd_totp_setup(args) -> int:
    import pyotp
    import qrcode

    from .web.auth import set_totp

    _settings, conn = _conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (args.username.lower(),)).fetchone()
    if row is None:
        print("Unbekannter Name.")
        return 1
    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name=row["display_name"], issuer_name="Schulblick")
    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    qr.print_ascii(invert=True)
    print(f"\nOder manuell eingeben: {secret}\n")
    code = input("Zur Bestätigung den 6-stelligen Code aus der App eingeben: ").strip().replace(" ", "")
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        print("Code stimmt nicht – nichts gespeichert. Bitte noch einmal versuchen.")
        return 1
    set_totp(conn, args.username, secret)
    print("Zweiter Faktor ist eingerichtet.")
    return 0


def cmd_totp_remove(args) -> int:
    from .web.auth import set_totp

    _settings, conn = _conn()
    print("Entfernt." if set_totp(conn, args.username, None) else "Unbekannter Name.")
    return 0


def cmd_check_login(_args) -> int:
    from .sph.client import SPHClient

    settings = get_settings()
    username, password = read_sph_credentials(settings.sph_secret_path)
    client = SPHClient(settings.school_id, username, password)
    try:
        client.login()
        print("Anmeldung am Schulportal erfolgreich.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Anmeldung fehlgeschlagen: {type(exc).__name__}: {exc}")
        return 1
    finally:
        client.close()


def cmd_sync(args) -> int:
    from .sync.runner import Syncer

    settings, conn = _conn()
    if args.fixtures:
        from .fixtures import FixtureClient

        client = FixtureClient(args.fixtures)
    else:
        from .sph.client import SPHClient

        username, password = read_sph_credentials(settings.sph_secret_path)
        client = SPHClient(settings.school_id, username, password)
    areas = tuple(args.areas) or Syncer.AREAS
    try:
        results = Syncer(settings, conn, client).run(areas)
    finally:
        client.close()
    for area, ok in results.items():
        msg = conn.execute("SELECT message FROM sync_runs WHERE area=? ORDER BY id DESC LIMIT 1", (area,)).fetchone()
        print(f"{area:<14} {'ok ' if ok else 'FEHLER'}  {msg['message'] if msg else ''}")
    return 0 if all(results.values()) else 1


def cmd_ai_status(_args) -> int:
    from .ai import AIClient, read_key

    settings, conn = _conn()
    client = AIClient(settings, conn)
    key = read_key(settings)
    print(f"KI eingeschaltet:  {'ja' if settings.ai.enabled else 'nein ([ai] enabled = false)'}")
    print(f"Schlüssel:         {'vorhanden' if key else 'fehlt (' + str(settings.ai.secret_path) + ')'}")
    print(f"Modelle:           {settings.ai.model} / Morgen: {settings.ai.digest_model} um {settings.ai.digest_time}")
    print(f"Anwesenheit an KI: {'ja' if settings.ai.include_attendance else 'nein'}")
    print(f"Aufrufe heute:     {client.calls_today()} von {settings.ai.max_calls_per_day}")
    done = conn.execute("SELECT COUNT(*) FROM message_ai WHERE summary IS NOT NULL").fetchone()[0]
    print(f"Zusammenfassungen: {done}")
    err = conn.execute("SELECT at, error FROM ai_usage WHERE ok=0 ORDER BY id DESC LIMIT 1").fetchone()
    if err:
        print(f"Letzter Fehler:    {err['at']} {err['error']}")
    return 0 if client.available else 1


def cmd_ai_run(_args) -> int:
    from .ai import AIClient, process_messages

    settings, conn = _conn()
    print(process_messages(conn, settings, AIClient(settings, conn)))
    return 0


def cmd_ai_digest(args) -> int:
    from datetime import datetime

    from .ai import AIClient, build_digests

    settings, conn = _conn()
    if args.neu:
        conn.execute("DELETE FROM daily_digests WHERE day=?", (datetime.now(settings.tz).date().isoformat(),))
    print(build_digests(conn, settings, AIClient(settings, conn)))
    for r in conn.execute("SELECT u.username, d.text FROM daily_digests d JOIN users u ON u.id=d.user_id "
                          "WHERE d.day=? ORDER BY u.id", (datetime.now(settings.tz).date().isoformat(),)):
        print(f"\n--- {r['username']} ---\n{r['text']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="schulblick", description="Schulblick Verwaltung")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("users").set_defaults(fn=cmd_users)
    p = sub.add_parser("set-password"); p.add_argument("username"); p.set_defaults(fn=cmd_set_password)
    p = sub.add_parser("totp-setup"); p.add_argument("username"); p.set_defaults(fn=cmd_totp_setup)
    p = sub.add_parser("totp-remove"); p.add_argument("username"); p.set_defaults(fn=cmd_totp_remove)
    sub.add_parser("check-login").set_defaults(fn=cmd_check_login)
    p = sub.add_parser("sync"); p.add_argument("areas", nargs="*"); p.add_argument("--fixtures")
    p.set_defaults(fn=cmd_sync)
    sub.add_parser("ai-status").set_defaults(fn=cmd_ai_status)
    sub.add_parser("ai-run").set_defaults(fn=cmd_ai_run)
    p = sub.add_parser("ai-digest"); p.add_argument("--neu", action="store_true"); p.set_defaults(fn=cmd_ai_digest)
    sub.add_parser("worker").set_defaults(fn=lambda _a: _run_worker())
    sub.add_parser("web").set_defaults(fn=lambda _a: _run_web())
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return args.fn(args)


def _run_worker() -> int:
    from .sync.worker import main as worker_main

    worker_main()
    return 0


def _run_web() -> int:
    from .web.app import run

    run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
