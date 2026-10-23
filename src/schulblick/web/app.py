"""FastAPI application: login and the family overview."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, db
from ..config import Settings, get_settings
from . import auth
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
        return templates.TemplateResponse(request, "overview.html", {"ov": ov, "version": __version__})

    return app


def run() -> None:  # pragma: no cover - container entry point
    import uvicorn

    logging.basicConfig(force=True, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("schulblick.web.app:create_app", factory=True, host="0.0.0.0", port=8000, proxy_headers=True,
                forwarded_allow_ips="*", access_log=False)
