"""HTTP client for Schulportal Hessen with the parent account.

Login flow follows liblanis (lanis-mobile, Aug 2026):
  1. POST login.schulportal.hessen.de/?i=<school>   -> 302 on success
  2. GET  connect.schulportal.hessen.de              -> 302 Location = login URL
  3. GET  <login URL>                                -> sets the `sid` cookie on start.schulportal
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import date, datetime, timedelta

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .cryptor import Cryptor

log = logging.getLogger(__name__)

BASE = "https://start.schulportal.hessen.de/"
LOGIN = "https://login.schulportal.hessen.de/"
CONNECT = "https://connect.schulportal.hessen.de"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Schulblick/0.1 (private family dashboard)"
SESSION_MAX_AGE = 15 * 60  # re-login after 15 minutes; the portal times out idle sessions
DOWN_MARKER = "Der Bereich der pädagogischen Organisation steht Ihnen aktuell nicht zur Verfügung"


class PortalError(Exception):
    """Generic portal problem (unexpected response)."""


class PortalDown(PortalError):
    """Maintenance or outage."""


class WrongCredentials(PortalError):
    """Username or password rejected. Do not retry automatically."""


class LoginLocked(PortalError):
    """Too many failed logins; the portal asks to wait."""

    def __init__(self, seconds: str):
        super().__init__(f"login locked, wait {seconds}s")
        self.seconds = seconds


class SPHClient:
    def __init__(self, school_id: str, username: str, password: str, *, delay: float = 0.6,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.school_id = school_id
        self.username = username
        self._password = password
        self.delay = delay
        self.http = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"},
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=False,
            transport=transport,
        )
        self.logged_in_at: float | None = None
        self.cryptor: Cryptor | None = None

    # -- session -----------------------------------------------------------------

    def close(self) -> None:
        self.http.close()

    def _pause(self) -> None:
        if self.delay:
            time.sleep(self.delay + random.random() * self.delay)

    def login(self) -> None:
        self.http.cookies.clear()
        self.cryptor = None

        check = self.http.get(BASE, follow_redirects=True)
        if check.status_code == 503 or DOWN_MARKER in check.text:
            raise PortalDown("Schulportal reports maintenance")
        self._pause()

        resp = self.http.post(
            LOGIN,
            params={"i": self.school_id},
            data={
                "user": f"{self.school_id}.{self.username}",
                "user2": self.username,
                "password": self._password,
            },
        )
        if resp.status_code == 503:
            raise PortalDown("login service unavailable")
        if "location" not in resp.headers:
            locktime = HTMLParser(resp.text).css_first("#authErrorLocktime")
            if locktime is not None:
                raise LoginLocked(locktime.text(strip=True))
            raise WrongCredentials("portal rejected the parent credentials")
        self._pause()

        connect = self.http.get(CONNECT)
        login_url = connect.headers.get("location")
        if not login_url:
            raise PortalError("connect.schulportal did not return a login URL")
        final = self.http.get(login_url, follow_redirects=True)
        if final.status_code >= 400:
            raise PortalError(f"login URL returned HTTP {final.status_code}")
        if not any(c.name == "sid" for c in self.http.cookies.jar):
            raise PortalError("no session cookie after login")
        self.logged_in_at = time.monotonic()
        log.info("Logged in to Schulportal (school %s)", self.school_id)

    def ensure_login(self) -> None:
        if self.logged_in_at is None or time.monotonic() - self.logged_in_at > SESSION_MAX_AGE:
            self.login()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        self.ensure_login()
        self._pause()
        url = path if path.startswith("http") else BASE + path
        resp = self.http.request(method, url, follow_redirects=True, **kwargs)
        if self._looks_logged_out(resp):
            log.info("Session expired, logging in again")
            self.login()
            self._pause()
            resp = self.http.request(method, url, follow_redirects=True, **kwargs)
            if self._looks_logged_out(resp):
                raise PortalError(f"still logged out after re-login ({path})")
        if resp.status_code == 503 or DOWN_MARKER in resp.text:
            raise PortalDown("Schulportal reports maintenance")
        resp.raise_for_status()
        return resp

    @staticmethod
    def _looks_logged_out(resp: httpx.Response) -> bool:
        host = resp.url.host or ""
        if host.startswith("login.") or host.startswith("connect."):
            return True
        text = resp.text[:20000]
        return 'name="password"' in text and "logout" not in text.lower()

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        headers = {"X-Requested-With": "XMLHttpRequest", **kwargs.pop("headers", {})}
        return self._request("POST", path, headers=headers, **kwargs)

    # -- pages -------------------------------------------------------------------

    def lessons_html(self) -> str:
        return self.get("meinunterricht.php", params={"cacheBreaker": int(time.time() * 1000)}).text

    def substitutions_html(self) -> str:
        return self.get("vertretungsplan.php").text

    def timetable_overview_html(self) -> str:
        return self.get("stundenplan.php").text

    def timetable_class_html(self, href: str) -> str:
        return self.get(href).text

    def calendar_events(self, start: date, end: date) -> list[dict]:
        resp = self.post("kalender.php", data={"f": "getEvents", "start": start.isoformat(),
                                               "end": end.isoformat()})
        data = resp.json()
        if not isinstance(data, list):
            raise PortalError("unexpected calendar response")
        return data

    # -- encrypted messages ----------------------------------------------------------

    def _ensure_cryptor(self) -> Cryptor:
        self.ensure_login()
        if self.cryptor and self.cryptor.ready:
            return self.cryptor
        cryptor = Cryptor()
        pub = self.post("ajax.php", params={"f": "rsaPublicKey"}).json()["publickey"]
        challenge = self.post(
            "ajax.php",
            params={"f": "rsaHandshake", "s": random.randint(0, 2000)},
            data={"key": cryptor.wrapped_key(pub)},
        ).json()["challenge"]
        if not cryptor.verify_challenge(challenge):
            raise PortalError("encryption handshake failed")
        self.cryptor = cryptor
        return cryptor

    def message_headers(self) -> list[dict]:
        cryptor = self._ensure_cryptor()
        resp = self.post("nachrichten.php", data={"a": "headers", "getType": "visibleOnly", "last": "0"})
        payload = resp.json()
        return json.loads(cryptor.decrypt(payload["rows"]))

    def message(self, uniquid: str) -> dict:
        """Load one conversation. Note: the portal marks it as read."""
        cryptor = self._ensure_cryptor()
        resp = self.post(
            "nachrichten.php",
            params={"a": "read", "msg": uniquid},
            data={"a": "read", "uniqid": cryptor.encrypt(uniquid)},
        )
        return json.loads(cryptor.decrypt(resp.json()["message"]))


def parse_portal_datetime(text: str, now: datetime) -> datetime:
    """Parse 'heute 09:00', 'gestern 07:01' or '19.10.2026 11:22'."""
    text = " ".join(text.split())
    if text.startswith("heute"):
        t = datetime.strptime(text.split(" ", 1)[1], "%H:%M")
        return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if text.startswith("gestern"):
        t = datetime.strptime(text.split(" ", 1)[1], "%H:%M")
        return (now - timedelta(days=1)).replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=now.tzinfo)
        except ValueError:
            continue
    raise ValueError(f"unknown portal date: {text!r}")
