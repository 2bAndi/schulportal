"""FastAPI application: login and the family overview."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, db, ics_import
from ..config import Settings, get_settings
from . import auth, calendar, family
from .views import OverviewBuilder

log = logging.getLogger("schulblick.web")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.init_db(settings.db_path)
        conn = db.connect(settings.db_path)
        try:
            auth.sync_users(conn, settings)
        finally:
            conn.close()
        yield

    app = FastAPI(title="Schulblick", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer")
            host = request.headers.get("host", "")
            if not origin or urlparse(origin).netloc != host:
                return PlainTextResponse("Anfrage abgelehnt.", status_code=403)
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def get_conn():
        conn = db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots() -> str:
        return "User-agent: *\nDisallow: /\n"

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None, "username": "", "needs_totp": False})

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, username: str = Form(""), password: str = Form(""), code: str = Form(""),
              conn=Depends(get_conn)):
        ip = client_ip(request)
        username = username.strip().lower()
        ctx = {"username": username, "needs_totp": False, "error": None}
        if auth.is_throttled(conn, username, ip):
            ctx["error"] = "Zu viele Fehlversuche. Bitte in 15 Minuten noch einmal versuchen."
            return templates.TemplateResponse(request, "login.html", ctx, status_code=429)
        result = auth.check_credentials(conn, settings, username, password, code or None)
        if result.user is None:
            if result.needs_totp and not result.error:
                ctx.update(needs_totp=True, error="Bitte zusätzlich den 6-stelligen Code aus der Authenticator-App eingeben.")
                return templates.TemplateResponse(request, "login.html", ctx, status_code=401)
            auth.record_failure(conn, username, ip)
            ctx.update(error=result.error, needs_totp=result.needs_totp)
            return templates.TemplateResponse(request, "login.html", ctx, status_code=401)
        auth.clear_failures(conn, username)
        token = auth.create_session(conn, settings, result.user, request.headers.get("user-agent", ""))
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(auth.COOKIE_NAME, token, max_age=settings.session_days * 86400, httponly=True,
                            secure=settings.cookie_secure, samesite="lax")
        log.info("login %s from %s", username, ip)
        return response

    @app.post("/logout")
    def logout(request: Request, conn=Depends(get_conn)):
        auth.end_session(conn, request.cookies.get(auth.COOKIE_NAME))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.COOKIE_NAME)
        return response

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, conn=Depends(get_conn)):
        user = auth.user_for_token(conn, request.cookies.get(auth.COOKIE_NAME))
        if user is None:
            return RedirectResponse("/login", status_code=303)
        since = auth.touch_visit(conn, user)
        ov = OverviewBuilder(conn, settings, user, since).build()
        return templates.TemplateResponse(request, "overview.html", {"ov": ov, "version": __version__,
                                                                         "active": "uebersicht"})

    # -- family calendar -----------------------------------------------------------------

    def current_user(request: Request, conn):
        return auth.user_for_token(conn, request.cookies.get(auth.COOKIE_NAME))

    def today() -> date:
        return datetime.now(settings.tz).date()

    def parse_day(value: str | None) -> date | None:
        try:
            return date.fromisoformat(value) if value else None
        except ValueError:
            return None

    def kids_for_form(conn) -> list:
        return OverviewBuilder(conn, settings, auth_parent_view, None).kids()

    # a pseudo parent used only to list all children for the parent form
    auth_parent_view = auth.User(id=0, username="", display_name="", role="parent", child_first_name=None,
                                 has_totp=False, prev_visit=None)

    def form_page(request, conn, user, values, errors, event=None, status=200, occurrence=None):
        own = family.child_id_for(conn, user)
        return templates.TemplateResponse(request, "event_form.html", {
            "user": user, "values": values, "errors": errors, "event": event, "own_child_id": own,
            "occurrence": occurrence, "repeats": family.REPEATS,
            "kids": kids_for_form(conn) if user.is_parent else [], "version": __version__,
            "active": "kalender",
        }, status_code=status)

    @app.get("/kalender", response_class=HTMLResponse)
    def calendar_page(request: Request, von: str | None = None, conn=Depends(get_conn)):
        user = current_user(request, conn)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        start = family.week_start(parse_day(von) or today())
        agenda = calendar.build_agenda(conn, settings, user, start, today=today())
        return templates.TemplateResponse(request, "calendar.html", {
            "user": user, "a": agenda, "version": __version__, "active": "kalender",
            "saved": request.query_params.get("ok")})

    @app.get("/kalender/neu", response_class=HTMLResponse)
    def new_event_form(request: Request, datum: str | None = None, conn=Depends(get_conn)):
        user = current_user(request, conn)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        own = family.child_id_for(conn, user)
        return form_page(request, conn, user, family.form_values(None, parse_day(datum) or today(), own,
                                                                  user.is_parent), [])

    @app.post("/kalender/neu", response_class=HTMLResponse)
    async def create_event(request: Request, conn=Depends(get_conn)):
        user = current_user(request, conn)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        data = {k: form.get(k) for k in form.keys()}
        data["audience"] = form.getlist("audience")
        own = family.child_id_for(conn, user)
        valid = {k.id for k in kids_for_form(conn)}
        res = family.parse_form(data, user, own, valid)
        if res.errors:
            return form_page(request, conn, user, res.values, res.errors, status=400)
        family.save_event(conn, user, res.data)
        return RedirectResponse(f"/kalender?von={res.data['start'][:10]}&ok=1", status_code=303)

    @app.get("/kalender/abo", response_class=HTMLResponse)
    def subscribe_page(request: Request, conn=Depends(get_conn)):
        user = current_user(request, conn)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        token = calendar.ics_token(conn, user)
        host = request.headers.get("host", "")
        https_url = f"https://{host}/ics/{token}.ics"
        return templates.TemplateResponse(request, "subscribe.html", {
            "user": user, "https_url": https_url, "webcal_url": "webcal://" + https_url[len("https://"):],
            "version": __version__, "active": "kalender", "rotated": request.query_params.get("neu")})

    @app.post("/kalender/abo/neu")
    def rotate_token(request: Request, conn=Depends(get_conn)):
        user = current_user(request, conn)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        calendar.ics_token(conn, user, rotate=True)
        return RedirectResponse("/kalender/abo?neu=1", status_code=303)

    @app.get("/kalender/{event_id:int}", response_class=HTMLResponse)
    def edit_event_form(request: Request, event_id: int, am: str | None = None, conn=Depends(get_conn)):
        user = current_user(request, conn)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        ev = family.get_event(conn, user, event_id)
        if ev is None or not ev.can_edit:
            return PlainTextResponse("Diesen Termin gibt es nicht oder du darfst ihn nicht bearbeiten.", status_code=404)
        own = family.child_id_for(conn, user)
        occurrence = parse_day(am) if ev.is_series else None
        return form_page(request, conn, user, family.form_values(ev, None, own, user.is_parent), [], event=ev,
                         occurrence=occurrence)

    @app.post("/kalender/{event_id:int}", response_class=HTMLResponse)
    async def update_event(request: Request, event_id: int, conn=Depends(get_conn)):
        user = current_user(request, conn)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        ev = family.get_event(conn, user, event_id)
        if ev is None or not ev.can_edit:
            return PlainTextResponse("Diesen Termin gibt es nicht oder du darfst ihn nicht bearbeiten.", status_code=404)
        form = await request.form()
        data = {k: form.get(k) for k in form.keys()}
        data["audience"] = form.getlist("audience")
        own = family.child_id_for(conn, user)
        res = family.parse_form(data, user, own, {k.id for k in kids_for_form(conn)})
        if res.errors:
            return form_page(request, conn, user, res.values, res.errors, event=ev, status=400)
        family.save_event(conn, user, res.data, event_id=event_id)
        return RedirectResponse(f"/kalender?von={res.data['start'][:10]}&ok=1", status_code=303)

    @app.post("/kalender/{event_id:int}/loeschen")
    async def remove_event(request: Request, event_id: int, conn=Depends(get_conn)):
        user = current_user(request, conn)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        ev = family.get_event(conn, user, event_id)
        if ev is None or not ev.can_edit:
            return PlainTextResponse("Diesen Termin gibt es nicht oder du darfst ihn nicht löschen.", status_code=404)
        form = await request.form()
        day = parse_day(form.get("am"))
        if form.get("confirm") != "ja":
            return RedirectResponse(f"/kalender/{event_id}" + (f"?am={day.isoformat()}" if day else ""),
                                    status_code=303)
        if ev.is_series and form.get("scope") == "one" and day is not None:
            family.cancel_occurrence(conn, event_id, day)
            return RedirectResponse(f"/kalender?von={day.isoformat()}&ok=3", status_code=303)
        family.delete_event(conn, event_id)
        return RedirectResponse(f"/kalender?von={(day or ev.first_day).isoformat()}&ok=2", status_code=303)

    # -- Outlook / iCalendar import (parents only) -------------------------------------------

    def import_page(request, conn, user, message=None, error=None, status=200):
        feeds = conn.execute("SELECT * FROM calendar_feeds ORDER BY id").fetchall()
        counts = {r["source"]: r["n"] for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM family_events WHERE source IS NOT NULL GROUP BY source")}
        return templates.TemplateResponse(request, "import.html", {
            "user": user, "kids": kids_for_form(conn), "feeds": feeds, "counts": counts, "message": message,
            "error": error, "version": __version__, "active": "kalender"}, status_code=status)

    def import_audience(form, conn) -> list[str]:
        raw = [a for a in form.getlist("audience") if a]
        if family.EVERYONE in raw:
            return [family.EVERYONE]
        kids = sorted({a for a in raw if a in {k.id for k in kids_for_form(conn)}})
        return kids or [family.PARENTS]

    def parent_or_none(request, conn):
        user = current_user(request, conn)
        return user if user is not None and user.is_parent else None

    @app.get("/kalender/import", response_class=HTMLResponse)
    def import_form(request: Request, conn=Depends(get_conn)):
        user = parent_or_none(request, conn)
        if user is None:
            return RedirectResponse("/kalender", status_code=303)
        return import_page(request, conn, user, message=request.query_params.get("msg"))

    @app.post("/kalender/import/datei", response_class=HTMLResponse)
    async def import_file(request: Request, conn=Depends(get_conn)):
        user = parent_or_none(request, conn)
        if user is None:
            return RedirectResponse("/kalender", status_code=303)
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            return import_page(request, conn, user, error="Bitte eine .ics-Datei auswählen.", status=400)
        data = await upload.read(ics_import.MAX_BYTES + 1)
        try:
            items = ics_import.parse_file(data, settings.tz, today(), form.get("private_mode") or "hide")
        except ics_import.ImportError_ as exc:
            return import_page(request, conn, user, error=str(exc), status=400)
        if not items:
            return import_page(request, conn, user, error="In der Datei wurden keine Termine gefunden.", status=400)
        new, updated = ics_import.import_items(conn, items, import_audience(form, conn), user.id, user.display_name)
        msg = f"{new} Termine übernommen" + (f", {updated} aktualisiert" if updated else "") + "."
        return RedirectResponse("/kalender/import?msg=" + msg.replace(" ", "+"), status_code=303)

    @app.post("/kalender/import/abo", response_class=HTMLResponse)
    async def add_feed(request: Request, conn=Depends(get_conn)):
        user = parent_or_none(request, conn)
        if user is None:
            return RedirectResponse("/kalender", status_code=303)
        form = await request.form()
        name = (form.get("name") or "").strip()[:60] or "Outlook"
        try:
            url = ics_import.normalise_url(form.get("url") or "")
        except ics_import.ImportError_ as exc:
            return import_page(request, conn, user, error=str(exc), status=400)
        private_mode = form.get("private_mode") if form.get("private_mode") in ("hide", "show", "skip") else "hide"
        cur = conn.execute(
            "INSERT INTO calendar_feeds(name, url, audience, private_mode, created_by, created_at) VALUES(?,?,?,?,?,?)",
            (name, url, db.dumps(import_audience(form, conn)), private_mode, user.id, db.utcnow()))
        ok, msg = ics_import.sync_feed(conn, cur.lastrowid, settings.tz, today())
        text = f"Kalender „{name}“ verbunden: {msg}." if ok else f"Kalender „{name}“ gespeichert, aber der Abruf klappte nicht: {msg}"
        return RedirectResponse("/kalender/import?msg=" + text.replace(" ", "+"), status_code=303)

    @app.post("/kalender/import/abo/{feed_id}/abrufen")
    def refresh_feed(request: Request, feed_id: int, conn=Depends(get_conn)):
        user = parent_or_none(request, conn)
        if user is None:
            return RedirectResponse("/kalender", status_code=303)
        ok, msg = ics_import.sync_feed(conn, feed_id, settings.tz, today())
        return RedirectResponse("/kalender/import?msg=" + (("Abgerufen: " if ok else "Fehler: ") + msg).replace(" ", "+"),
                                status_code=303)

    @app.post("/kalender/import/abo/{feed_id}/loeschen")
    async def remove_feed(request: Request, feed_id: int, conn=Depends(get_conn)):
        user = parent_or_none(request, conn)
        if user is None:
            return RedirectResponse("/kalender", status_code=303)
        form = await request.form()
        if form.get("confirm") == "ja":
            with db.transaction(conn):
                conn.execute("DELETE FROM family_events WHERE source=?", (f"feed:{feed_id}",))
                conn.execute("DELETE FROM calendar_feeds WHERE id=?", (feed_id,))
            return RedirectResponse("/kalender/import?msg=Verbindung+und+ihre+Termine+entfernt.", status_code=303)
        return RedirectResponse("/kalender/import", status_code=303)

    @app.post("/kalender/import/datei/loeschen")
    async def remove_file_import(request: Request, conn=Depends(get_conn)):
        user = parent_or_none(request, conn)
        if user is None:
            return RedirectResponse("/kalender", status_code=303)
        form = await request.form()
        if form.get("confirm") == "ja":
            n = conn.execute("DELETE FROM family_events WHERE source='file'").rowcount
            return RedirectResponse(f"/kalender/import?msg={n}+importierte+Termine+gelöscht.", status_code=303)
        return RedirectResponse("/kalender/import", status_code=303)

    @app.get("/ics/{token}.ics")
    def ics_feed(token: str, conn=Depends(get_conn)):
        user = calendar.user_for_ics_token(conn, token)
        if user is None:
            return PlainTextResponse("Unbekannter Kalender-Link.", status_code=404)
        body = calendar.build_ics(conn, settings, user, today=today())
        return Response(body, media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": 'inline; filename="schulblick.ics"'})

    return app


def run() -> None:  # pragma: no cover - container entry point
    import uvicorn

    logging.basicConfig(force=True, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("schulblick.web.app:create_app", factory=True, host="0.0.0.0", port=8000, proxy_headers=True,
                forwarded_allow_ips="*", access_log=False)
